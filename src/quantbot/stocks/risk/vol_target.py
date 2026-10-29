"""Volatility targeting：根据 realized vol 缩放仓位.

参考：Moreira & Muir (2017) "Volatility-Managed Portfolios"；
长期回测（1926-2025 SP500）显示 vol-targeted 组合 Sharpe +0.2~0.4 / max DD −30~50%。

核心思想：当 realized vol 高于 target 时减仓，反之加仓。
"""
from __future__ import annotations

from typing import Literal

import numpy as np

Regime = Literal["low", "normal", "high", "crisis"]


def realized_volatility(
    returns: np.ndarray,
    *,
    annualize: bool = True,
    periods_per_year: int = 252,
) -> float:
    """计算 realized volatility（年化或原始）.

    Args:
        returns: simple return 序列，长度 ≥ 2
        annualize: 是否年化（√periods_per_year）
        periods_per_year: 252 daily / 52 weekly / 12 monthly

    Returns:
        std dev（必要时年化）
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.size < 2:
        raise ValueError(f"need at least 2 samples, got {arr.size}")
    sigma = float(np.std(arr, ddof=1))
    if annualize:
        sigma *= float(np.sqrt(periods_per_year))
    return sigma


def vol_target_position_size(
    realized_vol: float,
    target_vol: float = 0.15,
) -> float:
    """根据 realized / target vol 比值给仓位缩放因子.

    Args:
        realized_vol: 当前实际波动率（年化），> 0
        target_vol: 目标波动率（年化），默认 15%

    Returns:
        scale factor ∈ (0, 1]：当 realized > target 时减仓，否则上限 1.0
        （避免在低波时盲目加杠杆——本函数不输出 > 1 的杠杆建议）
    """
    if realized_vol <= 0.0:
        raise ValueError(f"realized_vol must be > 0, got {realized_vol}")
    if target_vol <= 0.0:
        raise ValueError(f"target_vol must be > 0, got {target_vol}")
    return min(target_vol / realized_vol, 1.0)


def regime_classify(
    realized_vol: float,
    *,
    low_threshold: float = 0.10,
    high_threshold: float = 0.25,
) -> Regime:
    """根据 realized vol 分类市场 regime（年化口径）.

    阈值按 SP500 历史经验：
      - < 10%：low（牛市后半段、震荡走平）
      - 10-25%：normal
      - 25-50%：high（趋势市、宏观冲击）
      - ≥ 50%：crisis（2008、2020.3、2022.Q3 级别）

    Args:
        realized_vol: 年化 realized vol（必须 ≥ 0）
        low_threshold: low/normal 分界
        high_threshold: normal/high 分界（high/crisis 自动取 2× high_threshold）

    Returns:
        "low" / "normal" / "high" / "crisis"
    """
    if realized_vol < 0.0:
        raise ValueError(f"realized_vol must be >= 0, got {realized_vol}")
    if low_threshold <= 0.0 or high_threshold <= low_threshold:
        raise ValueError("require 0 < low_threshold < high_threshold")
    crisis_threshold = 2.0 * high_threshold
    if realized_vol < low_threshold:
        return "low"
    if realized_vol < high_threshold:
        return "normal"
    if realized_vol < crisis_threshold:
        return "high"
    return "crisis"


def vol_target_recommendation(
    returns: np.ndarray,
    target_vol: float = 0.15,
) -> dict[str, float | str]:
    """一站式：从历史收益输出 realized vol / regime / scale factor.

    Args:
        returns: 历史 simple return 序列
        target_vol: 目标年化波动率

    Returns:
        dict 含 keys: realized_vol, target_vol, scale_factor, regime
    """
    realized = realized_volatility(returns, annualize=True)
    scale = vol_target_position_size(realized, target_vol) if realized > 0 else 1.0
    regime = regime_classify(realized)
    return {
        "realized_vol": realized,
        "target_vol": target_vol,
        "scale_factor": scale,
        "regime": regime,
    }
