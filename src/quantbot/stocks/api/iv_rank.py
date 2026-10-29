"""API router for IV Rank & Volatility Monitor — Phase F.24."""

from fastapi import APIRouter, Query
from quantbot.stocks.iv_rank.engine import IVRankData, compute_iv_rank

router = APIRouter(tags=["iv-rank"])


@router.get("/iv-rank", response_model=None)
async def get_iv_rank(ticker: str = Query(..., description="股票代码，如 AAPL")) -> IVRankData:
    """获取期权 IV Rank、历史波动率与期限结构。始终返回 200；网络失败时 data_available=False。"""
    return compute_iv_rank(ticker.upper())
