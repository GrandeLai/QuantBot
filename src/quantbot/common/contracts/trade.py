"""交易记录数据契约."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class TradeRecord:
    """单笔交易记录."""

    symbol: str
    side: str             # 'buy' / 'sell'
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: float
    commission: float = 0.0
    slippage: float = 0.0

    @property
    def pnl(self) -> float:
        """净盈亏（扣除手续费和滑点）."""
        gross = (self.exit_price - self.entry_price) * self.quantity
        if self.side == "sell":
            gross = -gross
        return gross - self.commission - self.slippage

    @property
    def return_pct(self) -> float:
        """单笔收益率."""
        cost = self.entry_price * self.quantity
        return self.pnl / cost if cost != 0 else 0.0
