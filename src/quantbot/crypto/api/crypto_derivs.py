"""Crypto Derivatives API — funding/OI snapshot + extreme signal analytics.

端点：
  POST /crypto-derivs/snapshot          实时 funding + OI 聚合（Binance + OKX）
  POST /crypto-derivs/funding-stats     funding 历史分布 + 极值信号
  POST /crypto-derivs/etf-flow-stats    ETF flow 历史分布 + 极值信号
"""
from __future__ import annotations

from typing import Any

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantbot.crypto.crypto_derivs import (
    fetch_aggregated_derivs,
    flow_extreme_signal,
    funding_extreme_signal,
    funding_percentile_stats,
)

router = APIRouter(prefix="/crypto-derivs", tags=["加密衍生品"])


# --- /snapshot ---------------------------------------------------------------


class SnapshotRequest(BaseModel):
    asset: str = Field(default="BTC", description="base asset，如 BTC / ETH / SOL")


def _serialize_aggregated(result: dict[str, Any]) -> dict[str, Any]:
    """把 fetch_aggregated_derivs 的输出（含 pydantic 对象）序列化为 dict."""

    def _ser(obj: Any) -> Any:
        if obj is None:
            return None
        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        return obj

    return {
        "asset": result["asset"],
        "timestamp": result["timestamp"].isoformat()
        if hasattr(result["timestamp"], "isoformat")
        else result["timestamp"],
        "funding": {k: _ser(v) for k, v in result["funding"].items()},
        "open_interest": {k: _ser(v) for k, v in result["open_interest"].items()},
        "errors": result["errors"],
    }


@router.post("/snapshot")
async def post_snapshot(req: SnapshotRequest) -> dict[str, Any]:
    asset = req.asset.strip().upper()
    if not asset:
        raise HTTPException(status_code=400, detail="asset must be non-empty")
    try:
        result = await fetch_aggregated_derivs(asset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _serialize_aggregated(result)


# --- /funding-stats ----------------------------------------------------------


class FundingStatsRequest(BaseModel):
    history: list[float]
    current: float | None = None
    z_threshold: float = Field(default=2.0, gt=0.0)


@router.post("/funding-stats")
def post_funding_stats(req: FundingStatsRequest) -> dict[str, Any]:
    try:
        stats = funding_percentile_stats(req.history)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    out: dict[str, Any] = {"stats": stats, "n_samples": len(req.history)}
    if req.current is not None:
        try:
            out["signal"] = funding_extreme_signal(
                req.current, req.history, z_threshold=req.z_threshold
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        out["signal"] = None
    return out


# --- /etf-flow-stats ---------------------------------------------------------


class ETFFlowStatsRequest(BaseModel):
    history: list[float]
    current: float | None = None
    z_threshold: float = Field(default=2.0, gt=0.0)


def _flow_describe(history: list[float]) -> dict[str, float | int]:
    """ETF flow 通用描述统计（不复用 funding_percentile_stats —— ETF flow 有
    时只有少量样本，这里允许 ≥ 10）.
    """
    arr = np.asarray(history, dtype=np.float64)
    if arr.size < 10:
        raise ValueError(f"need at least 10 samples, got {arr.size}")
    return {
        "n_samples": int(arr.size),
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)),
        "p5": float(np.quantile(arr, 0.05)),
        "p25": float(np.quantile(arr, 0.25)),
        "p50": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p95": float(np.quantile(arr, 0.95)),
    }


@router.post("/etf-flow-stats")
def post_etf_flow_stats(req: ETFFlowStatsRequest) -> dict[str, Any]:
    try:
        stats = _flow_describe(req.history)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    out: dict[str, Any] = {"stats": stats}
    if req.current is not None:
        try:
            out["signal"] = flow_extreme_signal(
                req.current, req.history, z_threshold=req.z_threshold
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    else:
        out["signal"] = None
    return out
