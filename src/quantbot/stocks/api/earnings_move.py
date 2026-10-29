"""Earnings Move API — options-implied pre-earnings expected move endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.earnings_move.engine import (
    EarningsMoveData,
    compute_earnings_move,
)

router = APIRouter(prefix="/earnings-move", tags=["earnings-move"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class EarningsMoveResponse(BaseModel):
    ticker: str
    next_earnings_date: str | None
    days_to_earnings: int | None
    expected_move_pct: float | None       # ±% options-implied move
    atm_strike: float | None
    straddle_price: float | None
    current_price: float | None
    grade: str                            # EarningsMoveGrade literal
    interpretation: str
    as_of_date: date
    data_available: bool


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_response(d: EarningsMoveData) -> EarningsMoveResponse:
    return EarningsMoveResponse(
        ticker=d.ticker,
        next_earnings_date=d.next_earnings_date,
        days_to_earnings=d.days_to_earnings,
        expected_move_pct=d.expected_move_pct,
        atm_strike=d.atm_strike,
        straddle_price=d.straddle_price,
        current_price=d.current_price,
        grade=d.grade,
        interpretation=d.interpretation,
        as_of_date=d.as_of_date,
        data_available=d.data_available,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_model=EarningsMoveResponse)
async def get_earnings_move(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Compute options-implied expected move for the next earnings event.

    Uses the ATM straddle price (call + put) divided by current price to
    estimate the market's implied ±1σ earnings move.  Always returns HTTP 200;
    data_available=False when options or calendar data are unavailable.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_earnings_move, ticker)
    return _to_response(result)
