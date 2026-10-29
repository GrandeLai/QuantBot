"""Offline cross-market evidence for shared matching, partials and ledger replay."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json

import pytest

from quantbot.ai.bar_execution import BarMatcher, EXECUTION_VERSION
from quantbot.ai.config import load_config, validate_config
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot, stock_calendar
from quantbot.ai.ledger import cancel_pending
from quantbot.ai.local_execution import advance, new_account, queue_plan
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.runtime import prepare
from quantbot.intraday.execution import IntradayAccount


def fixture(market):
    config = load_config(environ={})
    config["strategy"].update(momentum_window=20)
    config["risk"].update(stock_stop_loss_pct=0.03, stock_take_profit_pct=0.06,
                          crypto_stop_loss_pct=0.03, crypto_take_profit_pct=0.06)
    config["execution"].update(initial_cash=10000, cost_bps=15, latency_seconds=1,
                              impact_bps_at_participation_cap=5, entry_ttl_bars=3)
    symbol = "SPY" if market == "stocks" else "BTC/USDT"
    spec, config = prepare({"market": market, "symbols": [symbol], "provider": "demo",
                            "bar": "1d" if market == "stocks" else "15m",
                            "data_path": "unused.csv"}, config)
    if market == "stocks":
        calendar = stock_calendar("2024-01-01", "2024-06-14")
        rows = [{"date": str(day.date()), "symbol": symbol, "open": 100, "high": 101,
                 "low": 99, "close": 100, "volume": 200}
                for day in calendar.sessions_in_range("2024-01-01", "2024-06-14")]
    else:
        calendar = None
        rows = [{"timestamp": (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(minutes=15 * i)).isoformat(),
                 "symbol": symbol, "bar": "15m", "open": 100, "high": 101, "low": 99,
                 "close": 100, "volume": 200} for i in range(80)]
    return config, spec, Snapshot(market, rows, config, "synthetic", calendar), symbol


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_partial_order_survives_json_restart_and_expiry_without_losing_protection(market):
    config, spec, snapshot, symbol = fixture(market)
    state = new_account(10000)
    times = snapshot.times[-5:]
    advance(state, snapshot, snapshot.context(times[0]), config, spec)
    queue_plan(state, {"orders": [{"client_order_id": "large-order", "symbol": symbol,
        "side": "buy", "quantity": "10", "stop": "97", "limit_price": "101",
        "stop_loss_pct": 0.03, "take_profit_pct": 0.06}]})
    events = []
    for at in times[1:]:
        state = json.loads(json.dumps(state))
        events += advance(state, snapshot, snapshot.context(at), config, spec)
    fills = [e for e in events if e["status"] == "filled"]
    assert fills and all(D(e["quantity"]) <= 2 for e in fills)
    assert all(e["partial"] and e["execution_version"] == EXECUTION_VERSION for e in fills)
    assert len({e["fill_id"] for e in fills}) == len(fills)
    assert any(e.get("reason") == "entry_expired" for e in events)
    assert not state["pending"]
    assert D(state["positions"][symbol]) == sum(D(e["quantity"]) for e in fills)
    assert D(state["stops"][symbol]) == 97 and D(state["take_profits"][symbol]) == 106
    assert D(state["cash"]) == 10000 - sum(D(e["price"]) * D(e["quantity"]) for e in fills)


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_shared_replay_waits_for_partials_and_independent_account_audit_accepts_them(tmp_path, market):
    config, spec, snapshot, symbol = fixture(market)

    class Enter:
        def decide(self, context, account, config):
            return DecisionBatch((Allocation(symbol, 0.2, 1, "synthetic"),), "fixture", "fixture")

    result = replay(snapshot, spec, config, Enter(), last_bars=6, journal_path=tmp_path / "replay.jsonl")
    assert result["execution_waits"] and not result["errors"]
    assert result["engineering_passed"] and not result["broker_paper_authorized"]
    assert len(result["decisions"]) + len(result["execution_waits"]) == 5
    assert result["return_fraction"] < 0  # Flat price still pays costs.
    records = inspect_replay(result["journal_path"])["records"]
    assert reconcile_replay(records)["status"] == "reconciled"
    altered = deepcopy(records)
    account = next(r for r in altered if r["kind"] == "account"
                   and any(o["status"] == "filled" for o in r["payload"]["orders"]))
    fill = next(o for o in account["payload"]["orders"] if o["status"] == "filled")
    account["payload"]["orders"].append(deepcopy(fill))
    with pytest.raises(ValueError, match="重复成交"):
        reconcile_replay(altered)


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_intraday_and_shared_matcher_produce_identical_fills_and_cash(market):
    config, spec, _, symbol = fixture(market)
    section = {**config, "max_daily_loss": 0.02, "max_drawdown": 0.05}
    model = {"one_way_cost_bps": [15], "fee_bps": [10], "half_spread_bps": [2.5],
             "slippage_bps": [2.5], "latency_seconds": [1], "quantity_crypto": {symbol: "0.00001"},
             "max_volume_participation": 0.01, "impact_bps_at_participation_cap": 5,
             "entry_ttl_seconds": 900}
    at = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = {symbol: {"open": "100", "high": "101", "low": "99", "close": "100", "volume": "200"}}
    account = IntradayAccount(market, section, model, cost_bps=15, latency_seconds=1, sessions=["2025-01-02"])
    account.advance(at, bars, day="2025-01-02", session_close=at + timedelta(hours=6))
    account.enter(symbol, at + timedelta(minutes=5), "2025-01-02", 100, 2, identity="same")
    state, orders = deepcopy(account.state), deepcopy(account.orders)
    engine = BarMatcher(state, orders, market, model, cost_bps=15, latency_seconds=1)
    events = engine.advance(at + timedelta(minutes=10), at + timedelta(minutes=15), bars, account.previous_volume)
    account.advance(at + timedelta(minutes=10), bars, day="2025-01-02", session_close=at + timedelta(hours=6))
    assert [e for e in account.events if e["status"] == "filled"] == events
    for field in ("cash", "positions", "average_cost", "costs", "realized_pnl", "stops", "take_profits"):
        assert account.state[field] == state[field]


def test_risk_pause_cancels_entries_but_retains_pending_exit():
    state = new_account(100)
    state["pending"] = [{"side": "buy", "client_order_id": "entry"},
                        {"side": "sell", "client_order_id": "stop"}]
    canceled = cancel_pending(state, "drawdown_limit", keep_exits=True)
    assert [e["client_order_id"] for e in canceled] == ["entry"]
    assert [e["client_order_id"] for e in state["pending"]] == ["stop"]


def test_dividend_preserves_triggered_zero_volume_exit_after_price_recovers():
    config, spec, snapshot, symbol = fixture("stocks")
    times = snapshot.times[-4:]
    days = [at.date().isoformat() for at in times]
    for row in snapshot.rows:
        if row["date"] == days[1]:
            row.update(open=96, high=96.5, low=95, close=96, volume=0)
        elif row["date"] >= days[2]:
            row.update(open=98, high=99, low=97, close=98, volume=100000)
    action = {"id": "cash-dividend", "type": "dividend", "symbol": symbol, "date": days[2],
              "available_at": times[1].isoformat(), "amount_per_share": "1", "pay_date": days[3],
              "currency": "USD", "distribution": "ordinary_cash"}
    snapshot = Snapshot("stocks", snapshot.rows, config, "synthetic", snapshot.calendar, [action])
    state = new_account(10000)
    advance(state, snapshot, snapshot.context(times[0]), config, spec)
    state.update(cash="9000", positions={symbol: "10"}, average_cost={symbol: "100"},
                 stops={symbol: "97"}, take_profits={symbol: "106"})
    advance(state, snapshot, snapshot.context(times[1]), config, spec)
    assert state["pending"][0]["reason"] == "protective_stop"
    old_id = state["pending"][0]["client_order_id"]
    advance(state, snapshot, snapshot.context(times[2]), config, spec)
    assert state["pending"][0]["reason"] == "protective_stop"
    assert state["pending"][0]["replaces_client_order_id"] == old_id
    assert D(state["stops"][symbol]) == 96
    events = advance(state, snapshot, snapshot.context(times[3]), config, spec)
    fills = [e for e in events if e["status"] == "filled"]
    assert len(fills) == 1 and fills[0]["reason"] == "protective_stop"
    assert D(fills[0]["reference_price"]) == 98  # Exit remains due even above the adjusted stop.
    assert not state["positions"] and not state["pending"]
    assert D(state["dividend_income"]) == 10


def test_reverse_split_resizes_triggered_exit_and_prior_volume_in_same_share_units():
    config, spec, snapshot, symbol = fixture("stocks")
    before, after = snapshot.times[-2:]
    for row in snapshot.rows:
        if row["date"] == before.date().isoformat():
            row["volume"] = 1000
        elif row["date"] == after.date().isoformat():
            row.update(open=1000, high=1001, low=999, close=1000, volume=200)
    action = {"id": "reverse-split", "type": "split", "symbol": symbol,
              "date": after.date().isoformat(), "available_at": before.isoformat(), "ratio": "0.1"}
    snapshot = Snapshot("stocks", snapshot.rows, config, "synthetic", snapshot.calendar, [action])
    state = new_account(10000)
    advance(state, snapshot, snapshot.context(before), config, spec)
    state.update(cash="0", positions={symbol: "100"}, average_cost={symbol: "100"},
                 stops={symbol: "97"}, take_profits={symbol: "106"})
    queue_plan(state, {"orders": [{"client_order_id": "triggered-exit", "symbol": symbol,
                                   "side": "sell", "quantity": "100", "reason": "manual_exit"}]})
    events = advance(state, snapshot, snapshot.context(after), config, spec)
    fill = next(e for e in events if e["status"] == "filled")
    assert D(fill["bar_capacity"]) == 1 and D(fill["quantity"]) == 1
    assert D(fill["remaining"]) == 9 and D(state["positions"][symbol]) == 9
    assert state["pending"][0]["reason"] == "manual_exit"
    assert state["pending"][0]["replaces_client_order_id"] == "triggered-exit"
    assert D(state["stops"][symbol]) == 970


def test_replaced_protective_exit_replays_through_journal_and_independent_reconciliation(tmp_path):
    config, spec, snapshot, symbol = fixture("stocks")
    times = snapshot.times[-6:]
    days = [at.date().isoformat() for at in times]
    for row in snapshot.rows:
        if row["date"] == days[2]:
            row.update(open=96, high=96.5, low=95, close=96, volume=0)
        elif row["date"] >= days[3]:
            row.update(open=98, high=99, low=97, close=98, volume=100000)
    action = {"id": "dividend-replay", "type": "dividend", "symbol": symbol, "date": days[3],
              "available_at": times[2].isoformat(), "amount_per_share": "1", "pay_date": days[4],
              "currency": "USD", "distribution": "ordinary_cash"}
    snapshot = Snapshot("stocks", snapshot.rows, config, "synthetic", snapshot.calendar, [action])

    class EnterOnce:
        def decide(self, context, account, config):
            weight = 0.2 if context.as_of == times[0] else 0
            return DecisionBatch((Allocation(symbol, weight, 1, "synthetic"),), "fixture", "fixture")

    result = replay(snapshot, spec, config, EnterOnce(), times=times, journal_path=tmp_path / "dividend.jsonl")
    fills = [e for e in result["orders"] if e["status"] == "filled"]
    assert any(e.get("replaces_client_order_id") and e["reason"] == "protective_stop" for e in fills)
    assert not result["account"]["positions"]
    assert reconcile_replay(inspect_replay(result["journal_path"])["records"])["status"] == "reconciled"


@pytest.mark.parametrize("key,value", [("max_volume_participation", 0), ("latency_seconds", -1),
                                       ("entry_ttl_bars", 0), ("impact_bps_at_participation_cap", float("nan"))])
def test_invalid_matching_assumptions_are_rejected(key, value):
    config = load_config(environ={})
    config["execution"][key] = value
    with pytest.raises(ValueError, match=key):
        validate_config(config)
