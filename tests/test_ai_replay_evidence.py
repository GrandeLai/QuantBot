"""回放证据必须先于调用落盘；失败、断电式退出和旧准入撤回的行为验证。"""

import io
import json
from dataclasses import replace
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest

from quantbot.ai import evaluation, replay_evidence
from quantbot.ai.backtest import backtest
from quantbot.ai.evaluation import evaluate, walk_forward
from quantbot.ai.models import DemoProvider
from quantbot.ai.local_execution import account_view, new_account
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.research import get_study, register_study, run_study
from quantbot.ai.runtime_cli import main
from quantbot.ai.transport import ProviderError
from quantbot.web.app import create_app
from tests.test_ai_research import CARD, PROTOCOL
from tests.test_ai_runtime import setup


class CrashAfterOneDecision:
    def __init__(self):
        self.calls = 0

    def decide(self, context, account, config):
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("controlled process failure")
        return DemoProvider().decide(context, account, config)


def test_successful_journal_keeps_exact_call_inputs_decisions_and_fills(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto")
    result = evaluate(store, job["id"], last_bars=4)
    journal = inspect_replay(result["candidate"]["journal_path"])
    assert journal["status"] == "completed" and journal["execution_completed"]
    assert journal["decision_attempts"] == journal["resolved_decisions"] == 3
    assert journal["unresolved_decision_calls"] == []
    assert journal["completed_steps"] == 4
    decisions = [r["payload"] for r in journal["records"] if r["kind"] == "decision"]
    assert decisions == json.loads(json.dumps(result["candidate"]["decisions"]))
    inputs = [r["payload"] for r in journal["records"] if r["kind"] == "decision_started"]
    assert [p["market"]["snapshot_id"] for p in inputs] == [d["snapshot_id"] for d in decisions]
    assert inputs[0]["account"]["positions"] == {}
    assert any(r["payload"]["orders"] for r in journal["records"] if r["kind"] == "account")
    assert Path(result["journal_directory"], "manifest.json").is_file()
    output = io.StringIO()
    assert main(["replay-show", result["candidate"]["journal_path"]], stdout=output) == 0
    assert json.loads(output.getvalue())["execution_completed"]
    assert result["candidate"]["equity_curve"][0]["positions"] == {}
    accounts = [r["payload"]["account"] for r in journal["records"] if r["kind"] == "account"]
    assert [r["positions"] for r in result["candidate"]["equity_curve"]] == [r["positions"] for r in accounts]


def test_account_snapshots_do_not_change_with_future_positions_stops_or_orders():
    state = new_account(1000)
    state.update(positions={"SPY": "1"}, stops={"SPY": "95"}, prices={"SPY": "100"}, average_cost={"SPY": "100"},
                 pending=[{"symbol": "SPY", "quantity": "1", "side": "buy"}])
    before = account_view(state)
    state["positions"]["SPY"] = "2"
    state["stops"]["SPY"] = "98"
    state["pending"][0]["quantity"] = "5"
    assert before["positions"] == {"SPY": "1"}
    assert before["stops"] == {"SPY": "95"}
    assert before["pending_orders"][0]["quantity"] == "1"


def test_rejected_plan_retains_response_usage_without_reusing_it_on_later_errors(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")

    class ControlledProvider:
        def __init__(self):
            self.calls = 0

        def decide(self, context, account, config):
            self.calls += 1
            if self.calls > 1:
                raise ProviderError("response unavailable")
            return replace(DemoProvider().decide(context, account, config), usage={"total_tokens": 31})

    def reject_plan(*args):
        raise ValueError("controlled plan rejection")

    monkeypatch.setattr(evaluation, "provider_for", lambda *args: ControlledProvider())
    monkeypatch.setattr(evaluation, "create_plan", reject_plan)
    result = evaluate(store, job["id"], last_bars=4)
    assert not result["passed"]
    errors = result["candidate"]["errors"]
    assert errors[0]["raw_decision"]["usage"] == {"total_tokens": 31}
    assert all("raw_decision" not in row for row in errors[1:])
    journal = inspect_replay(result["candidate"]["journal_path"])
    assert journal["status"] == "completed" and not journal["execution_completed"]
    assert journal["reported_tokens"] == 31 and journal["usage_reported_calls"] == 1
    assert journal["usage_missing_calls"] == 2 and journal["resolved_decisions"] == 3


def test_failed_replay_keeps_completed_evidence_and_revokes_prior_admission(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    evaluate(store, job["id"], last_bars=4)
    assert store.get(job["id"])["stage"] == "evaluated"
    monkeypatch.setattr(evaluation, "provider_for", lambda *args: CrashAfterOneDecision())
    with pytest.raises(RuntimeError, match="controlled process failure"):
        evaluate(store, job["id"], last_bars=4)
    attempts = [r for r in store.history(job["id"])["events"] if r["kind"] == "evaluation_started"]
    failed = inspect_replay(Path(attempts[0]["payload"]["journal_directory"]) / "candidate.jsonl")
    assert failed["status"] == "failed" and not failed["execution_completed"]
    assert failed["decision_attempts"] == 2 and failed["resolved_decisions"] == 1
    assert len(failed["unresolved_decision_calls"]) == 1
    assert failed["last_account"]["orders"][0]["status"] == "filled"
    assert store.get(job["id"])["stage"] == "candidate"
    with pytest.raises(ValueError, match="历史评估"):
        store.control(job["id"], "resume")


def test_journal_write_failure_stops_before_the_model_is_called(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    original = replay_evidence.append_json_line_locked
    calls = []

    def disk_failure(path, record):
        if record["kind"] == "decision_started":
            raise OSError("controlled disk full")
        original(path, record)

    monkeypatch.setattr(replay_evidence, "append_json_line_locked", disk_failure)
    monkeypatch.setattr(DemoProvider, "decide", lambda *args: calls.append(args))
    with pytest.raises(OSError, match="disk full"):
        evaluate(store, job["id"], last_bars=4)
    assert calls == [] and store.get(job["id"])["stage"] == "candidate"
    files = list((store.root / "evaluations").glob("*/candidate.jsonl"))
    assert len(files) == 1
    journal = inspect_replay(files[0])
    assert journal["status"] == "failed" and journal["decision_attempts"] == 0


def test_failure_report_is_saved_for_one_shot_cli_and_web(tmp_path, monkeypatch):
    _, job, prices, _ = setup(tmp_path, "crypto")
    monkeypatch.setattr(evaluation, "provider_for", lambda *args: CrashAfterOneDecision())
    output = tmp_path / "backtest"
    with pytest.raises(RuntimeError, match="failure.json"):
        backtest(job["config"], output=output, market="crypto", provider="demo", csv_data=prices.read_text(), last_bars=4)
    failure = json.loads((output / "failure.json").read_text())
    assert failure["status"] == "failed" and not failure["execution_completed"]
    assert failure["input_data"]["rows"] and failure["system_prompt"]
    assert failure["replays"][0]["resolved_decisions"] == 1
    assert not (output / "report.json").exists()
    client = TestClient(create_app(config=job["config"], runtime_dir=tmp_path / "web"))
    response = client.post("/api/quantbot/allocate", json={"market": "crypto", "mode": "simulate", "provider": "demo",
                           "csv_data": prices.read_text(), "last_bars": 4})
    assert response.status_code == 500 and "回测中断" in response.json()["detail"]["message"]
    downloaded = client.get(response.json()["detail"]["download_url"])
    assert downloaded.status_code == 200 and downloaded.json()["status"] == "failed"
    assert downloaded.json()["replays"][0]["resolved_decisions"] == 1
    assert client.get("/api/quantbot/backtests/invalid/failure").status_code == 404


def test_multi_candidate_failure_revokes_every_old_admission(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    other = store.create(job["spec"], job["config"])
    for item in (job, other):
        evaluate(store, item["id"], last_bars=4)
    monkeypatch.setattr(evaluation, "provider_for", lambda *args: CrashAfterOneDecision())
    with pytest.raises(RuntimeError, match="controlled process failure"):
        walk_forward(store, [job["id"], other["id"]], selection_bars=4, test_bars=3, folds=2)
    assert all(store.get(item["id"])["stage"] == "candidate" for item in (job, other))


EXIT_SCRIPT = '''
import os, sys
from quantbot.ai import evaluation, research
from quantbot.ai.models import DemoProvider
from quantbot.ai.runtime import RuntimeStore

class ExitAfterOne:
    def __init__(self): self.calls = 0
    def decide(self, context, account, config):
        self.calls += 1
        if self.calls == 2: os._exit(17)
        return DemoProvider().decide(context, account, config)

store = RuntimeStore(sys.argv[1])
if sys.argv[2] == 'evaluate':
    evaluation.provider_for = lambda *args: ExitAfterOne()
    evaluation.evaluate(store, sys.argv[3], last_bars=4)
else:
    research.provider_for = lambda *args: ExitAfterOne()
    research.run_study(store, sys.argv[3])
'''


def test_forced_process_exit_keeps_unresolved_call_and_counts_holdout_exposure(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    child = subprocess.run([sys.executable, "-c", EXIT_SCRIPT, str(store.root), "evaluate", job["id"]], capture_output=True, timeout=30)
    assert child.returncode == 17, child.stderr.decode()
    path = next((store.root / "evaluations").glob("*/candidate.jsonl"))
    journal = inspect_replay(path)
    assert journal["status"] == "unfinished" and journal["resolved_decisions"] == 1
    assert len(journal["unresolved_decision_calls"]) == 1 and not journal["execution_completed"]
    assert store.get(job["id"])["stage"] == "candidate"
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL, data_kind="synthetic")
    result = run_study(store, study["id"])
    assert "evaluation-run:" + path.parent.name in result["report"]["holdout_exposed_by"]
    assert result["report"]["checks"]["unexposed_holdout"] is False


def test_interrupted_research_recovers_evidence_without_repeating_a_call(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL, data_kind="synthetic")
    child = subprocess.run([sys.executable, "-c", EXIT_SCRIPT, str(store.root), "study", study["id"]], capture_output=True, timeout=30)
    assert child.returncode == 17, child.stderr.decode()
    assert get_study(store, study["id"])["status"] == "running"
    monkeypatch.setattr("quantbot.ai.research.provider_for", lambda *args: pytest.fail("must not retry interrupted requests"))
    recovered = run_study(store, study["id"])
    assert recovered["status"] == "failed" and recovered["report"]["reasons"] == ["interrupted"]
    trial = recovered["trials"][0]
    assert trial["status"] == "failed" and trial["result"]["replay_evidence"]["resolved_decisions"] == 1
    assert len(trial["result"]["replay_evidence"]["unresolved_decision_calls"]) == 1


def test_read_only_inspection_preserves_partial_tail_and_rejects_middle_corruption(tmp_path):
    path = tmp_path / "interrupted.jsonl"
    start = {"sequence": 0, "recorded_at": "2025-01-01T00:00:00+00:00", "kind": "started", "payload": {"schema_version": 1}}
    encoded = json.dumps(start) + "\n"
    path.write_text(encoded + '{"sequence":1,')
    result = inspect_replay(path)
    assert result["status"] == "unfinished" and result["incomplete_tail"] and result["record_count"] == 1
    output = io.StringIO()
    unused_root = tmp_path / "not-created"
    assert main(["--root", str(unused_root), "replay-show", str(path)], stdout=output) == 2
    assert not unused_root.exists() and json.loads(output.getvalue())["incomplete_tail"]
    # 中文正文可能在多字节字符中途被截断；不能因此失去此前完整记录。
    path.write_bytes(encoded.encode() + b'{"error":"' + "中".encode()[:1])
    partial_utf8 = inspect_replay(path)
    assert partial_utf8["incomplete_tail"] and partial_utf8["records"] == result["records"]
    path.write_bytes(encoded.encode() + b'{"error":"' + "中".encode()[:1] + b"\n")
    with pytest.raises(ValueError, match="第 2 行损坏"):
        inspect_replay(path)
    path.write_text(encoded + "bad-json\n" + encoded)
    with pytest.raises(ValueError, match="第 2 行损坏"):
        inspect_replay(path)
    path.write_text(encoded + json.dumps({"sequence": 1, "recorded_at": start["recorded_at"], "kind": "completed",
                                         "payload": {"execution_completed": "false", "account": {}}}) + "\n")
    with pytest.raises(ValueError, match="明确的执行结论"):
        inspect_replay(path)
