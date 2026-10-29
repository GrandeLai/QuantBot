"""WebSocket 实时推送端点.

端点:
  WS /ws/bars/{symbol}/{timeframe}  — 实时 K 线推送
  WS /ws/signals                    — 实时信号推送
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from loguru import logger

router = APIRouter(tags=["WebSocket"])


@router.websocket("/ws/bars/{symbol}/{timeframe}")
async def ws_bars(websocket: WebSocket, symbol: str, timeframe: str) -> None:
    """订阅指定标的和周期的实时 K 线数据.

    客户端连接后，每当有新 bar 推送到 Redis 频道 bars:{symbol}:{timeframe} 时，
    立即转发给客户端。

    Args:
        symbol: 标的代码，如 AAPL
        timeframe: K 线周期，如 1d
    """
    from quantbot.common.redis.client import RedisClient
    from quantbot.common.redis.pubsub import bar_channel

    await websocket.accept()
    channel = bar_channel(symbol.upper(), timeframe)
    logger.info(f"[WS] 客户端订阅 {channel}")

    r = RedisClient.get()
    pubsub = r.pubsub()
    await pubsub.subscribe(channel)

    try:
        async for message in pubsub.listen():
            if message["type"] == "message":
                await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        logger.info(f"[WS] 客户端断开 {channel}")
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


@router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket) -> None:
    """订阅所有交易信号的实时推送.

    客户端连接后，任何策略发布的信号（通过 signals:* 频道）都会实时转发。
    """
    from quantbot.common.redis.client import RedisClient

    await websocket.accept()
    channel = "signals:*"
    logger.info("[WS] 客户端订阅信号频道")

    r = RedisClient.get()
    pubsub = r.pubsub()
    await pubsub.psubscribe(channel)  # pattern subscribe

    try:
        async for message in pubsub.listen():
            if message["type"] == "pmessage":
                await websocket.send_text(message["data"])
    except WebSocketDisconnect:
        logger.info("[WS] 信号频道客户端断开")
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.punsubscribe(channel)
        await pubsub.aclose()
