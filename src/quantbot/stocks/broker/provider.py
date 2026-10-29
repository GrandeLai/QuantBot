"""交易 Provider 抽象与工厂."""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from quantbot.stocks.broker.futu import FutuTradingProvider
from quantbot.stocks.broker.mock import MockTradingProvider
from quantbot.stocks.broker.types import (
    TradingAccountOverview,
    TradingCancelResult,
    TradingCashFlow,
    TradingExecution,
    TradingOrder,
    TradingOrderEstimate,
    TradingOrderEstimateRequest,
    TradingOrderRequest,
    TradingPosition,
    TradingProviderStatus,
    TradingQuote,
    TradingSecurity,
    TradingSubmitResult,
)
from quantbot.common.config import get_settings


class TradingProvider(Protocol):
    """统一交易 provider 接口."""

    def get_status(self) -> TradingProviderStatus: ...

    def search_securities(self, query: str, *, limit: int = 20) -> list[TradingSecurity]: ...

    def get_quotes(self, symbols: list[str]) -> list[TradingQuote]: ...

    def get_account_overview(self) -> TradingAccountOverview: ...

    def get_positions(self) -> list[TradingPosition]: ...

    def get_today_orders(self) -> list[TradingOrder]: ...

    def get_history_orders(self) -> list[TradingOrder]: ...

    def get_order_detail(self, order_id: str) -> TradingOrder: ...

    def estimate_order(self, request: TradingOrderEstimateRequest) -> TradingOrderEstimate: ...

    def submit_order(self, request: TradingOrderRequest) -> TradingSubmitResult: ...

    def cancel_order(self, order_id: str) -> TradingCancelResult: ...

    def get_today_executions(self) -> list[TradingExecution]: ...

    def get_history_executions(self) -> list[TradingExecution]: ...

    def get_cash_flows(self) -> list[TradingCashFlow]: ...


@lru_cache(maxsize=1)
def get_trading_provider() -> TradingProvider:
    """返回当前交易 provider 单例.

    选择策略：
    - `QUANTBOT_TRADING_PROVIDER=futu` 时使用 Futu provider
    - `QUANTBOT_TRADING_PROVIDER=longbridge` 时优先尝试 Longbridge
    - `...=mock` 时强制 mock
    - `...=auto` 时优先 Longbridge，失败则回退到 mock
    """
    settings = get_settings()
    provider_name = settings.trading_provider.lower()

    if provider_name == "mock":
        return MockTradingProvider()

    if provider_name == "futu":
        return FutuTradingProvider()

    from quantbot.stocks.broker.longbridge import LongbridgeTradingProvider

    if provider_name == "longbridge":
        try:
            return LongbridgeTradingProvider()
        except Exception as exc:
            return MockTradingProvider(
                fallback_reason=f"Longbridge provider unavailable: {exc}",
                using_fallback=True,
            )

    try:
        return LongbridgeTradingProvider()
    except Exception as exc:
        return MockTradingProvider(
            fallback_reason=f"Longbridge provider unavailable: {exc}",
            using_fallback=True,
        )
