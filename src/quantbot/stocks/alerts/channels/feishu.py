"""飞书机器人 Webhook 通知渠道."""
from __future__ import annotations

import httpx
from loguru import logger

from quantbot.stocks.alerts.channels.base import NotificationChannel


class FeishuChannel(NotificationChannel):
    def __init__(self, webhook_url: str) -> None:
        self._webhook_url = webhook_url

    async def send(self, title: str, body: str) -> None:
        payload = {
            "msg_type": "post",
            "content": {"post": {"zh_cn": {"title": title, "content": [[{"tag": "text", "text": body}]]}}},
        }
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.post(self._webhook_url, json=payload)
                if resp.status_code != 200:
                    logger.warning(f"[Feishu] 推送失败: {resp.status_code}")
            except Exception as e:
                logger.error(f"[Feishu] 推送异常: {e}")
