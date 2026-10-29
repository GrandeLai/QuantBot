"""Systematic overfitting guardrails."""

from dataclasses import dataclass
from typing import List, Sequence


@dataclass(frozen=True)
class RegimeValidationResult:
    regime: str
    ic_mean: float


@dataclass(frozen=True)
class OverfitDecision:
    passed: bool
    reasons: List[str]
    required_ic: float


class OverfitGuard:
    def __init__(self, min_holdout_years: float = 2.0, min_positive_regimes: int = 3):
        self.min_holdout_years = min_holdout_years
        self.min_positive_regimes = min_positive_regimes

    def evaluate(
        self,
        holdout_years: float,
        tuned_on_holdout: bool,
        regime_results: Sequence[RegimeValidationResult],
        parameter_count: int,
        base_ic_threshold: float,
        observed_ic: float,
    ) -> OverfitDecision:
        reasons: List[str] = []
        if holdout_years < self.min_holdout_years:
            reasons.append("holdout")
        if tuned_on_holdout:
            reasons.append("data_snooping")
        positive_regimes = sum(1 for result in regime_results if result.ic_mean > 0)
        if positive_regimes < self.min_positive_regimes:
            reasons.append("regime_coverage")

        required_ic = base_ic_threshold + max(0, parameter_count - 5) * 0.001
        if observed_ic < required_ic:
            reasons.append("complexity_adjusted_ic")

        return OverfitDecision(
            passed=not reasons,
            reasons=reasons,
            required_ic=required_ic,
        )
