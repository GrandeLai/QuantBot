"""Price Momentum Signal engine.

Jegadeesh & Titman (1993) documented that stocks with high returns over the
past 6-12 months continue to outperform for the following 3-12 months.

The key signal is the **12-1 month momentum** — the cumulative return from
12 months ago to 1 month ago (skipping the most recent month to avoid
short-term reversal contamination).

Empirical evidence:
  - Long top-decile / short bottom-decile: ~1%/month alpha (JT 1993, 2001)
  - Works across 40+ countries (Rouwenhorst 1998, Asness et al. 2013)
  - Fama-French 5-factor + momentum is the standard academic benchmark

Data source: yfinance ticker.history (free).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import yfinance as yf
from loguru import logger


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

MomentumGrade = Literal[
    "strong_momentum",
    "momentum",
    "neutral",
    "reversal_risk",
    "strong_reversal",
]


@dataclass
class MomentumSignal:
    """Price momentum factor signal for a ticker."""

    ticker: str
    momentum_12_1: float    # 12-1 month return (%)  — primary Jegadeesh-Titman factor
    return_1m: float        # 1-month return (%)
    return_3m: float        # 3-month return (%)
    return_6m: float        # 6-month return (%)
    high_52w: float         # 52-week high price
    low_52w: float          # 52-week low price
    current_price: float
    proximity_52w_high: float   # current / 52w_high  ∈ [0, 1]
    grade: MomentumGrade
    interpretation: str
    as_of_date: date


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _momentum_grade(momentum_12_1_pct: float) -> MomentumGrade:
    """Grade 12-1 month momentum.

    Thresholds from empirical decile analysis (Jegadeesh-Titman style):
      strong_momentum: > +20%  (top 2 deciles)
      momentum:        > +5%   (deciles 7-8)
      neutral:         -5% to +5%
      reversal_risk:   -20% to -5%  (deciles 3-4)
      strong_reversal: < -20%  (bottom 2 deciles)
    """
    if momentum_12_1_pct > 20.0:
        return "strong_momentum"
    if momentum_12_1_pct > 5.0:
        return "momentum"
    if momentum_12_1_pct >= -5.0:
        return "neutral"
    if momentum_12_1_pct >= -20.0:
        return "reversal_risk"
    return "strong_reversal"


def _interpretation(grade: MomentumGrade, mom: float, ret_1m: float) -> str:
    msgs: dict[MomentumGrade, str] = {
        "strong_momentum": (
            f"12-1月动量 {mom:+.1f}%：强动量信号。Jegadeesh & Titman (1993) 研究显示此档股票"
            f"未来 3-12 个月历史上持续跑赢市场 ~1%/月。最近 1 月收益 {ret_1m:+.1f}%。"
        ),
        "momentum": (
            f"12-1月动量 {mom:+.1f}%：动量偏正，中等强度。过去 5-20% 区间的股票具有持续上行倾向。"
        ),
        "neutral": (
            f"12-1月动量 {mom:+.1f}%：动量中性，无显著趋势方向。价格表现接近基准。"
        ),
        "reversal_risk": (
            f"12-1月动量 {mom:+.1f}%：⚠ 近期弱势。低动量股票历史上跑输市场；"
            f"若基本面无催化剂，趋势可能持续。"
        ),
        "strong_reversal": (
            f"12-1月动量 {mom:+.1f}%：⚠⚠ 强反转风险！动量严重负向，属于历史最差分位。"
            f"做空信号明确；长线做多需等待底部确认信号。"
        ),
    }
    return msgs[grade]


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_momentum_signal(ticker: str) -> MomentumSignal | None:
    """Compute Jegadeesh-Titman momentum signal for a ticker.

    Uses ~13 months of daily price history.
    Returns None if insufficient data.  Never raises.
    """
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="13mo")

        if hist is None or hist.empty or len(hist) < 60:
            logger.warning(f"[Momentum] {ticker}: insufficient price history ({len(hist) if hist is not None else 0} bars)")
            return None

        closes = hist["Close"].dropna()
        if len(closes) < 60:
            return None

        current_price = float(closes.iloc[-1])

        def _ret(offset_bars: int) -> float:
            """Return from offset_bars ago to most recent close."""
            if offset_bars >= len(closes):
                return 0.0
            past = float(closes.iloc[-(offset_bars + 1)])
            if past <= 0:
                return 0.0
            return (current_price / past - 1.0) * 100.0

        # Approximate trading days: ~21/month
        # 1 month  ≈ 21  bars
        # 3 months ≈ 63  bars
        # 6 months ≈ 126 bars
        # 12 months≈ 252 bars

        n = len(closes)
        bars_1m  = min(21,  n - 1)
        bars_3m  = min(63,  n - 1)
        bars_6m  = min(126, n - 1)
        bars_12m = min(252, n - 1)

        return_1m  = _ret(bars_1m)
        return_3m  = _ret(bars_3m)
        return_6m  = _ret(bars_6m)

        # 12-1 momentum = return from 12m ago to 1m ago (skip most recent month)
        price_12m_ago = float(closes.iloc[-(bars_12m + 1)]) if bars_12m + 1 <= n else None
        price_1m_ago  = float(closes.iloc[-(bars_1m + 1)])  if bars_1m + 1 <= n else None

        if price_12m_ago is None or price_1m_ago is None or price_12m_ago <= 0:
            momentum_12_1 = return_6m  # fallback to 6m if no 12m data
        else:
            momentum_12_1 = (price_1m_ago / price_12m_ago - 1.0) * 100.0

        # 52-week high / low
        window = closes.iloc[-min(252, n):]
        high_52w = float(window.max())
        low_52w  = float(window.min())
        proximity = current_price / high_52w if high_52w > 0 else 1.0

        grade = _momentum_grade(momentum_12_1)
        interp = _interpretation(grade, momentum_12_1, return_1m)

        return MomentumSignal(
            ticker=ticker.upper(),
            momentum_12_1=round(momentum_12_1, 2),
            return_1m=round(return_1m, 2),
            return_3m=round(return_3m, 2),
            return_6m=round(return_6m, 2),
            high_52w=round(high_52w, 4),
            low_52w=round(low_52w, 4),
            current_price=round(current_price, 4),
            proximity_52w_high=round(proximity, 4),
            grade=grade,
            interpretation=interp,
            as_of_date=date.today(),
        )

    except Exception as exc:
        logger.error(f"[Momentum] {ticker} error: {exc}")
        return None
