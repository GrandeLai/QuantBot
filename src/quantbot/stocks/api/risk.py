"""Risk Engine API — 暴露 quantbot.stocks.risk 引擎为 HTTP 端点.

端点：
  POST /risk/kelly          凯利仓位（binary 或 returns 模式）
  POST /risk/vol-target     波动率目标 + regime 分类
  POST /risk/sharpe-decay   策略 Sharpe 衰减分析
  POST /risk/var            VaR / CVaR 报告
  POST /risk/summary        一站式风控摘要
"""
from __future__ import annotations

from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantbot.stocks.risk import (
    analyze_strategy_decay,
    capped_kelly,
    fractional_kelly,
    kelly_fraction_binary,
    kelly_fraction_from_returns,
    var_summary,
    vol_target_recommendation,
)

router = APIRouter(prefix="/risk", tags=["风险管理"])


# --- /risk/kelly -------------------------------------------------------------


class KellyRequest(BaseModel):
    mode: Literal["binary", "returns"] = "binary"
    win_rate: float | None = None
    payoff_ratio: float | None = None
    returns: list[float] | None = None
    fraction: float = Field(default=0.25, gt=0.0, le=1.0)
    cap: float = Field(default=0.25, gt=0.0, le=1.0)


@router.post("/kelly")
def post_kelly(req: KellyRequest) -> dict[str, Any]:
    try:
        if req.mode == "binary":
            if req.win_rate is None or req.payoff_ratio is None:
                raise HTTPException(
                    status_code=400,
                    detail="mode=binary requires win_rate and payoff_ratio",
                )
            full = kelly_fraction_binary(req.win_rate, req.payoff_ratio)
        else:
            if not req.returns:
                raise HTTPException(
                    status_code=400, detail="mode=returns requires non-empty returns"
                )
            full = kelly_fraction_from_returns(np.asarray(req.returns, dtype=np.float64))
        frac = fractional_kelly(full, req.fraction)
        capped = capped_kelly(frac, req.cap)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "full_kelly": full,
        "fractional_kelly": frac,
        "capped_kelly": capped,
        "mode": req.mode,
        "fraction": req.fraction,
        "cap": req.cap,
    }


# --- /risk/vol-target --------------------------------------------------------


class VolTargetRequest(BaseModel):
    returns: list[float]
    target_vol: float = Field(default=0.15, gt=0.0)


@router.post("/vol-target")
def post_vol_target(req: VolTargetRequest) -> dict[str, Any]:
    try:
        result = vol_target_recommendation(
            np.asarray(req.returns, dtype=np.float64), req.target_vol
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


# --- /risk/sharpe-decay ------------------------------------------------------


class SharpeDecayRequest(BaseModel):
    returns: list[float]
    recent_window: int = Field(default=63, ge=10)
    baseline_window: int = Field(default=252, ge=30)


@router.post("/sharpe-decay")
def post_sharpe_decay(req: SharpeDecayRequest) -> dict[str, Any]:
    try:
        result = analyze_strategy_decay(
            np.asarray(req.returns, dtype=np.float64),
            recent_window=req.recent_window,
            baseline_window=req.baseline_window,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


# --- /risk/var ---------------------------------------------------------------


class VarRequest(BaseModel):
    returns: list[float]
    confidences: list[float] = Field(default_factory=lambda: [0.95, 0.99])
    method: Literal["historical", "parametric", "both"] = "historical"


@router.post("/var")
def post_var(req: VarRequest) -> dict[str, Any]:
    if not req.confidences:
        raise HTTPException(status_code=400, detail="confidences must be non-empty")
    try:
        result = var_summary(
            np.asarray(req.returns, dtype=np.float64),
            confidences=tuple(req.confidences),
            method=req.method,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


# --- /risk/summary -----------------------------------------------------------


class SummaryRequest(BaseModel):
    returns: list[float]
    target_vol: float = Field(default=0.15, gt=0.0)
    recent_window: int = Field(default=63, ge=10)
    baseline_window: int = Field(default=252, ge=30)
    confidences: list[float] = Field(default_factory=lambda: [0.95, 0.99])


@router.post("/summary")
def post_summary(req: SummaryRequest) -> dict[str, Any]:
    arr = np.asarray(req.returns, dtype=np.float64)
    try:
        out: dict[str, Any] = {"n_samples": int(arr.size)}
        out["vol_target"] = vol_target_recommendation(arr, req.target_vol)
        # sharpe-decay 需要 recent + baseline + 10 个样本，不够则退化为 None
        min_decay = req.recent_window + req.baseline_window + 10
        if arr.size >= min_decay:
            out["sharpe_decay"] = analyze_strategy_decay(
                arr,
                recent_window=req.recent_window,
                baseline_window=req.baseline_window,
            )
        else:
            out["sharpe_decay"] = None
        out["var"] = var_summary(
            arr, confidences=tuple(req.confidences), method="historical"
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return out
