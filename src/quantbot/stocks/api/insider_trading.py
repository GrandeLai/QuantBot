"""API router for Insider Trading (Form 4) endpoint.

GET /api/insider-trading?ticker=AAPL
Always returns HTTP 200; data_available=False on graceful degradation.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.insider_trading.engine import (
    InsiderSignal,
    compute_insider_trading,
)

router = APIRouter(prefix="/insider-trading", tags=["insider-trading"])


class InsiderTransactionOut(BaseModel):
    insider_name: str
    title: str
    transaction_date: str
    shares: float
    price_per_share: float | None
    transaction_type: str
    is_10b5_plan: bool
    form_url: str


class InsiderTradingResponse(BaseModel):
    ticker: str
    cik: str | None
    signal: InsiderSignal
    cluster_buy_count: int
    cluster_sell_count: int
    net_shares_90d: float
    transactions: list[InsiderTransactionOut]
    interpretation: str
    as_of_date: str
    data_available: bool


@router.get("/", response_model=InsiderTradingResponse)
async def get_insider_trading(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> InsiderTradingResponse:
    """
    Fetch recent SEC Form 4 insider trading data for a ticker.

    Returns cluster buy/sell signal based on distinct insiders in 90-day window.
    Excludes 10b5-1 automatic plan trades. Always HTTP 200.
    """
    data = compute_insider_trading(ticker)
    return InsiderTradingResponse(
        ticker=data.ticker,
        cik=data.cik,
        signal=data.signal,
        cluster_buy_count=data.cluster_buy_count,
        cluster_sell_count=data.cluster_sell_count,
        net_shares_90d=data.net_shares_90d,
        transactions=[
            InsiderTransactionOut(
                insider_name=t.insider_name,
                title=t.title,
                transaction_date=str(t.transaction_date),
                shares=t.shares,
                price_per_share=t.price_per_share,
                transaction_type=t.transaction_type,
                is_10b5_plan=t.is_10b5_plan,
                form_url=t.form_url,
            )
            for t in data.transactions
        ],
        interpretation=data.interpretation,
        as_of_date=str(data.as_of_date),
        data_available=data.data_available,
    )
