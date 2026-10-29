"""Tax-aware sell ordering helpers for US equities."""

from dataclasses import dataclass
from datetime import date
from typing import Iterable, List


@dataclass(frozen=True)
class TaxLot:
    ticker: str
    quantity: float
    cost_basis: float
    market_price: float
    holding_days: int

    @property
    def unrealized_gain(self) -> float:
        return (self.market_price - self.cost_basis) * self.quantity


class TaxPolicy:
    def __init__(self, long_term_days: int = 365, preserve_gain_within_days: int = 30):
        self.long_term_days = long_term_days
        self.preserve_gain_within_days = preserve_gain_within_days

    def order_sell_candidates(self, lots: Iterable[TaxLot]) -> List[TaxLot]:
        return sorted(lots, key=self._sell_priority)

    def is_wash_sale_blocked(self, ticker: str, sold_loss_dates: Iterable[date], buy_date: date) -> bool:
        del ticker
        return any(abs((buy_date - sold_date).days) <= 30 for sold_date in sold_loss_dates)

    def _sell_priority(self, lot: TaxLot):
        if lot.unrealized_gain < 0 and lot.holding_days < self.long_term_days:
            bucket = 0
        elif lot.unrealized_gain > 0 and lot.holding_days >= self.long_term_days - self.preserve_gain_within_days:
            bucket = 2
        else:
            bucket = 1
        return (bucket, -abs(lot.unrealized_gain))
