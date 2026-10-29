"""API router for Index Rebalance endpoint.

GET /api/index-rebalance?ticker=AAPL
Always returns HTTP 200; data_available=False on graceful degradation.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.index_rebalance.engine import (
    IndexStatus,
    RebalanceRisk,
    compute_index_rebalance,
)

router = APIRouter(prefix="/index-rebalance", tags=["index-rebalance"])


class IndexMembershipOut(BaseModel):
    index_name: str
    status: IndexStatus
    market_cap_rank: int | None
    market_cap_pct: float | None
    rebalance_risk: RebalanceRisk


class IndexRebalanceResponse(BaseModel):
    ticker: str
    market_cap: float | None
    market_cap_b: float | None
    float_shares: float | None
    price: float | None
    eps_ttm: float | None
    indices: list[IndexMembershipOut]
    interpretation: str
    as_of_date: str
    data_available: bool


@router.get("/", response_model=IndexRebalanceResponse)
async def get_index_rebalance(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> IndexRebalanceResponse:
    """
    Check index membership (S&P 500, NASDAQ 100, Russell 2000) and assess
    rebalance addition/deletion risk based on market cap thresholds.
    Always HTTP 200.
    """
    data = compute_index_rebalance(ticker)
    return IndexRebalanceResponse(
        ticker=data.ticker,
        market_cap=data.market_cap,
        market_cap_b=data.market_cap_b,
        float_shares=data.float_shares,
        price=data.price,
        eps_ttm=data.eps_ttm,
        indices=[
            IndexMembershipOut(
                index_name=idx.index_name,
                status=idx.status,
                market_cap_rank=idx.market_cap_rank,
                market_cap_pct=idx.market_cap_pct,
                rebalance_risk=idx.rebalance_risk,
            )
            for idx in data.indices
        ],
        interpretation=data.interpretation,
        as_of_date=str(data.as_of_date),
        data_available=data.data_available,
    )
