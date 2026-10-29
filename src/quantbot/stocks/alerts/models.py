"""告警系统数据模型."""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class AlertConditionType(StrEnum):
    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    PCT_CHANGE_ABOVE = "pct_change_above"
    PCT_CHANGE_BELOW = "pct_change_below"


class AlertCondition(BaseModel):
    symbol: str
    condition_type: AlertConditionType
    threshold: float
    reference_price: float | None = None


class ChannelConfig(BaseModel):
    channel_type: Literal["feishu", "telegram", "webhook", "log"]
    webhook_url: str | None = None
    bot_token: str | None = None
    chat_id: str | None = None


class AlertRule(BaseModel):
    id: str
    name: str
    conditions: list[AlertCondition]
    channels: list[ChannelConfig]
    cooldown_seconds: int = 3600
    enabled: bool = True
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AlertEvent(BaseModel):
    rule_id: str
    rule_name: str
    symbol: str
    condition_type: str
    current_value: float
    threshold: float
    fired_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    message: str = ""
