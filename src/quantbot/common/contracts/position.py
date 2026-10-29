"""持仓数据契约."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    """持仓对象."""

    symbol: str
    quantity: float          # 正数多头，负数空头
    avg_price: float
    unrealized_pnl: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0
