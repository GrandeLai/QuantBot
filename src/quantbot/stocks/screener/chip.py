"""Chip structure analysis — profit ratio, cost concentration, support levels."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import polars as pl


@dataclass
class ChipAnalysis:
    avg_cost: float         # Volume-weighted average cost
    profit_ratio: float     # Fraction of shares currently profitable (0-1)
    concentration: float    # Chip concentration score (0-1, higher = more concentrated)
    support_levels: list[float]   # Key support price levels
    resistance_levels: list[float]


class ChipAnalyzer:
    """Approximate chip structure from OHLCV data using volume-price distribution."""

    def analyze(self, df: pl.DataFrame, n_bins: int = 20) -> ChipAnalysis:
        if len(df) < 10:
            return ChipAnalysis(0.0, 0.5, 0.5, [], [])

        closes = df["close"].to_numpy()
        volumes = df["volume"].to_numpy()

        avg_cost = float((closes * volumes).sum() / (volumes.sum() + 1e-9))
        current = float(closes[-1])
        profit_ratio = float((closes < current).mean())

        in_band = volumes[abs(closes - avg_cost) / (avg_cost + 1e-9) < 0.05].sum()
        concentration = float(min(in_band / (volumes.sum() + 1e-9) * 3, 1.0))

        support_levels = self._find_levels(closes, volumes, mode="support")
        resistance_levels = self._find_levels(closes, volumes, mode="resistance")

        return ChipAnalysis(
            avg_cost=round(avg_cost, 2),
            profit_ratio=round(profit_ratio, 3),
            concentration=round(concentration, 3),
            support_levels=support_levels,
            resistance_levels=resistance_levels,
        )

    def _find_levels(
        self, closes: np.ndarray, volumes: np.ndarray, mode: str
    ) -> list[float]:
        window = 5
        levels: list[float] = []
        for i in range(window, len(closes) - window):
            segment = closes[i - window : i + window + 1]
            if mode == "support" and closes[i] == segment.min():
                levels.append(float(round(closes[i], 2)))
            elif mode == "resistance" and closes[i] == segment.max():
                levels.append(float(round(closes[i], 2)))
        if levels:
            current = closes[-1]
            levels.sort(key=lambda x: abs(x - current))
            levels = levels[:3]
        return sorted(levels)
