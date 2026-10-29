"""研究流程的冻结证据、时间隔离、费用授权、失败留存及研究结论。"""

from copy import deepcopy
import io
import json

from fastapi.testclient import TestClient
import pytest

from quantbot.ai import research
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.models import DemoProvider
from quantbot.ai.research import get_study, list_studies, register_study, run_study
from quantbot.ai.research_protocol import ResearchProtocol, conclude
from quantbot.ai.runtime_cli import main
from quantbot.ai.transport import ProviderError
from quantbot.web.app import create_app
from quantbot.experiment_registry import ExperimentRegistry

from tests.test_ai_runtime import setup


CARD = {"title": "动量假设", "hypothesis": "正动量在后续区间仍有收益", "rationale": "检验趋势延续",
        "failure_condition": "成本提高后失效或不能超过规则对照则淘汰"}
PROTOCOL = {"development_bars": 6, "folds": 2, "gap_bars": 1, "holdout_bars": 6,
            "min_observations": 3, "placebo_seeds": [7, 42]}


def register(store, job, **kwargs):
    return register_study(store, job_ids=[job["id"]], card=CARD, protocol={**PROTOCOL, **kwargs}, data_kind="synthetic")


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_frozen_research_end_to_end_and_independent_accounts(tmp_path, market):
    store, job, path, _ = setup(tmp_path, market, count=100)
    original_account = deepcopy(store.get(job["id"])["account"])
    study = register(store, job)
    assert study["status"] == "registered" and study["trials"] == []
    assert study["plan"]["max_model_requests"] == 0
    assert study["plan"]["data_quality"]["point_in_time_verified"] is False
    assert study["plan"]["windows"]["development"][-1]["end"] < study["plan"]["windows"]["holdout"]["start"]
    path.write_text("外部 CSV 已改变，研究仍应读取冻结快照")
    result = run_study(store, study["id"])
    report = result["report"]
    assert report["execution_completed"] and result["status"] == "completed"
    assert report["research_passed"] is False  # 本例的通过运行不等于研究达标。
    assert report["checks"]["historical_data"] is False
    assert report["model_usage"]["provider_calls"] == 0
    assert len(result["trials"]) == 9  # 开发 2 + 留出/规则 2 + 压力 3 + 随机对照 2。
    assert {t["status"] for t in result["trials"]} == {"completed"}
    for trial in result["trials"]:
        assert trial["result"]["equity_curve"][0]["equity"] == job["config"]["execution"]["initial_cash"]
    raw = report["holdout"]["decisions"][0]["raw_decision"]
    assert raw["provider"] == "demo_rules" and raw["allocations"]
    assert report["stress"][0]["config"]["execution"]["cost_bps"] == 2 * job["config"]["execution"]["cost_bps"]
    assert store.get(job["id"])["account"] == original_account
    assert store.get(job["id"])["stage"] == "candidate"
    from pathlib import Path
    exported = json.loads(Path(report["report_path"]).read_text())
    assert exported["plan_hash"] == study["plan_hash"] and len(exported["trials"]) == 9
    with pytest.raises(ValueError, match="已经运行"):
        run_study(store, study["id"])


def test_selection_is_saved_before_holdout_and_stress_never_calls_model(tmp_path, monkeypatch):
    store, base, _, _ = setup(tmp_path, "crypto", count=100)
    jobs = []
    for weight in (0.1, 0.2):
        config = deepcopy(base["config"])
        config["risk"]["max_weight"] = weight
        jobs.append(store.create({**base["spec"], "provider": "ai", "name": str(weight)}, config))
    study = register_study(store, job_ids=[j["id"] for j in jobs], card=CARD, protocol=PROTOCOL)
    calls = []

    class CheckedProvider:
        def decide(self, context, account, config):
            if context.as_of.isoformat() >= study["plan"]["windows"]["holdout"]["start"]:
                selection = [e for e in get_study(store, study["id"])["events"] if e["kind"] == "research_selection"]
                assert len(selection) == 1
                assert selection[0]["payload"]["selected_job_id"] == jobs[1]["id"]
            calls.append(context.as_of.isoformat())
            return DecisionBatch((Allocation("BTC/USDT", config["risk"]["max_weight"], 1, "时点输入"),),
                                 "fixture", "fixture", usage={"total_tokens": 11})

    monkeypatch.setattr(research, "provider_for", lambda *args: CheckedProvider())
    with pytest.raises(ValueError, match="明确允许"):
        run_study(store, study["id"])
    assert not calls and get_study(store, study["id"])["status"] == "registered"
    result = run_study(store, study["id"], allow_model_calls=True)
    assert len(calls) == study["plan"]["max_model_requests"] == 25
    assert result["report"]["model_usage"]["reported_tokens"] == 275
    from quantbot.ai.research_ops import research_operations
    usage = research_operations(store)["model_usage"]
    assert usage["decision_calls"] == 25 and usage["reported_tokens"] == 275
    assert result["report"]["diagnostics"]["trial_count"] == 2
    assert result["report"]["diagnostics"]["pbo"]["status"] == "available"


def test_model_budget_and_invalid_protocol_rejected_before_fetch(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    ai = store.create({**job["spec"], "provider": "ai"}, job["config"])
    monkeypatch.setattr(research, "fetch_snapshot", lambda *a, **kw: pytest.fail("不应读取行情"))
    with pytest.raises(ValueError, match="超过预算"):
        register(store, ai, model_request_budget=0)
    for values in ({"folds": True}, {"min_dsr": float("nan")}, {"placebo_seeds": [1, 1]}, {"gap_bars": 0}):
        with pytest.raises(ValueError):
            register(store, job, **values)
    assert list_studies(store) == []


def test_failed_candidate_stays_in_ledger_and_blocks_validated_conclusion(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    config = deepcopy(job["config"])
    config["llm"]["model"] = "broken"
    broken = store.create(job["spec"], config)

    class BrokenProvider:
        def decide(self, *args):
            raise ProviderError("记录这个失败试验")

    monkeypatch.setattr(research, "provider_for", lambda spec, cfg:
                        BrokenProvider() if cfg["llm"]["model"] == "broken" else DemoProvider())
    study = register_study(store, job_ids=[job["id"], broken["id"]], card=CARD, protocol=PROTOCOL)
    result = run_study(store, study["id"])
    report = result["report"]
    assert report["conclusion"] == "insufficient" and report["declared_trial_count"] == 2
    assert report["completed_trial_count"] == 1 and report["diagnostics"]["trial_count"] == 2
    failures = [t for t in result["trials"] if t["job_id"] == broken["id"]]
    assert len(failures) == 2 and all(t["status"] == "failed" and t["result"]["errors"] for t in failures)
    assert list_studies(store)[0]["conclusion"] == "insufficient"


def test_holdout_exposure_detected_even_when_both_plans_were_registered_before_run(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    first, second = register(store, job), register(store, job)
    run_study(store, first["id"])
    result = run_study(store, second["id"])
    assert result["report"]["holdout_exposed_by"] == [first["id"]]
    assert result["report"]["conclusion"] == "insufficient"
    assert result["report"]["checks"]["unexposed_holdout"] is False


def test_frozen_input_tampering_rejected_before_calls(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register(store, job)
    monkeypatch.setattr(research, "provider_for", lambda *a: pytest.fail("不应调用模型"))
    from pathlib import Path
    path = Path(study["plan"]["snapshot"]["path"])
    payload = json.loads(path.read_text())
    payload["rows"][0]["close"] += 1
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="行情指纹"):
        run_study(store, study["id"])
    assert get_study(store, study["id"])["status"] == "registered"


def test_prior_basic_evaluation_is_not_an_unseen_holdout(tmp_path):
    from quantbot.ai.evaluation import evaluate
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    previous = evaluate(store, job["id"], last_bars=6)
    study = register(store, job)
    result = run_study(store, study["id"])
    assert "evaluation:" + previous["id"] in result["report"]["holdout_exposed_by"]
    assert result["report"]["conclusion"] == "insufficient"


def test_crash_records_partial_trials_and_does_not_retry(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register(store, job)

    class Crash:
        def decide(self, *args):
            raise RuntimeError("进程级异常")

    monkeypatch.setattr(research, "provider_for", lambda *a: Crash())
    with pytest.raises(RuntimeError, match="进程级异常"):
        run_study(store, study["id"])
    result = get_study(store, study["id"])
    assert result["status"] == "failed" and result["trials"][0]["status"] == "failed"
    assert result["trials"][0]["result"]["error"] == "进程级异常"
    with pytest.raises(ValueError, match="已经运行"):
        run_study(store, study["id"])
    # 模拟另一研究在进程被终止后遗留 running；重启只归档，不重新执行。
    another = register(store, job)
    with store.connect() as db:
        db.execute("UPDATE research_studies SET status='running' WHERE id=?", (another["id"],))
    interrupted = run_study(store, another["id"])
    assert interrupted["report"]["reasons"] == ["interrupted"] and interrupted["trials"] == []


def test_research_conclusion_distinguishes_failure_and_insufficient_evidence():
    candidate = {"return_fraction": 0.1, "max_drawdown_fraction": -0.01}
    diagnostics = {"observations": 40, "development_observations": 90,
                   "dsr": {"status": "available", "confidence": 0.99},
                   "pbo": {"status": "available", "probability": 0.1}}
    args = dict(execution_completed=True, development=candidate, holdout=candidate,
                baseline={"return_fraction": 0.01}, stress=[candidate] * 3,
                placebos=[{"return_fraction": 0.02}], diagnostics=diagnostics,
                holdout_exposed=False, comparable_trials=True)
    assert conclude(ResearchProtocol(), **args)["conclusion"] == "validated"
    assert conclude(ResearchProtocol(), **args, synthetic_data=True)["conclusion"] == "insufficient"
    assert conclude(ResearchProtocol(), **{**args, "holdout": {**candidate, "return_fraction": -0.1}})["conclusion"] == "rejected"
    assert conclude(ResearchProtocol(), **{**args, "holdout_exposed": True})["conclusion"] == "insufficient"
    diagnostics["observations"] = 2
    assert conclude(ResearchProtocol(), **args)["conclusion"] == "insufficient"


def test_api_cli_share_research_and_reject_unknown_fields(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    client = TestClient(create_app(config=job["config"], runtime_dir=tmp_path / "web"))
    # Web 与 CLI 使用同一 runtime 根目录。
    from quantbot.ai.runtime import RuntimeStore
    web_store = RuntimeStore(tmp_path / "web" / "automation")
    web_job = web_store.create(job["spec"], job["config"])
    body = {"job_ids": [web_job["id"]], "card": CARD, "protocol": PROTOCOL}
    route = "/api/quantbot/runtime/research"
    assert client.post(route, json={**body, "execute": True}).status_code == 422
    response = client.post(route, json=body)
    assert response.status_code == 200
    study = response.json()
    output = io.StringIO()
    assert main(["--root", str(web_store.root), "study-run", study["id"]], stdout=output) == 0
    result = json.loads(output.getvalue())
    assert result["report"]["execution_completed"]
    assert client.get(route + "/" + study["id"]).json()["report"] == result["report"]
    assert len(client.get(route + "/" + study["id"] + "/export").json()["input_snapshot"]["rows"]) == 100
    assert len(client.get(route).json()) == 1
    assert client.post(route + "/" + study["id"] + "/run", json={}).status_code == 400


def test_registry_counts_every_candidate_and_keeps_existing_families():
    from datetime import UTC, datetime
    registry = ExperimentRegistry()
    record = registry.register_candidates(experiment_id="study", market="crypto",
        candidate_hashes=["first", "second"], preregistered_at=datetime.now(UTC))
    registry.record_trial(record.experiment_id, record.trial_ids[0])
    assert registry.total_declared_trials == 2 and registry.completed_trial_count == 1
    with pytest.raises(ValueError, match="unique"):
        registry.register_candidates(experiment_id="duplicate", market="stocks",
            candidate_hashes=["same", "same"], preregistered_at=datetime.now(UTC))
