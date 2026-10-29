"""期权分析 API — Greeks 计算 + 隐含波动率 + 敏感度分析.

端点:
  POST /options/greeks        计算期权 Greeks
  POST /options/implied-vol   计算隐含波动率
  POST /options/sensitivity   Greeks 随标的价格变化曲线
  POST /options/scenario      期权价格情景矩阵（S × sigma 网格）
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from quantbot.stocks.options.greeks import BlackScholes

router = APIRouter(prefix="/options", tags=["期权分析"])


# ── 请求模型 ──────────────────────────────────────────────────────────────────

class GreeksRequest(BaseModel):
    S: float                              # 标的现价
    K: float                              # 行权价
    T: float                              # 到期时间（年）
    r: float = 0.05                       # 无风险利率
    sigma: float = 0.20                   # 波动率
    option_type: Literal["call", "put"] = "call"


class ImpliedVolRequest(BaseModel):
    market_price: float
    S: float
    K: float
    T: float
    r: float = 0.05
    option_type: Literal["call", "put"] = "call"


class SensitivityRequest(BaseModel):
    """Greeks 对标的价格的敏感度曲线."""
    K: float
    T: float
    r: float = 0.05
    sigma: float = 0.20
    option_type: Literal["call", "put"] = "call"
    S_center: float                       # 基准标的价格（曲线中心）
    points: int = Field(default=60, ge=10, le=200)  # 数据点数


class ScenarioRequest(BaseModel):
    """S × sigma 情景价格矩阵."""
    K: float
    T: float
    r: float = 0.05
    option_type: Literal["call", "put"] = "call"
    S_center: float
    S_range_pct: float = 0.40             # 标的价格范围 ±40%
    sigma_center: float = 0.20
    sigma_range: float = 0.20            # 波动率范围 ±0.20
    grid_size: int = Field(default=6, ge=3, le=10)


# ── 端点 ──────────────────────────────────────────────────────────────────────

@router.post("/greeks")
def compute_greeks(req: GreeksRequest) -> dict[str, Any]:
    """计算 Black-Scholes 期权 Greeks."""
    if req.T <= 0:
        raise HTTPException(status_code=400, detail="到期时间必须大于 0")
    bs = BlackScholes(
        S=req.S, K=req.K, T=req.T, r=req.r,
        sigma=req.sigma, option_type=req.option_type,
    )
    result = bs.compute()
    intrinsic = max(0.0, req.S - req.K) if req.option_type == "call" else max(0.0, req.K - req.S)
    time_value = max(0.0, result.price - intrinsic)
    return {
        "price":       round(result.price, 4),
        "delta":       round(result.delta, 4),
        "gamma":       round(result.gamma, 6),
        "theta":       round(result.theta, 4),
        "vega":        round(result.vega, 4),
        "rho":         round(result.rho, 4),
        "intrinsic":   round(intrinsic, 4),
        "time_value":  round(time_value, 4),
    }


@router.post("/implied-vol")
def compute_implied_vol(req: ImpliedVolRequest) -> dict[str, float]:
    """从市场价格反推隐含波动率."""
    iv = BlackScholes.implied_vol(
        market_price=req.market_price,
        S=req.S, K=req.K, T=req.T, r=req.r,
        option_type=req.option_type,
    )
    return {
        "implied_vol":     round(iv, 6),
        "implied_vol_pct": round(iv * 100, 4),
    }


@router.post("/sensitivity")
def compute_sensitivity(req: SensitivityRequest) -> dict[str, Any]:
    """计算 Greeks 随标的价格变化的敏感度曲线.

    返回从 S_center * 0.5 到 S_center * 1.5 范围内的 price / delta / gamma /
    theta / vega 数据点，可用于前端绘图。
    """
    if req.T <= 0:
        raise HTTPException(status_code=400, detail="到期时间必须大于 0")

    S_start = req.S_center * 0.5
    S_end   = req.S_center * 1.5
    step    = (S_end - S_start) / req.points
    data: list[dict[str, float]] = []

    for i in range(req.points + 1):
        s = S_start + i * step
        if s <= 0:
            continue
        bs = BlackScholes(
            S=s, K=req.K, T=req.T, r=req.r,
            sigma=req.sigma, option_type=req.option_type,
        )
        g = bs.compute()
        data.append({
            "S":     round(s, 2),
            "price": round(g.price, 4),
            "delta": round(g.delta, 4),
            "gamma": round(g.gamma, 6),
            "theta": round(g.theta, 4),
            "vega":  round(g.vega, 4),
        })

    return {"data": data, "K": req.K, "S_center": req.S_center}


@router.post("/scenario")
def compute_scenario(req: ScenarioRequest) -> dict[str, Any]:
    """计算 S × sigma 情景矩阵，用于期权价格热力图.

    返回 grid_size × grid_size 的价格矩阵，行为 sigma，列为 S。
    """
    import numpy as np

    S_vals = np.linspace(
        req.S_center * (1 - req.S_range_pct),
        req.S_center * (1 + req.S_range_pct),
        req.grid_size,
    ).tolist()
    sigma_vals = np.linspace(
        max(0.01, req.sigma_center - req.sigma_range),
        req.sigma_center + req.sigma_range,
        req.grid_size,
    ).tolist()

    matrix: list[list[float]] = []
    for sig in sigma_vals:
        row: list[float] = []
        for s in S_vals:
            bs = BlackScholes(
                S=s, K=req.K, T=req.T, r=req.r,
                sigma=sig, option_type=req.option_type,
            )
            row.append(round(bs.compute().price, 4))
        matrix.append(row)

    return {
        "S_labels":     [round(v, 2) for v in S_vals],
        "sigma_labels": [round(v * 100, 1) for v in sigma_vals],  # in %
        "matrix":       matrix,
    }
