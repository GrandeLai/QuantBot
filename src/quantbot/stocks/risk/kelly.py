"""Kelly Fraction 仓位建议（pure functions, numpy-only）.

参考：Kelly (1956)、Thorp (1969)、MacLean/Thorp/Ziemba (2010)。

四个函数的拓扑：
  - kelly_fraction_binary       离散二项（已知胜率 + 赔率）
  - kelly_fraction_from_returns 连续近似（用历史收益估算）
  - fractional_kelly            实战版：full Kelly × fraction（默认 0.25）
  - capped_kelly                单只上限（默认 25%）+ 负值截断
"""
from __future__ import annotations

import numpy as np


def kelly_fraction_binary(win_rate: float, payoff_ratio: float) -> float:
    """经典 Kelly：f* = p − q/b.

    Args:
        win_rate: 胜率 p ∈ [0, 1]
        payoff_ratio: 赔率 b > 0（赢一次的回报 / 输一次的损失）

    Returns:
        最优仓位比例，负值（负 EV）截断为 0.0
    """
    if not 0.0 <= win_rate <= 1.0:
        raise ValueError(f"win_rate must be in [0, 1], got {win_rate}")
    if payoff_ratio <= 0.0:
        raise ValueError(f"payoff_ratio must be > 0, got {payoff_ratio}")
    loss_rate = 1.0 - win_rate
    f_star = win_rate - loss_rate / payoff_ratio
    return max(f_star, 0.0)


def kelly_fraction_from_returns(returns: np.ndarray) -> float:
    """连续近似 Kelly：f* ≈ μ / σ²（Markowitz-Kelly）.

    Args:
        returns: 历史 simple return 序列（例如日收益），至少 10 个样本

    Returns:
        最优仓位比例；方差为 0 或负 EV 时返回 0.0
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.size < 10:
        raise ValueError(f"need at least 10 samples, got {arr.size}")
    mu = float(np.mean(arr))
    var = float(np.var(arr, ddof=1))
    # 用相对阈值判定 degenerate variance：常量序列因浮点误差有时给出极小残差
    if var <= 1e-12:
        return 0.0
    f_star = mu / var
    return max(f_star, 0.0)


def fractional_kelly(full_kelly: float, fraction: float = 0.25) -> float:
    """实战版：仅取 full Kelly 的一部分（降低破产概率）.

    Args:
        full_kelly: 来自 kelly_fraction_binary / kelly_fraction_from_returns 的值
        fraction: ∈ (0, 1]，默认 0.25（quarter Kelly）

    Returns:
        缩放后的仓位比例（不再截顶，由 capped_kelly 处理）
    """
    if not 0.0 < fraction <= 1.0:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    return max(full_kelly, 0.0) * fraction


def capped_kelly(full_kelly: float, cap: float = 0.25) -> float:
    """单只上限 + 负值截断.

    Args:
        full_kelly: 来自上游的仓位建议
        cap: 单只仓位上限 ∈ (0, 1]，默认 0.25

    Returns:
        clip 到 [0, cap]
    """
    if not 0.0 < cap <= 1.0:
        raise ValueError(f"cap must be in (0, 1], got {cap}")
    return min(max(full_kelly, 0.0), cap)
