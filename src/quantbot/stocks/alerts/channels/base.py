"""通知渠道抽象基类."""
from __future__ import annotations

from abc import ABC, abstractmethod


class NotificationChannel(ABC):
    @abstractmethod
    async def send(self, title: str, body: str) -> None:
        """发送通知."""
