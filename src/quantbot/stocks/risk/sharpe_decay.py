"""策略 Sharpe 衰减监控 — rolling Sharpe + z-score + alert level.

核心用途：发现一条历史良好的策略已开始失效，触发降权/下架。

参考：Bailey & Lopez de Prado (2014) "The Deflated Sharpe Ratio"——
单点 Sharpe 不可信，应与历史分布对比。
"""
from __future__ import annotations

from typing import Literal

import numpy as np

AlertLevel = Literal["green", "yellow", "red"]


def rolling_sharpe(
    returns: np.ndarray,
    *,
    window: int = 63,
    periods_per_year: int = 252,
) -> np.ndarray:
    """滚动窗口 annualized Sharpe ratio.

    Args:
        returns: simple return 序列
        window: 滚动窗口长度，默认 63（约 3 月）
        periods_per_year: 252 日 / 52 周 / 12 月

    Returns:
        长度同输入；前 `window-1` 个为 NaN
    """
    arr = np.asarray(returns, dtype=np.float64)
    if window < 10:
        raise ValueError(f"window must be >= 10, got {window}")
    if arr.size < window:
        raise ValueError(f"need at least window={window} samples, got {arr.size}")
    n = arr.size
    out = np.full(n, np.nan, dtype=np.float64)
    sqrt_p = float(np.sqrt(periods_per_year))
    for i in range(window - 1, n):
        slc = arr[i - window + 1 : i + 1]
        mu = float(np.mean(slc))
        sigma = float(np.std(slc, ddof=1))
        out[i] = (mu / sigma) * sqrt_p if sigma > 1e-12 else 0.0
    return out


def sharpe_z_score(current_sharpe: float, baseline: np.ndarray) -> float:
    """当前 Sharpe 相对历史 baseline 的 z-score.

    Args:
        current_sharpe: 最近窗口的 Sharpe
        baseline: 历史 rolling Sharpe 序列（NaN 会被自动剔除）

    Returns:
        z-score；若 baseline std 为 0 则返回 0.0
    """
    arr = np.asarray(baseline, dtype=np.float64)
    arr = arr[~np.isnan(arr)]
    if arr.size < 10:
        raise ValueError(f"baseline needs >= 10 valid samples, got {arr.size}")
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    if sigma <= 1e-12:
        return 0.0
    return (current_sharpe - mu) / sigma


def decay_alert_level(z_score: float) -> AlertLevel:
    """根据 z-score 给衰减告警等级.

    阈值：
      z >= -1   → green   （仍在历史范围内）
      -2..-1    → yellow  （明显偏低，注意）
      z < -2    → red     （显著衰减，建议降权/下架）

    Args:
        z_score: 来自 sharpe_z_score 的输出

    Returns:
        "green" / "yellow" / "red"
    """
    if z_score >= -1.0:
        return "green"
    if z_score >= -2.0:
        return "yellow"
    return "red"


def analyze_strategy_decay(
    returns: np.ndarray,
    *,
    recent_window: int = 63,
    baseline_window: int = 252,
) -> dict[str, float | str | int]:
    """一站式衰减分析：基于完整历史给最近 vs 基线对比.

    Args:
        returns: 完整策略历史 simple return 序列
        recent_window: 最近 Sharpe 计算窗口（默认 63 ~3 月）
        baseline_window: 基线 Sharpe 历史长度（默认 252 ~1 年），
            从 returns 中"较早"的部分取，以避免与 recent 重叠

    Returns:
        dict: recent_sharpe, baseline_mean, baseline_std, z_score,
              alert_level, n_baseline_samples
    """
    arr = np.asarray(returns, dtype=np.float64)
    min_required = recent_window + baseline_window + 10
    if arr.size < min_required:
        raise ValueError(
            f"need >= recent_window + baseline_window + 10 = {min_required} samples, "
            f"got {arr.size}"
        )

    # 最近窗口
    recent_returns = arr[-recent_window:]
    recent_mu = float(np.mean(recent_returns))
    recent_sigma = float(np.std(recent_returns, ddof=1))
    recent_sharpe = (
        (recent_mu / recent_sigma) * float(np.sqrt(252)) if recent_sigma > 1e-12 else 0.0
    )

    # 基线：取最近窗口之前的所有数据，跑滚动 Sharpe
    baseline_returns = arr[:-recent_window]
    rolling = rolling_sharpe(baseline_returns, window=recent_window)
    valid = rolling[~np.isnan(rolling)]
    # 仅保留最后 baseline_window 个滚动 Sharpe（避免太古老的样本）
    if valid.size > baseline_window:
        valid = valid[-baseline_window:]

    baseline_mean = float(np.mean(valid))
    baseline_std = float(np.std(valid, ddof=1))
    z = sharpe_z_score(recent_sharpe, valid)
    level = decay_alert_level(z)

    return {
        "recent_sharpe": recent_sharpe,
        "baseline_mean": baseline_mean,
        "baseline_std": baseline_std,
        "z_score": z,
        "alert_level": level,
        "n_baseline_samples": int(valid.size),
    }
