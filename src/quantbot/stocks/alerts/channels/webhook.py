"""通用 HTTP Webhook 通知渠道."""
from __future__ import annotations

import httpx
from loguru import logger

from quantbot.stocks.alerts.channels.base import NotificationChannel


class WebhookChannel(NotificationChannel):
    def __init__(self, url: str) -> None:
        self._url = url

    async def send(self, title: str, body: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.post(self._url, json={"title": title, "body": body})
                if resp.status_code >= 400:
                    logger.warning(f"[Webhook] 推送失败: {resp.status_code}")
            except Exception as e:
                logger.error(f"[Webhook] 推送异常: {e}")
