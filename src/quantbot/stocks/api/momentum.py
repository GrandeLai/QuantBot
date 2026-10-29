"""Momentum API — Jegadeesh-Titman price momentum factor endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.stocks.momentum.engine import MomentumSignal, compute_momentum_signal

router = APIRouter(prefix="/momentum", tags=["momentum"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response model
# ---------------------------------------------------------------------------


class MomentumSignalResponse(BaseModel):
    ticker: str
    momentum_12_1: float
    return_1m: float
    return_3m: float
    return_6m: float
    high_52w: float
    low_52w: float
    current_price: float
    proximity_52w_high: float
    grade: str  # MomentumGrade literal
    interpretation: str
    as_of_date: date


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _to_response(m: MomentumSignal) -> MomentumSignalResponse:
    return MomentumSignalResponse(
        ticker=m.ticker,
        momentum_12_1=m.momentum_12_1,
        return_1m=m.return_1m,
        return_3m=m.return_3m,
        return_6m=m.return_6m,
        high_52w=m.high_52w,
        low_52w=m.low_52w,
        current_price=m.current_price,
        proximity_52w_high=m.proximity_52w_high,
        grade=m.grade,
        interpretation=m.interpretation,
        as_of_date=m.as_of_date,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_model=MomentumSignalResponse)
async def get_momentum_signal(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Compute Jegadeesh-Titman 12-1 month price momentum factor signal.

    Returns price returns over 1m/3m/6m/12-1m windows plus 52-week
    high/low proximity. Grade reflects momentum decile position.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_momentum_signal, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unable to compute momentum signal for {ticker}. "
                "Requires ≥60 trading days of price history."
            ),
        )
    return _to_response(result)
