"""行情 Pub/Sub — 发布 OHLCV bar 到 Redis channel."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from loguru import logger

from quantbot.common.redis.client import RedisClient

if TYPE_CHECKING:
    from quantbot.common.data.models import OHLCVBar


def bar_channel(symbol: str, timeframe: str) -> str:
    """生成行情频道名称.

    Args:
        symbol: 标的代码
        timeframe: K 线周期

    Returns:
        Redis 频道名，如 "bars:AAPL:1d"
    """
    return f"bars:{symbol}:{timeframe}"


def signal_channel(symbol: str = "*") -> str:
    """生成信号频道名称.

    Args:
        symbol: 标的代码，"*" 表示全量信号频道

    Returns:
        Redis 频道名，如 "signals:AAPL" 或 "signals:*"
    """
    return f"signals:{symbol}"


class BarPublisher:
    """向 Redis 发布 OHLCV bar 事件."""

    async def publish(self, bar: OHLCVBar) -> int:
        """发布一根 K 线到对应频道.

        Args:
            bar: OHLCV K 线数据

        Returns:
            收到消息的订阅者数量
        """
        r = RedisClient.get()
        channel = bar_channel(bar.symbol, bar.timeframe)
        payload = json.dumps({
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "timestamp": bar.timestamp.isoformat(),
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
        })
        count: int = await r.publish(channel, payload)
        logger.debug(f"[PubSub] 发布 {channel}: {bar.close:.2f} → {count} 订阅者")
        return count
