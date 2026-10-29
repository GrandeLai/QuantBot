from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from quantbot.intraday.execution import IntradayAccount
from quantbot.intraday.research import audit_report


@pytest.fixture
def section():
    return {
        "risk": {
            "max_weight": 0.2,
            "max_gross_exposure": 0.6,
            "max_positions": 5,
            "risk_per_trade": 0.005,
            "max_portfolio_heat": 0.03,
            "cash_buffer": 0.1,
            "min_confidence": 0.7,
            "crypto_stop_loss_pct": 0.03,
            "crypto_take_profit_pct": 0.06,
            "stock_stop_loss_pct": 0.03,
            "stock_take_profit_pct": 0.06,
        },
        "strategy": {
            "crypto_cooldown_bars": 3,
            "stock_cooldown_bars": 3,
            "atr_stop_mult": 2.5,
            "atr_window": 14,
        },
        "execution": {"initial_cash": 10000, "limit_buffer_bps": 50},
        "max_daily_loss": 0.02,
        "max_drawdown": 0.05,
    }


@pytest.fixture
def model():
    return {
        "one_way_cost_bps": [15, 30],
        "fee_bps": [10, 20],
        "half_spread_bps": [2.5, 5],
        "slippage_bps": [2.5, 5],
        "latency_seconds": [1, 301],
        "quantity_crypto": {"BTC/USDT": "0.00001", "ETH/USDT": "0.0001"},
        "max_volume_participation": 0.01,
        "impact_bps_at_participation_cap": 5,
        "entry_ttl_seconds": 900,
    }


T = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)


def account(section, model, market="stocks", latency=1):
    return IntradayAccount(
        market,
        section,
        model,
        cost_bps=15,
        latency_seconds=latency,
        sessions=["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07"],
    )


def bar(symbol="SPY", **changes):
    return {
        symbol: {"open": "100", "high": "100.1", "low": "99.9", "close": "100", "volume": "200", **changes}
    }


def step(a, minute, rows=None, close=None):
    a.advance(
        T + timedelta(minutes=minute),
        rows or bar(),
        day="2025-01-02",
        session_close=close or T + timedelta(hours=6, minutes=30),
    )


def enter(a, symbol="SPY"):
    step(a, 0, bar(symbol))
    assert a.enter(symbol, T + timedelta(minutes=5), "2025-01-02", 100, 2, identity="test")


def fills(a):
    return [o for o in a.events if o["status"] == "filled"]


@pytest.mark.parametrize("latency,first_fill", [(1, 10), (301, 15)])
def test_latency_uses_next_complete_bar_not_signal_or_arrival_bar(section, model, latency, first_fill):
    a = account(section, model, latency=latency)
    enter(a)
    for minute in range(5, first_fill, 5):
        step(a, minute)
        assert not fills(a)
    step(a, first_fill)
    assert fills(a)[0]["at"] == (T + timedelta(minutes=first_fill)).isoformat()


def test_partial_fill_expiry_preserves_protection_and_integer_quantity(section, model):
    a = account(section, model)
    enter(a)
    for minute in (5, 10, 15, 20):
        step(a, minute)
    assert [f["quantity"] for f in fills(a)] == ["2", "2"]
    assert all(f["partial"] for f in fills(a))
    assert a.state["positions"] == {"SPY": "4"}
    assert a.state["stops"]["SPY"] == "97.00"
    assert not a.orders
    assert any(e.get("reason") == "entry_expired" for e in a.events)
    assert audit_report(a.report())["status"] == "reconciled"


def test_attribution_pairs_partial_fills_and_preserves_round_trip_cost(section, model):
    from quantbot.intraday.attribution import describe, episodes

    a = account(section, model)
    enter(a)
    for minute in (5, 10, 15, 20):
        step(a, minute)
    a.exit("SPY", T + timedelta(minutes=20), "test_exit")
    for minute in (25, 30):
        step(a, minute)
    rows = episodes(a.report())
    assert len(rows) == 1 and rows[0]["fill_count"] == 4
    assert D(rows[0]["gross_contribution"]) == 0
    assert D(rows[0]["net_pnl"]) == -D(rows[0]["cost"])
    # Scenario cost and actual round-trip cost are distinct; adding labels must not overwrite it.
    rows[0]["cost_bps"] = 15
    assert describe(rows)["median_round_trip_cost_bps"] == 40


def test_attribution_rejects_open_positions(section, model):
    from quantbot.intraday.attribution import episodes

    a = account(section, model)
    enter(a)
    step(a, 10)
    with pytest.raises(ValueError, match="残仓"):
        episodes(a.report())


def test_crypto_decimal_step_survives_partial_fills(section, model):
    a = account(section, model, "crypto")
    enter(a, "BTC/USDT")
    step(a, 5, bar("BTC/USDT", volume="0.1234"))
    step(a, 10, bar("BTC/USDT", volume="0.1234"))
    assert fills(a)[0]["quantity"] == "0.00123"
    assert D(a.state["positions"]["BTC/USDT"]) == D("0.00123")
    assert audit_report(a.report())["status"] == "reconciled"


def test_observed_volume_can_only_reduce_past_volume_budget(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5, bar(volume="100000"))
    step(a, 10, bar(volume="100"))
    assert fills(a)[0]["quantity"] == "1"
    assert fills(a)[0]["bar_capacity"] == "1"


def test_no_volume_does_not_manufacture_fills(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5)
    step(a, 10, bar(volume="0"))
    assert not fills(a)


def test_entry_gap_beyond_frozen_limit_cancels(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5)
    step(a, 10, bar(open="102", high="103", low="101", close="102"))
    assert not fills(a) and not a.orders
    assert a.events[-1]["reason"] == "open_above_entry_limit"


def test_stops_share_volume_with_entries_and_cannot_force_fill(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5)
    step(a, 10, bar(high="107", low="96", close="100"))
    assert len(fills(a)) == 1  # The buy consumed the whole two-share budget.
    assert a.state["positions"] == {"SPY": "2"}
    assert any(o["reason"] == "protective_stop" and o["side"] == "sell" for o in a.orders.values())
    step(a, 15, bar(open="96", high="97", low="95", close="96"))
    assert not a.state["positions"]
    assert fills(a)[-1]["reason"] == "protective_stop"
    assert D(fills(a)[-1]["price"]) < D("96")
    assert audit_report(a.report())["status"] == "reconciled"


def test_both_touch_stop_wins_and_daily_cooldown_is_not_minutes(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5, bar(volume="100000"))
    step(a, 10, bar(volume="100000"))
    step(a, 15, bar(volume="100000", high="107", low="96"))
    assert fills(a)[-1]["reason"] == "protective_stop"
    assert not a.state["positions"]
    assert a.cooling("SPY", T + timedelta(days=1), "2025-01-03")
    assert not a.cooling("SPY", T + timedelta(days=5), "2025-01-07")


def test_session_residual_is_reported_not_closed_at_invented_price(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5)
    step(a, 10)
    step(a, 15, bar(volume="0"), close=T + timedelta(minutes=20))
    assert a.session_violations[0]["reason"] == "unliquidated_session_end"
    assert a.report()["account"]["pending_orders"]


def test_no_buying_power_from_pending_exit(section, model):
    a = account(section, model)
    enter(a)
    buy = next(iter(a.orders.values()))
    original = D(buy["quantity"]) * D(buy["limit_price"])
    assert original <= D("2000")
    assert D(buy["quantity"]) * (D(buy["limit_price"]) - D(buy["stop"])) <= D("50")


def test_auditor_rejects_duplicate_or_changed_fill(section, model):
    a = account(section, model)
    enter(a)
    step(a, 5)
    step(a, 10)
    result = a.report()
    altered = deepcopy(result)
    altered["events"].append(deepcopy(fills(a)[0]))
    with pytest.raises(ValueError, match="重复"):
        audit_report(altered)
    altered = deepcopy(result)
    next(e for e in altered["events"] if e["status"] == "filled")["quantity"] = "100"
    with pytest.raises(ValueError, match="量能"):
        audit_report(altered)


def test_stock_pipeline_from_complete_minutes_to_flat_audited_account(section, model):
    import pandas as pd
    from quantbot.intraday.research import prepare_sessions, replay
    from quantbot.intraday.data import session_grid, FIELDS

    protocol = {
        "range": {"start": "2025-01-02", "end_exclusive": "2025-02-15"},
        "markets": {"stocks": {**section, "symbols": ["SPY", "QQQ"]}},
        "execution_model": model,
        "statistics": {"purge_sessions": 0, "embargo_sessions": 0},
    }
    sessions = session_grid(protocol, "stocks")
    rows = []
    for opening, closing in sessions.values():
        for i, at in enumerate(pd.date_range(opening, closing, freq="5min", inclusive="left")):
            close = 100 if i < 6 else 100.6
            for symbol in ("SPY", "QQQ"):
                rows.append(
                    [at, symbol, str(close), str(close + 0.4), str(close - 0.4), str(close), "100000"]
                )
    prepared = prepare_sessions(pd.DataFrame(rows, columns=FIELDS), sessions, ["SPY", "QQQ"])
    last = list(sessions)[-1]
    result = replay(
        protocol, "stocks", prepared, {"id": "fixture", "start": last, "end": last}, "control", 15, 1
    )
    assert result["entry_orders_filled"] == 2
    assert result["account"]["positions"] == {}
    assert not result["session_violations"]
    assert result["feature_coverage"] == 1
    assert audit_report(result)["status"] == "reconciled"
    assert result["net_return"] < 0  # Flat price after entry still pays the two-sided costs.
