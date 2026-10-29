"""Sector Momentum API — SPDR sector ETF rotation heatmap endpoint."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from quantbot.stocks.sector_momentum.engine import (
    SectorMomentumData,
    SectorReturn,
    compute_sector_momentum,
)

router = APIRouter(prefix="/sector-momentum", tags=["sector-momentum"])

_executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SectorReturnResponse(BaseModel):
    ticker: str
    sector_name: str
    return_1m: float
    return_3m: float
    return_6m: float
    vs_spy_1m: float
    grade: str          # "leading" / "in_line" / "lagging"


class SectorMomentumResponse(BaseModel):
    sectors: list[SectorReturnResponse]
    top3: list[SectorReturnResponse]
    bottom3: list[SectorReturnResponse]
    spy_return_1m: float
    as_of_date: date
    data_available: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sr_to_response(s: SectorReturn) -> SectorReturnResponse:
    return SectorReturnResponse(
        ticker=s.ticker,
        sector_name=s.sector_name,
        return_1m=s.return_1m,
        return_3m=s.return_3m,
        return_6m=s.return_6m,
        vs_spy_1m=s.vs_spy_1m,
        grade=s.grade,
    )


def _to_response(d: SectorMomentumData) -> SectorMomentumResponse:
    return SectorMomentumResponse(
        sectors=[_sr_to_response(s) for s in d.sectors],
        top3=[_sr_to_response(s) for s in d.top3],
        bottom3=[_sr_to_response(s) for s in d.bottom3],
        spy_return_1m=d.spy_return_1m,
        as_of_date=d.as_of_date,
        data_available=d.data_available,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get("/", response_model=SectorMomentumResponse)
async def get_sector_momentum() -> Any:
    """Fetch 1M/3M/6M returns for all 11 SPDR sector ETFs vs SPY benchmark.

    No ticker parameter required — returns the full sector rotation snapshot.
    Always returns HTTP 200; data_available=False when yfinance is unreachable.
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_sector_momentum)
    return _to_response(result)
