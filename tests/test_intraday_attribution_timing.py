"""Completed-close attribution uses only prices known before a modeled exit."""

import pandas as pd
import pytest

from quantbot.intraday.attribution import episodes, explain_signals


def context(*, symbol="BTC/USDT", entry_minute=20, exit_basis="intrabar_time_unknown"):
    opening = pd.Timestamp("2025-01-02T00:00Z")

    def stamp(minute):
        return (opening + pd.Timedelta(minutes=minute)).isoformat()

    row = {
        "symbol": symbol, "signal_at": stamp(15), "entry_at": stamp(entry_minute),
        "entry_execution_time_basis": "bar_open", "entry_reference": "100",
        "exit_at": stamp(30), "exit_execution_time_basis": exit_basis, "exit_bar_open": stamp(25),
    }
    bars = [(opening + pd.Timedelta(minutes=minute), {symbol: {
        "open": "100", "high": "102", "low": "99", "close": "101",
    }}) for minute in range(0, 40, 5)]
    prepared = {"2025-01-02": {
        "opening": opening, "closing": opening + pd.Timedelta(days=1), "bars": bars,
        "signals": {pd.Timestamp(row["signal_at"]): {symbol: {
            "base": True, "close": 101, "opening_high": 100,
            "relative_volume": 1.5, "session_vwap": 100,
        }}},
    }}
    return row, prepared


def set_close(prepared, minute, symbol, close):
    prepared["2025-01-02"]["bars"][minute // 5][1][symbol]["close"] = str(close)


@pytest.mark.parametrize("exit_bar_close", [120, 99])
def test_intrabar_exit_does_not_include_later_rebound_or_reference_break(exit_bar_close):
    row, prepared = context()
    set_close(prepared, 25, row["symbol"], exit_bar_close)
    explain_signals([row], prepared, "breakout")
    assert row["max_completed_close_gain_bps"] == pytest.approx(100)
    assert row["max_completed_close_at"] == "2025-01-02T00:25:00+00:00"
    assert row["reclosed_below_signal_reference"] is False
    assert row["reclosed_below_opening_high"] is False
    assert row["completed_close_count"] == 1
    assert row["completed_close_cutoff_at"] == "2025-01-02T00:25:00+00:00"
    assert row["completed_close_timing_status"] == "bounded_before_intrabar_exit"
    assert row["completed_close_evidence_version"] == 1


def test_stock_open_exit_retains_previous_close_and_excludes_next_close():
    row, prepared = context(symbol="SPY", exit_basis="bar_open")
    row["exit_bar_open"] = row["exit_at"]
    set_close(prepared, 25, "SPY", 102)
    set_close(prepared, 30, "SPY", 500)
    explain_signals([row], prepared, "vwap_reclaim")
    assert row["max_completed_close_gain_bps"] == pytest.approx(200)
    assert row["max_completed_close_at"] == row["exit_at"]
    assert row["completed_close_cutoff_at"] == row["exit_at"]
    assert row["completed_close_count"] == 2
    assert row["completed_close_timing_status"] == "exact_exit"


def test_delayed_entry_excludes_closes_between_signal_and_actual_fill():
    row, prepared = context(entry_minute=25, exit_basis="bar_open")
    row["exit_bar_open"] = row["exit_at"]
    set_close(prepared, 15, row["symbol"], 120)
    set_close(prepared, 20, row["symbol"], 125)
    explain_signals([row], prepared, "breakout")
    assert row["max_completed_close_gain_bps"] == pytest.approx(100)
    assert row["max_completed_close_at"] == row["exit_at"]
    assert row["completed_close_count"] == 1


def test_intrabar_exit_before_any_completed_close_does_not_mean_no_reference_break():
    row, prepared = context(entry_minute=25)
    explain_signals([row], prepared, "breakout")
    assert row["completed_close_count"] == 0
    assert row["completed_close_cutoff_at"] == row["entry_at"]
    assert row["completed_close_timing_status"] == "bounded_before_intrabar_exit"
    assert row["max_completed_close_at"] is None
    assert row["max_completed_close_gain_bps"] is None
    assert row["reclosed_below_signal_reference"] is None
    assert row["reclosed_below_opening_high"] is None


@pytest.mark.parametrize("missing_field", [
    "entry_execution_time_basis", "exit_execution_time_basis", "exit_bar_open",
])
def test_unknown_timing_withholds_completed_close_claims(missing_field):
    row, prepared = context()
    del row[missing_field]
    explain_signals([row], prepared, "breakout")
    assert row["completed_close_timing_status"] == "unknown_execution_timing"
    for field in (
        "completed_close_count", "completed_close_cutoff_at", "max_completed_close_at",
        "max_completed_close_gain_bps", "reclosed_below_signal_reference", "reclosed_below_opening_high",
    ):
        assert row[field] is None


def partial_fill_result():
    events = []
    for index, (side, basis) in enumerate([
        ("buy", "bar_open"), ("buy", "unknown"),
        ("sell", "intrabar_time_unknown"), ("sell", "bar_open"),
    ]):
        stamp = f"2025-01-02T00:{20 + index * 5}:00+00:00"
        events.append({
            "status": "filled", "symbol": "BTC/USDT", "side": side, "quantity": "1",
            "price": "100", "reference_price": "100", "estimated_cost": "0",
            "cost_components": {"fee": "0"}, "client_order_id": side,
            "created_at": "2025-01-02T00:15:00+00:00", "at": stamp,
            "remaining": "1" if index % 2 == 0 else "0", "bar_open": stamp,
            "bar_capacity": "10", "capacity_used_after": "1", "reason": "entry" if side == "buy" else "stop",
            "execution_time_basis": basis,
        })
    return {"events": events, "initial_equity": 1000,
            "account": {"cash": 1000, "positions": {}, "estimated_execution_cost": 0}}


def test_episodes_preserve_first_entry_and_final_exit_basis_across_partial_fills():
    result = partial_fill_result()
    row, = episodes(result)
    assert row["entry_execution_time_basis"] == "bar_open"
    assert row["exit_execution_time_basis"] == "bar_open"
    assert row["entry_at"] == result["events"][0]["at"]
    assert row["exit_at"] == result["events"][-1]["at"]
    assert row["exit_bar_open"] == result["events"][-1]["bar_open"]
    assert row["fill_count"] == 4
    assert row["net_pnl"] == "0"


def test_episodes_do_not_invent_missing_legacy_execution_basis():
    result = partial_fill_result()
    for event in result["events"]:
        del event["execution_time_basis"]
    row, = episodes(result)
    assert row["entry_execution_time_basis"] == "unknown"
    assert row["exit_execution_time_basis"] == "unknown"
