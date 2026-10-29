"""API router for Max Pain Calculator — Phase F.29."""

from __future__ import annotations

from fastapi import APIRouter, Query

from quantbot.stocks.max_pain.engine import MaxPainData, compute_max_pain

router = APIRouter(tags=["max-pain"])


@router.get("/max-pain", response_model=None)
async def get_max_pain(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> MaxPainData:
    """Return options max pain data for a given ticker.

    Always returns HTTP 200. On data failure, data_available=False.
    """
    return compute_max_pain(ticker.upper().strip())
