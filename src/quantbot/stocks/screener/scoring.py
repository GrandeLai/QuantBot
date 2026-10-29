"""Composite 100-point stock scoring engine."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import polars as pl


@dataclass
class ScoreBreakdown:
    """Per-factor scores (0-100) and weighted total."""
    trend: float = 0.0
    bias: float = 0.0
    volume: float = 0.0
    support: float = 0.0
    macd: float = 0.0
    rsi: float = 0.0
    total: float = 0.0
    details: dict[str, float] = field(default_factory=dict)


class ScoringEngine:
    """Compute composite 100-point score from OHLCV DataFrame.

    Weights: trend(30%), bias(20%), volume(15%), support(10%), macd(15%), rsi(10%).
    Input DataFrame must have columns: open, high, low, close, volume.
    Minimum 20 rows required; returns zeros for insufficient data.
    """

    WEIGHTS = {"trend": 0.30, "bias": 0.20, "volume": 0.15,
               "support": 0.10, "macd": 0.15, "rsi": 0.10}

    def score(self, df: pl.DataFrame) -> ScoreBreakdown:
        if len(df) < 20:
            return ScoreBreakdown()
        closes = df["close"].to_numpy()
        volumes = df["volume"].to_numpy()
        details: dict[str, float] = {}

        trend = self._score_trend(closes, details)
        bias = self._score_bias(closes, details)
        vol = self._score_volume(volumes, details)
        support = self._score_support(closes, details)
        macd = self._score_macd(closes, details)
        rsi_score = self._score_rsi(closes, details)

        total = (
            trend * self.WEIGHTS["trend"] +
            bias * self.WEIGHTS["bias"] +
            vol * self.WEIGHTS["volume"] +
            support * self.WEIGHTS["support"] +
            macd * self.WEIGHTS["macd"] +
            rsi_score * self.WEIGHTS["rsi"]
        )
        return ScoreBreakdown(
            trend=trend, bias=bias, volume=vol,
            support=support, macd=macd, rsi=rsi_score,
            total=round(total, 2), details=details,
        )

    def _ema(self, arr: np.ndarray, period: int) -> np.ndarray:
        k = 2 / (period + 1)
        result = np.empty(len(arr))
        result[0] = arr[0]
        for i in range(1, len(arr)):
            result[i] = arr[i] * k + result[i - 1] * (1 - k)
        return result

    def _score_trend(self, closes: np.ndarray, details: dict[str, float]) -> float:
        ema5 = self._ema(closes, 5)
        ema20 = self._ema(closes, 20)
        slope = float((ema5[-1] - ema5[-5]) / (ema5[-5] + 1e-9))
        above = 1.0 if closes[-1] > ema20[-1] else 0.0
        details["ema_slope"] = slope
        details["above_ema20"] = above
        slope_score = min(max(slope * 500 + 50, 0), 100)
        return float(slope_score * 0.6 + above * 40)

    def _score_bias(self, closes: np.ndarray, details: dict[str, float]) -> float:
        mean20 = closes[-20:].mean()
        bias = (closes[-1] - mean20) / (mean20 + 1e-9)
        details["bias"] = bias
        if bias < -0.15 or bias > 0.20:
            return 10.0
        score = 100 - abs(bias - 0.03) * 600
        return float(min(max(score, 0), 100))

    def _score_volume(self, volumes: np.ndarray, details: dict[str, float]) -> float:
        avg20 = volumes[-20:].mean()
        ratio = volumes[-1] / (avg20 + 1e-9)
        details["volume_ratio"] = ratio
        if ratio < 0.5:
            return 20.0
        if ratio > 5.0:
            return 50.0
        return float(min(ratio / 3.0 * 100, 100))

    def _score_support(self, closes: np.ndarray, details: dict[str, float]) -> float:
        period = min(60, len(closes))
        window = closes[-period:]
        support = window.min()
        resistance = window.max()
        rng = resistance - support
        if rng < 1e-9:
            return 50.0
        pos = (closes[-1] - support) / rng
        details["support_position"] = pos
        return float(min(max(100 - abs(pos - 0.55) * 180, 0), 100))

    def _score_macd(self, closes: np.ndarray, details: dict[str, float]) -> float:
        ema12 = self._ema(closes, 12)
        ema26 = self._ema(closes, 26)
        dif = ema12 - ema26
        dea = self._ema(dif, 9)
        hist = float(dif[-1] - dea[-1])
        prev_hist = float(dif[-2] - dea[-2]) if len(dif) >= 2 else hist
        details["macd_hist"] = hist
        details["macd_cross"] = float(hist > 0 and prev_hist <= 0)
        hist_pct = hist / (float(closes[-1]) + 1e-9) * 100
        if hist_pct > 0:
            return float(min(50 + hist_pct * 20, 100))
        return float(max(50 + hist_pct * 20, 0))

    def _score_rsi(self, closes: np.ndarray, details: dict[str, float]) -> float:
        period = 14
        if len(closes) < period + 1:
            return 50.0
        deltas = np.diff(closes[-(period + 1):])
        gains = np.where(deltas > 0, deltas, 0).mean()
        losses = np.where(deltas < 0, -deltas, 0).mean()
        rs = gains / (losses + 1e-9)
        rsi = float(100 - 100 / (1 + rs))
        details["rsi"] = rsi
        score = float(100 - abs(rsi - 55) * 2)
        return float(min(max(score, 0), 100))
