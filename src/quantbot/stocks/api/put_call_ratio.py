"""API router for Put/Call Ratio & Options Sentiment — Phase F.26."""

from fastapi import APIRouter, Query
from quantbot.stocks.put_call_ratio.engine import PCRData, compute_put_call_ratio

router = APIRouter(tags=["put-call-ratio"])


@router.get("/put-call-ratio", response_model=None)
async def get_put_call_ratio(
    ticker: str = Query(..., description="股票代码，如 AAPL"),
) -> PCRData:
    """获取 Put/Call Ratio 与期权情绪分析。始终返回 200；无期权或网络失败均有降级处理。"""
    return compute_put_call_ratio(ticker.upper())
