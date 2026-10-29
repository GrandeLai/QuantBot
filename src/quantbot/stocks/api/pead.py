"""PEAD API — Post-Earnings Announcement Drift signal endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.stocks.pead.engine import (
    EarningsEvent,
    PEADSignal,
    compute_pead_signal,
)

router = APIRouter(prefix="/pead", tags=["pead"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class EarningsEventResponse(BaseModel):
    earnings_date: date
    actual_eps: float
    estimated_eps: float
    surprise_pct: float
    grade: str  # EarningsSurpriseGrade literal


class PEADSignalResponse(BaseModel):
    ticker: str
    last_earnings: EarningsEventResponse
    expected_drift_30d: float
    expected_drift_60d: float
    expected_drift_90d: float
    next_earnings_date: date | None
    interpretation: str
    as_of_date: date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_to_response(e: EarningsEvent) -> EarningsEventResponse:
    return EarningsEventResponse(
        earnings_date=e.earnings_date,
        actual_eps=e.actual_eps,
        estimated_eps=e.estimated_eps,
        surprise_pct=e.surprise_pct,
        grade=e.grade,
    )


def _pead_to_response(p: PEADSignal) -> PEADSignalResponse:
    return PEADSignalResponse(
        ticker=p.ticker,
        last_earnings=_event_to_response(p.last_earnings),
        expected_drift_30d=p.expected_drift_30d,
        expected_drift_60d=p.expected_drift_60d,
        expected_drift_90d=p.expected_drift_90d,
        next_earnings_date=p.next_earnings_date,
        interpretation=p.interpretation,
        as_of_date=p.as_of_date,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/", response_model=PEADSignalResponse)
async def get_pead_signal(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Return Post-Earnings Announcement Drift signal for a ticker.

    Computes EPS surprise (actual vs. estimated) and expected PEAD drift
    based on Bernard & Thomas (1989) academic estimates.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_pead_signal, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unable to compute PEAD signal for {ticker}. "
                "Requires recent earnings data with both actual and estimated EPS."
            ),
        )
    return _pead_to_response(result)
