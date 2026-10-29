"""券商公共契约：幂等键传递与跨进程模拟订单稳定性。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.models import Calendar

from quantbot.price_action.broker.alpaca_broker import AlpacaBroker
from quantbot.price_action.broker.base import DryRunBroker
from quantbot.price_action.broker.sim_broker import SimBroker
from quantbot.price_action.data.session import instant


class _RecordingTradingClient:
    def __init__(self) -> None:
        self.requests = []
        self.orders_by_client_id = {}

    def get_order_by_client_id(self, client_order_id):
        if client_order_id not in self.orders_by_client_id:
            raise _NotFoundError()
        return self.orders_by_client_id[client_order_id]

    def get_orders(self, filter):
        return [
            order
            for order in self.orders_by_client_id.values()
            if getattr(order.status, "value", order.status)
            not in {"filled", "canceled", "expired", "rejected"}
        ]

    def submit_order(self, order_data):
        self.requests.append(order_data)
        order = _alpaca_order(
            order_id=f"alpaca-{len(self.requests)}",
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            shares=order_data.qty,
            side=order_data.side,
        )
        if order_data.client_order_id:
            self.orders_by_client_id[order_data.client_order_id] = order
        return order


@pytest.mark.parametrize(
    ("day", "previous", "close", "offset"),
    [("2026-09-08", "2026-09-04", "16:00", "-04:00"),
     ("2026-11-27", "2026-11-25", "13:00", "-05:00")],
)
def test_alpaca_calendar_preserves_holidays_early_closes_and_dst(day, previous, close, offset):
    broker = object.__new__(AlpacaBroker)
    requests = []

    def calendar(request):
        requests.append(request)
        return [Calendar(date=day, open="09:30", close=close),
                Calendar(date=previous, open="09:30", close="16:00")]

    broker.client = SimpleNamespace(get_calendar=calendar)
    session = broker.get_market_session(day)

    assert session.signal_date == previous
    assert session.opens_at == f"{day}T09:30:00{offset}"
    assert session.closes_at == f"{day}T{close}:00{offset}"
    assert session.contains_close_observation(instant(session.closes_at))
    assert requests[0].end.isoformat() == day


def test_alpaca_calendar_missing_execution_day_cannot_be_inferred_as_weekday():
    broker = object.__new__(AlpacaBroker)
    broker.client = SimpleNamespace(get_calendar=lambda request: [
        Calendar(date="2026-09-03", open="09:30", close="16:00"),
        Calendar(date="2026-09-04", open="09:30", close="16:00"),
    ])
    with pytest.raises(RuntimeError, match="不是可确认的交易会话"):
        broker.get_market_session("2026-09-07")


class _NotFoundError(Exception):
    status_code = 404


def _alpaca_order(
    *,
    order_id: str,
    client_order_id: str,
    symbol: str,
    shares: int,
    side: OrderSide,
    status: str = "new",
    filled_qty: int = 0,
    filled_avg_price: float | None = None,
):
    return SimpleNamespace(
        id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        qty=str(shares),
        side=side,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        status=SimpleNamespace(value=status),
        filled_qty=str(filled_qty),
        filled_avg_price=(
            None if filled_avg_price is None else str(filled_avg_price)
        ),
    )


def test_alpaca_passes_client_order_id_to_sdk_request():
    """确定性幂等键必须进入 Alpaca SDK 请求，而不是只留在本地日志。"""
    broker = object.__new__(AlpacaBroker)
    broker.client = _RecordingTradingClient()

    buy_id = broker.market_buy("BRK-B", 3, client_order_id="run-1-buy-brkb")
    sell_id = broker.market_sell("BRK-B", 2, client_order_id="run-1-sell-brkb")

    assert buy_id == "alpaca-1"
    assert sell_id == "alpaca-2"
    assert broker.client.requests[0].symbol == "BRK.B"
    assert broker.client.requests[0].client_order_id == "run-1-buy-brkb"
    assert broker.client.requests[1].client_order_id == "run-1-sell-brkb"


def test_alpaca_returns_existing_order_without_duplicate_submit():
    """重启后预查命中同一 client ID 时直接恢复原 broker order ID。"""
    broker = object.__new__(AlpacaBroker)
    broker.client = _RecordingTradingClient()
    broker.client.orders_by_client_id["stable-key"] = _alpaca_order(
        order_id="original-order",
        client_order_id="stable-key",
        symbol="TST",
        shares=10,
        side=OrderSide.BUY,
    )

    order_id = broker.market_buy("TST", 10, client_order_id="stable-key")

    assert order_id == "original-order"
    assert broker.client.requests == []


def test_alpaca_recovers_order_after_submit_response_is_lost():
    """submit 已落服务端但响应丢失时，只读恢复并返回原订单号。"""

    class _ResponseLostClient(_RecordingTradingClient):
        def submit_order(self, order_data):
            order = super().submit_order(order_data)
            assert order.id == "alpaca-1"
            raise TimeoutError("response lost")

    broker = object.__new__(AlpacaBroker)
    broker.client = _ResponseLostClient()

    order_id = broker.market_sell("TST", 4, client_order_id="lost-response")

    assert order_id == "alpaca-1"
    assert len(broker.client.requests) == 1


def test_alpaca_unknown_preflight_lookup_does_not_submit():
    """预查不是明确 404 时结果未知，必须阻止下单。"""

    class _LookupUnavailableClient(_RecordingTradingClient):
        def get_order_by_client_id(self, client_order_id):
            raise TimeoutError("lookup unavailable")

    broker = object.__new__(AlpacaBroker)
    broker.client = _LookupUnavailableClient()

    with pytest.raises(RuntimeError, match="无法确认"):
        broker.market_buy("TST", 2, client_order_id="unknown-key")
    assert broker.client.requests == []


def test_alpaca_public_order_reads_distinguish_not_found_from_unknown():
    broker = object.__new__(AlpacaBroker)
    broker.client = _RecordingTradingClient()
    broker.client.orders_by_client_id["known"] = _alpaca_order(
        order_id="alpaca-known",
        client_order_id="known",
        symbol="BRK.B",
        shares=3,
        side=OrderSide.BUY,
        status="partially_filled",
        filled_qty=1,
        filled_avg_price=101.25,
    )

    snapshot = broker.get_order_by_client_id("known")

    assert snapshot is not None
    assert snapshot.symbol == "BRK-B"
    assert snapshot.status == "partially_filled"
    assert snapshot.fill == (1, 101.25)
    assert snapshot.is_open
    assert broker.get_order_by_client_id("missing") is None
    assert [order.order_id for order in broker.list_open_orders()] == ["alpaca-known"]

    class _Unavailable(_RecordingTradingClient):
        def get_order_by_client_id(self, client_order_id):
            raise TimeoutError("network down")

        def get_orders(self, filter):
            raise TimeoutError("network down")

    broker.client = _Unavailable()
    with pytest.raises(RuntimeError, match="无法确认"):
        broker.get_order_by_client_id("unknown")
    with pytest.raises(RuntimeError, match="工作订单"):
        broker.list_open_orders()


def test_alpaca_account_fingerprint_is_stable_and_never_exposes_account_id():
    broker = object.__new__(AlpacaBroker)
    broker.paper = True
    broker.client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(id="raw-account-id-123")
    )

    fingerprint = broker.account_fingerprint()

    assert len(fingerprint) == 64
    assert fingerprint == broker.account_fingerprint()
    assert "raw-account-id-123" not in fingerprint


def test_alpaca_rejects_client_id_bound_to_different_intent():
    broker = object.__new__(AlpacaBroker)
    broker.client = _RecordingTradingClient()
    broker.client.orders_by_client_id["conflict"] = _alpaca_order(
        order_id="existing-sell",
        client_order_id="conflict",
        symbol="TST",
        shares=10,
        side=OrderSide.SELL,
    )

    with pytest.raises(RuntimeError, match="不同订单意图"):
        broker.market_buy("TST", 10, client_order_id="conflict")
    assert broker.client.requests == []


def test_old_two_argument_broker_calls_remain_valid():
    """新增可选参数不能破坏现有 runner 的两参数调用。"""
    broker = DryRunBroker()
    assert broker.market_buy("TST", 1) == "dry-run"
    assert broker.market_sell("TST", 1) == "dry-run"
    assert broker.market_buy("TST", 1, "decision-1") == "decision-1"


def test_sim_order_and_fill_are_stable_across_process_restart(tmp_path, monkeypatch):
    """重建 SimBroker 后，同一 client ID 不重复成交，订单号和 fill 均稳定。"""
    monkeypatch.setattr(SimBroker, "_latest_close", lambda self, symbol: 10.0)
    cfg = {
        "backtest": {"initial_capital": 1000.0},
        "live": {"cache_dir": str(tmp_path / "cache")},
    }
    state_path = tmp_path / "nested" / "sim_state.json"

    first = SimBroker(cfg, state_path=state_path)
    order_id = first.market_buy("TST", 10, client_order_id="decision-20260809-TST-buy")
    cash_after_first_fill = first.cash
    assert first.order_fill(order_id) is not None

    restarted = SimBroker(cfg, state_path=state_path)
    repeated_id = restarted.market_buy(
        "TST", 10, client_order_id="decision-20260809-TST-buy"
    )
    assert repeated_id == order_id
    assert restarted.cash == pytest.approx(cash_after_first_fill, abs=0.01)
    assert restarted.get_positions()["TST"].shares == 10
    assert restarted.wait_order(order_id)
    assert restarted.order_fill(order_id) == first.order_fill(order_id)

    next_id = restarted.market_sell("TST", 1)
    assert next_id != order_id, "无 client ID 的订单号也不能在进程重启后重用"
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["next_order_seq"] == 2
    assert saved["orders"][order_id]["client_order_id"] \
        == "decision-20260809-TST-buy"


def test_sim_rejects_reusing_client_id_for_different_intent(tmp_path, monkeypatch):
    """同一幂等键对应不同方向/数量时必须 fail-closed。"""
    monkeypatch.setattr(SimBroker, "_latest_close", lambda self, symbol: 10.0)
    cfg = {
        "backtest": {"initial_capital": 1000.0},
        "live": {"cache_dir": str(tmp_path / "cache")},
    }
    broker = SimBroker(cfg, state_path=tmp_path / "sim_state.json")
    broker.market_buy("TST", 10, client_order_id="same-key")

    with pytest.raises(ValueError, match="已用于不同订单"):
        broker.market_sell("TST", 10, client_order_id="same-key")


def test_sim_reserves_order_id_before_quote_failure(tmp_path, monkeypatch):
    """订单构建中断也要保留序号，重启后不得把 sim-N 分配给另一笔单。"""
    cfg = {
        "backtest": {"initial_capital": 1000.0},
        "live": {"cache_dir": str(tmp_path / "cache")},
    }
    state_path = tmp_path / "sim_state.json"
    broker = SimBroker(cfg, state_path=state_path)

    def _quote_failed(self, symbol):
        raise OSError("quote read failed")

    monkeypatch.setattr(SimBroker, "_latest_close", _quote_failed)
    with pytest.raises(OSError, match="quote read failed"):
        broker.market_buy("TST", 1, client_order_id="reserved-key")

    monkeypatch.setattr(SimBroker, "_latest_close", lambda self, symbol: 10.0)
    restarted = SimBroker(cfg, state_path=state_path)
    snapshot = restarted.get_order_by_client_id("reserved-key")
    assert snapshot is not None and snapshot.status == "created"
    assert [order.order_id for order in restarted.list_open_orders()] == ["sim-1"]
    assert restarted.market_buy("TST", 1, client_order_id="reserved-key") == "sim-1"
    assert restarted.get_positions() == {}, "未完成的保留单不得在重启时自行成交"
    assert restarted.market_buy("TST", 1) == "sim-2"
