from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from quantbot.ai.config import load_config
from quantbot.ai.development import cost_config, select_times, shortlist, summarize, validate_reconstructed_dividends
from quantbot.ai.development_strategies import development_provider
from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay


def test_long_explicit_replay_is_causal_cached_and_reconciles(tmp_path):
    config = load_config(environ={})
    config["market"].update(symbols=["BTC/USDT"], benchmark="BTC/USDT")
    config["execution"]["initial_cash"] = 1000
    rows = [{"timestamp": (datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
             "symbol": "BTC/USDT", "bar": "1d", "open": 100 + i * 0.1, "high": 102 + i * 0.1,
             "low": 99 + i * 0.1, "close": 101 + i * 0.1, "volume": 10000} for i in range(550)]
    snapshot = Snapshot("crypto", rows, config, "synthetic_engine_test_only")
    at = snapshot.times[40]
    original = snapshot.context(at)
    assert snapshot.context(at) is original
    changed = deepcopy(config)
    changed["strategy"]["atr_stop_mult"] = 1
    assert snapshot.with_config(changed).context(at).features["BTC/USDT"]["stop_candidate"] != \
        original.features["BTC/USDT"]["stop_candidate"]
    path = tmp_path / "replay.jsonl"
    result = replay(snapshot, {"market": "crypto", "bar": "1d", "max_daily_loss": .02, "max_drawdown": .05},
                    config, development_provider("crypto", "cash"), times=snapshot.times[40:], journal_path=path)
    assert len(result["equity_curve"]) == 510
    assert result["return_fraction"] == 0 and not result["errors"]
    assert reconcile_replay(inspect_replay(path)["records"])["status"] == "reconciled"


def test_cost_pressure_preserves_non_fee_entry_allowance():
    config = load_config(environ={})
    config["execution"].update(cost_bps=15, limit_buffer_bps=50)
    changed = cost_config(config, 2)
    assert changed["execution"]["cost_bps"] == 30
    assert changed["execution"]["limit_buffer_bps"] == 65
    assert config["execution"]["cost_bps"] == 15
    with pytest.raises(ValueError):
        cost_config(config, 0)


def test_window_rejects_reversed_and_duplicated_times():
    at = datetime(2025, 1, 1, tzinfo=UTC)
    snapshot = SimpleNamespace(times=[at, at, at + timedelta(days=1)], context=lambda _: None)
    with pytest.raises(ValueError):
        select_times(snapshot, {"start": "2025-01-01", "end": "2025-01-03"})
    with pytest.raises(ValueError):
        select_times(snapshot, {"start": "2025-01-03", "end": "2025-01-01"})
    snapshot.times = [at + timedelta(days=i) for i in (1, 2, 3)]
    with pytest.raises(ValueError, match="完整覆盖"):
        select_times(snapshot, {"start": "2025-01-01", "end": "2025-01-04", "expected_bars": 4,
                                "first_close": at.isoformat(), "last_close": snapshot.times[-1].isoformat()})


def test_liquidation_cost_excludes_unpaid_dividend_and_never_claims_sale():
    account = {"equity": 1100, "cash": 600, "dividend_receivable": 100,
               "dividend_income": 100, "positions": {"SPY": "4"}, "estimated_execution_cost": 2}
    curve = [{"as_of": "2024-12-31T21:00:00+00:00", "equity": 1000, "positions": {}},
             {"as_of": "2025-01-02T21:00:00+00:00", **account}]
    result = {"account": account, "equity_curve": curve, "orders": [], "initial_equity": 1000,
              "return_fraction": .1, "max_drawdown_fraction": 0, "risk_hold": None, "errors": [],
              "provider_calls": 1, "decisions": [{}]}
    result = summarize(result, {"execution": {"cost_bps": 30}}, 252)
    assert result["estimated_final_sell_cost"] == pytest.approx(1.2)
    assert result["net_liquidation_return_estimate"] == pytest.approx(.0988)
    assert result["fills"] == 0 and result["remaining_positions"] == {"SPY": "4"}


def test_shortlist_cannot_hide_losing_stress_window_or_risk_halt():
    rows = [{"candidate": "x", "window": window, "cost_multiplier": cost,
             "execution_checks": {"complete": True}, "reconciliation": {"status": "reconciled"},
             "net_liquidation_return_estimate": .01, "fills": 30, "sells": 10}
            for window in ("a", "b", "c", "continuous") for cost in (1, 2)]
    assert shortlist(rows, ["x"])["research_shortlist"] == ["x"]
    rows[-1]["execution_checks"]["no_risk_hold"] = False
    assert shortlist(rows, ["x"])["research_shortlist"] == []
    rows[-1]["execution_checks"]["no_risk_hold"] = True
    rows[1]["net_liquidation_return_estimate"] = -.02
    rows[3]["net_liquidation_return_estimate"] = -.02
    result = shortlist(rows, ["x"])
    assert result["research_shortlist"] == [] and result["formal_admission"] is False


def test_reconstructed_actions_do_not_fabricate_publication_time_or_accept_unknown_type():
    event = {"id": "spy-div", "symbol": "SPY", "date": "2025-03-21", "pay_date": "2025-04-30",
             "type": "dividend", "distribution": "ordinary_cash", "currency": "USD", "amount_per_share": "1"}
    validate_reconstructed_dividends([event], {"SPY"})
    assert "available_at" not in event
    with pytest.raises(ValueError):
        validate_reconstructed_dividends([{**event, "distribution": "unclassified_cash_distribution"}], {"SPY"})
    with pytest.raises(ValueError):
        validate_reconstructed_dividends([event, event], {"SPY"})
