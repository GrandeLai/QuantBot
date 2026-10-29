"""Pre-entry-only hypothesis filters and volatility-scaled stops."""

from __future__ import annotations

from datetime import datetime

from .domain import (
    DayTradeHypothesisConfig,
    EntryFilterDecision,
    IntradayFeatureSnapshot,
    aware,
    finite_number,
    positive_number,
    side_value,
)


def evaluate_entry_filters(
    *,
    context: IntradayFeatureSnapshot,
    config: DayTradeHypothesisConfig,
    signal_at: datetime,
    opening_range_width: float,
    proposed_price: float,
) -> EntryFilterDecision:
    if not isinstance(context, IntradayFeatureSnapshot):
        raise TypeError("context must be an IntradayFeatureSnapshot")
    if not isinstance(config, DayTradeHypothesisConfig):
        raise TypeError("config must be a DayTradeHypothesisConfig")
    decision_time = aware(signal_at, "signal_at")
    if context.observed_at > decision_time:
        raise ValueError("entry context cannot contain future observations")
    width = positive_number(opening_range_width, "opening_range_width")
    price = positive_number(proposed_price, "proposed_price")
    range_atr = width / context.atr
    direction = 1.0 if config.side == "long" else -1.0
    reasons = _numeric_filter_reasons(context, config, range_atr, direction)
    if config.side == "short":
        reasons.extend(_short_sale_reasons(context, config, price))
    return EntryFilterDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        opening_range_atr=range_atr,
    )


def risk_scaled_stop(
    *,
    entry_price: float,
    side: str,
    atr: float,
    opening_range_width: float,
    basis: str,
    multiplier: float,
) -> float:
    entry = positive_number(entry_price, "entry_price")
    atr_value = positive_number(atr, "atr")
    range_width = positive_number(opening_range_width, "opening_range_width")
    scale = positive_number(multiplier, "multiplier")
    if basis == "atr":
        distance = atr_value * scale
    elif basis == "opening_range":
        distance = range_width * scale
    else:
        raise ValueError("basis must be atr or opening_range")
    stop = entry - distance if side_value(side) == "long" else entry + distance
    if stop <= 0:
        raise ValueError("risk-scaled stop must remain positive")
    return stop


def _numeric_filter_reasons(context, config, range_atr, direction):
    reasons = []
    if range_atr < config.min_opening_range_atr or (
        config.max_opening_range_atr is not None
        and range_atr > config.max_opening_range_atr
    ):
        reasons.append("opening_range_atr")
    if context.relative_volume < config.min_relative_volume:
        reasons.append("relative_volume")
    directional_gap = direction * context.gap_fraction
    if not config.min_directional_gap <= directional_gap <= config.max_directional_gap:
        reasons.append("directional_gap")
    if context.premarket_volume < config.min_premarket_volume:
        reasons.append("premarket_volume")
    directional_fields = (
        (context.spy_return, config.min_directional_spy_return, "spy_direction"),
        (context.qqq_return, config.min_directional_qqq_return, "qqq_direction"),
        (
            context.sector_relative_strength,
            config.min_directional_sector_strength,
            "sector_relative_strength",
        ),
    )
    reasons.extend(
        name for value, threshold, name in directional_fields if direction * value < threshold
    )
    return reasons


def _short_sale_reasons(context, config, proposed_price):
    reasons = []
    if config.require_borrow_for_short and not context.borrow_available:
        reasons.append("borrow_unavailable")
    if abs(context.gap_fraction) > config.max_short_gap_abs:
        reasons.append("short_gap_risk")
    if config.enforce_ssr and context.ssr_active:
        if context.best_bid is None:
            reasons.append("short_sale_restriction_data_missing")
        elif proposed_price <= context.best_bid:
            reasons.append("short_sale_restriction")
    return reasons

