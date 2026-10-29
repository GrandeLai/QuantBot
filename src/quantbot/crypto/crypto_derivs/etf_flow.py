"""加密现货 ETF 净流入分析引擎.

零外部数据抓取——caller 自己提供 ETFFlowSnapshot 列表（来自 Farside / CoinGlass /
手动粘贴），本层做聚合、滚动 z-score、极值信号、AUM velocity 计算。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Literal

import numpy as np

from quantbot.crypto.crypto_derivs.models import ETFFlowSnapshot

# 美国上市的 BTC/ETH 现货 ETF（截至 2026.04，会随 SEC 批准变化）
BTC_SPOT_ETF_TICKERS: tuple[str, ...] = (
    "IBIT",  # iShares Bitcoin Trust (BlackRock)
    "FBTC",  # Fidelity Wise Origin Bitcoin Fund
    "ARKB",  # ARK 21Shares Bitcoin ETF
    "BITB",  # Bitwise Bitcoin ETF
    "BTCO",  # Invesco Galaxy Bitcoin ETF
    "HODL",  # VanEck Bitcoin Trust
    "BRRR",  # Valkyrie Bitcoin Fund
    "EZBC",  # Franklin Bitcoin ETF
    "BTCW",  # WisdomTree Bitcoin Fund
    "DEFI",  # Hashdex Bitcoin ETF
    "GBTC",  # Grayscale Bitcoin Trust
)

ETH_SPOT_ETF_TICKERS: tuple[str, ...] = (
    "ETHA",  # iShares Ethereum Trust
    "FETH",  # Fidelity Ethereum Fund
    "ETHV",  # VanEck Ethereum ETF
    "ETHE",  # Grayscale Ethereum Trust
    "ETH",   # Grayscale Ethereum Mini Trust
    "QETH",  # Invesco Galaxy Ethereum ETF
    "EZET",  # Franklin Ethereum ETF
    "CETH",  # 21Shares Core Ethereum ETF
)

FlowSignal = Literal["large_inflow", "large_outflow", "neutral"]


def aggregate_daily_flows(
    snapshots: list[ETFFlowSnapshot],
    *,
    tickers: tuple[str, ...] | None = None,
) -> dict[str, float]:
    """跨多个 ETF 按日聚合净流入.

    Args:
        snapshots: ETFFlowSnapshot 列表（任意顺序）
        tickers: 过滤白名单（不区分大小写）；None = 不过滤

    Returns:
        dict: "YYYY-MM-DD" → 当日跨白名单 ETF 总净流入（美元）
    """
    allow: set[str] | None = (
        {t.upper() for t in tickers} if tickers is not None else None
    )
    bucket: dict[str, float] = defaultdict(float)
    for s in snapshots:
        if allow is not None and s.ticker.upper() not in allow:
            continue
        bucket[s.date.isoformat()] += s.net_flow_usd
    return dict(bucket)


def flow_zscore(daily_flows: list[float], *, window: int = 30) -> list[float]:
    """滚动 z-score（标准化净流入），前 window-1 个为 NaN.

    Args:
        daily_flows: 按时间顺序的每日净流入序列
        window: 滚动窗口；要求 >= 5

    Returns:
        长度同输入；前 window-1 个 NaN，其余为 (x - mean) / std
    """
    if window < 5:
        raise ValueError(f"window must be >= 5, got {window}")
    arr = np.asarray(daily_flows, dtype=np.float64)
    if arr.size < window:
        raise ValueError(f"need at least window={window} samples, got {arr.size}")
    n = arr.size
    out = np.full(n, np.nan, dtype=np.float64)
    for i in range(window - 1, n):
        slc = arr[i - window + 1 : i + 1]
        mu = float(np.mean(slc))
        sigma = float(np.std(slc, ddof=1))
        out[i] = (arr[i] - mu) / sigma if sigma > 1e-12 else 0.0
    return out.tolist()


def flow_extreme_signal(
    current_flow: float,
    history: list[float],
    *,
    z_threshold: float = 2.0,
) -> dict[str, float | str]:
    """ETF flow 极值信号（与 funding 同形）.

    Args:
        current_flow: 当日净流入（美元）
        history: 历史净流入序列（≥ 30 个）
        z_threshold: |z| ≥ 此值触发信号

    Returns:
        {z_score, percentile (0-100), signal}
        signal:
          - "large_inflow"  z >= +threshold
          - "large_outflow" z <= -threshold
          - "neutral"
    """
    if z_threshold <= 0.0:
        raise ValueError(f"z_threshold must be > 0, got {z_threshold}")
    arr = np.asarray(history, dtype=np.float64)
    if arr.size < 30:
        raise ValueError(f"need at least 30 samples, got {arr.size}")
    mu = float(np.mean(arr))
    sigma = float(np.std(arr, ddof=1))
    z = (current_flow - mu) / sigma if sigma > 1e-12 else 0.0
    pct = float(np.mean(arr <= current_flow) * 100.0)
    signal: FlowSignal
    if z >= z_threshold:
        signal = "large_inflow"
    elif z <= -z_threshold:
        signal = "large_outflow"
    else:
        signal = "neutral"
    return {"z_score": z, "percentile": pct, "signal": signal}


def flow_aum_velocity(net_flow_usd: float, total_aum_usd: float) -> float:
    """当日净流入占总 AUM 的百分比（"velocity"）.

    > 5% 通常代表显著资金事件；可用作前端阈值告警.
    """
    if total_aum_usd <= 0.0:
        raise ValueError(f"total_aum_usd must be > 0, got {total_aum_usd}")
    return (net_flow_usd / total_aum_usd) * 100.0
