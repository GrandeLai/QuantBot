"""API router for Dividend Analysis — Phase F.28."""

from __future__ import annotations

from fastapi import APIRouter, Query

from quantbot.stocks.dividend_analysis.engine import (
    DividendAnalysisData,
    compute_dividend_analysis,
)

router = APIRouter(tags=["dividend-analysis"])


@router.get("/dividend-analysis", response_model=None)
async def get_dividend_analysis(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> DividendAnalysisData:
    """Return dividend analysis data for a given ticker.

    Always returns HTTP 200. On data failure, data_available=False.
    """
    return compute_dividend_analysis(ticker.upper().strip())
