"""EPS Revision Momentum engine.

Analyst estimate revisions are one of the most durable alpha signals in
academic literature (Stickel 1991, Chan et al. 1996):
  - Stocks with the most upward estimate revisions outperform by 6-12% annually.
  - 7-day revision momentum captures fast-moving analyst consensus shifts.
  - 30-day captures medium-term trend changes.

Data source: yfinance `eps_revisions` (free, no API key required).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import yfinance as yf
from loguru import logger


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

RevisionDirection = Literal[
    "strong_upgrade", "upgrade", "neutral", "downgrade", "strong_downgrade"
]

PERIOD_LABELS: dict[str, str] = {
    "0q": "本季度",
    "+1q": "下季度",
    "0y": "本年度",
    "+1y": "明年度",
}


@dataclass
class EpsRevisionPeriod:
    """Revision stats for one forecast period."""

    period: str        # "0q" | "+1q" | "0y" | "+1y"
    period_label: str  # 本季度 | 下季度 | 本年度 | 明年度
    up_7d: int
    down_7d: int
    up_30d: int
    down_30d: int
    revision_score_7d: float   # (up - down) / max(1, up + down) ∈ [-1, 1]
    revision_score_30d: float
    direction: RevisionDirection  # dominated by 30d signal


@dataclass
class AnalystTargets:
    """Analyst price target consensus."""

    current_price: float | None
    target_mean: float | None
    target_median: float | None
    target_high: float | None
    target_low: float | None
    upside_pct: float | None  # (mean - current) / current


@dataclass
class EpsRevisionMomentum:
    """Complete EPS revision momentum signal for a ticker."""

    ticker: str
    periods: list[EpsRevisionPeriod]
    targets: AnalystTargets | None
    overall_direction: RevisionDirection
    as_of_date: date


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _revision_score(up: int, down: int) -> float:
    """Compute revision score ∈ [-1, 1]."""
    total = max(1, up + down)
    return (up - down) / total


def _revision_direction(score: float) -> RevisionDirection:
    """Map score to human-readable direction."""
    if score > 0.5:
        return "strong_upgrade"
    if score > 0.1:
        return "upgrade"
    if score > -0.1:
        return "neutral"
    if score > -0.5:
        return "downgrade"
    return "strong_downgrade"


def _overall_direction(periods: list[EpsRevisionPeriod]) -> RevisionDirection:
    """Aggregate across all periods (weight 30d scores equally)."""
    if not periods:
        return "neutral"
    avg_score = sum(p.revision_score_30d for p in periods) / len(periods)
    return _revision_direction(avg_score)


def _safe_float(val: object, default: float | None = None) -> float | None:
    """Safely convert a value to float, returning default on failure."""
    if val is None:
        return default
    try:
        f = float(val)  # type: ignore[arg-type]
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: object, default: int = 0) -> int:
    """Safely convert a value to int."""
    if val is None:
        return default
    try:
        return int(float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_eps_revision_momentum(ticker: str) -> EpsRevisionMomentum | None:
    """Compute EPS revision momentum from yfinance analyst data.

    Returns None if no revision data is available.
    """
    try:
        t = yf.Ticker(ticker)

        # --- EPS revisions ---
        revisions_df = t.eps_revisions

        periods: list[EpsRevisionPeriod] = []

        if revisions_df is not None and not revisions_df.empty:
            # yfinance returns DataFrame with revision counts as index rows
            # and period codes as columns (0q, +1q, 0y, +1y).
            # Row index names vary by version; we look for up/down pattern.
            idx_lower = {str(i).lower(): i for i in revisions_df.index}

            def _get_row(patterns: list[str]) -> dict[str, int]:
                """Find a row matching any of the lowercase patterns."""
                for pat in patterns:
                    for lower_key, orig_key in idx_lower.items():
                        if pat in lower_key:
                            row = revisions_df.loc[orig_key]
                            return {str(col): _safe_int(val) for col, val in row.items()}
                return {}

            up_7d_row   = _get_row(["uplast7", "up_7", "up7"])
            down_7d_row = _get_row(["downlast7", "down_7", "down7"])
            up_30d_row  = _get_row(["uplast30", "up_30", "up30"])
            down_30d_row= _get_row(["downlast30", "down_30", "down30"])

            known_periods = ["0q", "+1q", "0y", "+1y"]
            # Also accept whatever columns appear in the DataFrame
            all_periods = [str(c) for c in revisions_df.columns] or known_periods

            for p in all_periods:
                u7  = up_7d_row.get(p, 0)
                d7  = down_7d_row.get(p, 0)
                u30 = up_30d_row.get(p, 0)
                d30 = down_30d_row.get(p, 0)
                s7  = _revision_score(u7, d7)
                s30 = _revision_score(u30, d30)
                periods.append(
                    EpsRevisionPeriod(
                        period=p,
                        period_label=PERIOD_LABELS.get(p, p),
                        up_7d=u7,
                        down_7d=d7,
                        up_30d=u30,
                        down_30d=d30,
                        revision_score_7d=round(s7, 4),
                        revision_score_30d=round(s30, 4),
                        direction=_revision_direction(s30),
                    )
                )

        if not periods:
            logger.warning(f"[EPS Revision] {ticker}: no revision data")
            return None

        # --- Analyst price targets ---
        targets: AnalystTargets | None = None
        try:
            info = t.info or {}
            current = _safe_float(info.get("currentPrice") or info.get("regularMarketPrice"))
            mean    = _safe_float(info.get("targetMeanPrice"))
            median  = _safe_float(info.get("targetMedianPrice"))
            high    = _safe_float(info.get("targetHighPrice"))
            low     = _safe_float(info.get("targetLowPrice"))
            upside: float | None = None
            if current and mean and current > 0:
                upside = round((mean - current) / current, 4)
            targets = AnalystTargets(
                current_price=current,
                target_mean=mean,
                target_median=median,
                target_high=high,
                target_low=low,
                upside_pct=upside,
            )
        except Exception as e:
            logger.warning(f"[EPS Revision] {ticker}: could not fetch targets: {e}")

        overall = _overall_direction(periods)

        return EpsRevisionMomentum(
            ticker=ticker.upper(),
            periods=periods,
            targets=targets,
            overall_direction=overall,
            as_of_date=date.today(),
        )

    except Exception as exc:
        logger.error(f"[EPS Revision] {ticker} error: {exc}")
        return None
