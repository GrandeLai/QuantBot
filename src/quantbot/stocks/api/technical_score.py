"""API router for Technical Momentum Score — Phase F.30."""

from __future__ import annotations

from fastapi import APIRouter, Query

from quantbot.stocks.technical_score.engine import (
    TechnicalScoreData,
    compute_technical_score,
)

router = APIRouter(tags=["technical-score"])


@router.get("/technical-score", response_model=None)
async def get_technical_score(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> TechnicalScoreData:
    """Return composite technical momentum score for a given ticker.

    Always returns HTTP 200. On data failure, data_available=False.
    """
    return compute_technical_score(ticker.upper().strip())
