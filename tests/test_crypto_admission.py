"""加密准出需有交易和独立证据；只读检查不会开启任何交易任务。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import io
import json
from pathlib import Path

import pytest

from quantbot.ai.crypto_admission import _metrics, assess_crypto_study, crypto_policy
from quantbot.ai.domain import validate_decision
from quantbot.ai.feeds import fetch_snapshot
from quantbot.ai.models import decision_schema
from quantbot.ai.research import register_study, run_study
from quantbot.ai.research_protocol import DelayedRecordedDecisionProvider, ResearchProtocol
from quantbot.ai.runtime_cli import main
from tests.test_ai_research import CARD, PROTOCOL
from tests.test_ai_runtime import setup


def test_crypto_policy_is_read_only_and_cannot_mutate_frozen_defaults(tmp_path):
    output = io.StringIO()
    assert main(["--root", str(tmp_path / "absent"), "crypto-policy"], stdout=output) == 0
    policy = json.loads(output.getvalue())
    assert policy["backtest"]["min_history_days"] == 180
    assert policy["okx_demo"]["min_elapsed_days"] == 60
    assert policy["small_live"]["automatic_activation"] is False
    policy["backtest"]["min_history_days"] = 0
    assert crypto_policy()["backtest"]["min_history_days"] == 180
    assert not (tmp_path / "absent").exists()


def test_zero_variance_no_trade_curve_has_no_sharpe_or_active_days():
    start = datetime(2025, 1, 1, tzinfo=UTC)
    result = {"initial_equity": 1000, "orders": [], "equity_curve": [
        {"as_of": (start + timedelta(days=i)).isoformat(), "equity": 1000, "positions": {}}
        for i in range(61)]}
    metrics = _metrics(result)
    assert metrics["elapsed_days"] == 60
    assert metrics["net_return"] == metrics["max_drawdown"] == 0
    assert metrics["fills"] == metrics["sell_fills"] == metrics["active_days"] == 0
    assert metrics["daily_sharpe"] is None
    result["equity_curve"][-1]["equity"] = float("nan")
    with pytest.raises(ValueError, match="有限"):
        _metrics(result)


def test_short_synthetic_research_is_not_okx_admission_even_with_complete_books(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register_study(store, job_ids=[job["id"]], card=CARD,
                           protocol={**PROTOCOL, "execution_delay_bars": 1},
                           data_kind="synthetic", promotion_policy="crypto_spot_v1")
    assert study["plan"]["promotion_policy"] == crypto_policy()
    result = run_study(store, study["id"])
    assert result["report"]["execution_completed"]
    assert len(result["trials"]) == 10
    assert result["report"]["model_usage"]["provider_calls"] == 0
    original = deepcopy(store.get(job["id"]))
    admission = assess_crypto_study(store, study["id"])
    from quantbot.ai.admission import assess_strategy_admission

    unified = assess_strategy_admission(store, job["id"], study_id=study["id"])
    assert unified["checks"]["selected_frozen_strategy_matches"]
    assert unified["evidence"]["backtest_passed"] == admission["backtest_passed"]
    assert unified["identity"] == admission["identity"]
    assert unified["broker_paper_authorized"] is False
    other = store.create({**job["spec"], "name": "unselected version"}, job["config"])
    unselected = assess_strategy_admission(store, other["id"], study_id=study["id"])
    assert not unselected["checks"]["selected_frozen_strategy_matches"]
    assert unselected["evidence"] is None
    assert admission["checks"]["policy_preregistered"]
    assert admission["checks"]["expected_trial_count"]
    assert admission["checks"]["journals_complete_and_bound"]
    assert admission["checks"]["independently_reconciled"]
    assert not admission["backtest_passed"]
    assert {"historical_data", "history_duration", "holdout_duration", "selected_ai_candidate",
            "enough_active_days"} <= set(admission["reasons"])
    assert not admission["okx_demo"]["execution_authorized"]
    assert not admission["small_live"]["eligible"]
    output = io.StringIO()
    assert main(["--root", str(store.root), "study-admission", study["id"]], stdout=output) == 2
    assert store.get(job["id"]) == original

    # 汇总数值篡改、丢失逐步日志，都必须破坏证据链。
    with store.connect() as db:
        holdout = next(t for t in result["trials"] if t["key"] == "holdout")["result"]
        holdout["equity_curve"][-1]["equity"] += 100
        db.execute("UPDATE research_trials SET result=? WHERE study_id=? AND key='holdout'",
                   (json.dumps(holdout), study["id"]))
    changed = assess_crypto_study(store, study["id"])
    assert not changed["checks"]["journals_complete_and_bound"]
    assert not changed["checks"]["holdout_report_bound"]
    Path(holdout["journal_path"]).unlink()
    missing = assess_crypto_study(store, study["id"])
    assert not missing["checks"]["independently_reconciled"]


def test_old_study_cannot_acquire_preregistered_policy_after_results(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto", count=100)
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)
    run_study(store, study["id"])
    result = assess_crypto_study(store, study["id"])
    assert not result["checks"]["policy_preregistered"]
    assert not result["backtest_passed"]


def test_no_qualified_development_stops_before_holdout(tmp_path, monkeypatch):
    from quantbot.ai import research
    from quantbot.ai.domain import Allocation, DecisionBatch

    store, job, _, _ = setup(tmp_path, "crypto", count=100)

    class CashOnly:
        def decide(self, context, account, config):
            return DecisionBatch(tuple(Allocation(s, 0, 1, "空仓对照") for s in context.features),
                                 "controlled_test", "cash")

    monkeypatch.setattr(research, "provider_for", lambda *args: CashOnly())
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL,
                           promotion_policy="crypto_spot_v1")
    result = run_study(store, study["id"])
    assert result["report"]["execution_completed"]
    assert result["report"]["conclusion"] == "rejected"
    assert result["report"]["holdout_used"] is False
    assert {t["stage"] for t in result["trials"]} == {"development"}
    assert not any(e["kind"] == "research_selection" for e in result["events"])
    assert not assess_crypto_study(store, study["id"])["backtest_passed"]


def test_snapshot_schema_and_indicator_units_follow_current_input(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto")
    snapshot = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    context = snapshot.context(snapshot.times[-2])
    schema = decision_schema(list(context.features), context=context)
    assert schema["properties"]["snapshot_id"]["enum"] == [context.snapshot_id]
    assert schema["properties"]["as_of"]["enum"] == [context.as_of.isoformat()]
    momentum = context.features["BTC/USDT"]["indicator_definitions"]["momentum"]
    assert momentum["lookback_bars"] == 20 and momentum["bar"] == "15m"
    assert momentum["unit"] == "fraction"
    with pytest.raises(ValueError, match="snapshot_id"):
        validate_decision({"snapshot_id": "wrong", "as_of": context.as_of.isoformat(), "decisions": []}, context)
    with pytest.raises(ValueError, match="as_of"):
        validate_decision({"snapshot_id": context.snapshot_id, "as_of": "wrong", "decisions": []}, context)


def test_delay_stress_uses_only_earlier_targets_without_new_model_calls(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto")
    snapshot = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    times = snapshot.times[-4:]
    decisions = [{"as_of": at.isoformat(), "raw_decision": {"model": "recorded",
                  "allocations": [{"symbol": "BTC/USDT", "target_weight": weight,
                                   "confidence": 1, "rationale": "冻结目标"}]}}
                 for at, weight in zip(times, [.1, .2, .3])]
    delayed = DelayedRecordedDecisionProvider(decisions, times, 1)
    weights = [delayed.decide(snapshot.context(at), None, job["config"]).allocations[0].target_weight
               for at in times[:3]]
    assert weights == [0, .1, .2]
    with pytest.raises(ValueError, match="execution_delay_bars"):
        ResearchProtocol(execution_delay_bars=-1)
