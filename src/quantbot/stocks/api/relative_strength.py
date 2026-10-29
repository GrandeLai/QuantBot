"""Relative Strength Score API — Phase F.33.

GET /api/relative-strength?ticker=<TICKER>
  → Always 200; data_available=False on yfinance failure.
  → 422 if ticker param is missing.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.relative_strength.engine import (
    PeriodRS,
    RSData,
    RSSignal,
    compute_rs,
)

router = APIRouter(tags=["relative-strength"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PeriodRSResponse(BaseModel):
    period: str
    stock_return: float
    spy_return: float
    relative_return: float


class RSDataResponse(BaseModel):
    ticker: str
    periods: list[PeriodRSResponse]
    rs_score: float
    signal: RSSignal
    interpretation: str
    as_of_date: str
    data_available: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_period_resp(p: PeriodRS) -> PeriodRSResponse:
    return PeriodRSResponse(
        period=p.period,
        stock_return=p.stock_return,
        spy_return=p.spy_return,
        relative_return=p.relative_return,
    )


def _to_response(data: RSData) -> RSDataResponse:
    return RSDataResponse(
        ticker=data.ticker,
        periods=[_to_period_resp(p) for p in data.periods],
        rs_score=data.rs_score,
        signal=data.signal,
        interpretation=data.interpretation,
        as_of_date=data.as_of_date,
        data_available=data.data_available,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/relative-strength", response_model=RSDataResponse)
async def get_relative_strength(
    ticker: str = Query(..., description="Stock ticker symbol (e.g. AAPL)"),
) -> RSDataResponse:
    """Return relative strength score for the given ticker vs SPY."""
    data = compute_rs(ticker.upper())
    return _to_response(data)
