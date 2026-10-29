"""API router for Earnings Calendar & Expected Move — Phase F.25."""

from fastapi import APIRouter, Query
from quantbot.stocks.earnings_calendar.engine import EarningsCalendarData, compute_earnings_calendar

router = APIRouter(tags=["earnings-calendar"])


@router.get("/earnings-calendar", response_model=None)
async def get_earnings_calendar(
    ticker: str = Query(..., description="股票代码，如 AAPL"),
) -> EarningsCalendarData:
    """获取下次财报日期、期权隐含预期波动与历史实际波动对比。始终返回 200。"""
    return compute_earnings_calendar(ticker.upper())
