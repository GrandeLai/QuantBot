"""统一评估的时间隔离、比较基准、授权与准入回归。"""

from copy import deepcopy
from datetime import datetime
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from quantbot.ai import evaluation
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import evaluate, walk_forward
from quantbot.ai.models import DemoProvider
from quantbot.ai.runtime import encode
from quantbot.ai.runtime_cli import main
from quantbot.ai.transport import ProviderError
from quantbot.web.app import create_app

from tests.test_ai_runtime import setup, write_rows


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_rolling_windows_share_snapshot_and_reset_account(tmp_path, market):
    store, job, _, _ = setup(tmp_path, market, count=100)
    config = deepcopy(job["config"])
    config["risk"]["max_weight"] = 0.1
    child = store.create({**job["spec"], "name": "较低仓位"}, config, parent=job["id"])
    result = walk_forward(store, [job["id"], child["id"]], selection_bars=5, test_bars=4, folds=3)
    assert result["passed"] and len(result["comparisons"]) == 2
    assert result["protocol"]["max_candidate_decisions"] == 42
    assert result["protocol"]["max_model_requests"] == 0
    previous_end = None
    for fold in result["folds"]:
        assert fold["selection_end"] < fold["test_start"] <= fold["test_end"]
        if previous_end:
            assert previous_end < fold["test_start"]
        previous_end = fold["test_end"]
    reports = [store.history(j["id"])["evaluations"][0]["report"] for j in (job, child)]
    assert reports[0]["snapshot"] == reports[1]["snapshot"]
    assert reports[0]["baseline"] == reports[1]["baseline"]
    for j, report in zip((job, child), reports, strict=True):
        assert report["candidate"]["windows"] == 3
        assert report["candidate"]["reported_tokens"] is None
        assert report["candidate"]["usage_missing_calls"] == 9
        assert report["candidate"]["return_fraction"] == pytest.approx(
            sum(f["candidate"]["return_fraction"] for f in report["folds"]) / 3
        )
        for fold in report["folds"]:
            for section, expected in (("selection", 5), ("candidate", 4)):
                replay = fold[section]
                assert len(replay["equity_curve"]) == expected
                assert replay["equity_curve"][0]["equity"] == j["config"]["execution"]["initial_cash"]
                assert replay["equity_curve"][0]["unrealized_pnl"] == 0
                assert replay["provider_calls"] == expected - 1
                assert replay["account"]["pending_orders"] == []
        assert store.get(j["id"])["account"]["as_of"] is None
    assert store.control(job["id"], "resume")["stage"] == "paper"


def test_reference_selection_is_committed_before_future_calls_and_never_uses_test_profit(
    tmp_path, monkeypatch
):
    store, job, path, rows = setup(tmp_path, "crypto", count=100)
    config = deepcopy(job["config"])
    config["llm"]["model"] = "cash-fixture"
    cash = store.create({**job["spec"], "name": "现金对照"}, config)
    # 两折：参考 8 根、间隔 2 根、测试各 5 根；首次测试从索引 90 开始。
    first_test = datetime.fromisoformat(rows[90]["timestamp"])
    for row in rows[91:]:
        for key in ("open", "high", "low", "close"):
            row[key] *= 0.9
    write_rows(path, rows)
    instances = []

    class CheckedProvider:
        def __init__(self):
            self.last = None
            instances.append(self)

        def decide(self, context, account, config):
            if self.last:
                assert context.as_of > self.last
            self.last = context.as_of
            index = (
                next(i for i, r in enumerate(rows) if datetime.fromisoformat(r["timestamp"]) >= context.as_of)
                - 1
            )
            assert context.prices["BTC/USDT"] == rows[index]["close"]
            if context.as_of > first_test:
                records = [
                    e for e in store.history(job["id"])["events"] if e["kind"] == "evaluation_selection"
                ]
                assert records and records[-1]["payload"]["selected_job_id"] == job["id"]
            target = 0 if config["llm"]["model"] == "cash-fixture" else 0.2
            return DecisionBatch((Allocation("BTC/USDT", target, 0.9, "受控时点输入"),), "fixture", "fixture")

    monkeypatch.setattr(evaluation, "provider_for", lambda *args: CheckedProvider())
    result = walk_forward(store, [job["id"], cash["id"]], selection_bars=8, test_bars=5, folds=2, gap_bars=2)
    assert len(instances) == 8  # 每个版本、每个窗口重新建立无历史会话的 provider。
    first = result["folds"][0]
    assert first["selected_job_id"] == job["id"]
    long_report = store.history(job["id"])["evaluations"][0]["report"]
    cash_report = store.history(cash["id"])["evaluations"][0]["report"]
    # 首次测试的跳空亏损未进入较早参考区间的选择。
    assert (
        long_report["folds"][0]["selection"]["return_fraction"]
        > cash_report["folds"][0]["selection"]["return_fraction"]
    )
    assert (
        long_report["folds"][0]["candidate"]["return_fraction"]
        <= cash_report["folds"][0]["candidate"]["return_fraction"]
    )
    assert result["protocol"]["gap_bars"] == 2


def test_incomplete_warmup_or_mismatched_comparison_rejected_before_calls(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "stocks", count=84)
    calls = []
    monkeypatch.setattr(DemoProvider, "decide", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="历史不足"):
        walk_forward(store, [job["id"]], selection_bars=20, test_bars=5, folds=2)
    with pytest.raises(ValueError, match="历史不足"):
        evaluate(store, job["id"], last_bars=30)
    different = deepcopy(job["config"])
    different["execution"]["cost_bps"] += 1
    child = store.create(job["spec"], different)
    with pytest.raises(ValueError, match="相同标的"):
        walk_forward(store, [job["id"], child["id"]], selection_bars=3, test_bars=3, folds=2)
    assert calls == [] and store.history(job["id"])["evaluations"] == []


def test_any_ai_candidate_requires_authorization_before_snapshot_fetch(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    ai = store.create({**job["spec"], "provider": "ai"}, job["config"])

    def forbidden(*args, **kwargs):
        pytest.fail("授权检查之前不应读取行情或调用模型")

    monkeypatch.setattr(evaluation, "fetch_snapshot", forbidden)
    with pytest.raises(ValueError, match="所有参与对比"):
        walk_forward(store, [job["id"], ai["id"]], selection_bars=3, test_bars=3, folds=2)


def test_failed_fold_revokes_previous_admission_without_blocking_other_version(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    evaluate(store, job["id"], last_bars=3)
    config = deepcopy(job["config"])
    config["llm"]["model"] = "valid-fixture"
    other = store.create(job["spec"], config)

    class BrokenProvider:
        def decide(self, *args):
            raise ProviderError("受控模型失败")

    monkeypatch.setattr(
        evaluation,
        "provider_for",
        lambda spec, cfg: DemoProvider() if cfg["llm"]["model"] == "valid-fixture" else BrokenProvider(),
    )
    result = walk_forward(store, [job["id"], other["id"]], selection_bars=3, test_bars=3, folds=2)
    assert not result["passed"]
    assert [r["passed"] for r in result["comparisons"]] == [False, True]
    assert all(f["selected_job_id"] == other["id"] for f in result["folds"])
    assert store.get(job["id"])["stage"] == "candidate"
    with pytest.raises(ValueError, match="历史评估"):
        store.control(job["id"], "resume")
    assert store.control(other["id"], "resume")["stage"] == "paper"


def test_no_eligible_reference_has_no_invented_selected_return(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")

    class BrokenProvider:
        def decide(self, *args):
            raise ProviderError("受控失败")

    monkeypatch.setattr(evaluation, "provider_for", lambda *args: BrokenProvider())
    result = walk_forward(store, [job["id"]], selection_bars=3, test_bars=3, folds=2)
    assert result["selected_strategy"]["unselected_folds"] == 2
    assert result["selected_strategy"]["return_fraction"] is None
    assert result["selected_strategy"]["windows"] == 0


def test_state_change_rolls_back_all_comparison_admissions(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    other = store.create(job["spec"], job["config"])
    original = DemoProvider.decide
    changed = False

    def change(self, *args):
        nonlocal changed
        if not changed:
            store.control(other["id"], "pause")
            changed = True
        return original(self, *args)

    monkeypatch.setattr(DemoProvider, "decide", change)
    with pytest.raises(ValueError, match="状态改变"):
        walk_forward(store, [job["id"], other["id"]], selection_bars=3, test_bars=3, folds=2)
    for candidate in (job, other):
        assert store.get(candidate["id"])["stage"] == "candidate"
        assert store.history(candidate["id"])["evaluations"] == []


def test_enable_requires_matching_latest_evidence_and_immutable_snapshot(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto")
    report = evaluate(store, job["id"], last_bars=3)
    snapshot = Path(report["snapshot"]["path"])
    original = snapshot.read_text()
    snapshot.write_text("{}")
    with pytest.raises(ValueError, match="指纹不一致"):
        store.control(job["id"], "resume")
    snapshot.write_text(original)
    changed = deepcopy(report)
    changed["config_hash"] = "wrong"
    with store.connect() as db:
        db.execute("UPDATE evaluations SET report=? WHERE id=?", (encode(changed), report["id"]))
    with pytest.raises(ValueError, match="一致的通过评估证据"):
        store.control(job["id"], "resume")


def test_code_changes_during_evaluation_cannot_be_admitted(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path, "crypto")
    current_hash = job["code_hash"]
    original = DemoProvider.decide

    def change_during_decision(self, *args):
        nonlocal current_hash
        current_hash = "changed-during-evaluation"
        return original(self, *args)

    monkeypatch.setattr(DemoProvider, "decide", change_during_decision)
    monkeypatch.setattr(evaluation, "source_tree_hash", lambda: current_hash)
    with pytest.raises(ValueError, match="评估期间代码版本改变"):
        walk_forward(store, [job["id"]], selection_bars=3, test_bars=3, folds=2)
    assert store.history(job["id"])["evaluations"] == []
    assert store.get(job["id"])["stage"] == "candidate"


def test_authorized_ai_protocol_and_missing_usage_are_recorded_per_window(tmp_path, monkeypatch):
    from quantbot.ai.transport import JsonTransport

    store, prior, _, _ = setup(tmp_path, "crypto")
    config = deepcopy(prior["config"])
    config["llm"].update(base_url="http://127.0.0.1:9999/v1", model="controlled-model")
    job = store.create({**prior["spec"], "provider": "ai"}, config)
    requests = []

    def respond(self, url, headers=None, payload=None):
        market = json.loads(payload["messages"][1]["content"])["market"]
        requests.append(market["as_of"])
        content = {
            "snapshot_id": market["snapshot_id"],
            "as_of": market["as_of"],
            "decisions": [
                {"symbol": s, "target_weight": 0.2, "confidence": 0.9, "rationale": "受控协议"}
                for s in market["features"]
            ],
        }
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "usage": {"total_tokens": 17} if len(requests) % 2 else {},
        }

    monkeypatch.setattr(JsonTransport, "post_json", respond)
    result = walk_forward(store, [job["id"]], selection_bars=3, test_bars=3, folds=2, allow_model_calls=True)
    assert result["passed"] and len(requests) == result["protocol"]["max_model_requests"] == 8
    report = store.history(job["id"])["evaluations"][0]["report"]
    assert report["usage"]["provider_calls"] == 8
    assert report["usage"]["reported_tokens"] == 4 * 17
    assert report["usage"]["usage_reported_calls"] == report["usage"]["usage_missing_calls"] == 4
    assert report["candidate"]["provider_calls"] == 4
    baseline_log = Path(report["folds"][0]["baseline"]["journal_path"]).read_text().splitlines()
    assert json.loads(baseline_log[0])["payload"]["spec"]["provider"] == "demo"
    with pytest.raises(ValueError, match="明确允许"):
        store.control(job["id"], "resume")


def test_api_and_cli_walk_forward_reports_share_the_runtime_store(tmp_path):
    _, job, _, _ = setup(tmp_path, "crypto")
    runtime_dir = tmp_path / "web"
    with TestClient(create_app(config=job["config"], runtime_dir=runtime_dir)) as client:
        created = client.post("/api/quantbot/runtime/jobs", json={"spec": job["spec"]}).json()
        payload = {"job_ids": [created["id"]], "selection_bars": 3, "test_bars": 3, "folds": 2}
        invalid = client.post(
            "/api/quantbot/runtime/evaluations/walk-forward", json={**payload, "folds": True}
        )
        assert invalid.status_code == 422
        response = client.post("/api/quantbot/runtime/evaluations/walk-forward", json=payload)
        assert response.status_code == 200 and response.json()["passed"]
        detail = client.get(f"/api/quantbot/runtime/jobs/{created['id']}").json()
        assert detail["evaluations"][0]["report"]["kind"] == "walk_forward"
    output = io.StringIO()
    assert (
        main(
            [
                "--root",
                str(runtime_dir / "automation"),
                "walk-forward",
                created["id"],
                "--selection-bars",
                "3",
                "--test-bars",
                "3",
                "--folds",
                "2",
            ],
            stdout=output,
        )
        == 0
    )
    assert json.loads(output.getvalue())["comparisons"][0]["job_id"] == created["id"]
