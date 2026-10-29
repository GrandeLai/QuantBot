"""Immutable inputs and decisions for preregistered intraday hypotheses."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal


Side = Literal["long", "short"]


def finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def positive_number(value: float, name: str) -> float:
    result = finite_number(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def non_negative_number(value: float, name: str) -> float:
    result = finite_number(value, name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def side_value(value: str) -> Side:
    if value not in {"long", "short"}:
        raise ValueError("side must be long or short")
    return value


@dataclass(frozen=True, slots=True)
class IntradayFeatureSnapshot:
    """Features observed no later than an entry decision."""

    symbol: str
    observed_at: datetime
    atr: float
    relative_volume: float
    gap_fraction: float
    premarket_volume: float
    spy_return: float
    qqq_return: float
    sector_relative_strength: float
    borrow_available: bool = False
    ssr_active: bool = False
    best_bid: float | None = None
    max_abs_correlation: float = 0.0
    annualized_borrow_rate: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        aware(self.observed_at, "observed_at")
        object.__setattr__(self, "atr", positive_number(self.atr, "atr"))
        for name in ("relative_volume", "premarket_volume"):
            object.__setattr__(
                self,
                name,
                non_negative_number(getattr(self, name), name),
            )
        for name in (
            "gap_fraction",
            "spy_return",
            "qqq_return",
            "sector_relative_strength",
        ):
            object.__setattr__(self, name, finite_number(getattr(self, name), name))
        correlation = non_negative_number(
            self.max_abs_correlation,
            "max_abs_correlation",
        )
        if correlation > 1:
            raise ValueError("max_abs_correlation must not exceed one")
        object.__setattr__(self, "max_abs_correlation", correlation)
        object.__setattr__(
            self,
            "annualized_borrow_rate",
            non_negative_number(
                self.annualized_borrow_rate,
                "annualized_borrow_rate",
            ),
        )
        for name in ("borrow_available", "ssr_active"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")
        if self.best_bid is not None:
            object.__setattr__(
                self,
                "best_bid",
                positive_number(self.best_bid, "best_bid"),
            )


@dataclass(frozen=True, slots=True)
class DayTradeHypothesisConfig:
    side: Side = "long"
    min_opening_range_atr: float = 0.0
    max_opening_range_atr: float | None = None
    min_relative_volume: float = 0.0
    min_directional_gap: float = -1.0
    max_directional_gap: float = 1.0
    min_premarket_volume: float = 0.0
    min_directional_spy_return: float = -1.0
    min_directional_qqq_return: float = -1.0
    min_directional_sector_strength: float = -1.0
    max_short_gap_abs: float = 1.0
    require_borrow_for_short: bool = True
    enforce_ssr: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "side", side_value(self.side))
        for name in (
            "min_opening_range_atr",
            "min_relative_volume",
            "min_premarket_volume",
            "max_short_gap_abs",
        ):
            object.__setattr__(
                self,
                name,
                non_negative_number(getattr(self, name), name),
            )
        if self.max_opening_range_atr is not None:
            object.__setattr__(
                self,
                "max_opening_range_atr",
                positive_number(self.max_opening_range_atr, "max_opening_range_atr"),
            )
            if self.max_opening_range_atr < self.min_opening_range_atr:
                raise ValueError("max_opening_range_atr must not be below its minimum")
        for name in (
            "min_directional_gap",
            "max_directional_gap",
            "min_directional_spy_return",
            "min_directional_qqq_return",
            "min_directional_sector_strength",
        ):
            object.__setattr__(self, name, finite_number(getattr(self, name), name))
        if self.max_directional_gap < self.min_directional_gap:
            raise ValueError("max_directional_gap must not be below its minimum")
        for name in ("require_borrow_for_short", "enforce_ssr"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class EntryFilterDecision:
    allowed: bool
    reasons: tuple[str, ...]
    opening_range_atr: float


@dataclass(frozen=True, slots=True)
class PortfolioHeatState:
    settled_equity: float
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    open_risk: float = 0.0

    def __post_init__(self) -> None:
        for name in ("settled_equity", "realized_pnl", "unrealized_pnl"):
            object.__setattr__(self, name, finite_number(getattr(self, name), name))
        object.__setattr__(self, "open_risk", non_negative_number(self.open_risk, "open_risk"))

    @property
    def mark_to_market_equity(self) -> float:
        return max(0.0, self.settled_equity + self.realized_pnl + self.unrealized_pnl)


@dataclass(frozen=True, slots=True)
class DayTradeRiskSizingConfig:
    per_trade_risk_fraction: float = 0.0035
    max_portfolio_heat_fraction: float = 0.02
    correlation_penalty: float = 0.50
    max_position_notional_fraction: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "per_trade_risk_fraction",
            "max_portfolio_heat_fraction",
            "max_position_notional_fraction",
        ):
            value = positive_number(getattr(self, name), name)
            if value > 1:
                raise ValueError(f"{name} must not exceed one")
            object.__setattr__(self, name, value)
        penalty = non_negative_number(self.correlation_penalty, "correlation_penalty")
        if penalty > 1:
            raise ValueError("correlation_penalty must not exceed one")
        object.__setattr__(self, "correlation_penalty", penalty)


@dataclass(frozen=True, slots=True)
class IntradayPositionSize:
    quantity: float
    risk_budget: float
    notional: float
    mark_to_market_equity: float
    available_heat: float
