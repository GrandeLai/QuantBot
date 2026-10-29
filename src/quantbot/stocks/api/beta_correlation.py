"""API router for Correlation & Beta Monitor — Phase F.31."""

from __future__ import annotations

from fastapi import APIRouter, Query

from quantbot.stocks.beta_correlation.engine import (
    BetaCorrelationData,
    compute_beta_correlation,
)

router = APIRouter(tags=["beta-correlation"])


@router.get("/beta-correlation", response_model=None)
async def get_beta_correlation(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> BetaCorrelationData:
    """Return beta and correlation data for a given ticker vs SPY and QQQ.

    Always returns HTTP 200. On data failure, data_available=False.
    """
    return compute_beta_correlation(ticker.upper().strip())
