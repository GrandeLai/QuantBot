"""Dashboard state models for Streamlit or other presentation layers."""

from dataclasses import dataclass
from typing import Dict, List, Mapping, Tuple

from .regime import Regime


@dataclass(frozen=True)
class DashboardSnapshot:
    regime: str
    gross_exposure: float
    top_positions: List[Tuple[str, float]]
    performance: Dict[str, float]
    factor_exposure: Dict[str, float]

    @classmethod
    def from_state(
        cls,
        regime: Regime,
        positions: Mapping[str, float],
        performance: Mapping[str, float],
        factor_exposure: Mapping[str, float],
        top_n: int = 10,
    ):
        top_positions = list(
            sorted(positions.items(), key=lambda item: (-abs(item[1]), item[0]))[:top_n]
        )
        return cls(
            regime=regime.value,
            gross_exposure=sum(abs(float(weight)) for weight in positions.values()),
            top_positions=top_positions,
            performance=dict(performance),
            factor_exposure=dict(factor_exposure),
        )
