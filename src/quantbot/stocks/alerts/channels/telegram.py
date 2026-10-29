"""Telegram Bot 通知渠道."""
from __future__ import annotations

import httpx
from loguru import logger

from quantbot.stocks.alerts.channels.base import NotificationChannel


class TelegramChannel(NotificationChannel):
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._bot_token = bot_token
        self._chat_id = chat_id

    async def send(self, title: str, body: str) -> None:
        url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                resp = await client.post(url, json={"chat_id": self._chat_id, "text": f"*{title}*\n{body}", "parse_mode": "Markdown"})
                if resp.status_code != 200:
                    logger.warning(f"[Telegram] 推送失败: {resp.status_code}")
            except Exception as e:
                logger.error(f"[Telegram] 推送异常: {e}")
