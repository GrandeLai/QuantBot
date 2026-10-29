"""Short interest + short squeeze risk detection engine.

Background:
    Short interest measures bearish conviction. When combined with positive price
    momentum, high short interest creates the "dry kindling" for a short squeeze:
    short sellers must buy-to-cover as the price rises, further fuelling the move.

    Key signals (Cohen et al. 2007, Boehmer et al. 2010):
    - Short Interest % of Float > 20% AND Days-to-Cover > 5 → classic squeeze setup
    - Short Interest increasing MoM despite stable fundamentals → institutional bear thesis
    - Short Interest decreasing fast → short covering rally signal

Data: yfinance ticker.info (updated monthly by FINRA/exchanges, free).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import yfinance as yf
from loguru import logger


SqueezeSignal = Literal["squeeze_setup", "high_short", "moderate", "low_short"]


@dataclass
class ShortInterestData:
    """Short interest metrics and squeeze risk for a single ticker."""

    ticker: str
    short_pct_float: float | None       # fraction of float sold short (0-1)
    short_ratio: float | None           # days to cover (shares_short / avg_daily_vol)
    shares_short: int | None
    shares_short_prior_month: int | None
    short_change_pct: float | None      # MoM change in shares_short
    float_shares: int | None
    avg_daily_volume: int | None
    price_vs_52w_high: float | None     # currentPrice / 52wHigh ∈ (0, 1]
    squeeze_risk_score: float           # 0.0 – 1.0
    signal: SqueezeSignal
    as_of_date: date


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe(val: object, default: float = 0.0) -> float:
    try:
        f = float(val)  # type: ignore[arg-type]
        return f if math.isfinite(f) else default
    except (TypeError, ValueError):
        return default


def _safe_int(val: object) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _compute_squeeze_score(
    short_pct_float: float | None,
    short_ratio: float | None,
    price_vs_52w_high: float | None,
) -> float:
    """Compute squeeze risk score in [0, 1].

    Three components (equal importance):
        1. Short intensity: short_pct_float vs 30% max
        2. Days-to-cover difficulty: short_ratio vs 10 days
        3. Positive momentum: price_vs_52w_high (closer to 52w high = more momentum)
    """
    if short_pct_float is None or short_pct_float == 0.0:
        return 0.0

    # Component 1: short intensity (30% float = max pressure)
    intensity = min(1.0, short_pct_float / 0.30)

    # Component 2: DTC difficulty (10 days = max)
    dtc = min(1.0, _safe(short_ratio) / 10.0) if short_ratio else 0.5

    # Component 3: momentum — price close to 52w high = fuel for squeeze
    if price_vs_52w_high and price_vs_52w_high > 0:
        # Start scoring momentum above 70% of 52w high
        momentum = max(0.0, price_vs_52w_high - 0.70) / 0.30
        momentum = min(1.0, momentum)
    else:
        momentum = 0.0

    score = intensity * 0.40 + dtc * 0.40 + momentum * 0.20
    return round(min(1.0, score), 4)


def _squeeze_signal(
    short_pct_float: float | None,
    squeeze_score: float,
    price_vs_52w_high: float | None,
) -> SqueezeSignal:
    pct = short_pct_float or 0.0
    h = price_vs_52w_high or 0.0

    # Classic squeeze setup: high short + positive momentum
    if squeeze_score >= 0.60 and h >= 0.80:
        return "squeeze_setup"
    if pct >= 0.15:
        return "high_short"
    if pct >= 0.05:
        return "moderate"
    return "low_short"


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_short_interest(ticker: str) -> ShortInterestData | None:
    """Compute short interest metrics and squeeze risk from yfinance.

    Returns None if essential data (short float %) is unavailable.
    """
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        # --- Short interest metrics ---
        short_pct_raw = info.get("shortPercentOfFloat")
        short_pct = _safe(short_pct_raw) if short_pct_raw is not None else None

        short_ratio_raw = info.get("shortRatio")
        short_ratio = _safe(short_ratio_raw) if short_ratio_raw is not None else None

        shares_short = _safe_int(info.get("sharesShort"))
        shares_short_prior = _safe_int(info.get("sharesShortPriorMonth"))
        float_shares = _safe_int(info.get("floatShares"))
        avg_vol = _safe_int(info.get("averageDailyVolume10Day") or info.get("averageVolume10days"))

        # MoM change
        short_change: float | None = None
        if shares_short and shares_short_prior and shares_short_prior > 0:
            short_change = round((shares_short - shares_short_prior) / shares_short_prior, 4)

        # Price vs 52w high
        price = _safe(info.get("currentPrice") or info.get("regularMarketPrice"))
        high_52w = _safe(info.get("fiftyTwoWeekHigh"))
        pvh: float | None = None
        if price > 0 and high_52w > 0:
            pvh = round(price / high_52w, 4)

        if short_pct is None:
            logger.warning(f"[ShortInterest] {ticker}: no shortPercentOfFloat data")
            return None

        score  = _compute_squeeze_score(short_pct, short_ratio, pvh)
        signal = _squeeze_signal(short_pct, score, pvh)

        return ShortInterestData(
            ticker=ticker.upper(),
            short_pct_float=round(short_pct, 4),
            short_ratio=round(short_ratio, 2) if short_ratio else None,
            shares_short=shares_short,
            shares_short_prior_month=shares_short_prior,
            short_change_pct=short_change,
            float_shares=float_shares,
            avg_daily_volume=avg_vol,
            price_vs_52w_high=pvh,
            squeeze_risk_score=score,
            signal=signal,
            as_of_date=date.today(),
        )

    except Exception as exc:
        logger.error(f"[ShortInterest] {ticker}: {exc}")
        return None
