"""Trading cost model for US equity execution."""

from dataclasses import dataclass


@dataclass(frozen=True)
class OrderCost:
    commission: float
    spread_cost: float
    impact_cost: float
    slippage_cost: float

    @property
    def total_cost(self) -> float:
        return self.commission + self.spread_cost + self.impact_cost + self.slippage_cost


class TradingCostModel:
    def __init__(
        self,
        commission_per_share: float = 0.0,
        spread_bps: float = 3.0,
        impact_bps: float = 5.0,
        slippage_bps: float = 4.0,
    ):
        self.commission_per_share = commission_per_share
        self.spread_bps = spread_bps
        self.impact_bps = impact_bps
        self.slippage_bps = slippage_bps

    def estimate_order_cost(self, notional: float, shares: float) -> OrderCost:
        return OrderCost(
            commission=shares * self.commission_per_share,
            spread_cost=_bps_cost(notional, self.spread_bps),
            impact_cost=_bps_cost(notional, self.impact_bps),
            slippage_cost=_bps_cost(notional, self.slippage_bps),
        )

    def annual_turnover_drag(self, two_way_cost_rate: float, annual_turnover: float) -> float:
        return two_way_cost_rate * annual_turnover


def _bps_cost(notional: float, bps: float) -> float:
    return notional * bps / 10_000
