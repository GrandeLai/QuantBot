"""Short interest + squeeze risk API endpoints."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.stocks.short_interest.engine import (
    ShortInterestData,
    compute_short_interest,
)

router = APIRouter(prefix="/short-interest", tags=["short-interest"])
_executor = ThreadPoolExecutor(max_workers=8)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ShortInterestResponse(BaseModel):
    ticker: str
    short_pct_float: float | None
    short_ratio: float | None
    shares_short: int | None
    shares_short_prior_month: int | None
    short_change_pct: float | None
    float_shares: int | None
    avg_daily_volume: int | None
    price_vs_52w_high: float | None
    squeeze_risk_score: float
    signal: str
    as_of_date: date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_resp(d: ShortInterestData) -> ShortInterestResponse:
    return ShortInterestResponse(
        ticker=d.ticker,
        short_pct_float=d.short_pct_float,
        short_ratio=d.short_ratio,
        shares_short=d.shares_short,
        shares_short_prior_month=d.shares_short_prior_month,
        short_change_pct=d.short_change_pct,
        float_shares=d.float_shares,
        avg_daily_volume=d.avg_daily_volume,
        price_vs_52w_high=d.price_vs_52w_high,
        squeeze_risk_score=d.squeeze_risk_score,
        signal=d.signal,
        as_of_date=d.as_of_date,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=ShortInterestResponse)
async def get_short_interest_summary(
    ticker: str = Query(..., description="Stock ticker, e.g. GME"),
) -> Any:
    """Return short interest metrics and squeeze risk score for a ticker."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_short_interest, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No short interest data available for {ticker}.",
        )
    return _to_resp(result)


@router.get("/squeeze-scan", response_model=list[ShortInterestResponse])
async def get_squeeze_scan(
    tickers: str = Query(
        ...,
        description="Comma-separated ticker symbols, e.g. GME,AMC,BBBY",
    ),
) -> Any:
    """Batch short interest scan for multiple tickers.

    Returns results sorted by squeeze_risk_score descending.
    Tickers with no data are silently excluded.
    """
    ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not ticker_list:
        return []
    if len(ticker_list) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 tickers per scan.")

    loop = asyncio.get_event_loop()
    tasks = [
        loop.run_in_executor(_executor, compute_short_interest, t)
        for t in ticker_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    output: list[ShortInterestResponse] = []
    for r in results:
        if isinstance(r, ShortInterestData):
            output.append(_to_resp(r))

    output.sort(key=lambda x: x.squeeze_risk_score, reverse=True)
    return output
