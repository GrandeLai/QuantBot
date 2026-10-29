"""API router for Macro Dashboard — Phase F.27."""

from fastapi import APIRouter
from quantbot.stocks.macro_dashboard.engine import MacroDashboardData, compute_macro_dashboard

router = APIRouter(tags=["macro-dashboard"])


@router.get("/macro-dashboard", response_model=None)
async def get_macro_dashboard() -> MacroDashboardData:
    """获取宏观仪表盘数据（VIX、收益率曲线、DXY、黄金、原油）。始终返回 200。"""
    return compute_macro_dashboard()
