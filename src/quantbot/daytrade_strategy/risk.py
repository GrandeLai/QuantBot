"""Mark-to-market heat and correlation-aware intraday position sizing."""

from __future__ import annotations

from .domain import (
    DayTradeRiskSizingConfig,
    IntradayPositionSize,
    PortfolioHeatState,
    finite_number,
    positive_number,
)


def size_intraday_position(
    *,
    entry_price: float,
    stop_price: float,
    state: PortfolioHeatState,
    config: DayTradeRiskSizingConfig,
    max_abs_correlation: float = 0.0,
) -> IntradayPositionSize:
    if not isinstance(state, PortfolioHeatState):
        raise TypeError("state must be a PortfolioHeatState")
    if not isinstance(config, DayTradeRiskSizingConfig):
        raise TypeError("config must be a DayTradeRiskSizingConfig")
    entry = positive_number(entry_price, "entry_price")
    stop = positive_number(stop_price, "stop_price")
    per_share_risk = abs(entry - stop)
    if per_share_risk == 0:
        raise ValueError("entry_price and stop_price must differ")
    correlation = abs(finite_number(max_abs_correlation, "max_abs_correlation"))
    correlation = min(correlation, 1.0)
    equity = state.mark_to_market_equity
    heat_cap = equity * config.max_portfolio_heat_fraction
    available_heat = max(0.0, heat_cap - state.open_risk)
    base_risk = min(equity * config.per_trade_risk_fraction, available_heat)
    adjusted_risk = base_risk * (1.0 - config.correlation_penalty * correlation)
    risk_quantity = adjusted_risk / per_share_risk
    notional_quantity = (
        equity * config.max_position_notional_fraction / entry if equity > 0 else 0.0
    )
    quantity = max(0.0, min(risk_quantity, notional_quantity))
    actual_risk = quantity * per_share_risk
    return IntradayPositionSize(
        quantity=quantity,
        risk_budget=actual_risk,
        notional=quantity * entry,
        mark_to_market_equity=equity,
        available_heat=available_heat,
    )

