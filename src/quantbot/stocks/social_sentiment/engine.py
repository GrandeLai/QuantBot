"""Social Sentiment & Pump Risk engine.

Uses StockTwits public API (no API key required) to measure retail investor
sentiment for US stocks.  Extreme bullish crowding is a counter-trading signal
— when retail mania peaks, institutions typically exit (Baker & Wurgler 2006,
Barber & Odean 2008).

API endpoint (public, free, rate-limited):
    GET https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json
    Returns 30 most recent messages with optional Bullish/Bearish labels.

Graceful degradation: if the API is unavailable, the engine always returns a
SocialSentimentData with api_accessible=False rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import httpx
from loguru import logger


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

SentimentGrade = Literal[
    "very_bullish", "bullish", "neutral", "bearish", "very_bearish"
]

PumpRiskLevel = Literal["high", "elevated", "low"]

_STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
_TIMEOUT = 10.0  # seconds


@dataclass
class SocialSentimentData:
    """StockTwits-derived retail sentiment signal."""

    ticker: str
    total_messages: int
    bullish_count: int
    bearish_count: int
    bullish_ratio: float        # bullish / max(1, bullish + bearish)
    sentiment_grade: SentimentGrade
    pump_risk_level: PumpRiskLevel
    pump_risk_score: float      # ∈ [0, 1]
    interpretation: str
    as_of_date: date
    api_accessible: bool        # False when StockTwits API is unreachable


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _sentiment_grade(bullish_ratio: float) -> SentimentGrade:
    """Grade sentiment from bullish_ratio ∈ [0, 1]."""
    if bullish_ratio > 0.70:
        return "very_bullish"
    if bullish_ratio > 0.55:
        return "bullish"
    if bullish_ratio > 0.45:
        return "neutral"
    if bullish_ratio > 0.30:
        return "bearish"
    return "very_bearish"


def _pump_risk_score(bullish_ratio: float, message_count: int) -> float:
    """Compute pump risk score ∈ [0, 1].

    High score = extreme bullish crowding + high message volume.
    This is a *counter-trading* signal — when retail mania peaks, risk is elevated.

    Formula:
        bullish_extremity = max(0, bullish_ratio - 0.5) × 2   # 0 if <50%, 1 if 100%
        volume_factor     = min(1.0, message_count / 20)        # saturates at 20 msgs
        score             = 0.7 × bullish_extremity + 0.3 × volume_factor
    """
    bullish_extremity = max(0.0, (bullish_ratio - 0.5) * 2.0)
    volume_factor = min(1.0, message_count / 20.0)
    return round(0.7 * bullish_extremity + 0.3 * volume_factor, 4)


def _pump_risk_level(score: float) -> PumpRiskLevel:
    if score > 0.6:
        return "high"
    if score > 0.3:
        return "elevated"
    return "low"


def _interpretation(
    grade: SentimentGrade,
    pump_risk: PumpRiskLevel,
    bullish_ratio: float,
    count: int,
    api_accessible: bool,
) -> str:
    if not api_accessible:
        return "StockTwits API 暂时不可访问，情绪数据不可用。请稍后重试。"

    if pump_risk == "high":
        return (
            f"⚠ 高 Pump 风险！散户极度看多（{bullish_ratio * 100:.0f}% 看涨 / {count} 条消息）。"
            "历史上此类极值往往是庄家出货信号，散户散布后价格均值回归风险大。建议谨慎追高。"
        )
    if pump_risk == "elevated":
        return (
            f"散户情绪偏乐观（{bullish_ratio * 100:.0f}% 看涨）。"
            "中等 Pump 风险，建议关注异常成交量和主力资金动向再决策。"
        )
    if grade == "very_bearish":
        return (
            f"散户极度悲观（仅 {bullish_ratio * 100:.0f}% 看涨）。"
            "极端看空情绪历史上是潜在反弹信号（空头回补），可配合技术面判断入场。"
        )
    return (
        f"散户情绪正常（{bullish_ratio * 100:.0f}% 看涨，{count} 条消息）。"
        "无明显极值信号，情绪面中性。"
    )


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------

def compute_social_sentiment(ticker: str) -> SocialSentimentData:
    """Fetch StockTwits sentiment for a ticker.

    Always returns a SocialSentimentData (never None, never raises).
    api_accessible=False when the API is unreachable.
    """
    ticker = ticker.strip().upper()

    def _degraded(reason: str) -> SocialSentimentData:
        logger.warning(f"[SocialSentiment] {ticker}: {reason}")
        return SocialSentimentData(
            ticker=ticker,
            total_messages=0,
            bullish_count=0,
            bearish_count=0,
            bullish_ratio=0.5,
            sentiment_grade="neutral",
            pump_risk_level="low",
            pump_risk_score=0.0,
            interpretation=_interpretation("neutral", "low", 0.5, 0, False),
            as_of_date=date.today(),
            api_accessible=False,
        )

    try:
        url = _STOCKTWITS_URL.format(ticker=ticker)
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, headers={"Accept": "application/json"})

        if resp.status_code == 429:
            return _degraded("StockTwits rate-limit (429)")
        if resp.status_code != 200:
            return _degraded(f"StockTwits HTTP {resp.status_code}")

        data: dict[str, Any] = resp.json()
        messages: list[dict[str, Any]] = data.get("messages", [])

        total = len(messages)
        bullish = 0
        bearish = 0

        for msg in messages:
            ent = msg.get("entities") or {}
            sent = ent.get("sentiment") or {}
            basic = (sent.get("basic") or "").lower()
            if basic == "bullish":
                bullish += 1
            elif basic == "bearish":
                bearish += 1

        labeled = bullish + bearish
        bullish_ratio = bullish / labeled if labeled > 0 else 0.5

        grade = _sentiment_grade(bullish_ratio)
        score = _pump_risk_score(bullish_ratio, total)
        risk = _pump_risk_level(score)
        interp = _interpretation(grade, risk, bullish_ratio, total, True)

        return SocialSentimentData(
            ticker=ticker,
            total_messages=total,
            bullish_count=bullish,
            bearish_count=bearish,
            bullish_ratio=round(bullish_ratio, 4),
            sentiment_grade=grade,
            pump_risk_level=risk,
            pump_risk_score=score,
            interpretation=interp,
            as_of_date=date.today(),
            api_accessible=True,
        )

    except (httpx.TimeoutException, httpx.ConnectError) as exc:
        return _degraded(f"network error: {exc}")
    except Exception as exc:
        logger.error(f"[SocialSentiment] {ticker} unexpected error: {exc}")
        return _degraded(f"unexpected: {exc}")
