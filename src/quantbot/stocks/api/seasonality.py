"""Seasonality Pattern Analysis API — Phase F.32.

GET /api/seasonality?ticker=<TICKER>
  → Always 200; data_available=False on yfinance failure.
  → 422 if ticker param is missing.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.seasonality.engine import (
    MonthStats,
    SeasonalSignal,
    SeasonalityData,
    compute_seasonality,
)

router = APIRouter(tags=["seasonality"])


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class MonthStatsResponse(BaseModel):
    month: int
    month_name: str
    avg_return: float
    median_return: float
    positive_rate: float
    sample_size: int


class SeasonalityResponse(BaseModel):
    ticker: str
    current_month: int
    current_month_stats: MonthStatsResponse | None
    all_months: list[MonthStatsResponse]
    best_month: int | None
    worst_month: int | None
    signal: SeasonalSignal
    interpretation: str
    as_of_date: str
    data_available: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_month_stats_resp(s: MonthStats) -> MonthStatsResponse:
    return MonthStatsResponse(
        month=s.month,
        month_name=s.month_name,
        avg_return=s.avg_return,
        median_return=s.median_return,
        positive_rate=s.positive_rate,
        sample_size=s.sample_size,
    )


def _to_response(data: SeasonalityData) -> SeasonalityResponse:
    return SeasonalityResponse(
        ticker=data.ticker,
        current_month=data.current_month,
        current_month_stats=(
            _to_month_stats_resp(data.current_month_stats)
            if data.current_month_stats is not None
            else None
        ),
        all_months=[_to_month_stats_resp(s) for s in data.all_months],
        best_month=data.best_month,
        worst_month=data.worst_month,
        signal=data.signal,
        interpretation=data.interpretation,
        as_of_date=data.as_of_date,
        data_available=data.data_available,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/seasonality", response_model=SeasonalityResponse)
async def get_seasonality(
    ticker: str = Query(..., description="Stock ticker symbol (e.g. AAPL)"),
) -> SeasonalityResponse:
    """Return seasonality statistics for the given ticker."""
    data = compute_seasonality(ticker.upper())
    return _to_response(data)
