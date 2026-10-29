"""Quant signals API — Beneish M-Score + Russell rebalancing preview + Sloan Accruals + Piotroski F-Score."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.stocks.quant_signals.engine import (
    BeneishMScore,
    PiotroskiScore,
    RussellMembership,
    SloanAccruals,
    compute_beneish_mscore,
    compute_piotroski_score,
    compute_sloan_accruals,
    estimate_russell_membership,
)

router = APIRouter(prefix="/quant-signals", tags=["quant-signals"])

_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class BeneishMScoreResponse(BaseModel):
    ticker: str
    m_score: float
    risk_level: str  # "safe" | "grey" | "manipulator"
    ratios: dict[str, float]
    interpretation: str
    as_of_date: date


class RussellMembershipResponse(BaseModel):
    ticker: str
    market_cap_usd: float
    estimated_rank: int | None
    current_index: str
    proximity_score: float
    rebalance_signal: str


class SloanAccrualsResponse(BaseModel):
    ticker: str
    accrual_ratio: float
    grade: str  # "low_accrual" | "normal" | "elevated_accrual" | "high_accrual"
    net_income: float
    operating_cash_flow: float
    avg_total_assets: float
    interpretation: str
    as_of_date: date


class PiotroskiCriteriaResponse(BaseModel):
    # Profitability
    roa_positive: bool
    cfo_positive: bool
    roa_improving: bool
    accruals_ok: bool
    # Leverage / Liquidity
    leverage_ok: bool
    liquidity_ok: bool
    no_dilution: bool
    # Operating Efficiency
    margin_ok: bool
    turnover_ok: bool


class PiotroskiScoreResponse(BaseModel):
    ticker: str
    f_score: int   # 0-9
    grade: str     # "strong" | "neutral" | "weak"
    criteria: PiotroskiCriteriaResponse
    interpretation: str
    as_of_date: date


class QuantSignalsSummaryResponse(BaseModel):
    ticker: str
    beneish: BeneishMScoreResponse | None
    russell: RussellMembershipResponse | None
    sloan: SloanAccrualsResponse | None
    piotroski: PiotroskiScoreResponse | None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _beneish_to_response(b: BeneishMScore) -> BeneishMScoreResponse:
    return BeneishMScoreResponse(
        ticker=b.ticker,
        m_score=b.m_score,
        risk_level=b.risk_level,
        ratios=b.ratios,
        interpretation=b.interpretation,
        as_of_date=b.as_of_date,
    )


def _russell_to_response(r: RussellMembership) -> RussellMembershipResponse:
    return RussellMembershipResponse(
        ticker=r.ticker,
        market_cap_usd=r.market_cap_usd,
        estimated_rank=r.estimated_rank,
        current_index=r.current_index,
        proximity_score=r.proximity_score,
        rebalance_signal=r.rebalance_signal,
    )


def _sloan_to_response(s: SloanAccruals) -> SloanAccrualsResponse:
    return SloanAccrualsResponse(
        ticker=s.ticker,
        accrual_ratio=s.accrual_ratio,
        grade=s.grade,
        net_income=s.net_income,
        operating_cash_flow=s.operating_cash_flow,
        avg_total_assets=s.avg_total_assets,
        interpretation=s.interpretation,
        as_of_date=s.as_of_date,
    )


def _piotroski_to_response(p: PiotroskiScore) -> PiotroskiScoreResponse:
    c = p.criteria
    return PiotroskiScoreResponse(
        ticker=p.ticker,
        f_score=p.f_score,
        grade=p.grade,
        criteria=PiotroskiCriteriaResponse(
            roa_positive=c.roa_positive,
            cfo_positive=c.cfo_positive,
            roa_improving=c.roa_improving,
            accruals_ok=c.accruals_ok,
            leverage_ok=c.leverage_ok,
            liquidity_ok=c.liquidity_ok,
            no_dilution=c.no_dilution,
            margin_ok=c.margin_ok,
            turnover_ok=c.turnover_ok,
        ),
        interpretation=p.interpretation,
        as_of_date=p.as_of_date,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/beneish", response_model=BeneishMScoreResponse)
async def get_beneish_mscore(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Compute Beneish M-Score earnings manipulation signal for a ticker."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_beneish_mscore, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unable to compute Beneish M-Score for {ticker}. "
                   "Insufficient financial data (requires ≥2 years of statements).",
        )
    return _beneish_to_response(result)


@router.get("/russell", response_model=RussellMembershipResponse)
async def get_russell_membership(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Estimate Russell index membership and annual rebalancing signal."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, estimate_russell_membership, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unable to estimate Russell membership for {ticker}. "
                   "No market cap data available.",
        )
    return _russell_to_response(result)


@router.get("/sloan", response_model=SloanAccrualsResponse)
async def get_sloan_accruals(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Compute Sloan Accrual Ratio for earnings quality assessment."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_sloan_accruals, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unable to compute Sloan Accruals for {ticker}. "
                   "Requires net income, operating cash flow, and total assets.",
        )
    return _sloan_to_response(result)


@router.get("/piotroski", response_model=PiotroskiScoreResponse)
async def get_piotroski_score(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Compute Piotroski F-Score (0-9) for financial health and earnings quality."""
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_executor, compute_piotroski_score, ticker)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"Unable to compute Piotroski F-Score for {ticker}. "
                   "Requires ≥2 years of income, balance sheet, and cash flow data.",
        )
    return _piotroski_to_response(result)


@router.get("/summary", response_model=QuantSignalsSummaryResponse)
async def get_quant_signals_summary(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> Any:
    """Return Beneish M-Score, Russell membership, Sloan Accruals, and Piotroski F-Score.

    Any component may be None if data is unavailable; the endpoint
    never raises 404 for summary requests.
    """
    ticker = ticker.strip().upper()
    loop = asyncio.get_event_loop()

    beneish_fut    = loop.run_in_executor(_executor, compute_beneish_mscore, ticker)
    russell_fut    = loop.run_in_executor(_executor, estimate_russell_membership, ticker)
    sloan_fut      = loop.run_in_executor(_executor, compute_sloan_accruals, ticker)
    piotroski_fut  = loop.run_in_executor(_executor, compute_piotroski_score, ticker)

    beneish_result, russell_result, sloan_result, piotroski_result = await asyncio.gather(
        beneish_fut, russell_fut, sloan_fut, piotroski_fut, return_exceptions=True
    )

    beneish_resp:   BeneishMScoreResponse | None   = None
    russell_resp:   RussellMembershipResponse | None = None
    sloan_resp:     SloanAccrualsResponse | None   = None
    piotroski_resp: PiotroskiScoreResponse | None  = None

    if isinstance(beneish_result, BeneishMScore):
        beneish_resp = _beneish_to_response(beneish_result)

    if isinstance(russell_result, RussellMembership):
        russell_resp = _russell_to_response(russell_result)

    if isinstance(sloan_result, SloanAccruals):
        sloan_resp = _sloan_to_response(sloan_result)

    if isinstance(piotroski_result, PiotroskiScore):
        piotroski_resp = _piotroski_to_response(piotroski_result)

    return QuantSignalsSummaryResponse(
        ticker=ticker,
        beneish=beneish_resp,
        russell=russell_resp,
        sloan=sloan_resp,
        piotroski=piotroski_resp,
    )
