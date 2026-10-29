"""Frozen normal-frequency experiment registration."""

from __future__ import annotations

from datetime import datetime, timezone

from ..experiment_registry import ExperimentRegistry


NORMAL_H1_PREREGISTERED_AT = datetime(2026, 7, 17, tzinfo=timezone.utc)
NORMAL_H2_PREREGISTERED_AT = datetime(2026, 7, 26, tzinfo=timezone.utc)

NORMAL_H1_EXPERIMENT_ID = "normal-h1-quality-diversification"
NORMAL_H2_EXPERIMENT_ID = "normal-h2-capital-efficiency"


def preregistered_normal_experiments() -> ExperimentRegistry:
    """The complete normal-family multiplicity ledger.

    Both declarations count towards global DSR/PBO multiplicity.  H2 changes
    only how the allocator deploys capital under its existing constraints; it
    shares H1's baseline sleeve so the two hypotheses stay independent.
    """

    registry = ExperimentRegistry()
    registry.register_single_family(
        experiment_id=NORMAL_H1_EXPERIMENT_ID,
        strategy_family="normal",
        baseline_components=("residual_momentum_12_1_neutral",),
        candidate_components=(
            "residual_momentum_12_1_neutral",
            "quality_profitability_neutral",
        ),
        parameter_name="include_quality",
        parameter_values=(True,),
        preregistered_at=NORMAL_H1_PREREGISTERED_AT,
    )
    registry.register_single_family(
        experiment_id=NORMAL_H2_EXPERIMENT_ID,
        strategy_family="normal",
        baseline_components=("residual_momentum_12_1_neutral",),
        candidate_components=(
            "residual_momentum_12_1_neutral",
            "capital_efficient_allocation",
        ),
        parameter_name="capital_efficient_allocation",
        parameter_values=(True,),
        preregistered_at=NORMAL_H2_PREREGISTERED_AT,
    )
    return registry


__all__ = (
    "NORMAL_H1_EXPERIMENT_ID",
    "NORMAL_H1_PREREGISTERED_AT",
    "NORMAL_H2_EXPERIMENT_ID",
    "NORMAL_H2_PREREGISTERED_AT",
    "preregistered_normal_experiments",
)
