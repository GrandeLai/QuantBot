"""Entry decisions retain their original outcome and auditable rejection state."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest

from tests.test_intraday_execution import T, account, bar, enter, step
from tests.test_intraday_execution import model as model, section as section


@pytest.fixture(params=[("crypto", "BTC/USDT"), ("stocks", "SPY")])
def market_symbol(request):
    return request.param


def attempt(a, symbol, **overrides):
    values = {"day": "2025-01-02", "reference": 100, "atr": 2, "identity": "entry-audit"}
    values.update(overrides)
    return a.enter(symbol, T + timedelta(minutes=5), **values)


def rejection(a):
    return a.attempts[-1]["rejection"]


def test_rejection_priority_preserves_short_circuit_and_records_each_attempt(section, model, market_symbol):
    market, symbol = market_symbol
    a = account(section, model, market)
    a.state["risk_latched"] = "daily_loss_limit"
    a.state["last_exit_bar"][symbol] = "not parsed while risk is latched"
    a.state["positions"][symbol] = "2"
    a.orders["existing"] = {
        "client_order_id": "existing", "symbol": symbol, "side": "sell", "remaining": "2",
    }
    assert not attempt(a, symbol, atr=0)
    assert rejection(a)["reason"] == "risk_latched"
    assert rejection(a)["context"]["risk_latched"] == "daily_loss_limit"

    a.state["risk_latched"] = None
    a.state["last_exit_bar"][symbol] = T.isoformat()
    assert not attempt(a, symbol, atr=0)
    assert rejection(a) == {"reason": "cooldown", "context": {
        "last_exit_bar": T.isoformat(), "day": "2025-01-02", "market": market,
        "cooldown_bars": 3, "cooldown_unit": "sessions" if market == "stocks" else "calendar_days",
    }}

    a.state["last_exit_bar"].clear()
    assert not attempt(a, symbol, atr=0)
    assert rejection(a) == {"reason": "position_exists", "context": {"quantity": "2"}}

    a.state["positions"].clear()
    assert not attempt(a, symbol, atr=0)
    assert rejection(a) == {"reason": "pending_order_exists", "context": {
        "client_order_id": "existing", "side": "sell", "remaining": "2",
    }}

    a.orders.clear()
    a.section["risk"]["max_positions"] = 0
    assert not attempt(a, symbol, atr=0)
    assert rejection(a) == {"reason": "invalid_stop", "context": {
        "reference": "100", "atr": "0", "stop": "100.0", "limit": "100.500",
    }}

    assert not attempt(a, symbol)
    assert rejection(a) == {"reason": "max_positions", "context": {
        "positions_count": 0, "pending_buys_count": 0, "max_positions": 0,
    }}

    a.section["risk"].update(max_positions=5, cash_buffer=1)
    assert not attempt(a, symbol)
    assert rejection(a)["reason"] == "nonpositive_quantity"
    assert D(rejection(a)["context"]["cash_available"]) == 0
    assert [r["rejection"]["reason"] for r in a.attempts] == [
        "risk_latched", "cooldown", "position_exists", "pending_order_exists", "invalid_stop",
        "max_positions", "nonpositive_quantity",
    ]
    assert all(r["symbol"] == symbol and r["at"] == (T + timedelta(minutes=5)).isoformat()
               and r["accepted"] is False for r in a.attempts)
    assert not a.events


@pytest.mark.parametrize("field,value,cap", [
    ("cash_buffer", 1, "cash"),
    ("max_gross_exposure", 0, "gross_exposure"),
    ("max_weight", 0, "max_weight"),
    ("risk_per_trade", 0, "risk_per_trade"),
    ("max_portfolio_heat", 0, "portfolio_heat"),
])
def test_nonpositive_quantity_identifies_each_limit(section, model, market_symbol, field, value, cap):
    market, symbol = market_symbol
    a = account(section, model, market)
    a.section["risk"][field] = value
    assert not attempt(a, symbol)
    detail = rejection(a)
    assert detail["reason"] == "nonpositive_quantity"
    limits = detail["context"]["quantity_limits"]
    assert list(limits) == ["cash", "gross_exposure", "max_weight", "risk_per_trade", "portfolio_heat"]
    assert D(limits[cap]) == 0
    assert all(D(q) > 0 for name, q in limits.items() if name != cap)
    assert D(detail["context"]["quantity"]) == 0
    assert not a.orders and not a.events


def test_positive_limits_below_quantity_step_remain_distinct_from_zero_cash(section, model, market_symbol):
    market, symbol = market_symbol
    a = account(section, model, market)
    a.state["cash"] = "0.00000001"
    assert not attempt(a, symbol)
    detail = rejection(a)["context"]
    assert all(D(q) > 0 for q in detail["quantity_limits"].values())
    assert 0 < min(D(q) for q in detail["quantity_limits"].values()) < D(detail["quantity_step"])
    assert D(detail["cash_available"]) > 0
    assert D(detail["quantity"]) == 0
    assert D(detail["quantity_step"]) == (D("1") if market == "stocks" else D("0.00001"))


def test_rejection_snapshots_do_not_follow_account_order_or_policy_mutation(section, model, market_symbol):
    market, symbol = market_symbol
    a = account(section, model, market)
    a.state["risk_latched"] = "drawdown_limit"
    a.state["positions"][symbol] = "2"
    a.state["prices"][symbol] = "100"
    assert not attempt(a, symbol)
    frozen = deepcopy(a.attempts[0])
    a.state["positions"][symbol] = "3"
    a.state["prices"][symbol] = "105"
    a.state["peak"] = "12345"
    a.section["max_drawdown"] = 0.5
    assert a.attempts[0] == frozen

    a.state["risk_latched"] = None
    a.state["positions"].clear()
    a.orders["buy"] = {
        "client_order_id": "buy", "symbol": symbol, "side": "buy", "remaining": "4",
    }
    assert not attempt(a, symbol)
    frozen = deepcopy(a.attempts[-1])
    a.orders["buy"]["remaining"] = "1"
    a._cancel("buy", "entry_expired", T + timedelta(minutes=20))
    assert a.attempts[-1] == frozen


def test_max_positions_keeps_existing_position_plus_pending_buy_count(section, model, market_symbol):
    market, symbol = market_symbol
    other = "ETH/USDT" if market == "crypto" else "QQQ"
    a = account(section, model, market)
    a.state["positions"][other] = "1"
    a.state["average_cost"][other] = "100"
    a.state["prices"][other] = "100"
    a.state["stops"][other] = "97"
    a.orders["partial"] = {
        "client_order_id": "partial", "symbol": other, "side": "buy", "remaining": "1",
        "limit_price": "100.5", "stop": "97",
    }
    a.section["risk"]["max_positions"] = 2
    assert not attempt(a, symbol)
    assert rejection(a) == {"reason": "max_positions", "context": {
        "positions_count": 1, "pending_buys_count": 1, "max_positions": 2,
    }}


@pytest.mark.parametrize("path", ["unfilled_cancel", "partial_expiry"])
def test_accepted_attempt_remains_accepted_after_cancellation_or_partial_fill(
    section, model, market_symbol, path,
):
    market, symbol = market_symbol
    a = account(section, model, market)
    enter(a, symbol)
    accepted = {"symbol": symbol, "at": (T + timedelta(minutes=5)).isoformat(), "accepted": True}
    assert a.attempts == [accepted]
    step(a, 5, bar(symbol))
    if path == "unfilled_cancel":
        step(a, 10, bar(symbol, open="102", high="103", low="101", close="102"))
        assert a.events[-1]["reason"] == "open_above_entry_limit"
        assert not a.state["positions"]
    else:
        for minute in (10, 15, 20):
            step(a, minute, bar(symbol))
        assert any(e["status"] == "filled" and e["partial"] for e in a.events)
        assert any(e["status"] == "canceled" and e["reason"] == "entry_expired" for e in a.events)
        assert D(a.state["positions"][symbol]) == 4
    assert a.report()["attempts"] == [accepted]
