"""EPS Revision Momentum API — analyst estimate revisions and price targets."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.stocks.eps_revision.engine import (
    AnalystTargets,
    EpsRevisionMomentum,
    EpsRevisionPeriod,
    compute_eps_revision_momentum,
)

router = APIRouter(prefix="/eps-revision", tags=["eps-revision"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class EpsRevisionPeriodResponse(BaseModel):
    period: str
    period_label: str
    up_7d: int
    down_7d: int
    up_30d: int
    down_30d: int
    revision_score_7d: float
    revision_score_30d: float
    direction: str


class AnalystTargetsResponse(BaseModel):
    current_price: float | None
    target_mean: float | None
    target_median: float | None
    target_high: float | None
    target_low: float | None
    upside_pct: float | None


class EpsRevisionMomentumResponse(BaseModel):
    ticker: str
    periods: list[EpsRevisionPeriodResponse]
    targets: AnalystTargetsResponse | None
    overall_direction: str
    as_of_date: date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _period_to_resp(p: EpsRevisionPeriod) -> EpsRevisionPeriodResponse:
    return EpsRevisionPeriodResponse(
        period=p.period,
        period_label=p.period_label,
        up_7d=p.up_7d,
        down_7d=p.down_7d,
        up_30d=p.up_30d,
        down_30d=p.down_30d,
        revision_score_7d=p.revision_score_7d,
        revision_score_30d=p.revision_score_30d,
        direction=p.direction,
    )


def _targets_to_resp(t: AnalystTargets) -> AnalystTargetsResponse:
    return AnalystTargetsResponse(
        current_price=t.current_price,
        target_mean=t.target_mean,
        target_median=t.target_median,
        target_high=t.target_high,
        target_low=t.target_low,
        upside_pct=t.upside_pct,
    )


def _to_response(m: EpsRevisionMomentum) -> EpsRevisionMomentumResponse:
    return EpsRevisionMomentumResponse(
        ticker=m.ticker,
        periods=[_period_to_resp(p) for p in m.periods],
        targets=_targets_to_resp(m.targets) if m.targets else None,
        overall_direction=m.overall_direction,
        as_of_date=m.as_of_date,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/summary", response_model=EpsRevisionMomentumResponse)
async def get_eps_revision_summary(
    ticker: str = Query(..., description="Stock ticker, e.g. AAPL"),
) -> Any:
    """Return EPS revision momentum + analyst price targets for a ticker."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor, compute_eps_revision_momentum, ticker
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"No analyst revision data available for {ticker}.",
        )
    return _to_response(result)


@router.get("/targets", response_model=AnalystTargetsResponse)
async def get_analyst_targets(
    ticker: str = Query(..., description="Stock ticker, e.g. AAPL"),
) -> Any:
    """Return analyst price target consensus for a ticker."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor, compute_eps_revision_momentum, ticker
    )
    if result is None or result.targets is None:
        raise HTTPException(
            status_code=404,
            detail=f"No analyst price target data for {ticker}.",
        )
    return _targets_to_resp(result.targets)
