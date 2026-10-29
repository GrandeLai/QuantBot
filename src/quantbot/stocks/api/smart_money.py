"""API router for Smart Money Flow endpoint.

GET /api/smart-money?ticker=AAPL
Always returns HTTP 200; data_available=False on graceful degradation.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.stocks.smart_money.engine import (
    SmartMoneySignal,
    compute_smart_money,
)

router = APIRouter(prefix="/smart-money", tags=["smart-money"])


class DailyFlowOut(BaseModel):
    date: str
    large_buy_usd: float
    large_sell_usd: float
    buy_pressure_pct: float
    total_large_usd: float
    large_bar_count: int


class SmartMoneyResponse(BaseModel):
    ticker: str
    signal: SmartMoneySignal
    today_buy_pressure_pct: float | None
    avg_5d_buy_pressure_pct: float | None
    large_threshold_usd: float | None
    daily_flows: list[DailyFlowOut]
    interpretation: str
    as_of_date: str
    data_available: bool


@router.get("/", response_model=SmartMoneyResponse)
async def get_smart_money(
    ticker: str = Query(..., description="Stock ticker symbol, e.g. AAPL"),
) -> SmartMoneyResponse:
    """
    Compute smart money flow signal from 1-minute OHLCV bars.

    Identifies large bars (> 2× median dollar volume) and classifies them
    as buyer- or seller-initiated based on bar direction.
    Always HTTP 200.
    """
    data = compute_smart_money(ticker)
    return SmartMoneyResponse(
        ticker=data.ticker,
        signal=data.signal,
        today_buy_pressure_pct=data.today_buy_pressure_pct,
        avg_5d_buy_pressure_pct=data.avg_5d_buy_pressure_pct,
        large_threshold_usd=data.large_threshold_usd,
        daily_flows=[
            DailyFlowOut(
                date=f.date,
                large_buy_usd=f.large_buy_usd,
                large_sell_usd=f.large_sell_usd,
                buy_pressure_pct=f.buy_pressure_pct,
                total_large_usd=f.total_large_usd,
                large_bar_count=f.large_bar_count,
            )
            for f in data.daily_flows
        ],
        interpretation=data.interpretation,
        as_of_date=str(data.as_of_date),
        data_available=data.data_available,
    )
