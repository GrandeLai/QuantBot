"""Analyst Consensus API — Wall Street rating & price target endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.analyst_consensus.engine import (
    AnalystConsensusData,
    compute_analyst_consensus,
)

router = APIRouter(prefix="/analyst-consensus", tags=["analyst-consensus"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class AnalystConsensusResponse(BaseModel):
    ticker: str
    recommendation_mean: float | None     # 1-5 (1=Strong Buy)
    recommendation_key: str | None
    num_analysts: int
    target_mean_price: float | None
    target_high_price: float | None
    target_low_price: float | None
    current_price: float | None
    upside_pct: float | None              # (target_mean / current - 1) × 100
    grade: str                            # AnalystGrade literal
    interpretation: str
    as_of_date: date
    data_available: bool


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_response(d: AnalystConsensusData) -> AnalystConsensusResponse:
    return AnalystConsensusResponse(
        ticker=d.ticker,
        recommendation_mean=d.recommendation_mean,
        recommendation_key=d.recommendation_key,
        num_analysts=d.num_analysts,
        target_mean_price=d.target_mean_price,
        target_high_price=d.target_high_price,
        target_low_price=d.target_low_price,
        current_price=d.current_price,
        upside_pct=d.upside_pct,
        grade=d.grade,
        interpretation=d.interpretation,
        as_of_date=d.as_of_date,
        data_available=d.data_available,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_model=AnalystConsensusResponse)
async def get_analyst_consensus(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Fetch Wall Street analyst consensus rating and price targets.

    Returns the yfinance recommendationMean (1=Strong Buy, 5=Strong Sell),
    number of analyst opinions, and mean/high/low price targets.
    Always returns HTTP 200; data_available=False when no coverage exists.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_analyst_consensus, ticker)
    return _to_response(result)
