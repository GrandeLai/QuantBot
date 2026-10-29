from datetime import UTC, datetime, timedelta
from dataclasses import replace
import io
import json
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.journal import RuntimeBusyError, runtime_lock
from quantbot.ai.research import get_study, register_study, run_study
from quantbot.ai.models import DemoProvider
from quantbot.ai.research_ops import cancel_queued_study, enqueue_study, research_operations, run_due_study, run_research_worker
from quantbot.ai.runtime import RuntimeStore
from quantbot.ai.runtime_cli import main
from quantbot.web.app import create_app
from tests.test_ai_research import CARD, PROTOCOL, register
from tests.test_ai_runtime import setup


def enqueue(store, study, **kwargs):
    return enqueue_study(store, study["id"], planned_at=datetime.now(UTC).isoformat(), **kwargs)


def test_schedule_waits_until_due_runs_once_and_shows_actual_timeline(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register(store, job)
    planned = datetime.now(UTC) + timedelta(hours=1)
    queued = enqueue_study(store, study["id"], planned_at=planned.isoformat())
    assert run_due_study(store)["executed"] is None
    assert research_operations(store)["queue_counts"] == {"queued": 1}
    # 模拟执行器在计划时间到达后进行检查。
    result = run_due_study(store, now=planned + timedelta(seconds=1))
    assert result["executed"]["id"] == queued["id"] and result["executed"]["status"] == "completed"
    assert run_due_study(store, now=planned + timedelta(seconds=2))["executed"] is None
    ops = research_operations(store)
    assert ops["study_counts"] == {"completed": 1}
    assert ops["latest_success"] is not None and ops["latest_failure"] is None
    assert ops["queue"][0]["started"] is not None and ops["queue"][0]["duration_seconds"] >= 0
    assert ops["studies"][0]["conclusion"] == "insufficient"
    assert ops["model_usage"]["decision_calls"] == 0 and ops["model_usage"]["reported_tokens"] is None
    assert store.get(job["id"])["stage"] == "candidate"


def test_queue_requires_explicit_ai_authorization_and_reserves_budget(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    ai = store.create({**job["spec"], "provider": "ai"}, job["config"])
    study = register_study(store, job_ids=[ai["id"]], card=CARD, protocol=PROTOCOL)
    with pytest.raises(ValueError, match="明确允许"):
        enqueue(store, study)
    with pytest.raises(ValueError, match="布尔值"):
        enqueue(store, study, allow_model_calls="false")
    entry = enqueue(store, study, allow_model_calls=True)
    assert research_operations(store)["reserved_model_requests"] == study["plan"]["max_model_requests"]
    with pytest.raises(ValueError, match="已有"):
        enqueue(store, study, allow_model_calls=True)
    cancel_queued_study(store, entry["id"])
    assert research_operations(store)["reserved_model_requests"] == 0
    assert run_due_study(store)["executed"] is None
    with pytest.raises(ValueError, match="只能取消"):
        cancel_queued_study(store, entry["id"])


def test_queue_and_direct_execution_share_lock_without_consuming_plan(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    entry = enqueue(store, register(store, job))
    with runtime_lock(store.root / "research"):
        with pytest.raises(RuntimeBusyError):
            run_due_study(store)
    assert research_operations(store)["queue"][0]["id"] == entry["id"]
    assert research_operations(store)["queue"][0]["status"] == "queued"


def test_interrupted_queue_retains_partial_study_and_never_retries(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register(store, job)
    entry = enqueue(store, study)
    with store.connect() as db:
        db.execute("UPDATE research_queue SET status='running',started=? WHERE id=?", (time.time(), entry["id"]))
        db.execute("UPDATE research_studies SET status='running' WHERE id=?", (study["id"],))
    result = run_due_study(store)
    assert result["executed"] is None and result["recovered"][0]["status"] == "interrupted"
    archived = get_study(store, study["id"])
    assert archived["status"] == "failed" and archived["report"]["reasons"] == ["interrupted"]
    assert archived["trials"] == []
    assert run_due_study(store) == {"executed": None, "recovered": []}


def test_corrupt_frozen_input_records_failure_and_is_not_retried(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register(store, job)
    enqueue(store, study)
    Path(study["plan"]["snapshot"]["path"]).write_text("{}")
    result = run_due_study(store)
    assert result["executed"]["status"] == "failed"
    assert "行情指纹" in result["executed"]["error"]
    assert run_due_study(store)["executed"] is None
    assert get_study(store, study["id"])["trials"] == []


@pytest.mark.parametrize("status", ["queued", "running"])
def test_corrupted_plan_is_retained_as_terminal_queue_failure(tmp_path, status):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register(store, job)
    entry = enqueue(store, study)
    with store.connect() as db:
        db.execute("UPDATE research_studies SET plan_hash='invalid' WHERE id=?", (study["id"],))
        db.execute("UPDATE research_queue SET status=? WHERE id=?", (status, entry["id"]))
    result = run_due_study(store)
    outcome = result["executed"] if status == "queued" else result["recovered"][0]
    assert outcome["status"] in {"failed", "interrupted"} and "指纹" in outcome["error"]
    assert run_due_study(store) == {"executed": None, "recovered": []}


def test_api_and_cli_share_queue_without_starting_trade_worker(tmp_path):
    _, base, _, _ = setup(tmp_path, "crypto", count=100)
    store = RuntimeStore(tmp_path / "web" / "automation")
    job = store.create(base["spec"], base["config"])
    study = register(store, job)
    client = TestClient(create_app(config=job["config"], runtime_dir=tmp_path / "web"))
    route = "/api/quantbot/runtime"
    body = {"planned_at": datetime.now(UTC).isoformat()}
    assert client.post(f"{route}/research/{study['id']}/queue", json={**body, "allow_model_calls": "false"}).status_code == 422
    assert client.post(f"{route}/research/{study['id']}/queue", json=body).status_code == 200
    output = io.StringIO()
    assert main(["--root", str(store.root), "research-worker", "--once"], stdout=output) == 0
    assert json.loads(output.getvalue())["executed"]["status"] == "completed"
    assert client.get(route + "/operations").json()["queue_counts"] == {"completed": 1}
    assert client.post(route + "/research-queue/run", json={}).json()["executed"] is None
    assert store.overview()["worker_online"] is False


def test_worker_heartbeat_stops_cleanly_without_starting_any_job(tmp_path):
    store = RuntimeStore(tmp_path / "runtime")
    stop = threading.Event()
    thread = threading.Thread(target=run_research_worker, args=(store, stop))
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if research_operations(store)["worker_online"]:
                break
            time.sleep(0.02)
        assert research_operations(store)["worker_online"]
    finally:
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not research_operations(store)["worker_online"]
    assert store.overview()["jobs"] == []


def test_failed_ai_trial_counts_durable_attempts_and_partial_usage(tmp_path, monkeypatch):
    store, base, _, _ = setup(tmp_path, "crypto", count=100)
    job = store.create({**base["spec"], "provider": "ai"}, base["config"])
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL, data_kind="synthetic")

    class ControlledProvider:
        def __init__(self):
            self.calls = 0

        def decide(self, context, account, config):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("controlled failure after one reported response")
            return replace(DemoProvider().decide(context, account, config), usage={"total_tokens": 31})

    monkeypatch.setattr("quantbot.ai.research.provider_for", lambda *args: ControlledProvider())
    with pytest.raises(RuntimeError, match="controlled failure"):
        run_study(store, study["id"], allow_model_calls=True)
    usage = research_operations(store)["model_usage"]
    assert usage == {"decision_calls": 2, "reported_tokens": 31, "missing_usage_calls": 1,
                     "unknown_trials": 0, "unresolved_calls": 1}
    assert get_study(store, study["id"])["status"] == "failed"
