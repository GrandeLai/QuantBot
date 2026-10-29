"""最新价格缓存 — Redis Hash + 各标的独立 Key."""
from __future__ import annotations

from loguru import logger

from quantbot.common.redis.client import RedisClient

_HASH_KEY = "quantbot:prices"  # Redis Hash: field=symbol, value=price_str


class PriceCache:
    """用 Redis Hash 存储各标的最新收盘价.

    所有标的存储在同一个 Hash Key 下，支持批量读取。
    """

    async def set_price(self, symbol: str, price: float) -> None:
        """写入标的最新价格.

        Args:
            symbol: 标的代码
            price: 最新价格
        """
        r = RedisClient.get()
        await r.hset(_HASH_KEY, symbol, str(price))  # type: ignore[misc]
        logger.debug(f"[PriceCache] {symbol} = {price}")

    async def get_price(self, symbol: str) -> float | None:
        """读取标的最新价格.

        Returns:
            float 价格，或 None（不存在时）
        """
        r = RedisClient.get()
        val = await r.hget(_HASH_KEY, symbol)  # type: ignore[misc]
        return float(val) if val is not None else None

    async def get_all(self) -> dict[str, float]:
        """读取所有标的的最新价格.

        Returns:
            {symbol: price} 字典
        """
        r = RedisClient.get()
        raw: dict[str, str] = await r.hgetall(_HASH_KEY)  # type: ignore[misc]
        return {k: float(v) for k, v in raw.items()}

    async def delete_price(self, symbol: str) -> None:
        """删除标的价格缓存."""
        r = RedisClient.get()
        await r.hdel(_HASH_KEY, symbol)  # type: ignore[misc]
