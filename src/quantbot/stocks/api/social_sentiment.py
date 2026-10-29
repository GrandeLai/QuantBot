"""Social Sentiment API — StockTwits retail sentiment & pump risk endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.social_sentiment.engine import (
    SocialSentimentData,
    compute_social_sentiment,
)

router = APIRouter(prefix="/social-sentiment", tags=["social-sentiment"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class SocialSentimentResponse(BaseModel):
    ticker: str
    total_messages: int
    bullish_count: int
    bearish_count: int
    bullish_ratio: float
    sentiment_grade: str  # SentimentGrade literal
    pump_risk_level: str  # PumpRiskLevel literal
    pump_risk_score: float
    interpretation: str
    as_of_date: date
    api_accessible: bool


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_response(d: SocialSentimentData) -> SocialSentimentResponse:
    return SocialSentimentResponse(
        ticker=d.ticker,
        total_messages=d.total_messages,
        bullish_count=d.bullish_count,
        bearish_count=d.bearish_count,
        bullish_ratio=d.bullish_ratio,
        sentiment_grade=d.sentiment_grade,
        pump_risk_level=d.pump_risk_level,
        pump_risk_score=d.pump_risk_score,
        interpretation=d.interpretation,
        as_of_date=d.as_of_date,
        api_accessible=d.api_accessible,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_model=SocialSentimentResponse)
async def get_social_sentiment(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Fetch StockTwits retail sentiment & pump risk for a ticker.

    Always returns HTTP 200.  When the StockTwits API is unreachable the
    response carries api_accessible=False with neutral defaults — the caller
    should surface a graceful-degradation message to the user.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_social_sentiment, ticker)
    return _to_response(result)
