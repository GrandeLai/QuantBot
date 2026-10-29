"""Redis 客户端测试 — Task 1 验收."""
from __future__ import annotations

import fakeredis.aioredis
import pytest

from quantbot.common.redis.client import RedisClient


@pytest.fixture(autouse=True)
async def reset_client():
    """每个测试前后重置单例状态."""
    RedisClient._instance = None
    yield
    if RedisClient._instance is not None:
        await RedisClient.disconnect()
    RedisClient._instance = None


async def _fake_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


class TestRedisClient:
    async def test_connect_sets_instance(self) -> None:
        fake = await _fake_client()
        RedisClient._instance = fake
        assert RedisClient.get() is fake

    async def test_get_before_connect_raises(self) -> None:
        with pytest.raises(RuntimeError, match="未连接"):
            RedisClient.get()

    async def test_disconnect_clears_instance(self) -> None:
        fake = await _fake_client()
        RedisClient._instance = fake
        await RedisClient.disconnect()
        assert RedisClient._instance is None

    async def test_ping_returns_true_when_connected(self) -> None:
        fake = await _fake_client()
        RedisClient._instance = fake
        result = await RedisClient.ping()
        assert result is True

    async def test_ping_returns_false_when_disconnected(self) -> None:
        result = await RedisClient.ping()
        assert result is False

    async def test_health_check(self) -> None:
        fake = await _fake_client()
        RedisClient._instance = fake
        ok = await RedisClient.ping()
        assert ok is True
