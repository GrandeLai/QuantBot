"""Redis 异步客户端单例."""
from __future__ import annotations

import redis.asyncio as aioredis
from loguru import logger


class RedisClient:
    """全局 Redis 异步客户端单例.

    用法:
        await RedisClient.connect(url)
        r = RedisClient.get()
        await RedisClient.disconnect()
    """

    _instance: aioredis.Redis | None = None  # type: ignore[type-arg]

    @classmethod
    async def connect(cls, url: str) -> None:
        """建立 Redis 连接.

        Args:
            url: Redis 连接 URL，如 redis://host:6379 或 redis://:password@host:6379/0
        """
        cls._instance = aioredis.from_url(url, decode_responses=True)
        await cls._instance.ping()  # type: ignore[misc]
        logger.info(f"[Redis] 已连接: {url.split('@')[-1]}")  # 隐藏密码

    @classmethod
    async def disconnect(cls) -> None:
        """关闭 Redis 连接."""
        if cls._instance is not None:
            await cls._instance.aclose()
            cls._instance = None
            logger.info("[Redis] 连接已关闭")

    @classmethod
    def get(cls) -> aioredis.Redis:  # type: ignore[type-arg]
        """获取 Redis 客户端实例.

        Raises:
            RuntimeError: 未调用 connect() 时
        """
        if cls._instance is None:
            raise RuntimeError("Redis 未连接: 请先调用 RedisClient.connect()")
        return cls._instance

    @classmethod
    async def ping(cls) -> bool:
        """检查 Redis 连通性.

        Returns:
            True 表示连通，False 表示未连接或超时
        """
        try:
            return bool(await cls.get().ping())  # type: ignore[misc]
        except Exception:
            return False
