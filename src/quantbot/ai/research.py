"""预登记研究闭环：冻结输入、分段开发、一次留出、压力测试、归档结论。"""

from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import json
from pathlib import Path
import time
from uuid import uuid4

from quantbot.crypto.ai import INTERVALS
from quantbot.evidence import hash_payload, source_tree_hash
from quantbot.experiment_registry import ExperimentRegistry

from .evaluation import admission_checks, check_candidates, replay, usage_summary, window_summary
from .feeds import fetch_snapshot, validate_rows
from .journal import runtime_lock
from .models import DemoProvider, PriceActionProvider, SYSTEM_PROMPT
from .research_protocol import (
    DelayedRecordedDecisionProvider, PlaceboProvider, RecordedDecisionProvider, ResearchProtocol, conclude, hypothesis_card,
    returns_of, statistics,
)
from .runtime import encode, provider_for
from .replay_evidence import inspect_replay
from .vintages import load_vintages


BOUNDARY = (
    "仅评估预登记候选在冻结历史行情下的研究标准，不批准模拟盘或实盘。"
    "统计诊断只覆盖本研究的全部候选；其他目录、人工和外部试验未计入。"
    "行情的历史可得性、公司行动完整性及模型训练数据污染未获证明。"
    "仓位压力测试固定原始决策，随机对照不保证相同成交频率或实际暴露。"
)


def _compatible(jobs):
    first = jobs[0]
    spec_keys = ("market", "symbols", "bar", "source", "data_path", "start",
                 "data_delay_seconds", "max_daily_loss", "max_drawdown")
    for job in jobs[1:]:
        if (any(job["spec"][k] != first["spec"][k] for k in spec_keys)
                or job["spec"].get("sec_context_path", "") != first["spec"].get("sec_context_path", "")
                or any(job["config"]["execution"][k] != first["config"]["execution"][k]
                       for k in ("initial_cash", "cost_bps", "limit_buffer_bps"))):
            raise ValueError("研究候选必须使用相同标的、行情、资金、成本和账户熔断阈值")


def register_study(store, *, job_ids, card, protocol=None, data_kind="historical_unverified", promotion_policy=None):
    card = hypothesis_card(card)
    if data_kind not in {"historical_unverified", "synthetic"}:
        raise ValueError("data_kind 必须为 historical_unverified 或 synthetic")
    protocol = ResearchProtocol(**(protocol if protocol is not None else {}))
    if (not isinstance(job_ids, list) or not 1 <= len(job_ids) <= 6
            or any(not isinstance(j, str) for j in job_ids) or len(set(job_ids)) != len(job_ids)):
        raise ValueError("研究需要 1..6 个不重复候选 ID")
    jobs = [store.get(j) for j in job_ids]
    check_candidates(jobs, True)  # 登记只冻结输入，不构造或调用模型。
    _compatible(jobs)
    if promotion_policy is not None:
        from .crypto_admission import crypto_policy

        if promotion_policy != "crypto_spot_v1" or jobs[0]["spec"]["market"] != "crypto":
            raise ValueError("promotion_policy 只支持加密现货 crypto_spot_v1")
        promotion_policy = crypto_policy()
    if protocol.execution_delay_bars and jobs[0]["spec"]["market"] != "crypto":
        raise ValueError("延迟目标压力测试当前仅支持加密现货")
    max_requests = sum(
        protocol.folds * (protocol.development_bars - 1) * (1 + len(j["config"]["llm"]["fallback_models"]))
        for j in jobs if j["spec"]["provider"] == "ai"
    ) + max(((protocol.holdout_bars - 1) * (1 + len(j["config"]["llm"]["fallback_models"]))
             for j in jobs if j["spec"]["provider"] == "ai"), default=0)
    if max_requests > protocol.model_request_budget:
        raise ValueError(f"预登记最多 {max_requests} 次模型请求，超过预算 {protocol.model_request_budget}")
    # 成本压力保留原始价格容差，避免悄悄把测试变成更严格的入场限价测试。
    for job in jobs:
        execution = job["config"]["execution"]
        if execution["cost_bps"] <= 0:
            raise ValueError("研究需要正的估计执行成本，以便进行成本压力测试")
        if execution["limit_buffer_bps"] + execution["cost_bps"] * (protocol.cost_multiplier - 1) > 1000:
            raise ValueError("压力测试后的执行成本/限价容差超过配置上限")
    now = datetime.now(UTC)
    snapshot = fetch_snapshot(jobs[0]["spec"], jobs[0]["config"], now, store.root, fresh=False)
    if snapshot.provenance and snapshot.provenance["data_kind"] == "synthetic":
        data_kind = "synthetic"
    if snapshot.sec_events is not None and snapshot.sec_events.quality()["data_kind"] == "synthetic":
        data_kind = "synthetic"
    times = snapshot.times[-protocol.required_bars:]
    if len(times) != protocol.required_bars:
        raise ValueError(f"研究需要 {protocol.required_bars} 根预热后的行情，当前区间不足")
    for job in jobs:
        snapshot.with_config(job["config"]).context(times[0])
    from .corporate_actions import check_action_window

    check_action_window(snapshot.actions, snapshot.times[0].date().isoformat(), times[-1].date().isoformat())
    frozen = snapshot.freeze(store.root / "snapshots")
    identifier = uuid4().hex
    candidates = [{key: j[key] for key in ("id", "spec", "config", "code_hash")} for j in jobs]
    hashes = [hash_payload({"provider": j["spec"]["provider"], "config": j["config"]}) for j in jobs]
    registry = ExperimentRegistry()
    record = registry.register_candidates(experiment_id=identifier, market=snapshot.market,
                                          candidate_hashes=hashes, preregistered_at=now)
    for candidate, trial_id in zip(candidates, record.trial_ids, strict=True):
        candidate["trial_id"] = trial_id
    windows = [{"start": times[i * protocol.development_bars].isoformat(),
                "end": times[(i + 1) * protocol.development_bars - 1].isoformat()}
               for i in range(protocol.folds)]
    plan = {
        "schema_version": 1, "card": card, "protocol": asdict(protocol), "candidates": candidates,
        "registered_at": now.isoformat(), "code_hash": source_tree_hash(), "snapshot": frozen,
        "prompt": {"system_prompt": SYSTEM_PROMPT, "hash": hash_payload(SYSTEM_PROMPT)},
        "windows": {"development": windows,
                    "holdout": {"start": times[-protocol.holdout_bars].isoformat(),
                                "end": times[-1].isoformat()}},
        "selection_rule": "complete_development_mean_return_desc_then_drawdown_then_id",
        "account_basis": "independent_cash_per_window_no_compounding",
        "max_model_requests": max_requests, "declared_trial_count": record.declared_trial_count,
        "trial_ids": list(record.trial_ids),
        "data_quality": {
            **snapshot.quality(), "data_kind": data_kind,
            "checks": [*snapshot.quality()["checks"], "各候选预热"],
        },
        "evidence_boundary": BOUNDARY,
    }
    if promotion_policy is not None:
        plan["promotion_policy"] = promotion_policy
        plan["selection_rule"] += "_positive_mean_and_positive_window_fraction"
    with store.connect() as db:
        db.execute("INSERT INTO research_studies VALUES (?,?,?,'registered',NULL,?,?)",
                   (identifier, encode(plan), hash_payload(plan), time.time(), time.time()))
        store.event(db, None, "research_registered", {"study_id": identifier})
    return get_study(store, identifier)


def list_studies(store):
    with store.connect() as db:
        rows = db.execute("SELECT * FROM research_studies ORDER BY created DESC").fetchall()
    return [{"id": r["id"], "title": json.loads(r["plan"])["card"]["title"], "status": r["status"],
             "created": r["created"], "updated": r["updated"],
             "conclusion": json.loads(r["report"])["conclusion"] if r["report"] else "not_assessed"}
            for r in rows]


def get_study(store, identifier):
    with store.connect() as db:
        row = db.execute("SELECT * FROM research_studies WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("研究不存在")
        result = dict(row)
        result["plan"] = json.loads(result["plan"])
        if hash_payload(result["plan"]) != result["plan_hash"]:
            raise ValueError("研究方案与冻结指纹不一致")
        result["report"] = json.loads(result["report"]) if result["report"] else None
        result["trials"] = [dict(r) for r in db.execute(
            "SELECT * FROM research_trials WHERE study_id=? ORDER BY rowid", (identifier,))]
        for trial in result["trials"]:
            trial["result"] = json.loads(trial["result"]) if trial["result"] else None
        result["events"] = [{**dict(r), "payload": json.loads(r["payload"])} for r in db.execute(
            "SELECT * FROM events WHERE json_extract(payload,'$.study_id')=? ORDER BY id", (identifier,))]
    return result


def _stage(store, identifier, stage, **details):
    with store.connect() as db:
        db.execute("UPDATE research_studies SET updated=? WHERE id=?", (time.time(), identifier))
        store.event(db, None, "research_" + stage, {"study_id": identifier, **details})


def export_study(store, identifier):
    """下载证据包含实际冻结行情，离开本机路径后仍可检查输入。"""
    study = get_study(store, identifier)
    snapshot = study["plan"]["snapshot"]
    payload = json.loads(Path(snapshot["path"]).read_text())
    if hash_payload(payload) != snapshot["hash"]:
        raise ValueError("冻结行情指纹不一致，不能导出研究证据")
    return {**study, "input_snapshot": payload}


def _holdout_exposures(store, identifier, plan):
    spec = plan["candidates"][0]["spec"]
    window = plan["windows"]["holdout"]
    with store.connect() as db:
        previous = db.execute(
            """SELECT DISTINCT s.id,s.plan FROM research_studies s JOIN research_trials t ON t.study_id=s.id
               WHERE s.id!=? AND t.start<=? AND t.end>=?""",
            (identifier, window["end"], window["start"]),
        ).fetchall()
        evaluations = db.execute(
            "SELECT e.id,e.report,j.spec FROM evaluations e JOIN jobs j ON e.job_id=j.id"
        ).fetchall()
        attempts = db.execute("SELECT e.payload,j.spec FROM events e JOIN jobs j ON e.job_id=j.id WHERE e.kind='evaluation_started'").fetchall()
    exposed = [r["id"] for r in previous if any(
        j["spec"]["market"] == spec["market"] and set(j["spec"]["symbols"]) & set(spec["symbols"])
        for j in json.loads(r["plan"])["candidates"]
    )]
    # 基础回测/滚动评估已看过的同市场标的区间，也不能重新称为未使用的留出集。
    for row in evaluations:
        previous_spec, report = json.loads(row["spec"]), json.loads(row["report"])
        if (previous_spec["market"] != spec["market"]
                or not set(previous_spec["symbols"]) & set(spec["symbols"])):
            continue
        results = ([report["candidate"]] if report["kind"] == "replay" else
                   [fold[section] for fold in report["folds"] for section in ("selection", "candidate")])
        if any(curve and curve[0]["as_of"] <= window["end"] and curve[-1]["as_of"] >= window["start"]
               for curve in (r["equity_curve"] for r in results)):
            exposed.append("evaluation:" + row["id"])
    completed_runs = {json.loads(row["report"]).get("run_id") for row in evaluations}
    for row in attempts:
        previous_spec, attempt = json.loads(row["spec"]), json.loads(row["payload"])
        if (attempt.get("run_id") in completed_runs or not attempt.get("window_start")
                or previous_spec["market"] != spec["market"]
                or not set(previous_spec["symbols"]) & set(spec["symbols"])):
            continue
        if attempt["window_start"] <= window["end"] and attempt["window_end"] >= window["start"]:
            key = "evaluation-run:" + attempt["run_id"]
            if key not in exposed:
                exposed.append(key)
    return exposed


def trial_journal_path(store, identifier, key):
    return store.root / "research" / identifier / "replays" / (hash_payload(key) + ".jsonl")


def _trial(store, identifier, key, stage, job, snapshot, times, provider, config=None):
    with store.connect() as db:
        db.execute("INSERT INTO research_trials VALUES (?,?,?,?,?,?,'started',NULL)",
                   (identifier, key, stage, job["id"], times[0].isoformat(), times[-1].isoformat()))
    try:
        result = replay(snapshot, job["spec"], config or job["config"], provider, times=times,
                        journal_path=trial_journal_path(store, identifier, key))
        result["execution_checks"] = admission_checks(result, len(times))
        result["execution_completed"] = all(result["execution_checks"].values())
    except Exception as exc:
        # 记录失败后继续向上抛出；不能把异常变成成功，也不重复已暴露窗口。
        path = trial_journal_path(store, identifier, key)
        failure = {"error": str(exc)}
        if path.exists():
            failure["replay_evidence"] = inspect_replay(path)
        with store.connect() as db:
            db.execute("UPDATE research_trials SET status='failed',result=? WHERE study_id=? AND key=?",
                       (encode(failure), identifier, key))
        raise
    with store.connect() as db:
        db.execute("UPDATE research_trials SET status=?,result=? WHERE study_id=? AND key=?",
                   ("completed" if result["execution_completed"] else "failed", encode(result), identifier, key))
    return result


def _finish(store, identifier, report):
    status = "completed" if report["execution_completed"] else "failed"
    report["evidence_boundary"] = BOUNDARY
    path = store.root / "research" / identifier / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    report["report_path"] = str(path)
    # 数据库保留已完成的逐窗口记录，即使导出文件或进程随后失败也能取回证据。
    with store.connect() as db:
        db.execute("UPDATE research_studies SET status=?,report=?,updated=? WHERE id=?",
                   (status, encode(report), time.time(), identifier))
        store.event(db, None, "research_finished", {"study_id": identifier, "conclusion": report["conclusion"]})
    exported = export_study(store, identifier)
    path.write_text(json.dumps(exported, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return exported


def run_study(store, identifier, *, allow_model_calls=False):
    # 所有研究串行取得锁，防止两个已登记研究同时使用同一留出窗口却互相不可见。
    with runtime_lock(store.root / "research"):
        return _run_study(store, identifier, allow_model_calls=allow_model_calls)


def _run_study(store, identifier, *, allow_model_calls=False):
    """调用方须持有整个研究目录的互斥锁；队列与直接运行共用同一执行路径。"""
    study = get_study(store, identifier)
    plan = study["plan"]
    if study["status"] == "running":
        # 持锁后仍为 running 说明前一个进程已退出；不重试可能已收费或暴露的窗口。
        for trial in study["trials"]:
            if trial["status"] != "started":
                continue
            path = trial_journal_path(store, identifier, trial["key"])
            failure = {"error": "先前研究进程中断，未确认调用不能自动重试"}
            if path.exists():
                failure["replay_evidence"] = inspect_replay(path)
            with store.connect() as db:
                db.execute("UPDATE research_trials SET status='failed',result=? WHERE study_id=? AND key=?",
                           (encode(failure), identifier, trial["key"]))
        return _finish(store, identifier, {"execution_completed": False, "research_passed": False,
            "conclusion": "insufficient", "reasons": ["interrupted"],
            "error": "先前研究中断，已保留逐窗口证据；本研究不能重复运行"})
    if study["status"] != "registered":
        raise ValueError("研究已经运行，不能重新使用最终留出区间；可查看和导出原始报告")
    jobs = plan["candidates"]
    current = [store.get(j["id"]) for j in jobs]
    check_candidates(current, allow_model_calls)
    if (source_tree_hash() != plan["code_hash"] or any(
            any(c[k] != j[k] for k in ("spec", "config", "code_hash"))
            for c, j in zip(current, jobs, strict=True))):
        raise ValueError("代码或候选配置在登记后改变，必须创建新的研究方案")
    protocol = ResearchProtocol(**plan["protocol"])
    payload = json.loads(Path(plan["snapshot"]["path"]).read_text())
    if hash_payload(payload) != plan["snapshot"]["hash"]:
        raise ValueError("冻结行情指纹不一致，拒绝执行研究")
    if "vintage_input" in payload:
        snapshot = load_vintages(payload["vintage_input"], jobs[0]["spec"], jobs[0]["config"], datetime.now(UTC))
        if snapshot.rows != payload["rows"] or (snapshot.actions or []) != payload["actions"]:
            raise ValueError("冻结首版行情与版本历史不一致")
    else:
        snapshot = validate_rows(payload["rows"], jobs[0]["spec"], jobs[0]["config"], datetime.now(UTC),
                                 fresh=False, actions=payload["actions"])
    if "sec_event_input" in payload:
        from .sec_context import attach_sec_context

        snapshot = attach_sec_context(snapshot, payload["sec_event_input"])
    data = {j["id"]: snapshot.with_config(j["config"]) for j in jobs}
    with store.connect() as db:
        db.execute("UPDATE research_studies SET status='running',updated=? WHERE id=?",
                   (time.time(), identifier))
    (store.root / "research" / identifier).mkdir(exist_ok=True)
    try:
        return _execute(store, identifier, plan, protocol, jobs, data)
    except Exception as exc:
        _finish(store, identifier, {"execution_completed": False, "research_passed": False,
                                   "conclusion": "insufficient", "reasons": ["execution_error"],
                                   "error": str(exc)})
        raise

def _execute(store, identifier, plan, protocol, jobs, data):
    def window_times(snapshot, window):
        return [at for at in snapshot.times if window["start"] <= at.isoformat() <= window["end"]]

    _stage(store, identifier, "development")
    development = {}
    for job in jobs:
        snapshot = data[job["id"]]
        development[job["id"]] = [
            _trial(store, identifier, f"{job['trial_id']}:development:{i}", "development", job, snapshot,
                   window_times(snapshot, window), provider_for(job["spec"], job["config"]))
            for i, window in enumerate(plan["windows"]["development"])
        ]
    summaries = [{"job_id": j["id"], "name": j["spec"]["name"], "trial_id": j["trial_id"],
                  "execution_completed": all(r["execution_completed"] for r in development[j["id"]]),
                  **window_summary(development[j["id"]])} for j in jobs]
    eligible = [s for s in summaries if s["execution_completed"]]
    if plan.get("promotion_policy"):
        limits = plan["promotion_policy"]["backtest"]
        eligible = [s for s in eligible if s["return_fraction"] > limits["min_net_return_exclusive"]
                    and sum(r["return_fraction"] > 0 for r in development[s["job_id"]]) / protocol.folds
                    >= limits["min_positive_window_fraction"]]
        if not eligible:
            complete = all(s["execution_completed"] for s in summaries)
            return _finish(store, identifier, {
                "execution_completed": complete, "research_passed": False,
                "conclusion": "rejected" if complete else "insufficient",
                "reasons": ["no_candidate_passed_development_gate"], "development": summaries,
                "holdout_used": False,
                "model_usage": usage_summary([r for j in jobs if j["spec"]["provider"] == "ai"
                                               for r in development[j["id"]]]),
            })
    if not eligible:
        return _finish(store, identifier, {"execution_completed": False, "research_passed": False,
            "conclusion": "insufficient", "reasons": ["no_complete_candidate"], "development": summaries})
    selected = min(eligible, key=lambda s: (-s["return_fraction"], -s["max_drawdown_fraction"], s["job_id"]))
    job = next(j for j in jobs if j["id"] == selected["job_id"])
    snapshot = data[job["id"]]
    times = window_times(snapshot, plan["windows"]["holdout"])
    exposed = _holdout_exposures(store, identifier, plan)
    # 选择及依据先落盘，才请求最终留出窗口的任何决策。
    _stage(store, identifier, "selection", selected_job_id=job["id"], development=summaries,
           holdout_exposed_by=exposed)
    holdout = _trial(store, identifier, "holdout", "holdout", job, snapshot, times,
                     provider_for(job["spec"], job["config"]))
    first = jobs[0]
    baseline = _trial(store, identifier, "rules", "control", first, data[first["id"]], times,
                      PriceActionProvider() if snapshot.market == "stocks" else DemoProvider())
    _stage(store, identifier, "robustness")
    stress = []
    if holdout["execution_completed"]:
        for name, factor in (("cost", protocol.cost_multiplier),
                             ("weight_lower", 1 - protocol.weight_perturbation),
                             ("weight_higher", 1 + protocol.weight_perturbation)):
            config = deepcopy(job["config"])
            if name == "cost":
                cost = config["execution"]["cost_bps"]
                config["execution"]["cost_bps"] *= factor
                config["execution"]["limit_buffer_bps"] += cost * (factor - 1)
            else:
                config["risk"]["max_weight"] = min(1.0, config["risk"]["max_weight"] * factor)
            result = _trial(store, identifier, name, "stress", job, snapshot, times,
                            RecordedDecisionProvider(holdout["decisions"]), config)
            stress.append({"scenario": name, "factor": factor, "config": config, **result})
        if protocol.execution_delay_bars:
            result = _trial(store, identifier, "execution_delay", "stress", job, snapshot, times,
                            DelayedRecordedDecisionProvider(holdout["decisions"], times,
                                                            protocol.execution_delay_bars))
            stress.append({"scenario": "execution_delay", "delay_bars": protocol.execution_delay_bars,
                           "config": deepcopy(job["config"]), **result})
    placebos = [{"seed": seed, **_trial(store, identifier, f"placebo:{seed}", "control", job, snapshot,
                                       times, PlaceboProvider(seed))} for seed in protocol.placebo_seeds]
    trial_returns = {j["id"]: [v for r in development[j["id"]] for v in returns_of(r)] for j in jobs}
    periods = 252 if snapshot.market == "stocks" else int(365 * 86400 / INTERVALS[job["spec"]["bar"]].total_seconds())
    diagnostics = statistics(trial_returns, job["id"], returns_of(holdout), periods)
    diagnostics["development_observations"] = len(trial_returns[job["id"]])
    complete = (all(s["execution_completed"] for s in summaries)
                and len(stress) == 3 + bool(protocol.execution_delay_bars)
                and all(r["execution_completed"] for r in [holdout, baseline, *stress, *placebos]))
    outcome = conclude(protocol, execution_completed=complete, development=selected, holdout=holdout,
                       baseline=baseline, stress=stress, placebos=placebos, diagnostics=diagnostics,
                       holdout_exposed=bool(exposed), comparable_trials=all(s["execution_completed"] for s in summaries),
                       synthetic_data=plan["data_quality"]["data_kind"] == "synthetic")
    model_results = [r for j in jobs if j["spec"]["provider"] == "ai" for r in development[j["id"]]]
    if job["spec"]["provider"] == "ai":
        model_results.append(holdout)
    if source_tree_hash() != plan["code_hash"]:
        raise ValueError("研究期间代码发生变化，结果不能按已冻结方案验收")
    return _finish(store, identifier, {
        **outcome, "execution_completed": complete, "selected_job_id": job["id"],
        "development": summaries, "holdout": holdout, "baseline": baseline,
        "baseline_name": "price_action" if snapshot.market == "stocks" else "momentum_rules",
        "cash_baseline_return": 0, "stress": stress, "placebos": placebos, "diagnostics": diagnostics,
        "holdout_exposed_by": exposed, "model_usage": usage_summary(model_results),
        "declared_trial_count": plan["declared_trial_count"],
        "completed_trial_count": sum(s["execution_completed"] for s in summaries),
    })
