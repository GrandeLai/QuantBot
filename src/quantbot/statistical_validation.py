"""Multiple-testing diagnostics from the DSR and CSCV/PBO papers."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import exp, fsum, isfinite, log, sqrt, ulp
from statistics import NormalDist
from types import MappingProxyType
from typing import Mapping, Sequence


EULER_MASCHERONI = 0.5772156649015329


@dataclass(frozen=True, slots=True)
class DeflatedSharpeResult:
    confidence: float
    test_statistic: float
    observed_sharpe_annual: float
    expected_max_sharpe_annual: float
    skewness: float
    kurtosis: float
    observations: int
    trial_count: int


@dataclass(frozen=True, slots=True)
class ProbabilityOfBacktestOverfittingResult:
    probability: float
    logits: tuple[float, ...]
    selected_strategy_ids: tuple[str, ...]
    combination_count: int
    folds: int
    strategy_count: int


def deflated_sharpe_ratio(
    returns: Sequence[float],
    trial_sharpes_annual: Sequence[float],
    periods_per_year: int = 252,
) -> DeflatedSharpeResult:
    """Compute DSR as PSR against the expected maximum across all trials."""

    values = _finite_series(returns, "returns", minimum_length=3)
    trials = _finite_series(
        trial_sharpes_annual,
        "trial_sharpes",
        minimum_length=1,
    )
    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year < 1:
        raise ValueError("periods_per_year must be a positive integer")
    mean = fsum(values) / len(values)
    sample_variance = fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    scale = max(abs(value) for value in values)
    variance_tolerance = max(
        scale * scale * 1e-24,
        (ulp(scale) * len(values)) ** 2 if scale else 0.0,
    )
    if sample_variance <= variance_tolerance or not isfinite(sample_variance):
        raise ValueError("returns variance must be finite and positive")
    sample_deviation = sqrt(sample_variance)
    observed_period_sharpe = mean / sample_deviation
    annualizer = sqrt(periods_per_year)
    observed_annual = observed_period_sharpe * annualizer
    expected_annual = _expected_maximum_null_sharpe(trials)
    threshold_period = expected_annual / annualizer
    skewness, kurtosis = _standardized_moments(values, mean)
    variance_term = (
        1.0
        - skewness * observed_period_sharpe
        + ((kurtosis - 1.0) / 4.0) * observed_period_sharpe**2
    )
    if variance_term <= 0 or not isfinite(variance_term):
        raise ValueError("DSR variance correction must be finite and positive")
    statistic = (
        (observed_period_sharpe - threshold_period)
        * sqrt(len(values) - 1)
        / sqrt(variance_term)
    )
    confidence = NormalDist().cdf(statistic)
    return DeflatedSharpeResult(
        confidence=confidence,
        test_statistic=statistic,
        observed_sharpe_annual=observed_annual,
        expected_max_sharpe_annual=expected_annual,
        skewness=skewness,
        kurtosis=kurtosis,
        observations=len(values),
        trial_count=len(trials),
    )


@dataclass(frozen=True, slots=True)
class CSCVPartition:
    """A fold count plus the observations that had to be dropped to reach it."""

    folds: int
    dropped_observations: int
    observations: int
    trial_returns: Mapping[str, tuple[float, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "trial_returns",
            MappingProxyType(dict(sorted(self.trial_returns.items()))),
        )


DEFAULT_CSCV_FOLD_PREFERENCE = (16, 12, 10, 8, 6, 4, 2)


def cscv_partition(
    trial_returns: Mapping[str, Sequence[float]],
    *,
    fold_preference: Sequence[int] = DEFAULT_CSCV_FOLD_PREFERENCE,
    min_fold_size: int = 2,
) -> CSCVPartition | None:
    """Pick a CSCV fold count, trimming the remainder rather than giving up.

    ``cscv_probability_of_backtest_overfitting`` requires the observation count
    to divide exactly into equal folds.  Demanding that the raw sample already
    divides evenly makes the PBO gate disappear on roughly half of all runs,
    because an odd session count has no even divisor at all.  A gate that
    switches off based on the parity of the session count is not a gate.

    CSCV drops the remainder by construction, so this trims the oldest
    ``observations % folds`` rows and reports exactly how many were dropped.
    Returns ``None`` only when no fold count can produce usable folds.
    """

    matrix = _aligned_trial_matrix(trial_returns)
    observations = len(next(iter(matrix.values())))
    for folds in fold_preference:
        if isinstance(folds, bool) or not isinstance(folds, int) or folds < 2 or folds % 2:
            raise ValueError("fold_preference must contain even integers of at least two")
        trimmed = observations - observations % folds
        if trimmed and trimmed // folds >= min_fold_size:
            dropped = observations - trimmed
            return CSCVPartition(
                folds=folds,
                dropped_observations=dropped,
                observations=trimmed,
                trial_returns={
                    identifier: tuple(values[dropped:])
                    for identifier, values in matrix.items()
                },
            )
    return None


def cscv_probability_of_backtest_overfitting(
    trial_returns: Mapping[str, Sequence[float]],
    *,
    folds: int = 8,
) -> ProbabilityOfBacktestOverfittingResult:
    """Estimate PBO from symmetric IS/OOS combinations and OOS rank logits."""

    matrix = _aligned_trial_matrix(trial_returns)
    if isinstance(folds, bool) or not isinstance(folds, int) or folds < 2 or folds % 2:
        raise ValueError("folds must be an even integer of at least two")
    observations = len(next(iter(matrix.values())))
    if observations % folds:
        raise ValueError("aligned observations must divide into equal folds")
    fold_size = observations // folds
    if fold_size < 2:
        raise ValueError("each CSCV fold requires at least two observations")
    identifiers = tuple(matrix)
    fold_indexes = tuple(
        tuple(range(index * fold_size, (index + 1) * fold_size))
        for index in range(folds)
    )
    logits = []
    selected = []
    for train_fold_ids in combinations(range(folds), folds // 2):
        train_set = set(train_fold_ids)
        train_indexes = tuple(
            row for fold in train_fold_ids for row in fold_indexes[fold]
        )
        test_indexes = tuple(
            row
            for fold in range(folds)
            if fold not in train_set
            for row in fold_indexes[fold]
        )
        train_scores = {
            identifier: _subsample_sharpe(matrix[identifier], train_indexes)
            for identifier in identifiers
        }
        selected_id = max(identifiers, key=lambda item: (train_scores[item], item))
        test_scores = {
            identifier: _subsample_sharpe(matrix[identifier], test_indexes)
            for identifier in identifiers
        }
        rank = _ascending_average_rank(test_scores, selected_id)
        relative_rank = rank / (len(identifiers) + 1.0)
        logits.append(log(relative_rank / (1.0 - relative_rank)))
        selected.append(selected_id)
    probability = sum(value <= 0.0 for value in logits) / len(logits)
    return ProbabilityOfBacktestOverfittingResult(
        probability=probability,
        logits=tuple(logits),
        selected_strategy_ids=tuple(selected),
        combination_count=len(logits),
        folds=folds,
        strategy_count=len(identifiers),
    )


def _expected_maximum_null_sharpe(trials: tuple[float, ...]) -> float:
    if len(trials) == 1:
        return 0.0
    mean = fsum(trials) / len(trials)
    variance = fsum((value - mean) ** 2 for value in trials) / (len(trials) - 1)
    if variance == 0:
        return 0.0
    deviation = sqrt(variance)
    normal = NormalDist()
    count = len(trials)
    expected_max_z = (
        (1.0 - EULER_MASCHERONI) * normal.inv_cdf(1.0 - 1.0 / count)
        + EULER_MASCHERONI
        * normal.inv_cdf(1.0 - 1.0 / (count * exp(1.0)))
    )
    return deviation * expected_max_z


def _standardized_moments(values, mean):
    population_variance = fsum((value - mean) ** 2 for value in values) / len(values)
    deviation = sqrt(population_variance)
    skewness = fsum(((value - mean) / deviation) ** 3 for value in values) / len(values)
    kurtosis = fsum(((value - mean) / deviation) ** 4 for value in values) / len(values)
    return skewness, kurtosis


def _finite_series(values, name, *, minimum_length):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a numeric sequence")
    try:
        result = tuple(float(value) for value in values)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must contain finite numbers") from error
    if len(result) < minimum_length or any(not isfinite(value) for value in result):
        raise ValueError(
            f"{name} must contain at least {minimum_length} finite numbers"
        )
    return result


def _aligned_trial_matrix(values):
    if not isinstance(values, Mapping) or len(values) < 2:
        raise ValueError("trial_returns must contain at least two aligned strategies")
    matrix = {}
    for identifier, returns in values.items():
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("trial strategy IDs must be non-empty strings")
        normalized = identifier.strip()
        if normalized in matrix:
            raise ValueError("trial strategy IDs must be unique")
        matrix[normalized] = _finite_series(returns, "trial returns", minimum_length=4)
    lengths = {len(series) for series in matrix.values()}
    if len(lengths) != 1:
        raise ValueError("trial return series must be aligned and equal length")
    return MappingProxyType(dict(sorted(matrix.items())))


def _subsample_sharpe(values, indexes):
    selected = tuple(values[index] for index in indexes)
    mean = fsum(selected) / len(selected)
    variance = fsum((value - mean) ** 2 for value in selected) / (len(selected) - 1)
    if variance == 0:
        if mean > 0:
            return float("inf")
        if mean < 0:
            return float("-inf")
        return 0.0
    return mean / sqrt(variance)


def _ascending_average_rank(scores, selected_id):
    selected_score = scores[selected_id]
    lower = sum(value < selected_score for value in scores.values())
    equal = sum(value == selected_score for value in scores.values())
    return lower + (equal + 1.0) / 2.0


__all__ = (
    "CSCVPartition",
    "DEFAULT_CSCV_FOLD_PREFERENCE",
    "DeflatedSharpeResult",
    "ProbabilityOfBacktestOverfittingResult",
    "cscv_partition",
    "cscv_probability_of_backtest_overfitting",
    "deflated_sharpe_ratio",
)
