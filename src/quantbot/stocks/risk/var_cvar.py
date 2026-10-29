"""组合层尾部风险 — Historical VaR / CVaR / Parametric VaR.

约定：所有风险数值返回**正数**（亏损绝对值），便于与 max drawdown 等
其它正数风险指标统一展示。例如 95% 1-day VaR = 0.025 表示
"95% 概率下，单日亏损不超过 2.5%"。

EVT、Cornish-Fisher、Monte Carlo VaR 留到完整版。
"""
from __future__ import annotations

from typing import Literal

import numpy as np
from scipy import stats

VarMethod = Literal["historical", "parametric", "both"]


def _validate(returns: np.ndarray, confidence: float) -> np.ndarray:
    arr = np.asarray(returns, dtype=np.float64)
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if arr.size < 30:
        raise ValueError(f"need at least 30 samples, got {arr.size}")
    return arr


def historical_var(returns: np.ndarray, *, confidence: float = 0.95) -> float:
    """Historical VaR：经验分位数法.

    Args:
        returns: simple return 序列（≥ 30 个样本）
        confidence: 置信水平 ∈ (0, 1)，默认 0.95

    Returns:
        正数（亏损绝对值）；若分位数为正（极少见，全样本上涨）返回 0.0
    """
    arr = _validate(returns, confidence)
    q = float(np.quantile(arr, 1.0 - confidence))
    return max(-q, 0.0)


def historical_cvar(returns: np.ndarray, *, confidence: float = 0.95) -> float:
    """Conditional VaR / Expected Shortfall：尾部均值.

    Args:
        returns: simple return 序列（≥ 30）
        confidence: 置信水平 ∈ (0, 1)，默认 0.95

    Returns:
        尾部平均亏损（正数）；若尾部为空（极少）退化为 historical_var
    """
    arr = _validate(returns, confidence)
    q = float(np.quantile(arr, 1.0 - confidence))
    tail = arr[arr <= q]
    if tail.size == 0:
        return max(-q, 0.0)
    return max(-float(np.mean(tail)), 0.0)


def parametric_var(returns: np.ndarray, *, confidence: float = 0.95) -> float:
    """高斯参数法 VaR：μ + z·σ.

    Args:
        returns: simple return 序列（≥ 30）
        confidence: 置信水平 ∈ (0, 1)，默认 0.95

    Returns:
        正数（亏损绝对值）；若 σ=0 返回 0.0
    """
    arr = _validate(returns, confidence)
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    if sigma <= 1e-12:
        return 0.0
    z = float(stats.norm.ppf(1.0 - confidence))
    return max(-(mu + z * sigma), 0.0)


def var_summary(
    returns: np.ndarray,
    *,
    confidences: tuple[float, ...] = (0.95, 0.99),
    method: VarMethod = "historical",
) -> dict[str, float | str | int]:
    """一站式 VaR 报告.

    Args:
        returns: simple return 序列
        confidences: 置信度元组，默认 (0.95, 0.99)
        method: "historical" / "parametric" / "both"

    Returns:
        dict 含：n_samples, worst_loss, method,
        以及每个置信度对应的 var_<pct>, cvar_<pct>，
        method="both" 时额外含 parametric_var_<pct>。
    """
    arr = np.asarray(returns, dtype=np.float64)
    if arr.size < 30:
        raise ValueError(f"need at least 30 samples, got {arr.size}")
    if method not in ("historical", "parametric", "both"):
        raise ValueError(f"unknown method {method!r}")

    out: dict[str, float | str | int] = {
        "n_samples": int(arr.size),
        "worst_loss": max(-float(np.min(arr)), 0.0),
        "method": method,
    }
    for c in confidences:
        pct = int(round(c * 100))
        if method in ("historical", "both"):
            out[f"var_{pct}"] = historical_var(arr, confidence=c)
            out[f"cvar_{pct}"] = historical_cvar(arr, confidence=c)
        if method in ("parametric", "both"):
            out[f"parametric_var_{pct}"] = parametric_var(arr, confidence=c)
    return out
