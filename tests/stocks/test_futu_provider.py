"""Unit tests for the Futu trading provider mapping layer."""

from __future__ import annotations

from types import SimpleNamespace

from quantbot.stocks.broker.futu import FutuTradingProvider
from quantbot.stocks.broker.types import (
    TradingOrderEstimateRequest,
    TradingOrderRequest,
    TradingOrderSide,
    TradingOrderType,
    TradingProviderKind,
)


class _FakeSDK:
    RET_OK = 0

    class TrdEnv:
        SIMULATE = "SIMULATE"

    class TrdSide:
        BUY = "BUY"
        SELL = "SELL"

    class OrderType:
        NORMAL = "NORMAL"
        MARKET = "MARKET"

    class ModifyOrderOp:
        CANCEL = "CANCEL"


class _FakeQuoteContext:
    def get_market_snapshot(self, symbols: list[str]) -> tuple[int, list[dict[str, object]]]:
        return (
            0,
            [
                {
                    "code": symbols[0],
                    "stock_name": "Apple Inc.",
                    "lot_size": 1,
                    "last_price": 192.84,
                    "prev_close_price": 190.10,
                    "suspension": False,
                }
            ],
        )


class _FakeTradeContext:
    def __init__(self) -> None:
        self.placed_order: dict[str, object] | None = None
        self.modified_order: dict[str, object] | None = None

    def get_acc_list(self) -> tuple[int, list[dict[str, object]]]:
        return (0, [{"acc_id": 1001, "trd_env": "SIMULATE"}])

    def accinfo_query(self, **_: object) -> tuple[int, list[dict[str, object]]]:
        return (
            0,
            [
                {
                    "total_assets": 120000.0,
                    "available_funds": 50000.0,
                    "avl_withdrawal_cash": 49000.0,
                    "power": 80000.0,
                    "market_val": 70000.0,
                    "unrealized_pl": 1200.0,
                    "realized_pl": 350.0,
                    "currency": "USD",
                    "frozen_cash": 1000.0,
                }
            ],
        )

    def position_list_query(self, **_: object) -> tuple[int, list[dict[str, object]]]:
        return (
            0,
            [
                {
                    "code": "US.AAPL",
                    "stock_name": "Apple Inc.",
                    "qty": 10,
                    "can_sell_qty": 10,
                    "cost_price": 180.0,
                    "nominal_price": 192.84,
                    "market_val": 1928.4,
                    "pl_val": 128.4,
                    "pl_ratio": 0.0713,
                    "today_pl_val": 22.0,
                    "today_buy_qty": 0,
                    "today_sell_qty": 0,
                    "currency": "USD",
                }
            ],
        )

    def order_list_query(self, **_: object) -> tuple[int, list[dict[str, object]]]:
        return (
            0,
            [
                {
                    "order_id": "FUTU-1",
                    "code": "US.AAPL",
                    "stock_name": "Apple Inc.",
                    "trd_side": "BUY",
                    "order_type": "NORMAL",
                    "order_status": "SUBMITTED",
                    "qty": 10,
                    "dealt_qty": 0,
                    "price": 190.0,
                    "create_time": "2026-04-14T10:00:00+00:00",
                    "updated_time": "2026-04-14T10:00:00+00:00",
                    "currency": "USD",
                }
            ],
        )

    def history_order_list_query(self, **kwargs: object) -> tuple[int, list[dict[str, object]]]:
        return self.order_list_query(**kwargs)

    def deal_list_query(self, **_: object) -> tuple[int, list[dict[str, object]]]:
        return (
            0,
            [
                {
                    "deal_id": "DEAL-1",
                    "order_id": "FUTU-1",
                    "code": "US.AAPL",
                    "stock_name": "Apple Inc.",
                    "trd_side": "BUY",
                    "price": 190.0,
                    "qty": 10,
                    "create_time": "2026-04-14T10:01:00+00:00",
                    "currency": "USD",
                }
            ],
        )

    def history_deal_list_query(self, **kwargs: object) -> tuple[int, list[dict[str, object]]]:
        return self.deal_list_query(**kwargs)

    def acctradinginfo_query(self, **_: object) -> tuple[int, list[dict[str, object]]]:
        return (0, [{"max_cash_buy": 200, "max_position_sell": 10}])

    def place_order(self, **kwargs: object) -> tuple[int, list[dict[str, object]]]:
        self.placed_order = kwargs
        return (0, [{"order_id": "FUTU-NEW", "order_status": "SUBMITTED"}])

    def modify_order(self, **kwargs: object) -> tuple[int, str]:
        self.modified_order = kwargs
        return (0, "success")


def _provider() -> FutuTradingProvider:
    settings = SimpleNamespace(
        futu_host="127.0.0.1",
        futu_port=11111,
        futu_market="US",
        futu_unlock_password="",
    )
    return FutuTradingProvider(
        sdk=_FakeSDK(),
        quote_context=_FakeQuoteContext(),
        trade_context=_FakeTradeContext(),
        settings=settings,
    )


def test_futu_provider_maps_quotes_account_and_positions() -> None:
    provider = _provider()

    status = provider.get_status()
    quotes = provider.get_quotes(["AAPL.US"])
    overview = provider.get_account_overview()
    positions = provider.get_positions()
    estimate = provider.estimate_order(
        TradingOrderEstimateRequest(
            symbol="AAPL.US",
            side=TradingOrderSide.BUY,
            order_type=TradingOrderType.MARKET,
        )
    )

    assert status.provider == TradingProviderKind.FUTU
    assert status.configured is True
    assert quotes[0].symbol == "AAPL.US"
    assert quotes[0].last_price == 192.84
    assert overview.provider == TradingProviderKind.FUTU
    assert overview.available_cash == 50000.0
    assert positions[0].symbol == "AAPL.US"
    assert estimate.cash_max_qty == 200


def test_futu_write_adapter_is_retired():
    import pytest
    provider = _provider()
    with pytest.raises(RuntimeError, match="统一交易入口"):
        provider.submit_order(None)
    with pytest.raises(RuntimeError, match="统一交易入口"):
        provider.cancel_order("old-id")
    assert provider._trade_context.placed_order is None
