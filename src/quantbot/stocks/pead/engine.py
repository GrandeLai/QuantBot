"""PEAD — Post-Earnings Announcement Drift engine.

Bernard & Thomas (1989) documented that stocks with large positive earnings
surprises continue to drift upward for 60-90 days after the announcement, and
vice versa for negative surprises.  This is one of the most robust and
persistent market anomalies across 30+ years and 20+ countries.

Drift estimates (Bernard & Thomas 1989, Livnat & Mendenhall 2006):
  large_beat (>+10%): 30d=+3.5%, 60d=+5.2%, 90d=+6.8%
  beat (+2–10%):      30d=+1.8%, 60d=+2.8%, 90d=+3.5%
  inline (-2–+2%):    30d≈0%
  miss (-10– -2%):    30d=−1.8%, 60d=−2.8%, 90d=−3.5%
  large_miss (<-10%): 30d=−3.5%, 60d=−5.2%, 90d=−6.8%

Data source: yfinance `ticker.earnings_dates` (free, no API key required).
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

EarningsSurpriseGrade = Literal[
    "large_beat", "beat", "inline", "miss", "large_miss"
]


@dataclass
class EarningsEvent:
    """A single earnings announcement with surprise measurement."""

    earnings_date: date
    actual_eps: float
    estimated_eps: float
    surprise_pct: float          # (actual - estimate) / |estimate| × 100
    grade: EarningsSurpriseGrade


@dataclass
class PEADSignal:
    """Post-Earnings Announcement Drift signal for a ticker."""

    ticker: str
    last_earnings: EarningsEvent
    # Expected PEAD drift (academic estimates, %)
    expected_drift_30d: float
    expected_drift_60d: float
    expected_drift_90d: float
    next_earnings_date: date | None   # upcoming earnings date if available
    interpretation: str
    as_of_date: date


# ---------------------------------------------------------------------------
# Academic PEAD drift estimates (Bernard & Thomas 1989, Livnat & Mendenhall 2006)
# ---------------------------------------------------------------------------

PEAD_DRIFT: dict[EarningsSurpriseGrade, tuple[float, float, float]] = {
    "large_beat": ( 3.5,  5.2,  6.8),
    "beat":       ( 1.8,  2.8,  3.5),
    "inline":     ( 0.0,  0.0,  0.0),
    "miss":       (-1.8, -2.8, -3.5),
    "large_miss": (-3.5, -5.2, -6.8),
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _surprise_grade(pct: float) -> EarningsSurpriseGrade:
    """Grade an EPS surprise percentage.

    Thresholds (inclusive on lower bound of each tier):
      large_beat: > +10%
      beat:       ≥ +2%  (and ≤ +10%)
      inline:     > −2%  (and < +2%)
      miss:       ≥ −10% (and ≤ −2%)
      large_miss: < −10%
    """
    if pct > 10.0:
        return "large_beat"
    if pct >= 2.0:
        return "beat"
    if pct > -2.0:
        return "inline"
    if pct >= -10.0:
        return "miss"
    return "large_miss"


def _interpretation(grade: EarningsSurpriseGrade, surprise_pct: float, d30: float) -> str:
    msgs: dict[EarningsSurpriseGrade, str] = {
        "large_beat": (
            f"大幅超预期 {surprise_pct:+.1f}%：PEAD 研究显示此类股票未来 30 日平均漂移 {d30:+.1f}%，"
            "90 日持续向上。建议关注价格强势动量。"
        ),
        "beat": (
            f"超预期 {surprise_pct:+.1f}%：PEAD 效应预期 30 日漂移 {d30:+.1f}%。"
            "业绩超预期后的正向漂移通常在公告后 5-10 日内启动。"
        ),
        "inline": (
            f"符合预期（{surprise_pct:+.1f}%）：PEAD 漂移接近零，价格方向将更多由宏观和行业因素决定。"
        ),
        "miss": (
            f"低于预期 {surprise_pct:+.1f}%：PEAD 研究显示此类股票 30 日平均漂移 {d30:+.1f}%。"
            "盈利低预期后的负向漂移风险较高，建议审慎持仓。"
        ),
        "large_miss": (
            f"⚠ 大幅低于预期 {surprise_pct:+.1f}%：PEAD 效应预期 30 日漂移 {d30:+.1f}%，"
            "90 日持续下行风险显著。注意管理回撤。"
        ),
    }
    return msgs[grade]


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_pead_signal(ticker: str) -> PEADSignal | None:
    """Compute PEAD signal for a ticker.

    Returns None if insufficient earnings data.  Never raises.
    """
    try:
        t = yf.Ticker(ticker)
        ed = t.earnings_dates

        if ed is None or ed.empty:
            logger.warning(f"[PEAD] {ticker}: no earnings_dates")
            return None

        # earnings_dates columns may vary; normalise
        ed = ed.copy()
        col_map: dict[str, str] = {}
        for c in ed.columns:
            cl = str(c).lower().strip()
            if "reported" in cl or "actual" in cl:
                col_map[c] = "actual"
            elif "estimate" in cl:
                col_map[c] = "estimate"
            elif "surprise" in cl and "%" in cl:
                col_map[c] = "surprise_pct"
        ed = ed.rename(columns=col_map)

        # Filter to rows where Reported EPS is not NaN
        if "actual" in ed.columns:
            reported_rows = ed[ed["actual"].notna()].copy()
        elif "Reported EPS" in ed.columns:
            reported_rows = ed[ed["Reported EPS"].notna()].copy()
        else:
            logger.warning(f"[PEAD] {ticker}: no 'Reported EPS' column")
            return None

        if reported_rows.empty:
            logger.warning(f"[PEAD] {ticker}: no reported earnings found")
            return None

        # Sort descending by date to get most recent
        reported_rows = reported_rows.sort_index(ascending=False)
        row = reported_rows.iloc[0]

        def _f(row_val: object) -> float | None:
            try:
                v = float(row_val)  # type: ignore[arg-type]
                if math.isfinite(v):
                    return v
            except (TypeError, ValueError):
                pass
            return None

        actual = _f(row.get("actual", row.get("Reported EPS")))
        estimate = _f(row.get("estimate", row.get("EPS Estimate")))
        surprise_raw = _f(row.get("surprise_pct", row.get("Surprise(%)")))

        if actual is None or estimate is None:
            logger.warning(f"[PEAD] {ticker}: actual or estimate is None")
            return None

        # Compute surprise %
        if surprise_raw is not None:
            surprise_pct = float(surprise_raw)
        elif abs(estimate) > 1e-9:
            surprise_pct = (actual - estimate) / abs(estimate) * 100.0
        else:
            surprise_pct = 0.0

        surprise_pct = round(surprise_pct, 2)
        grade = _surprise_grade(surprise_pct)
        d30, d60, d90 = PEAD_DRIFT[grade]

        # earnings_date from the DataFrame index
        try:
            idx = reported_rows.index[0]
            earnings_date = idx.date() if hasattr(idx, "date") else date.today()
        except Exception:
            earnings_date = date.today()

        # Next earnings date — look for rows with NaN actual (upcoming)
        next_earnings: date | None = None
        try:
            reported_col = "actual" if "actual" in ed.columns else "Reported EPS" if "Reported EPS" in ed.columns else None
            if reported_col:
                future_rows = ed[ed[reported_col].isna()].sort_index(ascending=True)
            else:
                future_rows = ed.iloc[0:0]  # empty

            # Filter to dates in the future
            today_ts = __import__("pandas").Timestamp.now()
            future_rows = future_rows[future_rows.index > today_ts]
            if not future_rows.empty:
                next_idx = future_rows.index[0]
                next_earnings = next_idx.date() if hasattr(next_idx, "date") else None
        except Exception:
            pass

        # Also try ticker.calendar for next earnings
        if next_earnings is None:
            try:
                cal = t.calendar
                if cal is not None:
                    ed_list = cal.get("Earnings Date") or []
                    if isinstance(ed_list, list) and ed_list:
                        candidate = ed_list[0]
                        if hasattr(candidate, "date"):
                            next_earnings = candidate.date()
                        else:
                            import pandas as pd
                            ts = pd.Timestamp(candidate)
                            next_earnings = ts.date()
            except Exception:
                pass

        event = EarningsEvent(
            earnings_date=earnings_date,
            actual_eps=round(actual, 4),
            estimated_eps=round(estimate, 4),
            surprise_pct=surprise_pct,
            grade=grade,
        )
        interp = _interpretation(grade, surprise_pct, d30)

        return PEADSignal(
            ticker=ticker.upper(),
            last_earnings=event,
            expected_drift_30d=d30,
            expected_drift_60d=d60,
            expected_drift_90d=d90,
            next_earnings_date=next_earnings,
            interpretation=interp,
            as_of_date=date.today(),
        )

    except Exception as exc:
        logger.error(f"[PEAD] {ticker} error: {exc}")
        return None
