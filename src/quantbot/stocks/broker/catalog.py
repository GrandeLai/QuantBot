"""交易标的目录.

用于：
- mock provider 本地演示
- Longbridge provider 的代码 / 名称搜索补全

目录只覆盖常见港美股票与 ETF，用户仍可通过精确 symbol 调用 Longbridge
接口查询更多标的；当前版本不伪造不存在的期权 / OTC / 盘前盘后交易入口。
"""

from __future__ import annotations

from quantbot.stocks.broker.types import (
    TradingAssetType,
    TradingMarket,
    TradingSecurity,
)

SUPPORTED_SECURITIES: list[TradingSecurity] = [
    TradingSecurity(
        symbol="AAPL.US",
        name="Apple Inc.",
        market=TradingMarket.US,
        currency="USD",
        asset_type=TradingAssetType.STOCK,
        shortable=True,
    ),
    TradingSecurity(
        symbol="TSLA.US",
        name="Tesla, Inc.",
        market=TradingMarket.US,
        currency="USD",
        asset_type=TradingAssetType.STOCK,
        shortable=True,
    ),
    TradingSecurity(
        symbol="NVDA.US",
        name="NVIDIA Corporation",
        market=TradingMarket.US,
        currency="USD",
        asset_type=TradingAssetType.STOCK,
        shortable=True,
    ),
    TradingSecurity(
        symbol="MSFT.US",
        name="Microsoft Corporation",
        market=TradingMarket.US,
        currency="USD",
        asset_type=TradingAssetType.STOCK,
        shortable=True,
    ),
    TradingSecurity(
        symbol="SPY.US",
        name="SPDR S&P 500 ETF Trust",
        market=TradingMarket.US,
        currency="USD",
        asset_type=TradingAssetType.ETF,
        shortable=True,
    ),
    TradingSecurity(
        symbol="QQQ.US",
        name="Invesco QQQ Trust",
        market=TradingMarket.US,
        currency="USD",
        asset_type=TradingAssetType.ETF,
        shortable=True,
    ),
    TradingSecurity(
        symbol="700.HK",
        name="Tencent Holdings",
        market=TradingMarket.HK,
        currency="HKD",
        asset_type=TradingAssetType.STOCK,
        lot_size=100,
    ),
    TradingSecurity(
        symbol="9988.HK",
        name="Alibaba Group Holding",
        market=TradingMarket.HK,
        currency="HKD",
        asset_type=TradingAssetType.STOCK,
        lot_size=100,
    ),
    TradingSecurity(
        symbol="2800.HK",
        name="Tracker Fund of Hong Kong",
        market=TradingMarket.HK,
        currency="HKD",
        asset_type=TradingAssetType.ETF,
        lot_size=100,
    ),
]


def find_supported_securities(query: str, *, limit: int = 20) -> list[TradingSecurity]:
    """按代码或名称搜索常见支持标的."""
    normalized = query.strip().lower()
    if not normalized:
        return SUPPORTED_SECURITIES[:limit]

    results = [
        item
        for item in SUPPORTED_SECURITIES
        if normalized in item.symbol.lower() or normalized in item.name.lower()
    ]
    return results[:limit]


def find_security_by_symbol(symbol: str) -> TradingSecurity | None:
    """按精确 symbol 查找目录条目."""
    normalized = symbol.strip().upper()
    for item in SUPPORTED_SECURITIES:
        if item.symbol.upper() == normalized:
            return item
    return None
