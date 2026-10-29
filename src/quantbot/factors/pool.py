"""Candidate factor pool de-correlation."""

from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping

from .evaluation import FactorMetrics


@dataclass(frozen=True)
class SignalDiversityDecision:
    passed: bool
    correlation: float
    abs_correlation: float
    reasons: List[str]


@dataclass(frozen=True)
class PoolAverageCorrelationDecision:
    passed: bool
    average_abs_correlation: float
    pair_count: int
    reasons: List[str]


def select_representative_factors(
    factor_values: Mapping[str, Iterable[float]],
    metrics: Mapping[str, FactorMetrics],
    corr_threshold: float = 0.65,
) -> List[str]:
    """Greedily keep the best-ICIR representative from correlated clusters."""

    ordered = sorted(
        factor_values,
        key=lambda name: metrics[name].icir if name in metrics else float("-inf"),
        reverse=True,
    )
    selected: List[str] = []
    ranked_values: Dict[str, List[float]] = {
        name: _ranks([float(value) for value in values])
        for name, values in factor_values.items()
    }

    for name in ordered:
        if all(
            abs(_pearson(ranked_values[name], ranked_values[existing])) <= corr_threshold
            for existing in selected
        ):
            selected.append(name)

    return selected


def evaluate_signal_diversity(
    candidate_values: Iterable[float],
    reference_values: Iterable[float],
    max_abs_correlation: float = 0.40,
) -> SignalDiversityDecision:
    correlation = _pearson(
        [float(value) for value in candidate_values],
        [float(value) for value in reference_values],
    )
    abs_correlation = abs(correlation)
    reasons = [] if abs_correlation <= max_abs_correlation else ["correlation"]
    return SignalDiversityDecision(
        passed=not reasons,
        correlation=correlation,
        abs_correlation=abs_correlation,
        reasons=reasons,
    )


def evaluate_pool_average_correlation(
    factor_values: Mapping[str, Iterable[float]],
    max_average_abs_correlation: float = 0.40,
) -> PoolAverageCorrelationDecision:
    ranked_values = {
        name: _ranks([float(value) for value in values])
        for name, values in factor_values.items()
    }
    names = sorted(ranked_values)
    correlations = []
    for left_index, left_name in enumerate(names):
        for right_name in names[left_index + 1:]:
            correlations.append(abs(_pearson(ranked_values[left_name], ranked_values[right_name])))

    average = sum(correlations) / len(correlations) if correlations else 0.0
    reasons = [] if average <= max_average_abs_correlation else ["average_correlation"]
    return PoolAverageCorrelationDecision(
        passed=not reasons,
        average_abs_correlation=average,
        pair_count=len(correlations),
        reasons=reasons,
    )


def _ranks(values: List[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index + 1
        while end < len(indexed) and indexed[end][1] == indexed[index][1]:
            end += 1
        average_rank = (index + 1 + end) / 2
        for ranked_index in range(index, end):
            original_index = indexed[ranked_index][0]
            ranks[original_index] = average_rank
        index = end
    return ranks


def _pearson(x_values: List[float], y_values: List[float]) -> float:
    if len(x_values) != len(y_values) or len(x_values) < 2:
        return 0.0
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    numerator = sum(
        (x_value - x_mean) * (y_value - y_mean)
        for x_value, y_value in zip(x_values, y_values)
    )
    x_var = sum((x_value - x_mean) ** 2 for x_value in x_values)
    y_var = sum((y_value - y_mean) ** 2 for y_value in y_values)
    denominator = (x_var * y_var) ** 0.5
    return numerator / denominator if denominator else 0.0
