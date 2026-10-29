"""Portfolio-level risk scaling helpers."""

from dataclasses import dataclass
from typing import Dict, Mapping


@dataclass(frozen=True)
class PortfolioCapacityEstimate:
    max_capital: float
    bottleneck_ticker: str
    capacity_by_ticker: Dict[str, float]


class PortfolioRiskScaler:
    def __init__(self, max_portfolio_vol_annual: float = 0.18):
        self.max_portfolio_vol_annual = max_portfolio_vol_annual

    def scale_for_volatility(
        self,
        weights: Mapping[str, float],
        estimated_vol_annual: float,
    ) -> Dict[str, float]:
        if estimated_vol_annual <= 0 or estimated_vol_annual <= self.max_portfolio_vol_annual:
            return dict(weights)
        scale = self.max_portfolio_vol_annual / estimated_vol_annual
        return {
            ticker: round(float(weight) * scale, 10)
            for ticker, weight in weights.items()
        }


def estimate_portfolio_capacity(
    weights: Mapping[str, float],
    adv_dollars: Mapping[str, float],
    max_adv_participation: float = 0.01,
) -> PortfolioCapacityEstimate:
    capacity_by_ticker = {}
    for ticker, weight in weights.items():
        abs_weight = abs(float(weight))
        if abs_weight <= 0:
            continue
        tradable_notional = float(adv_dollars.get(ticker, 0.0)) * max_adv_participation
        capacity_by_ticker[ticker] = tradable_notional / abs_weight

    if not capacity_by_ticker:
        return PortfolioCapacityEstimate(0.0, "", {})

    bottleneck = min(capacity_by_ticker, key=lambda ticker: (capacity_by_ticker[ticker], ticker))
    return PortfolioCapacityEstimate(
        max_capital=capacity_by_ticker[bottleneck],
        bottleneck_ticker=bottleneck,
        capacity_by_ticker=capacity_by_ticker,
    )


def apply_hedge_overlay(
    weights: Mapping[str, float],
    hedge_symbol: str = "SPY",
    hedge_weight: float = -0.05,
    enabled: bool = False,
) -> Dict[str, float]:
    result = dict(weights)
    if enabled and hedge_weight != 0:
        result[hedge_symbol] = float(hedge_weight)
    return result
