"""Seasonality Pattern Analysis engine — Phase F.32.

Computes historical monthly return statistics (avg, median, positive_rate)
using yfinance monthly data (up to 10 years). Never raises; degrades gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import yfinance as yf

SeasonalSignal = Literal[
    "strong_season",
    "positive_season",
    "neutral",
    "negative_season",
    "strong_negative",
    "no_data",
]

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


@dataclass
class MonthStats:
    month: int  # 1-12
    month_name: str
    avg_return: float
    median_return: float
    positive_rate: float  # fraction of months with positive return, 0.0-1.0
    sample_size: int


@dataclass
class SeasonalityData:
    ticker: str
    current_month: int
    current_month_stats: MonthStats | None
    all_months: list[MonthStats] = field(default_factory=list)
    best_month: int | None = None   # month number (1-12) with highest avg_return
    worst_month: int | None = None  # month number (1-12) with lowest avg_return
    signal: SeasonalSignal = "no_data"
    interpretation: str = ""
    as_of_date: str = ""
    data_available: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_monthly_stats(monthly_returns: "dict[int, list[float]]") -> list[MonthStats]:
    """Build MonthStats for each month that has at least one sample.

    Args:
        monthly_returns: mapping month (1-12) → list of pct returns (as decimals, e.g. 0.05).

    Returns:
        List of MonthStats sorted by month number.
    """
    import statistics

    stats: list[MonthStats] = []
    for m in range(1, 13):
        returns = monthly_returns.get(m, [])
        if not returns:
            stats.append(MonthStats(
                month=m,
                month_name=_MONTH_NAMES[m - 1],
                avg_return=0.0,
                median_return=0.0,
                positive_rate=0.0,
                sample_size=0,
            ))
            continue
        avg = sum(returns) / len(returns)
        med = statistics.median(returns)
        pos_rate = sum(1 for r in returns if r > 0) / len(returns)
        stats.append(MonthStats(
            month=m,
            month_name=_MONTH_NAMES[m - 1],
            avg_return=avg,
            median_return=med,
            positive_rate=pos_rate,
            sample_size=len(returns),
        ))
    return stats


def _classify_signal(avg_return: float | None) -> SeasonalSignal:
    """Classify seasonality signal based on current month's historical avg return.

    Thresholds (avg expressed as decimal fraction, e.g. 0.03 = 3%):
        > 0.03  → strong_season
        > 0.01  → positive_season
        > -0.01 → neutral
        > -0.03 → negative_season
        ≤ -0.03 → strong_negative
        None    → no_data
    """
    if avg_return is None:
        return "no_data"
    if avg_return > 0.03:
        return "strong_season"
    if avg_return > 0.01:
        return "positive_season"
    if avg_return > -0.01:
        return "neutral"
    if avg_return > -0.03:
        return "negative_season"
    return "strong_negative"


def _build_interpretation(
    ticker: str,
    current_month: int,
    stats: MonthStats | None,
    signal: SeasonalSignal,
    best_month: int | None,
    worst_month: int | None,
) -> str:
    """Build a human-readable interpretation string."""
    month_name = _MONTH_NAMES[current_month - 1]
    signal_labels: dict[SeasonalSignal, str] = {
        "strong_season": f"{month_name} 历史强季节性（月均收益 >3%），统计上偏多头。",
        "positive_season": f"{month_name} 历史轻度正季节性（月均收益 1-3%），略偏多头。",
        "neutral": f"{month_name} 历史季节性中性（月均收益 -1% ~ 1%），无明显方向偏差。",
        "negative_season": f"{month_name} 历史负季节性（月均收益 -3% ~ -1%），略偏空头。",
        "strong_negative": f"{month_name} 历史强负季节性（月均收益 ≤-3%），统计上偏空头。",
        "no_data": f"{ticker} 历史数据不足，无法计算季节性统计。",
    }
    parts = [signal_labels.get(signal, "")]
    if stats and stats.sample_size > 0:
        parts.append(
            f"基于 {stats.sample_size} 年数据：平均 {stats.avg_return * 100:.1f}%，"
            f"中位 {stats.median_return * 100:.1f}%，正收益率 {stats.positive_rate * 100:.0f}%。"
        )
    if best_month and worst_month:
        parts.append(
            f"历史最强月：{_MONTH_NAMES[best_month - 1]}；历史最弱月：{_MONTH_NAMES[worst_month - 1]}。"
        )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_seasonality(ticker: str, max_years: int = 10) -> SeasonalityData:
    """Compute seasonality statistics for *ticker* using up to *max_years* of monthly data.

    Never raises. Returns SeasonalityData with data_available=False on hard failure.
    """
    current_month = date.today().month
    as_of = date.today().isoformat()

    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period=f"{max_years}y", interval="1mo")
    except Exception:
        return SeasonalityData(
            ticker=ticker,
            current_month=current_month,
            current_month_stats=None,
            all_months=[],
            best_month=None,
            worst_month=None,
            signal="no_data",
            interpretation=f"{ticker} 数据获取失败（yfinance 异常），无法计算季节性。",
            as_of_date=as_of,
            data_available=False,
        )

    # Compute monthly returns
    try:
        if hist.empty or "Close" not in hist.columns:
            raise ValueError("empty history")
        close = hist["Close"].dropna()
        monthly_ret = close.pct_change().dropna()
        if len(monthly_ret) < 12:
            raise ValueError("insufficient data")
    except Exception:
        # Graceful degradation: data available but insufficient
        return SeasonalityData(
            ticker=ticker,
            current_month=current_month,
            current_month_stats=None,
            all_months=[],
            best_month=None,
            worst_month=None,
            signal="no_data",
            interpretation=f"{ticker} 历史月度数据不足 12 个月，无法计算季节性统计。",
            as_of_date=as_of,
            data_available=True,
        )

    # Group returns by month
    monthly_returns: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for ts, ret in monthly_ret.items():
        m = ts.month if hasattr(ts, "month") else ts.to_pydatetime().month
        monthly_returns[m].append(float(ret))

    all_months = _compute_monthly_stats(monthly_returns)

    # Best / worst month (by avg_return, only consider months with data)
    months_with_data = [s for s in all_months if s.sample_size > 0]
    best_month: int | None = None
    worst_month: int | None = None
    if months_with_data:
        best = max(months_with_data, key=lambda s: s.avg_return)
        worst = min(months_with_data, key=lambda s: s.avg_return)
        best_month = best.month
        worst_month = worst.month

    # Current month stats
    current_stats = next((s for s in all_months if s.month == current_month), None)
    if current_stats and current_stats.sample_size == 0:
        current_stats = None

    # Signal
    avg_for_signal = current_stats.avg_return if current_stats else None
    signal = _classify_signal(avg_for_signal)

    interpretation = _build_interpretation(
        ticker, current_month, current_stats, signal, best_month, worst_month
    )

    return SeasonalityData(
        ticker=ticker,
        current_month=current_month,
        current_month_stats=current_stats,
        all_months=all_months,
        best_month=best_month,
        worst_month=worst_month,
        signal=signal,
        interpretation=interpretation,
        as_of_date=as_of,
        data_available=True,
    )
