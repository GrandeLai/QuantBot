"""订单数据契约."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class OrderSide(StrEnum):
    """订单方向."""

    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    """订单类型."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


@dataclass
class Order:
    """订单对象."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: float | None = None
    stop_price: float | None = None
    strategy_id: str = ""
    comment: str = ""
