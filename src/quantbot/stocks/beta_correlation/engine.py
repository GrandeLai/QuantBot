"""Correlation & Beta Monitor engine — Phase F.31.

计算股票相对于 S&P 500（SPY）和 NASDAQ（QQQ）的 Beta、相关性、R²
以及特质波动率（idiosyncratic volatility）。

数据来源：yfinance 历史价格（免费）
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 类型
# ---------------------------------------------------------------------------

BetaSignal = Literal[
    "high_beta",      # β ≥ 1.5
    "moderate_beta",  # 1.0 ≤ β < 1.5
    "low_beta",       # 0.5 ≤ β < 1.0
    "defensive",      # β < 0.5
    "no_data",
]

_BETA_HIGH = 1.5
_BETA_MODERATE = 1.0
_BETA_LOW = 0.5

_BENCHMARKS = {"SPY": "^GSPC", "QQQ": "QQQ"}
_MIN_BARS = 30


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class BetaCorrelationData:
    ticker: str
    beta_1y: float | None             # 1年 Beta vs SPY
    beta_63d: float | None            # 63日（约3个月）Beta vs SPY
    corr_spy_1y: float | None         # 1年相关性 vs SPY（-1 to 1）
    corr_qqq_1y: float | None         # 1年相关性 vs QQQ（-1 to 1）
    r_squared_1y: float | None        # corr_spy_1y² = 系统性风险占比（0-1）
    idio_vol_ann: float | None        # 年化特质波动率（小数，如 0.25 = 25%）
    signal: BetaSignal
    interpretation: str
    as_of_date: str
    data_available: bool


# ---------------------------------------------------------------------------
# 内部计算
# ---------------------------------------------------------------------------

def _compute_beta(
    stock_ret: pd.Series,
    bench_ret: pd.Series,
) -> float | None:
    """Beta = Cov(stock, bench) / Var(bench)."""
    if len(stock_ret) < _MIN_BARS or len(bench_ret) < _MIN_BARS:
        return None
    # Align on common index
    aligned = pd.concat([stock_ret, bench_ret], axis=1, join="inner").dropna()
    if len(aligned) < _MIN_BARS:
        return None
    s = aligned.iloc[:, 0]
    b = aligned.iloc[:, 1]
    var_b = float(b.var())
    if var_b == 0 or math.isnan(var_b):
        return None
    cov = float(s.cov(b))
    if math.isnan(cov):
        return None
    return round(cov / var_b, 4)


def _compute_correlation(
    stock_ret: pd.Series,
    bench_ret: pd.Series,
) -> float | None:
    """Pearson correlation between stock and benchmark returns."""
    aligned = pd.concat([stock_ret, bench_ret], axis=1, join="inner").dropna()
    if len(aligned) < _MIN_BARS:
        return None
    corr = float(aligned.iloc[:, 0].corr(aligned.iloc[:, 1]))
    return round(corr, 4) if not math.isnan(corr) else None


def _compute_r_squared(corr: float | None) -> float | None:
    """R² = corr²."""
    if corr is None:
        return None
    return round(corr ** 2, 4)


def _compute_idio_vol(
    stock_ret: pd.Series,
    bench_ret: pd.Series,
    beta: float,
) -> float | None:
    """Idiosyncratic (residual) annualized volatility.

    residual_ret = stock_ret - beta × bench_ret
    idio_vol = std(residual) × sqrt(252)
    """
    aligned = pd.concat([stock_ret, bench_ret], axis=1, join="inner").dropna()
    if len(aligned) < _MIN_BARS:
        return None
    residual = aligned.iloc[:, 0] - beta * aligned.iloc[:, 1]
    vol = float(residual.std()) * math.sqrt(252)
    return round(vol, 6) if not math.isnan(vol) else None


def _classify_signal(beta: float | None) -> BetaSignal:
    if beta is None:
        return "no_data"
    if beta >= _BETA_HIGH:
        return "high_beta"
    if beta >= _BETA_MODERATE:
        return "moderate_beta"
    if beta >= _BETA_LOW:
        return "low_beta"
    return "defensive"


def _build_interpretation(
    ticker: str,
    beta_1y: float | None,
    beta_63d: float | None,
    corr_spy: float | None,
    corr_qqq: float | None,
    r2: float | None,
    idio_vol: float | None,
    signal: BetaSignal,
) -> str:
    parts: list[str] = []

    signal_map = {
        "high_beta": "高 Beta（跟涨跟跌放大）",
        "moderate_beta": "中等 Beta（跟随市场）",
        "low_beta": "低 Beta（市场敏感性低）",
        "defensive": "防御性（与市场相关性弱）",
        "no_data": "数据不足",
    }
    parts.append(signal_map.get(signal, ""))

    if beta_1y is not None:
        delta_str = ""
        if beta_63d is not None:
            diff = beta_63d - beta_1y
            delta_str = f"，近期 {beta_63d:.2f}（{'↑' if diff > 0.1 else '↓' if diff < -0.1 else '≈'}）"
        parts.append(f"Beta(1Y) {beta_1y:.2f}{delta_str}")

    if corr_spy is not None:
        parts.append(f"相关性 SPY {corr_spy:.2f}")

    if r2 is not None:
        parts.append(f"R² {r2:.2f}（系统性风险占比 {r2 * 100:.0f}%）")

    if idio_vol is not None:
        parts.append(f"特质波动率 {idio_vol * 100:.1f}%")

    return "；".join(p for p in parts if p) + "。" if parts else "数据不足。"


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------

def compute_beta_correlation(ticker: str) -> BetaCorrelationData:
    """计算 Beta 与相关性数据。永不 raise。yfinance 失败时 data_available=False。"""
    today_str = date.today().isoformat()

    _default = BetaCorrelationData(
        ticker=ticker,
        beta_1y=None,
        beta_63d=None,
        corr_spy_1y=None,
        corr_qqq_1y=None,
        r_squared_1y=None,
        idio_vol_ann=None,
        signal="no_data",
        interpretation="数据不可用。",
        as_of_date=today_str,
        data_available=False,
    )

    try:
        tk = yf.Ticker(ticker)
        stock_hist = tk.history(period="1y")

        spy_tk = yf.Ticker("SPY")
        spy_hist = spy_tk.history(period="1y")

        qqq_tk = yf.Ticker("QQQ")
        qqq_hist = qqq_tk.history(period="1y")
    except Exception as e:
        logger.warning(f"[BetaCorr] yfinance fetch failed for {ticker}: {e}")
        return _default

    # Check stock data
    if stock_hist is None or stock_hist.empty or "Close" not in stock_hist.columns:
        return BetaCorrelationData(
            ticker=ticker, beta_1y=None, beta_63d=None, corr_spy_1y=None,
            corr_qqq_1y=None, r_squared_1y=None, idio_vol_ann=None,
            signal="no_data", interpretation="无历史价格数据。",
            as_of_date=today_str, data_available=True,
        )

    # Compute returns
    stock_ret = stock_hist["Close"].dropna().pct_change().dropna()
    spy_ret = spy_hist["Close"].dropna().pct_change().dropna() if (spy_hist is not None and not spy_hist.empty and "Close" in spy_hist.columns) else pd.Series(dtype=float)
    qqq_ret = qqq_hist["Close"].dropna().pct_change().dropna() if (qqq_hist is not None and not qqq_hist.empty and "Close" in qqq_hist.columns) else pd.Series(dtype=float)

    if len(stock_ret) < _MIN_BARS:
        return BetaCorrelationData(
            ticker=ticker, beta_1y=None, beta_63d=None, corr_spy_1y=None,
            corr_qqq_1y=None, r_squared_1y=None, idio_vol_ann=None,
            signal="no_data",
            interpretation=f"历史数据不足（{len(stock_ret)} 日，需 ≥ {_MIN_BARS}）。",
            as_of_date=today_str, data_available=True,
        )

    # 1Y Beta + 63-day Beta
    beta_1y = _compute_beta(stock_ret, spy_ret)
    stock_ret_63 = stock_ret.iloc[-63:]
    spy_ret_63 = spy_ret.iloc[-63:] if not spy_ret.empty else pd.Series(dtype=float)
    beta_63d = _compute_beta(stock_ret_63, spy_ret_63)

    # Correlations
    corr_spy = _compute_correlation(stock_ret, spy_ret)
    corr_qqq = _compute_correlation(stock_ret, qqq_ret)

    # R²
    r_squared = _compute_r_squared(corr_spy)

    # Idio vol
    idio_vol: float | None = None
    if beta_1y is not None and not spy_ret.empty:
        idio_vol = _compute_idio_vol(stock_ret, spy_ret, beta_1y)

    signal = _classify_signal(beta_1y)
    interpretation = _build_interpretation(
        ticker, beta_1y, beta_63d, corr_spy, corr_qqq, r_squared, idio_vol, signal
    )

    return BetaCorrelationData(
        ticker=ticker,
        beta_1y=beta_1y,
        beta_63d=beta_63d,
        corr_spy_1y=corr_spy,
        corr_qqq_1y=corr_qqq,
        r_squared_1y=r_squared,
        idio_vol_ann=idio_vol,
        signal=signal,
        interpretation=interpretation,
        as_of_date=today_str,
        data_available=True,
    )
