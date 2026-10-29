"""Adapters from canonical TOML runtime values to hypothesis configuration."""

from __future__ import annotations

from .domain import DayTradeHypothesisConfig


def hypothesis_config_from_runtime(runtime) -> DayTradeHypothesisConfig:
    required = (
        "side",
        "min_opening_range_atr",
        "max_opening_range_atr",
        "min_relative_volume",
        "min_directional_gap",
        "max_directional_gap",
        "min_premarket_volume",
        "min_directional_spy_return",
        "min_directional_qqq_return",
        "min_directional_sector_strength",
        "max_short_gap_abs",
    )
    missing = tuple(name for name in required if not hasattr(runtime, name))
    if missing:
        raise TypeError(
            "runtime daytrade config lacks fields: " + ", ".join(missing)
        )
    return DayTradeHypothesisConfig(
        side=runtime.side,
        min_opening_range_atr=runtime.min_opening_range_atr,
        max_opening_range_atr=runtime.max_opening_range_atr,
        min_relative_volume=runtime.min_relative_volume,
        min_directional_gap=runtime.min_directional_gap,
        max_directional_gap=runtime.max_directional_gap,
        min_premarket_volume=runtime.min_premarket_volume,
        min_directional_spy_return=runtime.min_directional_spy_return,
        min_directional_qqq_return=runtime.min_directional_qqq_return,
        min_directional_sector_strength=runtime.min_directional_sector_strength,
        max_short_gap_abs=runtime.max_short_gap_abs,
    )


__all__ = ("hypothesis_config_from_runtime",)
