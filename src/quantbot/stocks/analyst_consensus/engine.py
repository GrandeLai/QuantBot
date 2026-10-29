"""Analyst Consensus & Price Target engine.

Uses yfinance to retrieve Wall Street analyst consensus (recommendationMean
on the 1–5 scale: 1=Strong Buy, 5=Strong Sell) plus price target statistics.

Methodology:
    recommendation_mean: 1-2=strong_buy, 2-3=buy, 3-4=hold, 4-5=sell,
                         with extreme values → strong_buy/strong_sell
    upside_pct = (target_mean_price / current_price - 1) × 100

Graceful degradation: always returns AnalystConsensusData (never None, never
raises). data_available=False when yfinance has no analyst data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import yfinance as yf
from loguru import logger

AnalystGrade = Literal["strong_buy", "buy", "hold", "sell", "strong_sell", "no_coverage"]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AnalystConsensusData:
    """Wall Street analyst consensus for a ticker."""

    ticker: str
    recommendation_mean: float | None    # 1–5 scale (1=Strong Buy)
    recommendation_key: str | None       # "strongBuy" / "buy" / "hold" / "sell"
    num_analysts: int                    # number of analyst opinions
    target_mean_price: float | None      # consensus price target
    target_high_price: float | None
    target_low_price: float | None
    current_price: float | None
    upside_pct: float | None            # (target_mean / current - 1) × 100
    grade: AnalystGrade
    interpretation: str
    as_of_date: date
    data_available: bool


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _analyst_grade(rec_mean: float | None) -> AnalystGrade:
    """Classify analyst grade from recommendationMean (1–5 scale)."""
    if rec_mean is None:
        return "no_coverage"
    if rec_mean <= 1.5:
        return "strong_buy"
    if rec_mean <= 2.5:
        return "buy"
    if rec_mean <= 3.5:
        return "hold"
    if rec_mean <= 4.5:
        return "sell"
    return "strong_sell"


def _interpretation(
    grade: AnalystGrade,
    rec_mean: float | None,
    num_analysts: int,
    upside: float | None,
    data_available: bool,
) -> str:
    if not data_available:
        return "分析师数据不可用（yfinance 无法获取该股票的分析师评级）。"

    if grade == "no_coverage":
        return "该股票暂无分析师覆盖（可能为小盘股或非常规上市公司）。"

    rec_str = f"{rec_mean:.2f}" if rec_mean is not None else "N/A"
    upside_str = f"{upside:+.1f}%" if upside is not None else "N/A"

    grade_labels = {
        "strong_buy": "强烈买入 ✓✓",
        "buy": "买入 ✓",
        "hold": "持有",
        "sell": "卖出 ✗",
        "strong_sell": "强烈卖出 ✗✗",
    }
    label = grade_labels.get(grade, grade)

    base = f"分析师共识：{label}（均值 {rec_str}，{num_analysts} 位分析师覆盖）。"
    if upside is not None:
        if upside > 20:
            return base + f"目标价隐含上行空间 {upside_str}，分析师认为股价被显著低估。"
        if upside > 5:
            return base + f"目标价隐含上行空间 {upside_str}，分析师偏多。"
        if upside < -10:
            return base + f"目标价低于当前价 {upside_str}，分析师认为股价偏高。"
        return base + f"目标价与当前价接近（{upside_str}），空间有限。"
    return base


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


def compute_analyst_consensus(ticker: str) -> AnalystConsensusData:
    """Fetch Wall Street analyst consensus and price target from yfinance.

    Always returns AnalystConsensusData (never None, never raises).
    data_available=False when yfinance has no analyst data.
    """
    ticker = ticker.strip().upper()

    def _degraded(reason: str) -> AnalystConsensusData:
        logger.warning(f"[AnalystConsensus] {ticker}: {reason}")
        return AnalystConsensusData(
            ticker=ticker,
            recommendation_mean=None,
            recommendation_key=None,
            num_analysts=0,
            target_mean_price=None,
            target_high_price=None,
            target_low_price=None,
            current_price=None,
            upside_pct=None,
            grade="no_coverage",
            interpretation=_interpretation("no_coverage", None, 0, None, False),
            as_of_date=date.today(),
            data_available=False,
        )

    try:
        yt = yf.Ticker(ticker)
        info = yt.info or {}

        if not info:
            return _degraded("empty yfinance info")

        # Current price
        current_price: float | None = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("previousClose")
        )
        if current_price:
            current_price = float(current_price)

        # Analyst consensus
        rec_mean_raw = info.get("recommendationMean")
        rec_mean: float | None = float(rec_mean_raw) if rec_mean_raw is not None else None
        rec_key: str | None = info.get("recommendationKey")
        num_analysts: int = int(info.get("numberOfAnalystOpinions") or 0)

        # Price targets
        target_mean_raw = info.get("targetMeanPrice")
        target_high_raw = info.get("targetHighPrice")
        target_low_raw = info.get("targetLowPrice")

        target_mean: float | None = float(target_mean_raw) if target_mean_raw else None
        target_high: float | None = float(target_high_raw) if target_high_raw else None
        target_low: float | None = float(target_low_raw) if target_low_raw else None

        # Upside
        upside: float | None = None
        if target_mean and current_price and current_price > 0:
            upside = round((target_mean / current_price - 1) * 100, 2)

        # No data if neither rec nor price targets available
        if rec_mean is None and target_mean is None and num_analysts == 0:
            return _degraded("no analyst data in yfinance info")

        grade = _analyst_grade(rec_mean)
        interp = _interpretation(grade, rec_mean, num_analysts, upside, True)

        return AnalystConsensusData(
            ticker=ticker,
            recommendation_mean=round(rec_mean, 2) if rec_mean is not None else None,
            recommendation_key=rec_key,
            num_analysts=num_analysts,
            target_mean_price=round(target_mean, 2) if target_mean else None,
            target_high_price=round(target_high, 2) if target_high else None,
            target_low_price=round(target_low, 2) if target_low else None,
            current_price=round(current_price, 2) if current_price else None,
            upside_pct=upside,
            grade=grade,
            interpretation=interp,
            as_of_date=date.today(),
            data_available=True,
        )

    except Exception as exc:
        logger.error(f"[AnalystConsensus] {ticker} unexpected error: {exc}")
        return _degraded(f"unexpected: {exc}")
