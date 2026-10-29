"""End-to-end daily trading workflow orchestration."""

from dataclasses import dataclass
from typing import Dict, Mapping, Set

from .execution import AutoExecutor
from .regime import Regime
from .risk import RiskManager
from .trading_agent import TradingAgent


@dataclass(frozen=True)
class WorkflowInputs:
    factor_matrix: Mapping[str, Mapping[str, float]]
    regime: Regime
    factor_weights: Mapping[Regime, Mapping[str, float]]
    average_dollar_volume: Mapping[str, float]
    sectors: Mapping[str, str]
    current_holdings: Mapping[str, float]
    top_n: int = 30
    earnings_window: Set[str] = None


@dataclass(frozen=True)
class WorkflowResult:
    scores: Dict[str, float]
    watchlist: list
    target_weights: Dict[str, float]


class DailyTradingWorkflow:
    def __init__(self, broker, risk_manager=None):
        self.broker = broker
        self.risk_manager = risk_manager or RiskManager()

    def run(self, inputs: WorkflowInputs) -> WorkflowResult:
        candidate_factors = _candidate_factor_names(inputs.factor_matrix)
        agent = TradingAgent(candidate_factors=candidate_factors, weight_config=inputs.factor_weights)
        scores = agent.compute_composite_scores(inputs.factor_matrix, inputs.regime)
        watchlist = agent.update_watchlist(
            scores,
            top_n=inputs.top_n,
            average_dollar_volume=inputs.average_dollar_volume,
            earnings_window=inputs.earnings_window or set(),
        )
        target_weights = self.risk_manager.build_target_weights(
            watchlist=watchlist,
            sectors=inputs.sectors,
            regime=inputs.regime,
        )
        AutoExecutor(self.broker).rebalance(target_weights, inputs.current_holdings)
        return WorkflowResult(
            scores=scores,
            watchlist=watchlist,
            target_weights=target_weights,
        )


def _candidate_factor_names(factor_matrix: Mapping[str, Mapping[str, float]]):
    names = []
    seen = set()
    for factors in factor_matrix.values():
        for name in factors:
            if name not in seen:
                seen.add(name)
                names.append(name)
    return names
