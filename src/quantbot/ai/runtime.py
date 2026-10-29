"""单工作进程的持久化任务与运行风控。账户、订单回报和运行记录原子提交。"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import fcntl
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from uuid import uuid4

from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.evidence import hash_payload, source_tree_hash

from .config import validate_config
from .admission import strategy_identity
from .feeds import fetch_snapshot
from .gateway import decision_provider
from .journal import runtime_lock
from .local_execution import (
    account_view,
    advance,
    cancel_pending,
    create_plan,
    model_account,
    new_account,
    queue_exit,
    queue_plan,
)
from .models import CryptoRuleProvider, DemoProvider, PriceActionProvider
from .transport import ProviderError


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


@dataclass
class JobSpec:
    name: str = "量化模拟任务"
    market: str = "stocks"
    symbols: list[str] | None = None
    provider: str = "ai"
    source: str = "csv"
    data_path: str = ""
    sec_context_path: str = ""
    start: str = ""
    bar: str = "1d"
    interval_seconds: int = 60
    data_delay_seconds: int = 900
    max_daily_loss: float = 0.03
    max_drawdown: float = 0.10
    max_model_failures: int = 3


def prepare(spec, config):
    spec = asdict(JobSpec(**spec))
    config = deepcopy(config)
    validation = deepcopy(config)
    if spec["market"] == "crypto":
        validation["market"].update(symbols=["SPY"], benchmark="SPY")
    validate_config(validation)
    if spec["market"] not in {"stocks", "crypto"} or spec["provider"] not in {
        "ai", "demo", "price_action", "crypto_rules",
    }:
        raise ValueError("未知任务市场或决策方式")
    if spec["provider"] == "crypto_rules" and spec["market"] != "crypto":
        raise ValueError("crypto_rules 仅支持加密现货")
    if not isinstance(spec["name"], str) or not 1 <= len(spec["name"].strip()) <= 80:
        raise ValueError("任务名称必须为 1..80 字符")
    symbols = spec["symbols"] or (config["market"]["symbols"] if spec["market"] == "stocks" else ["BTC/USDT"])
    if (
        not isinstance(symbols, list)
        or not 1 <= len(symbols) <= 20
        or any(not isinstance(s, str) for s in symbols)
        or len(set(symbols)) != len(symbols)
    ):
        raise ValueError("任务标的必须为 1..20 个不重复代码")
    spec["symbols"] = symbols
    if spec["market"] == "stocks":
        config["market"]["symbols"] = symbols
        config["market"]["benchmark"] = symbols[0]
        validate_config(config)
        if spec["bar"] != "1d" or spec["source"] not in {"csv", "yahoo", "vintage"}:
            raise ValueError("美股任务仅支持 CSV/Yahoo/历史版本日线")
    else:
        instruments = {i.symbol for i in MockExchange().list_instruments("spot") if i.quote_asset == "USDT"}
        if set(symbols) - instruments or spec["source"] not in {"csv", "okx_public", "vintage"}:
            raise ValueError("加密任务仅支持已定义模拟精度的 USDT 对与 CSV/公开行情")
        if spec["bar"] not in {"15m", "1h", "1d"} or spec["provider"] == "price_action":
            raise ValueError("加密任务支持 15m/1h/1d 与 AI/演示规则/加密研究规则")
        config["market"]["symbols"] = symbols
        if spec["provider"] == "crypto_rules":
            rule = config["strategy"].get("crypto_rule", "momentum_hold")
            if rule in {"trend_filtered", "strength_rotation"} and not config["risk"].get("crypto_stop_loss_pct"):
                raise ValueError("趋势过滤和强弱轮动必须显式配置固定止盈止损")
            if rule == "strength_rotation" and set(symbols) != {"BTC/USDT", "ETH/USDT"}:
                raise ValueError("strength_rotation 需要同时提供 BTC/USDT 与 ETH/USDT")
    if spec["source"] in {"csv", "vintage"}:
        if not spec["data_path"]:
            raise ValueError("本地行情需要 data_path")
        spec["data_path"] = str(Path(spec["data_path"]).expanduser().resolve())
    if not isinstance(spec["sec_context_path"], str):
        raise ValueError("sec_context_path 必须为本地 JSON 路径")
    if spec["sec_context_path"]:
        if spec["market"] != "stocks":
            raise ValueError("SEC 公告输入当前仅支持美股回测与研究")
        spec["sec_context_path"] = str(Path(spec["sec_context_path"]).expanduser().resolve())
    if not spec["start"]:
        spec["start"] = str(
            (datetime.now(UTC) - timedelta(days=max(400, config["strategy"]["momentum_window"] * 3))).date()
        )
    datetime.fromisoformat(spec["start"])
    for key, lower, upper in (
        ("interval_seconds", 1, 86400),
        ("data_delay_seconds", 0, 86400),
        ("max_model_failures", 1, 100),
    ):
        if type(spec[key]) is not int or not lower <= spec[key] <= upper:
            raise ValueError(f"{key} 必须为 {lower}..{upper} 的整数")
    for key in ("max_daily_loss", "max_drawdown"):
        v = spec[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 < v < 1:
            raise ValueError(f"{key} 必须在 0..1 之间")
    config["strategy"]["mode"] = "price_action" if spec["provider"] == "price_action" else "ai"
    return spec, config


def provider_for(spec, config):
    if spec["provider"] == "demo":
        return DemoProvider()
    if spec["provider"] == "price_action":
        return PriceActionProvider()
    if spec["provider"] == "crypto_rules":
        return CryptoRuleProvider()
    return decision_provider(config)


class RuntimeStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # Switching a new database to WAL needs a schema lock that concurrent
        # connections cannot always resolve via SQLite's busy timeout.
        # Serialize initialization across API threads and worker processes.
        with (self.root / ".schema.lock").open("a+") as lock, self.connect() as db:
            fcntl.flock(lock, fcntl.LOCK_EX)
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, family TEXT NOT NULL, parent TEXT, spec TEXT NOT NULL,
                    config TEXT NOT NULL, code_hash TEXT NOT NULL, stage TEXT NOT NULL,
                    status TEXT NOT NULL, account TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    next_run REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, model_authorized INTEGER NOT NULL DEFAULT 0,
                    last_snapshot TEXT, row_hashes TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS cycles (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, as_of TEXT NOT NULL,
                    snapshot TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL);
                CREATE INDEX IF NOT EXISTS cycle_job ON cycles(job_id, started);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, job_id TEXT, at REAL NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS evaluations (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, report TEXT NOT NULL, at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS worker (id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS research_studies (
                    id TEXT PRIMARY KEY, plan TEXT NOT NULL, plan_hash TEXT NOT NULL,
                    status TEXT NOT NULL, report TEXT, created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS research_trials (
                    study_id TEXT NOT NULL, key TEXT NOT NULL, stage TEXT NOT NULL,
                    job_id TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, PRIMARY KEY(study_id, key));
                CREATE TABLE IF NOT EXISTS research_queue (
                    id TEXT PRIMARY KEY, study_id TEXT NOT NULL, planned REAL NOT NULL,
                    status TEXT NOT NULL, model_authorized INTEGER NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL, error TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS research_queue_active ON research_queue(study_id)
                    WHERE status IN ('queued','running');
                CREATE TABLE IF NOT EXISTS research_worker (
                    id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / "runtime.sqlite", timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def event(db, job_id, kind, payload):
        db.execute(
            "INSERT INTO events(job_id,at,kind,payload) VALUES (?,?,?,?)",
            (job_id, time.time(), kind, encode(payload)),
        )

    @staticmethod
    def unpack(row):
        if row is None:
            raise ValueError("任务不存在")
        job = dict(row)
        for key in ("spec", "config", "account", "row_hashes"):
            job[key] = json.loads(job[key])
        job["account_view"] = account_view(job["account"])
        job["strategy_identity"] = strategy_identity(job["spec"], job["config"], job["code_hash"])
        # The persisted legacy stage 'paper' is a local ledger observation only.
        job["execution"] = "local_observation"
        job["broker_paper_authorized"] = False
        job["live_authorized"] = False
        return job

    def get(self, job_id):
        with self.connect() as db:
            return self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def create(self, spec, config, *, parent=None):
        spec, config = prepare(spec, config)
        job_id = uuid4().hex
        family = self.get(parent)["family"] if parent else job_id
        with self.connect() as db:
            db.execute(
                """INSERT INTO jobs(id,family,parent,spec,config,code_hash,stage,status,account,created)
                          VALUES (?,?,?,?,?,?,'candidate','paused',?,?)""",
                (
                    job_id,
                    family,
                    parent,
                    encode(spec),
                    encode(config),
                    source_tree_hash(),
                    encode(new_account(config["execution"]["initial_cash"])),
                    time.time(),
                ),
            )
            self.event(db, job_id, "created", {"parent": parent, "execution": "local_observation"})
        return self.get(job_id)

    def overview(self):
        with self.connect() as db:
            jobs = [self.unpack(row) for row in db.execute("SELECT * FROM jobs ORDER BY created DESC")]
            heartbeat = db.execute("SELECT heartbeat FROM worker WHERE id=1").fetchone()
        return {
            "jobs": jobs,
            "worker_heartbeat": heartbeat[0] if heartbeat else None,
            "worker_online": bool(heartbeat and time.time() - heartbeat[0] < 30),
            "execution": "local_observation",
            "broker_paper_authorized": False,
            "live_enabled": False,
        }

    def history(self, job_id, limit=100):
        self.get(job_id)
        limit = min(max(int(limit), 1), 1000)
        with self.connect() as db:
            cycles = [
                {**dict(r), "result": json.loads(r["result"])}
                for r in db.execute(
                    "SELECT * FROM cycles WHERE job_id=? ORDER BY started DESC LIMIT ?", (job_id, limit)
                )
            ]
            events = [
                {**dict(r), "payload": json.loads(r["payload"])}
                for r in db.execute(
                    "SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit)
                )
            ]
            evaluations = [
                {"id": r["id"], "at": r["at"], "report": json.loads(r["report"])}
                for r in db.execute(
                    "SELECT * FROM evaluations WHERE job_id=? ORDER BY at DESC LIMIT ?", (job_id, limit)
                )
            ]
        return {"cycles": cycles, "events": events, "evaluations": evaluations}

    def control(self, job_id, action, *, allow_model_calls=False, acknowledge_risk=False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            account = job["account"]
            if action == "resume":
                if job["spec"].get("sec_context_path"):
                    raise ValueError("SEC 公告输入当前仅支持回测与研究")
                if job["spec"]["source"] == "vintage":
                    raise ValueError("历史版本行情目前仅支持回测与研究")
                if job["stage"] == "candidate":
                    raise ValueError("候选版本须先完成无错误的历史评估")
                if job["code_hash"] != source_tree_hash():
                    raise ValueError("代码版本已改变；创建新任务版本并重新评估")
                evidence = db.execute(
                    "SELECT report FROM evaluations WHERE job_id=? ORDER BY at DESC,rowid DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
                report = json.loads(evidence["report"]) if evidence else {}
                if (
                    report.get("engineering_passed", report.get("passed")) is not True
                    or report.get("code_hash") != job["code_hash"]
                    or report.get("config_hash") != hash_payload(job["config"])
                    or report.get("spec_hash") != hash_payload(job["spec"])
                ):
                    raise ValueError("缺少与当前版本一致的通过评估证据；请重新评估候选版本（仅工程完整性）")
                snapshot = report["snapshot"]
                try:
                    data = json.loads(Path(snapshot["path"]).read_text())
                except (OSError, ValueError):
                    raise ValueError("评估行情快照缺失或损坏；请恢复证据或重新评估候选版本") from None
                if hash_payload(data) != snapshot["hash"]:
                    raise ValueError("评估行情快照指纹不一致；请恢复证据或重新评估候选版本")
                if job["spec"]["provider"] == "ai" and not (allow_model_calls or job["model_authorized"]):
                    raise ValueError("启用 AI 任务需要明确允许持续调用模型")
                if job["last_error"] and not acknowledge_risk:
                    raise ValueError("恢复前需确认并处理暂停原因，传入 acknowledge_risk")
                # 风险基准不会随恢复重置；仍超限的账户会再次熔断。
                account["risk_latched"] = None
                status, stage = "active", "paper"
                siblings = db.execute(
                    "SELECT id,account FROM jobs WHERE family=? AND id<>? AND status='active'",
                    (job["family"], job_id),
                ).fetchall()
                for sibling in siblings:
                    prior = json.loads(sibling["account"])
                    canceled = cancel_pending(prior, "version_switched")
                    db.execute(
                        "UPDATE jobs SET status='paused',account=?,revision=revision+1 WHERE id=?",
                        (encode(prior), sibling["id"]),
                    )
                    self.event(
                        db, sibling["id"], "version_paused", {"replacement": job_id, "orders": canceled}
                    )
            elif action in {"pause", "exit"}:
                canceled = cancel_pending(account, "user_" + action)
                if action == "exit":
                    canceled += queue_exit(account)
                self.event(db, job_id, "orders", canceled)
                status, stage = (
                    ("closing" if action == "exit" and account["positions"] else "paused"),
                    job["stage"],
                )
            else:
                raise ValueError("只支持 resume/pause/exit")
            db.execute(
                """UPDATE jobs SET status=?,stage=?,account=?,revision=revision+1,next_run=0,
                          model_authorized=?,failures=0,last_error=? WHERE id=?""",
                (
                    status,
                    stage,
                    encode(account),
                    int(allow_model_calls or job["model_authorized"]),
                    None if action == "resume" else job["last_error"],
                    job_id,
                ),
            )
            self.event(db, job_id, action, {"acknowledge_risk": acknowledge_risk})
        return self.get(job_id)

    def hold(self, job_id, error):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            canceled = cancel_pending(job["account"], "runtime_hold", keep_exits=True)
            if job["last_error"] != error or canceled:
                self.event(db, job_id, "alert", {"error": error, "orders": canceled})
            db.execute(
                "UPDATE jobs SET status='held',last_error=?,account=?,revision=revision+1 WHERE id=?",
                (error, encode(job["account"]), job_id),
            )

    def cancel_order(self, job_id, client_order_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            state = job["account"]
            order = next((o for o in state["pending"] if o["client_order_id"] == client_order_id), None)
            if order is None:
                raise ValueError("该订单已成交、已取消或不存在")
            state["pending"] = [o for o in state["pending"] if o["client_order_id"] != client_order_id]
            status = "paused" if job["status"] == "closing" and not state["pending"] else job["status"]
            db.execute(
                "UPDATE jobs SET account=?,status=?,revision=revision+1 WHERE id=?",
                (encode(state), status, job_id),
            )
            self.event(db, job_id, "orders", [{**order, "status": "canceled", "reason": "user_cancel"}])
        return self.get(job_id)

    def recover(self):
        """只能在取得工作进程互斥锁后调用；中断推理不自动重试。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            interrupted = db.execute("SELECT id,job_id FROM cycles WHERE status='started'").fetchall()
            for row in interrupted:
                job = self.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone())
                error = "上次决策中断；账户检查点已保留，需确认后恢复"
                orders = cancel_pending(job["account"], "interrupted", keep_exits=True)
                self.event(db, row["job_id"], "alert", {"error": error, "orders": orders})
                db.execute(
                    "UPDATE jobs SET status='held',account=?,last_error=?,revision=revision+1 WHERE id=?",
                    (encode(job["account"]), error, row["job_id"]),
                )
                db.execute(
                    "UPDATE cycles SET status='interrupted',finished=? WHERE id=?", (time.time(), row["id"])
                )


def _process(store, job_id, snapshot, now):
    context = snapshot.context()
    frozen = snapshot.freeze(store.root / "snapshots")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        job = store.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if job["stage"] != "paper":
            return
        state, spec, config = job["account"], job["spec"], job["config"]
        hashes = snapshot.row_hashes()
        if any(hashes[k] != v for k, v in job["row_hashes"].items() if k in hashes):
            raise ValueError("历史行情已修订；暂停任务以免混用不同价格基准")
        if state["positions"] and any(
            a["symbol"] in state["positions"] and a["date"] > state["as_of"][:10]
            for a in snapshot.actions or []
        ):
            raise ValueError("持仓区间出现拆股或分红；需要处理公司行动后重新评估")
        if job["last_snapshot"] == context.snapshot_id:
            return
        if state["as_of"] and context.as_of <= datetime.fromisoformat(state["as_of"]):
            raise ValueError("拒绝早于或修订过的已处理行情")
        events = advance(state, snapshot, context, config, spec)
        status = job["status"]
        if state["risk_latched"]:
            status = "held"
            events += cancel_pending(state, state["risk_latched"], keep_exits=True)
            store.event(db, job_id, "alert", {"error": state["risk_latched"]})
        if status == "closing" and not state["pending"]:
            status = "paused"
        waiting = status == "active" and bool(state["pending"])
        cycle_status = "awaiting_fills" if waiting else "started" if status == "active" else "monitored"
        run_id = uuid4().hex
        result = {
            "snapshot": frozen,
            "account": account_view(state),
            "orders": events,
            "as_of": context.as_of.isoformat(),
            "provider_calls": 0,
            "status": "awaiting_fills" if waiting else "started" if status == "active" else status,
            "execution": "local_observation", "broker_paper_authorized": False,
        }
        db.execute(
            "INSERT INTO cycles VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                job_id,
                context.as_of.isoformat(),
                context.snapshot_id,
                cycle_status,
                encode(result),
                now.timestamp(),
                None if cycle_status == "started" else now.timestamp(),
            ),
        )
        db.execute(
            "UPDATE jobs SET account=?,status=?,last_snapshot=?,row_hashes=?,last_error=? WHERE id=?",
            (
                encode(state),
                status,
                context.snapshot_id,
                encode(hashes),
                state["risk_latched"] or job["last_error"],
                job_id,
            ),
        )
        if events:
            store.event(db, job_id, "orders", events)
    if status != "active" or waiting:
        return result
    started = time.monotonic()
    error, plan = None, None
    try:
        if spec["provider"] == "ai" and not job["model_authorized"]:
            raise ValueError("未授权持续模型调用")
        decision = provider_for(spec, config).decide(
            context, model_account(state, context, spec["market"]), config
        )
        plan = create_plan(state, context, decision, config, spec["market"], job_id)
    except (ProviderError, ValueError) as exc:
        error = str(exc)
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        current = store.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        result.update(
            {
                "provider_calls": 1,
                "model_calls": int(spec["provider"] == "ai"),
                "decision_seconds": time.monotonic() - started,
                "plan": plan,
                "error": error,
            }
        )
        if current["revision"] != job["revision"] or current["status"] != "active":
            result["discarded"] = "任务控制状态已改变；本次计划未提交"
            cycle_status = "discarded"
        elif error:
            failures = current["failures"] + 1
            status = "held" if failures >= spec["max_model_failures"] else "active"
            db.execute(
                "UPDATE jobs SET failures=?,status=?,last_error=? WHERE id=?",
                (failures, status, error, job_id),
            )
            store.event(db, job_id, "alert", {"error": error, "consecutive_failures": failures})
            cycle_status = "model_error"
        else:
            events = queue_plan(state, plan)
            result["orders"] += events
            result["account"] = account_view(state)
            db.execute(
                "UPDATE jobs SET account=?,failures=0,last_error=NULL WHERE id=?", (encode(state), job_id)
            )
            if events:
                store.event(db, job_id, "orders", events)
            cycle_status = "completed"
        result["status"] = cycle_status
        db.execute(
            "UPDATE cycles SET status=?,result=?,finished=? WHERE id=?",
            (cycle_status, encode(result), time.time(), run_id),
        )
    return result


def tick(store, *, now=None):
    now = now or datetime.now(UTC)
    with runtime_lock(store.root / "worker-lock"):
        store.recover()
        with store.connect() as db:
            jobs = db.execute(
                "SELECT id FROM jobs WHERE stage='paper' AND next_run<=?", (now.timestamp(),)
            ).fetchall()
        results = []
        code_hash = source_tree_hash()
        for row in jobs:
            job = store.get(row["id"])
            with store.connect() as db:
                db.execute(
                    "UPDATE jobs SET next_run=? WHERE id=?",
                    (now.timestamp() + job["spec"]["interval_seconds"], job["id"]),
                )
            try:
                if job["code_hash"] != code_hash:
                    raise ValueError("运行代码已改变；需新建版本并重新评估")
                snapshot = fetch_snapshot(job["spec"], job["config"], now, store.root)
                result = _process(store, job["id"], snapshot, now)
                if result:
                    results.append({"job_id": job["id"], **result})
            except Exception as exc:
                # 工作进程隔离任务故障；未知异常只记录类型，不输出可能包含密钥的响应。
                error = str(exc) if isinstance(exc, (ValueError, ProviderError)) else type(exc).__name__
                store.hold(job["id"], error)
                results.append({"job_id": job["id"], "status": "held", "error": error})
        return results


def run_worker(store, stop=None):
    stop = stop or threading.Event()
    # 独立心跳让长耗时的模型请求不会被误报为工作进程离线。
    with runtime_lock(store.root / "process-lock"):

        def heartbeat():
            while not stop.is_set():
                with store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO worker VALUES (1,?)", (time.time(),))
                stop.wait(5)

        heart = threading.Thread(target=heartbeat, daemon=True)
        heart.start()
        try:
            while not stop.is_set():
                tick(store)
                stop.wait(1)
        finally:
            stop.set()
            heart.join()
            with store.connect() as db:
                db.execute("DELETE FROM worker")
