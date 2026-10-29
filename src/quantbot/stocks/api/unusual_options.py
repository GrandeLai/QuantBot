"""Unusual Options Activity API — volume/OI anomaly scanner endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.unusual_options.engine import (
    UnusualContract,
    UnusualOptionsData,
    compute_unusual_options,
)

router = APIRouter(prefix="/unusual-options", tags=["unusual-options"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class UnusualContractResponse(BaseModel):
    ticker: str
    expiry: str
    strike: float
    option_type: str          # "call" or "put"
    volume: int
    open_interest: int
    volume_oi_ratio: float
    implied_volatility: float
    in_the_money: bool
    is_unusual: bool


class UnusualOptionsResponse(BaseModel):
    ticker: str
    total_unusual_calls: int
    total_unusual_puts: int
    total_call_volume: int
    total_put_volume: int
    put_call_ratio: float
    grade: str                # OptionsGrade literal
    top_unusual: list[UnusualContractResponse]
    interpretation: str
    as_of_date: date
    data_available: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _contract_to_response(c: UnusualContract) -> UnusualContractResponse:
    return UnusualContractResponse(
        ticker=c.ticker,
        expiry=c.expiry,
        strike=c.strike,
        option_type=c.option_type,
        volume=c.volume,
        open_interest=c.open_interest,
        volume_oi_ratio=c.volume_oi_ratio,
        implied_volatility=c.implied_volatility,
        in_the_money=c.in_the_money,
        is_unusual=c.is_unusual,
    )


def _to_response(d: UnusualOptionsData) -> UnusualOptionsResponse:
    return UnusualOptionsResponse(
        ticker=d.ticker,
        total_unusual_calls=d.total_unusual_calls,
        total_unusual_puts=d.total_unusual_puts,
        total_call_volume=d.total_call_volume,
        total_put_volume=d.total_put_volume,
        put_call_ratio=d.put_call_ratio,
        grade=d.grade,
        top_unusual=[_contract_to_response(c) for c in d.top_unusual],
        interpretation=d.interpretation,
        as_of_date=d.as_of_date,
        data_available=d.data_available,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_model=UnusualOptionsResponse)
async def get_unusual_options(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Scan options chains for unusual volume/OI activity.

    Returns contracts where volume exceeds 3× open interest — a signal of
    potential institutional pre-positioning.  Always returns HTTP 200;
    data_available=False when yfinance options data is unavailable.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_unusual_options, ticker)
    return _to_response(result)
