"""DCF + Monte Carlo valuation API endpoints."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.stocks.dcf.engine import (
    DCFResult,
    WACCComponents,
    compute_dcf,
    compute_wacc,
)

router = APIRouter(prefix="/dcf", tags=["dcf"])
_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WACCResponse(BaseModel):
    cost_of_equity: float
    cost_of_debt: float
    tax_rate: float
    debt_weight: float
    equity_weight: float
    wacc: float
    beta: float
    risk_free_rate: float


class DCFResponse(BaseModel):
    ticker: str
    current_price: float
    fair_value_p5: float
    fair_value_p50: float
    fair_value_p95: float
    wacc_components: WACCResponse
    base_fcf: float
    npv_fcf: float
    terminal_value_pv: float
    margin_of_safety: float
    valuation: str
    projected_fcfs: list[float]
    as_of_date: date


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wacc_to_resp(w: WACCComponents) -> WACCResponse:
    return WACCResponse(
        cost_of_equity=w.cost_of_equity,
        cost_of_debt=w.cost_of_debt,
        tax_rate=w.tax_rate,
        debt_weight=w.debt_weight,
        equity_weight=w.equity_weight,
        wacc=w.wacc,
        beta=w.beta,
        risk_free_rate=w.risk_free_rate,
    )


def _dcf_to_resp(d: DCFResult) -> DCFResponse:
    return DCFResponse(
        ticker=d.ticker,
        current_price=d.current_price,
        fair_value_p5=d.fair_value_p5,
        fair_value_p50=d.fair_value_p50,
        fair_value_p95=d.fair_value_p95,
        wacc_components=_wacc_to_resp(d.wacc_components),
        base_fcf=d.base_fcf,
        npv_fcf=d.npv_fcf,
        terminal_value_pv=d.terminal_value_pv,
        margin_of_safety=d.margin_of_safety,
        valuation=d.valuation,
        projected_fcfs=d.projected_fcfs,
        as_of_date=d.as_of_date,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/valuation", response_model=DCFResponse)
async def get_dcf_valuation(
    ticker: str = Query(..., description="Stock ticker, e.g. AAPL"),
    risk_free_rate: float = Query(default=0.045, ge=0.0, le=0.15),
    terminal_growth: float = Query(default=0.025, ge=0.0, le=0.05),
    projection_years: int = Query(default=5, ge=1, le=10),
) -> Any:
    """Run a full DCF + Monte Carlo valuation for a ticker.

    Returns P5/P50/P95 fair value per share, WACC, and margin of safety.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: compute_dcf(
            ticker,
            risk_free_rate=risk_free_rate,
            terminal_growth=terminal_growth,
            projection_years=projection_years,
        ),
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not compute DCF for {ticker}. "
                   "Requires positive FCF history and market cap data.",
        )
    return _dcf_to_resp(result)


@router.get("/wacc", response_model=WACCResponse)
async def get_wacc(
    ticker: str = Query(..., description="Stock ticker, e.g. AAPL"),
    risk_free_rate: float = Query(default=0.045, ge=0.0, le=0.15),
) -> Any:
    """Compute WACC components for a ticker (beta, cost of equity/debt, weights)."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: compute_wacc(ticker, risk_free_rate=risk_free_rate),
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not compute WACC for {ticker}.",
        )
    return _wacc_to_resp(result)
