"""加密衍生品分析引擎 — Spot-Perp basis / funding 统计 / OI 动量.

纯函数，numpy-only。caller 从 collector 拿到原始数据后调用此层。

参考：
  - cash-and-carry: long spot + short perp，收益 ≈ funding × fundings_per_year
  - funding 极值反向：funding > 95% 分位数 → 多头拥挤 → 短期反向做空
"""
from __future__ import annotations

from typing import Literal

import numpy as np

ContrarianSignal = Literal["contrarian_short", "contrarian_long", "neutral"]
OIDirection = Literal["up", "down", "flat"]


def compute_basis(
    spot_price: float,
    perp_price: float,
    funding_rate: float,
    *,
    fundings_per_year: int = 1095,
) -> dict[str, float]:
    """Spot-Perp basis + cash-and-carry 年化 yield.

    Args:
        spot_price: 现货价（USD）
        perp_price: 永续合约价（USD）
        funding_rate: 当前 funding rate（如 0.0001 = 0.01%/8h）
        fundings_per_year: 每年 funding 次数；默认 1095 = 365×3（8h funding）

    Returns:
        dict: spot_price, perp_price, basis_abs, basis_bps, funding_rate, funding_apr
    """
    if spot_price <= 0.0 or perp_price <= 0.0:
        raise ValueError("prices must be positive")
    basis_abs = perp_price - spot_price
    basis_bps = (basis_abs / spot_price) * 10_000.0
    funding_apr = funding_rate * fundings_per_year
    return {
        "spot_price": spot_price,
        "perp_price": perp_price,
        "basis_abs": basis_abs,
        "basis_bps": basis_bps,
        "funding_rate": funding_rate,
        "funding_apr": funding_apr,
    }


def funding_percentile_stats(history: list[float]) -> dict[str, float]:
    """funding rate 历史分布统计.

    Args:
        history: funding rate 历史序列，至少 30 个样本

    Returns:
        dict: mean, std, p5, p25, p50, p75, p95
    """
    arr = np.asarray(history, dtype=np.float64)
    if arr.size < 30:
        raise ValueError(f"need at least 30 samples, got {arr.size}")
    return {
        "mean": float(np.mean(arr)),
        "std": float(np.std(arr, ddof=1)),
        "p5": float(np.quantile(arr, 0.05)),
        "p25": float(np.quantile(arr, 0.25)),
        "p50": float(np.quantile(arr, 0.50)),
        "p75": float(np.quantile(arr, 0.75)),
        "p95": float(np.quantile(arr, 0.95)),
    }


def funding_extreme_signal(
    current_funding: float,
    history: list[float],
    *,
    z_threshold: float = 2.0,
) -> dict[str, float | str]:
    """funding 极值反向信号.

    Args:
        current_funding: 当前 funding rate
        history: funding 历史序列（≥ 30）
        z_threshold: |z| ≥ 此值触发反向信号（默认 2.0 = 2σ）

    Returns:
        dict: z_score, percentile (0-100), signal
        signal:
          - "contrarian_short": z >= z_threshold（多头拥挤 → 反向做空）
          - "contrarian_long": z <= -z_threshold（空头拥挤 → 反向做多）
          - "neutral": 其它
    """
    if z_threshold <= 0.0:
        raise ValueError(f"z_threshold must be > 0, got {z_threshold}")
    arr = np.asarray(history, dtype=np.float64)
    if arr.size < 30:
        raise ValueError(f"need at least 30 samples, got {arr.size}")
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    z = (current_funding - mu) / sigma if sigma > 1e-12 else 0.0
    # 百分位：当前值小于等于多少比例的历史值
    pct = float(np.mean(arr <= current_funding) * 100.0)
    signal: ContrarianSignal
    if z >= z_threshold:
        signal = "contrarian_short"
    elif z <= -z_threshold:
        signal = "contrarian_long"
    else:
        signal = "neutral"
    return {
        "z_score": z,
        "percentile": pct,
        "signal": signal,
    }


def oi_momentum(current_oi: float, prior_oi: float) -> dict[str, float | str]:
    """Open interest 动量（短期 ↑/↓/平盘）.

    Args:
        current_oi: 当前 OI
        prior_oi: 之前一个观察点（须 > 0）

    Returns:
        dict: current_oi, prior_oi, oi_change_pct, direction
    """
    if prior_oi <= 0.0:
        raise ValueError(f"prior_oi must be > 0, got {prior_oi}")
    if current_oi < 0.0:
        raise ValueError(f"current_oi must be >= 0, got {current_oi}")
    pct = (current_oi - prior_oi) / prior_oi * 100.0
    direction: OIDirection
    if pct > 1.0:
        direction = "up"
    elif pct < -1.0:
        direction = "down"
    else:
        direction = "flat"
    return {
        "current_oi": current_oi,
        "prior_oi": prior_oi,
        "oi_change_pct": pct,
        "direction": direction,
    }
