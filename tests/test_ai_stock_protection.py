"""美股整股计划的保护订单、交易日冷却及公司行动；行情均为离线测试样本。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json

import pytest

from quantbot.ai.config import load_config, validate_config
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot, stock_calendar
from quantbot.ai.local_execution import advance, create_plan, new_account, queue_plan
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.runtime import prepare
from quantbot.ai.risk import _stock_cooldown_end, stock_in_cooldown


def setup():
    config = load_config(environ={})
    config["strategy"].update(momentum_window=20, stock_cooldown_bars=3)
    config["risk"].update(stock_stop_loss_pct=0.03, stock_take_profit_pct=0.06)
    config["execution"].update(initial_cash=10000)
    spec, config = prepare({"market": "stocks", "symbols": ["SPY"], "provider": "price_action",
                            "data_path": "unused.csv"}, config)
    calendar = stock_calendar("2024-01-01", "2024-06-14")
    rows = [{"date": str(day.date()), "symbol": "SPY", "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 1000000}
            for day in calendar.sessions_in_range("2024-01-01", "2024-06-14")]
    return config, spec, rows, calendar


def context(snapshot, day):
    return snapshot.context(snapshot.calendar.session_close(day).to_pydatetime())


def request(weight=0.2):
    return DecisionBatch((Allocation("SPY", weight, 1, "controlled test request"),), "test", "test")


@pytest.mark.parametrize("atr_stop,expected", [(95, 97), (98, 98)])
def test_stock_plan_uses_integer_shares_and_tighter_fill_anchored_stop(atr_stop, expected):
    config, spec, rows, calendar = setup()
    snapshot = Snapshot("stocks", rows, config, "test-fixture", calendar)
    state = new_account(10000)
    current = context(snapshot, "2024-05-22")
    current = replace(current, features={"SPY": {**current.features["SPY"], "suggested_stop": atr_stop}})
    advance(state, snapshot, current, config, spec)
    plan = create_plan(state, current, request(), config, "stocks", "test")
    order = plan["orders"][0]
    quantity = D(str(order["quantity"]))
    assert quantity == quantity.to_integral_value() and quantity > 0
    assert order["stop"] == atr_stop  # 风险预算仍按原 ATR 距离保守约束。
    assert order["stop_loss_pct"] == 0.03 and order["take_profit_pct"] == 0.06
    queue_plan(state, plan)
    events = advance(state, snapshot, context(snapshot, "2024-05-23"), config, spec)
    assert len(events) == 1 and events[0]["status"] == "filled"
    assert D(state["stops"]["SPY"]) == expected and D(state["take_profits"]["SPY"]) == 106
    assert D(state["cash"]) == 10000 - D("100.05") * quantity


def test_stock_same_bar_touches_both_levels_stops_first_and_reconciles(tmp_path):
    config, spec, rows, calendar = setup()
    for row in rows:
        if row["date"] == "2024-05-23":
            row.update(low=96, high=107)
    snapshot = Snapshot("stocks", rows, config, "test-fixture", calendar)

    class AlwaysEnter:
        def decide(self, context, account, config):
            return request()

    times = [at for at in snapshot.times if "2024-05-22" <= at.date().isoformat() <= "2024-05-24"]
    result = replay(snapshot, spec, config, AlwaysEnter(), times=times, journal_path=tmp_path / "stop.jsonl")
    fills = [order for order in result["orders"] if order["status"] == "filled"]
    assert len(fills) == 2 and fills[-1]["reason"] == "protective_stop"
    assert fills[-1]["both_touched"] and D(fills[-1]["reference_price"]) == 97
    assert not result["account"]["positions"]
    assert "SPY:entry_cooldown" in result["decisions"][-1]["adjustments"]
    assert reconcile_replay(inspect_replay(result["journal_path"])["records"])["status"] == "reconciled"


def test_stock_exit_cooldown_counts_sessions_not_weekend_or_holiday_and_survives_reload():
    config, spec, rows, calendar = setup()
    snapshot = Snapshot("stocks", rows, config, "test-fixture", calendar)
    state = new_account(10000)
    advance(state, snapshot, context(snapshot, "2024-05-22"), config, spec)
    queue_plan(state, create_plan(state, context(snapshot, "2024-05-22"), request(), config, "stocks", "test"))
    advance(state, snapshot, context(snapshot, "2024-05-23"), config, spec)
    queue_plan(state, create_plan(state, context(snapshot, "2024-05-23"), request(0), config, "stocks", "exit"))
    advance(state, snapshot, context(snapshot, "2024-05-24"), config, spec)
    assert state["last_exit_bar"]["SPY"] == context(snapshot, "2024-05-24").as_of.isoformat()
    state = json.loads(json.dumps(state))
    # 周五退出，周一 Memorial Day 休市；周二、周三都不能重新入场。
    for day in ("2024-05-24", "2024-05-28", "2024-05-29"):
        plan = create_plan(state, context(snapshot, day), request(), config, "stocks", "test")
        assert not plan["orders"] and "SPY:entry_cooldown" in plan["adjustments"]
    plan = create_plan(state, context(snapshot, "2024-05-30"), request(), config, "stocks", "test")
    assert plan["orders"]  # 三个完整交易日后收盘计划，最早周五开盘成交。


def test_split_and_dividend_adjust_both_brackets_and_reconcile(tmp_path):
    config, spec, rows, calendar = setup()
    for row in rows:
        if row["date"] >= "2024-05-24":
            price = 50 if row["date"] == "2024-05-24" else 49
            row.update(open=price, close=price, low=price - 0.4, high=price + 0.4)
        if row["date"] == "2024-05-29":
            row.update(high=53, close=52)
    actions = [
        {"id": "split", "symbol": "SPY", "type": "split", "date": "2024-05-24", "ratio": "2",
         "available_at": "2024-05-23T20:00:00+00:00"},
        {"id": "dividend", "symbol": "SPY", "type": "dividend", "date": "2024-05-28",
         "amount_per_share": "1", "pay_date": "2024-05-30", "currency": "USD",
         "distribution": "ordinary_cash", "available_at": "2024-05-24T20:00:00+00:00"},
    ]
    snapshot = Snapshot("stocks", rows, config, "test-fixture", calendar, actions)

    class EnterOnce:
        def decide(self, context, account, config):
            if context.as_of.date().isoformat() == "2024-05-22":
                return request()
            return request(account.positions.get("SPY", 0) * context.prices["SPY"] / account.equity)

    times = [at for at in snapshot.times if "2024-05-22" <= at.date().isoformat() <= "2024-05-30"]
    result = replay(snapshot, spec, config, EnterOnce(), times=times, journal_path=tmp_path / "actions.jsonl")
    views = {row["as_of"][:10]: row for row in result["equity_curve"]}
    assert D(views["2024-05-24"]["stops"]["SPY"]) == D("48.5")
    assert D(views["2024-05-24"]["take_profits"]["SPY"]) == 53
    assert D(views["2024-05-28"]["stops"]["SPY"]) == D("47.5")
    assert D(views["2024-05-28"]["take_profits"]["SPY"]) == 52
    fills = [order for order in result["orders"] if order["status"] == "filled"]
    assert len(fills) == 2 and fills[-1]["reason"] == "take_profit"
    assert D(fills[-1]["reference_price"]) == 52
    assert D(fills[-1]["quantity"]) == 2 * D(str(fills[0]["quantity"]))
    assert result["account"]["dividend_receivable"] == 0 and result["account"]["dividend_income"] > 0
    assert reconcile_replay(inspect_replay(result["journal_path"])["records"])["status"] == "reconciled"


def test_stock_defaults_do_not_enable_protection_or_cooldown():
    config, spec, rows, calendar = setup()
    defaults = load_config(environ={})
    config["strategy"]["stock_cooldown_bars"] = defaults["strategy"]["stock_cooldown_bars"]
    for key in ("stock_stop_loss_pct", "stock_take_profit_pct"):
        config["risk"][key] = defaults["risk"][key]
    snapshot = Snapshot("stocks", rows, config, "test-fixture", calendar)
    current = context(snapshot, "2024-05-22")
    state = new_account(10000)
    state["last_exit_bar"]["SPY"] = current.as_of.isoformat()
    plan = create_plan(state, current, request(), config, "stocks", "test")
    assert plan["orders"] and "stop_loss_pct" not in plan["orders"][0]
    assert "take_profit_pct" not in plan["orders"][0]


def test_cooling_stock_does_not_consume_another_symbols_position_budget():
    config, _, rows, calendar = setup()
    config["risk"]["max_positions"] = 1
    snapshot = Snapshot("stocks", rows, config, "test-fixture", calendar)
    current = context(snapshot, "2024-05-22")
    current = replace(current, features={
        "SPY": {**current.features["SPY"], "factor_score": 2},
        "AAPL": {**current.features["SPY"], "factor_score": 1},
    })
    state = new_account(10000)
    state["last_exit_bar"]["SPY"] = current.as_of.isoformat()
    decision = DecisionBatch(tuple(Allocation(s, 0.2, 1, "test") for s in ("SPY", "AAPL")), "test", "test")
    plan = create_plan(state, current, decision, config, "stocks", "test")
    assert [order["symbol"] for order in plan["orders"]] == ["AAPL"]
    assert "SPY:entry_cooldown" in plan["adjustments"]


def test_stock_protection_settings_do_not_change_crypto_plan():
    config = load_config(environ={})
    config["strategy"]["stock_cooldown_bars"] = 100
    config["risk"].update(stock_stop_loss_pct=0.09, stock_take_profit_pct=0.18,
                          crypto_stop_loss_pct=0.03, crypto_take_profit_pct=0.06)
    _, config = prepare({"market": "crypto", "symbols": ["BTC/USDT"], "provider": "crypto_rules",
                         "bar": "1d", "data_path": "unused.csv"}, config)
    rows = [{"timestamp": (datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
             "symbol": "BTC/USDT", "bar": "1d", "open": 100, "high": 101, "low": 99,
             "close": 100, "volume": 1000000} for i in range(70)]
    snapshot = Snapshot("crypto", rows, config, "test-fixture")
    current = snapshot.context(snapshot.times[50])
    state = new_account(10000)
    state["last_exit_bar"]["BTC/USDT"] = current.as_of.isoformat()
    decision = DecisionBatch((Allocation("BTC/USDT", 0.2, 1, "test"),), "test", "test")
    order = create_plan(state, current, decision, config, "crypto", "test")["orders"][0]
    assert order["stop_loss_pct"] == 0.03 and order["take_profit_pct"] == 0.06


def test_cached_cooldown_matches_session_count_across_holidays_and_old_exits():
    calendar = stock_calendar("2019-01-02", "2025-06-30")
    _stock_cooldown_end.cache_clear()
    cases = [
        ("2024-05-24", 3, ("2024-05-24", "2024-05-28", "2024-05-29", "2024-05-30", "2025-06-02")),
        ("2024-11-27", 3, ("2024-11-27", "2024-11-29", "2024-12-02", "2024-12-03")),
        ("2019-01-02", 100, ("2019-01-02", "2019-05-24", "2019-06-03", "2025-06-02")),
    ]
    for exited, bars, days in cases:
        exits = {"SPY": calendar.session_close(exited).to_pydatetime().isoformat()}
        for day in days:
            at = calendar.session_close(day).to_pydatetime()
            expected = len(calendar.sessions_in_range(exited, day)) - 1 < bars
            assert stock_in_cooldown(at, exits, "SPY", bars) == expected
            assert stock_in_cooldown(at, exits, "SPY", 0) is False
    info = _stock_cooldown_end.cache_info()
    assert info.misses == 3 and info.hits == 10


@pytest.mark.parametrize("section,key,value", [
    ("risk", "stock_stop_loss_pct", 0.03),
    ("risk", "stock_take_profit_pct", float("nan")),
    ("strategy", "stock_cooldown_bars", True),
    ("strategy", "stock_cooldown_bars", -1),
])
def test_invalid_stock_protection_config_is_rejected(section, key, value):
    config = load_config(environ={})
    config[section][key] = value
    with pytest.raises(ValueError):
        validate_config(config)
