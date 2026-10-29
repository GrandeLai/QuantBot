"""市场状态洞察 API.

端点:
  POST /insights/regime      识别价格序列的市场状态
  POST /insights/correlate   分析 P&L 与市场状态的关联
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from quantbot.stocks.insights.analyzer import InsightAnalyzer
from quantbot.stocks.insights.regime import RegimeTagger

router = APIRouter(prefix="/insights", tags=["市场洞察"])

_tagger = RegimeTagger()
_analyzer = InsightAnalyzer()


class RegimeRequest(BaseModel):
    prices: list[float]
    fast_window: int = 50
    slow_window: int = 200


class CorrelateRequest(BaseModel):
    prices: list[float]
    pnl_series: list[float]


@router.post("/regime")
def get_regime(req: RegimeRequest) -> dict[str, Any]:
    """识别当前市场状态."""
    tagger = RegimeTagger(fast_window=req.fast_window, slow_window=req.slow_window)
    regime = tagger.tag(req.prices)
    series = tagger.tag_series(req.prices)
    regime_counts = {"bull": 0, "bear": 0, "sideways": 0}
    for r in series:
        regime_counts[r.value] += 1
    return {
        "current_regime": regime.value,
        "regime_counts": regime_counts,
        "total_bars": len(req.prices),
    }


@router.post("/correlate")
def correlate_regime_pnl(req: CorrelateRequest) -> dict[str, Any]:
    """分析 P&L 在不同市场状态下的表现."""
    analyzer = InsightAnalyzer()
    result = analyzer.correlate(req.prices, req.pnl_series)
    return {"correlation": result}
