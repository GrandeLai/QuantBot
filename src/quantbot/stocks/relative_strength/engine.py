"""Relative Strength Score engine — Phase F.33.

Computes stock price performance relative to SPY (S&P 500 ETF) over
1M / 3M / 6M / 12M timeframes, then produces a composite RS score (0-100)
similar in spirit to IBD's RS Rating. Never raises; degrades gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import pandas as pd
import yfinance as yf

RSSignal = Literal[
    "strong_outperformer",
    "outperformer",
    "neutral",
    "underperformer",
    "strong_underperformer",
    "no_data",
]

# Lookback periods in trading days (approximate)
_PERIOD_DAYS: dict[str, int] = {
    "1M": 21,
    "3M": 63,
    "6M": 126,
    "12M": 252,
}

# Weights from most-recent to oldest (1M / 3M / 6M / 12M)
_WEIGHTS: dict[str, float] = {
    "1M": 2.0,
    "3M": 1.5,
    "6M": 1.0,
    "12M": 0.5,
}


@dataclass
class PeriodRS:
    period: str          # "1M", "3M", "6M", "12M"
    stock_return: float  # total return as decimal (e.g. 0.15 = 15%)
    spy_return: float
    relative_return: float  # stock_return - spy_return


@dataclass
class RSData:
    ticker: str
    periods: list[PeriodRS] = field(default_factory=list)
    rs_score: float = 50.0      # 0-100
    signal: RSSignal = "no_data"
    interpretation: str = ""
    as_of_date: str = ""
    data_available: bool = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_period_returns(
    close: pd.Series,  # type: ignore[type-arg]
    n_days: int,
) -> float | None:
    """Return total return over the last *n_days* bars of *close*.

    Returns None if there are fewer than n_days bars.
    """
    if len(close) < n_days + 1:
        return None
    start_price = float(close.iloc[-(n_days + 1)])
    end_price = float(close.iloc[-1])
    if start_price <= 0:
        return None
    return (end_price - start_price) / start_price


def _period_to_rs_score(relative_return: float) -> float:
    """Map a relative return (stock - SPY, as decimal) to a 0-100 score.

    Linear mapping: -50% → 0, 0% → 50, +50% → 100. Clipped at boundaries.
    """
    clipped = max(-0.50, min(0.50, relative_return))
    return (clipped + 0.50) / 1.0 * 100.0


def _classify_signal(rs_score: float | None) -> RSSignal:
    """Classify signal based on composite RS score (0-100)."""
    if rs_score is None:
        return "no_data"
    if rs_score >= 80:
        return "strong_outperformer"
    if rs_score >= 60:
        return "outperformer"
    if rs_score >= 40:
        return "neutral"
    if rs_score >= 20:
        return "underperformer"
    return "strong_underperformer"


def _build_interpretation(
    ticker: str,
    rs_score: float,
    signal: RSSignal,
    periods: list[PeriodRS],
) -> str:
    labels: dict[RSSignal, str] = {
        "strong_outperformer": f"{ticker} RS={rs_score:.0f}：强领跑股，历史上此区间显著跑赢大盘，关注趋势延续机会。",
        "outperformer": f"{ticker} RS={rs_score:.0f}：领跑股，近期多个时间框架跑赢 SPY，适合趋势追踪策略。",
        "neutral": f"{ticker} RS={rs_score:.0f}：中性，与 SPY 表现接近，无明显超额或滞后。",
        "underperformer": f"{ticker} RS={rs_score:.0f}：滞后股，近期多个时间框架落后 SPY，需谨慎或等待企稳。",
        "strong_underperformer": f"{ticker} RS={rs_score:.0f}：强滞后股，历史上此区间显著落后大盘，注意趋势动能缺失风险。",
        "no_data": f"{ticker} 数据不足，无法计算相对强度评分。",
    }
    base = labels.get(signal, "")
    if periods:
        best = max(periods, key=lambda p: p.relative_return)
        worst = min(periods, key=lambda p: p.relative_return)
        if best.relative_return > 0:
            base += f" {best.period} 超额最强（{best.relative_return * 100:+.1f}%）。"
        if worst.relative_return < 0:
            base += f" {worst.period} 相对最弱（{worst.relative_return * 100:+.1f}%）。"
    return base


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_rs(ticker: str) -> RSData:
    """Compute relative strength data for *ticker* vs SPY.

    Never raises. Returns RSData with data_available=False on hard failure.
    """
    as_of = date.today().isoformat()

    try:
        tk_stock = yf.Ticker(ticker)
        tk_spy = yf.Ticker("SPY")
        hist_stock = tk_stock.history(period="2y", interval="1d")
        hist_spy = tk_spy.history(period="2y", interval="1d")
    except Exception:
        return RSData(
            ticker=ticker,
            data_available=False,
            signal="no_data",
            interpretation=f"{ticker} 数据获取失败（yfinance 异常）。",
            as_of_date=as_of,
        )

    # Validate basic shape
    if (
        hist_stock.empty
        or hist_spy.empty
        or "Close" not in hist_stock.columns
        or "Close" not in hist_spy.columns
    ):
        return RSData(
            ticker=ticker,
            data_available=True,
            signal="no_data",
            interpretation=f"{ticker} 历史价格数据为空，无法计算相对强度。",
            as_of_date=as_of,
        )

    close_stock = hist_stock["Close"].dropna()
    close_spy = hist_spy["Close"].dropna()

    # Minimum data check
    if len(close_stock) < 21 or len(close_spy) < 21:
        return RSData(
            ticker=ticker,
            data_available=True,
            signal="no_data",
            interpretation=f"{ticker} 历史数据不足 20 个交易日，无法计算相对强度。",
            as_of_date=as_of,
        )

    # Compute per-period relative returns
    period_list: list[PeriodRS] = []
    for period_label, n_days in _PERIOD_DAYS.items():
        sr = _compute_period_returns(close_stock, n_days)
        br = _compute_period_returns(close_spy, n_days)
        if sr is None or br is None:
            continue
        period_list.append(PeriodRS(
            period=period_label,
            stock_return=sr,
            spy_return=br,
            relative_return=sr - br,
        ))

    if not period_list:
        return RSData(
            ticker=ticker,
            data_available=True,
            periods=[],
            signal="no_data",
            interpretation=f"{ticker} 有效数据不足，无法计算任何时间框架的相对强度。",
            as_of_date=as_of,
        )

    # Composite RS score
    total_weight = 0.0
    weighted_score = 0.0
    for p in period_list:
        w = _WEIGHTS.get(p.period, 1.0)
        score = _period_to_rs_score(p.relative_return)
        weighted_score += score * w
        total_weight += w

    rs_score = weighted_score / total_weight if total_weight > 0 else 50.0
    signal = _classify_signal(rs_score)
    interpretation = _build_interpretation(ticker, rs_score, signal, period_list)

    return RSData(
        ticker=ticker,
        periods=period_list,
        rs_score=round(rs_score, 1),
        signal=signal,
        interpretation=interpretation,
        as_of_date=as_of,
        data_available=True,
    )
