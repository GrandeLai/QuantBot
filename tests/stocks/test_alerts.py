"""告警系统测试 — T-2.3 验收."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from quantbot.stocks.alerts.engine import AlertEngine
from quantbot.stocks.alerts.models import (
    AlertCondition,
    AlertConditionType,
    AlertRule,
)


@pytest.fixture
def price_above_rule() -> AlertRule:
    return AlertRule(
        id="rule_001",
        name="BTC突破75000",
        conditions=[
            AlertCondition(
                symbol="BTC/USDT",
                condition_type=AlertConditionType.PRICE_ABOVE,
                threshold=75000.0,
            )
        ],
        channels=[],
        cooldown_seconds=3600,
    )


class TestAlertCondition:
    def test_price_above_triggers(self, price_above_rule: AlertRule) -> None:
        engine = AlertEngine()
        triggered = engine.evaluate_rule(price_above_rule, {"BTC/USDT": 76000.0})
        assert triggered is True

    def test_price_above_not_triggered(self, price_above_rule: AlertRule) -> None:
        engine = AlertEngine()
        triggered = engine.evaluate_rule(price_above_rule, {"BTC/USDT": 74000.0})
        assert triggered is False

    def test_price_below_triggers(self) -> None:
        rule = AlertRule(
            id="rule_002",
            name="价格跌破支撑",
            conditions=[
                AlertCondition(
                    symbol="AAPL",
                    condition_type=AlertConditionType.PRICE_BELOW,
                    threshold=150.0,
                )
            ],
            channels=[],
            cooldown_seconds=0,
        )
        engine = AlertEngine()
        assert engine.evaluate_rule(rule, {"AAPL": 145.0}) is True
        assert engine.evaluate_rule(rule, {"AAPL": 155.0}) is False

    def test_pct_change_triggers(self) -> None:
        rule = AlertRule(
            id="rule_003",
            name="涨幅超5%",
            conditions=[
                AlertCondition(
                    symbol="TSLA",
                    condition_type=AlertConditionType.PCT_CHANGE_ABOVE,
                    threshold=5.0,
                    reference_price=200.0,
                )
            ],
            channels=[],
            cooldown_seconds=0,
        )
        engine = AlertEngine()
        assert engine.evaluate_rule(rule, {"TSLA": 211.0}) is True
        assert engine.evaluate_rule(rule, {"TSLA": 209.0}) is False


class TestAlertCooldown:
    def test_cooldown_prevents_duplicate(self, price_above_rule: AlertRule) -> None:
        engine = AlertEngine()
        prices = {"BTC/USDT": 76000.0}
        assert engine.evaluate_rule(price_above_rule, prices) is True
        engine.record_fired(price_above_rule.id)
        assert engine.evaluate_rule(price_above_rule, prices) is False

    def test_no_cooldown_fires_always(self) -> None:
        rule = AlertRule(
            id="no_cool",
            name="无冷却",
            conditions=[
                AlertCondition(
                    symbol="X",
                    condition_type=AlertConditionType.PRICE_ABOVE,
                    threshold=100.0,
                )
            ],
            channels=[],
            cooldown_seconds=0,
        )
        engine = AlertEngine()
        prices = {"X": 110.0}
        assert engine.evaluate_rule(rule, prices) is True
        engine.record_fired(rule.id)
        assert engine.evaluate_rule(rule, prices) is True


@pytest.mark.asyncio
class TestFeishuChannel:
    async def test_send_feishu_message(self) -> None:
        from quantbot.stocks.alerts.channels.feishu import FeishuChannel
        channel = FeishuChannel(webhook_url="https://open.feishu.cn/open-apis/bot/v2/hook/test")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value.status_code = 200
            await channel.send("BTC突破75000", "BTC/USDT 当前价格 76000")
            assert mock_post.called


@pytest.mark.asyncio
class TestTelegramChannel:
    async def test_send_telegram_message(self) -> None:
        from quantbot.stocks.alerts.channels.telegram import TelegramChannel
        channel = TelegramChannel(bot_token="123:TOKEN", chat_id="456789")
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value.status_code = 200
            await channel.send("价格告警", "AAPL 跌破 150")
            assert mock_post.called
