"""API router for Earnings Quality endpoint.

GET /api/earnings-quality?ticker=AAPL
Always returns HTTP 200; data_available=False on graceful degradation.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.earnings_quality.engine import (
    AccrualQuality,
    EarningsQualityGrade,
    FScoreGrade,
    compute_earnings_quality,
)

router = APIRouter(prefix="/earnings-quality", tags=["earnings-quality"])


class EarningsQualityResponse(BaseModel):
    ticker: str
    f_score: int | None
    f_score_grade: FScoreGrade | None
    f_score_components: dict[str, bool]
    m_score: float | None
    manipulation_risk: bool
    accrual_ratio: float | None
    accrual_quality: AccrualQuality | None
    quality_grade: EarningsQualityGrade
    interpretation: str
    as_of_date: str
    data_available: bool


@router.get("/", response_model=EarningsQualityResponse)
async def get_earnings_quality(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> EarningsQualityResponse:
    """
    Compute earnings quality scores for a given ticker.

    Returns Piotroski F-Score (0-9), Beneish M-Score (manipulation flag),
    and Sloan Accrual ratio. Always HTTP 200; data_available=False if yfinance
    cannot provide annual financial statements.
    """
    data = compute_earnings_quality(ticker)
    return EarningsQualityResponse(
        ticker=data.ticker,
        f_score=data.f_score,
        f_score_grade=data.f_score_grade,
        f_score_components=data.f_score_components,
        m_score=data.m_score,
        manipulation_risk=data.manipulation_risk,
        accrual_ratio=data.accrual_ratio,
        accrual_quality=data.accrual_quality,
        quality_grade=data.quality_grade,
        interpretation=data.interpretation,
        as_of_date=str(data.as_of_date),
        data_available=data.data_available,
    )
