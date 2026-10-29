"""研究计划与运行观察。仅消费已冻结研究；中断不自动重试，单次只能运行一个研究。"""

from datetime import UTC, datetime
import json
import shutil
import sqlite3
import threading
import time
from uuid import uuid4

from .journal import RuntimeBusyError, runtime_lock
from .research import _run_study, get_study
from .vintages import instant


def enqueue_study(store, study_id, *, planned_at, allow_model_calls=False):
    if type(allow_model_calls) is not bool:
        raise ValueError("模型调用授权必须为布尔值")
    planned = instant(planned_at).timestamp()
    study = get_study(store, study_id)
    if study["status"] != "registered":
        raise ValueError("只有尚未运行的冻结研究可以排队")
    if study["plan"]["max_model_requests"] and not allow_model_calls:
        raise ValueError("该研究使用 AI；排队前必须明确允许模型调用")
    identifier = uuid4().hex
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT status FROM research_studies WHERE id=?", (study_id,)).fetchone()[0] != "registered":
            raise ValueError("研究已开始，不能排队")
        try:
            db.execute("INSERT INTO research_queue VALUES (?,?,?,'queued',?,?,NULL,NULL,NULL)",
                       (identifier, study_id, planned, int(allow_model_calls), time.time()))
        except sqlite3.IntegrityError:
            raise ValueError("该研究已有等待或运行中的计划") from None
        store.event(db, None, "research_queued", {"study_id": study_id, "queue_id": identifier,
                    "planned_at": planned_at, "allow_model_calls": allow_model_calls})
        return dict(db.execute("SELECT * FROM research_queue WHERE id=?", (identifier,)).fetchone())


def cancel_queued_study(store, identifier):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        entry = db.execute("SELECT * FROM research_queue WHERE id=?", (identifier,)).fetchone()
        if entry is None or entry["status"] != "queued":
            raise ValueError("只能取消尚未开始的研究计划")
        db.execute("UPDATE research_queue SET status='canceled',finished=? WHERE id=?", (time.time(), identifier))
        store.event(db, None, "research_queue_canceled", {"study_id": entry["study_id"], "queue_id": identifier})
        return dict(db.execute("SELECT * FROM research_queue WHERE id=?", (identifier,)).fetchone())


def _finish_queue(store, identifier, status, error=None):
    with store.connect() as db:
        db.execute("UPDATE research_queue SET status=?,finished=?,error=? WHERE id=?",
                   (status, time.time(), error, identifier))
        entry = dict(db.execute("SELECT * FROM research_queue WHERE id=?", (identifier,)).fetchone())
        store.event(db, None, "research_queue_finished", {"study_id": entry["study_id"], "queue_id": identifier,
                    "status": status, "error": error})
        return entry


def run_due_study(store, *, now=None):
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("调度时间必须带时区")
    # 与手动运行共享同一锁。取得锁后，旧 running 记录才可确定为已中断。
    with runtime_lock(store.root / "research"):
        with store.connect() as db:
            interrupted = db.execute("SELECT id,study_id FROM research_queue WHERE status='running'").fetchall()
        recovered = []
        for entry in interrupted:
            try:
                study = get_study(store, entry["study_id"])
            except (ValueError, OSError) as exc:
                recovered.append(_finish_queue(store, entry["id"], "interrupted", "研究证据损坏；未重试：" + str(exc)))
                continue
            if study["status"] == "completed":
                recovered.append(_finish_queue(store, entry["id"], "completed"))
                continue
            error = "进程中断；已保留证据，本计划不会自动重试"
            if study["status"] == "running":
                try:
                    _run_study(store, entry["study_id"])
                except (OSError, ValueError) as exc:
                    error += "；归档错误：" + str(exc)
            recovered.append(_finish_queue(store, entry["id"], "interrupted", error))
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entry = db.execute("SELECT * FROM research_queue WHERE status='queued' AND planned<=? ORDER BY planned,created LIMIT 1",
                               (now.timestamp(),)).fetchone()
            if entry is None:
                return {"executed": None, "recovered": recovered}
            db.execute("UPDATE research_queue SET status='running',started=? WHERE id=?", (time.time(), entry["id"]))
        try:
            study = get_study(store, entry["study_id"])
            if study["status"] != "registered":
                return {"executed": _finish_queue(store, entry["id"], "skipped", "研究已由其他入口处理，未重复运行"),
                        "recovered": recovered}
            result = _run_study(store, entry["study_id"], allow_model_calls=bool(entry["model_authorized"]))
            complete = result["report"]["execution_completed"]
            executed = _finish_queue(store, entry["id"], "completed" if complete else "failed", result["report"].get("error"))
        except Exception as exc:
            # 外部异常保留失败状态；未知异常不把可能含敏感响应的全文写入看板。
            error = str(exc) if isinstance(exc, (ValueError, OSError, RuntimeError)) else type(exc).__name__
            executed = _finish_queue(store, entry["id"], "failed", error)
        return {"executed": executed, "recovered": recovered}


def research_operations(store):
    now = time.time()
    with store.connect() as db:
        studies = [dict(r) for r in db.execute("""SELECT id,status,created,updated,
            json_extract(plan,'$.card.title') AS title,
            json_extract(plan,'$.data_quality') AS data_quality,
            json_extract(plan,'$.max_model_requests') AS max_model_requests,
            json_extract(report,'$.conclusion') AS conclusion,
            json_extract(report,'$.error') AS error,
            (SELECT COUNT(*) FROM research_trials t WHERE t.study_id=s.id) AS trials,
            (SELECT COUNT(*) FROM research_trials t WHERE t.study_id=s.id AND t.status='failed') AS failed_trials
            FROM research_studies s ORDER BY updated DESC LIMIT 50""")]
        for study in studies:
            study["data_quality"] = json.loads(study["data_quality"])
        queue = [dict(r) for r in db.execute("""SELECT q.*,json_extract(s.plan,'$.card.title') AS title
            FROM research_queue q JOIN research_studies s ON s.id=q.study_id
            ORDER BY q.created DESC LIMIT 100""")]
        for entry in queue:
            entry["delay_seconds"] = max(0, (entry["started"] or now) - entry["planned"]) if entry["status"] in {"queued", "running", "completed", "failed", "interrupted"} else None
            entry["duration_seconds"] = max(0, (entry["finished"] or now) - entry["started"]) if entry["started"] is not None else None
        counts = dict(db.execute("SELECT status,COUNT(*) FROM research_studies GROUP BY status"))
        queue_counts = dict(db.execute("SELECT status,COUNT(*) FROM research_queue GROUP BY status"))
        heartbeat = db.execute("SELECT heartbeat FROM research_worker WHERE id=1").fetchone()
        latest_success = db.execute("SELECT MAX(updated) FROM research_studies WHERE status='completed'").fetchone()[0]
        latest_failure = db.execute("SELECT MAX(updated) FROM research_studies WHERE status='failed'").fetchone()[0]
        budget = db.execute("""SELECT COALESCE(SUM(json_extract(s.plan,'$.max_model_requests')),0)
            FROM research_queue q JOIN research_studies s ON s.id=q.study_id WHERE q.status IN ('queued','running')""").fetchone()[0]
        usage = db.execute("""SELECT
            COALESCE(SUM(COALESCE(json_extract(t.result,'$.provider_calls'),
                json_extract(t.result,'$.replay_evidence.decision_attempts'))),0) AS decision_calls,
            SUM(COALESCE(json_extract(t.result,'$.reported_tokens'),
                json_extract(t.result,'$.replay_evidence.reported_tokens'))) AS reported_tokens,
            COALESCE(SUM(COALESCE(json_extract(t.result,'$.usage_missing_calls'),
                json_extract(t.result,'$.replay_evidence.usage_missing_calls'),
                json_extract(t.result,'$.replay_evidence.decision_attempts'))),0) AS missing_usage_calls,
            SUM(CASE WHEN COALESCE(json_extract(t.result,'$.provider_calls'),
                json_extract(t.result,'$.replay_evidence.decision_attempts')) IS NULL THEN 1 ELSE 0 END) AS unknown_trials,
            COALESCE(SUM(json_array_length(t.result,'$.replay_evidence.unresolved_decision_calls')),0) AS unresolved_calls
            FROM research_trials t JOIN research_studies s ON s.id=t.study_id,
            json_each(s.plan,'$.candidates') candidate
            WHERE json_extract(candidate.value,'$.id')=t.job_id
            AND json_extract(candidate.value,'$.spec.provider')='ai'
            AND t.stage IN ('development','holdout')""").fetchone()
    disk = shutil.disk_usage(store.root)
    return {"as_of": now, "studies": studies, "queue": queue, "study_counts": counts, "queue_counts": queue_counts,
            "latest_success": latest_success, "latest_failure": latest_failure,
            "worker_heartbeat": heartbeat[0] if heartbeat else None,
            "worker_online": bool(heartbeat and 0 <= now - heartbeat[0] < 30),
            "concurrency_limit": 1, "reserved_model_requests": budget, "model_usage": dict(usage),
            "disk": {"free_bytes": disk.free, "total_bytes": disk.total},
            "basis": "冻结历史窗口；旧历史行情不按距今天数判为过期。调用数为决策次数，不能当作供应方 HTTP 请求数或费用。"}


def run_research_worker(store, stop=None):
    stop = stop or threading.Event()
    with runtime_lock(store.root / "research-worker-lock"):
        def heartbeat():
            while not stop.is_set():
                with store.connect() as db:
                    db.execute("INSERT OR REPLACE INTO research_worker VALUES (1,?)", (time.time(),))
                stop.wait(5)

        heart = threading.Thread(target=heartbeat, daemon=True)
        heart.start()
        try:
            while not stop.is_set():
                try:
                    run_due_study(store)
                except RuntimeBusyError:
                    # 手动研究占用同一执行槽；保留计划，下一轮再检查。
                    pass
                stop.wait(1)
        finally:
            stop.set()
            heart.join()
            with store.connect() as db:
                db.execute("DELETE FROM research_worker")
