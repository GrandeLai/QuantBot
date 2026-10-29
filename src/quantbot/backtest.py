"""Backtest analytics for factor validation."""

from dataclasses import dataclass
from datetime import date
from math import isfinite
from typing import Dict, Mapping

from .factors.evaluation import pearson_ic


@dataclass(frozen=True)
class ICSummary:
    name: str
    ic_mean: float | None
    icir: float | None
    ic_positive_rate: float | None
    observations: int = 0
    missing_observations: int = 0
    status: str = "insufficient_evidence"


@dataclass(frozen=True)
class QuantileBacktestResult:
    quantile_returns: Dict[int, float]
    long_short_return: float
    is_monotonic: bool


@dataclass(frozen=True)
class AlphaSubsetAcceptanceReport:
    passed: bool
    average_ic: float
    monotonic_rate: float
    reasons: list


@dataclass(frozen=True)
class RegimeICDifferentiationReport:
    passed: bool
    differentiated_factors: list
    ic_spread_by_factor: Dict[str, float]
    reasons: list


@dataclass(frozen=True)
class OOSBacktestWindowReport:
    passed: bool
    reasons: list


def compute_ic_series(
    factor_scores: Mapping[date, Mapping[str, float]],
    forward_returns: Mapping[date, Mapping[str, float]],
) -> Dict[date, float | None]:
    series: Dict[date, float | None] = {}
    for signal_date in sorted(set(factor_scores).intersection(forward_returns)):
        series[signal_date] = pearson_ic(factor_scores[signal_date], forward_returns[signal_date])
    return series


def summarize_ic(name: str, ic_series: Mapping[date, float | None]) -> ICSummary:
    values = [value for value in ic_series.values()
              if value is not None and isfinite(value) and -1 <= value <= 1]
    missing = len(ic_series) - len(values)
    if not values:
        return ICSummary(name, None, None, None, 0, missing)

    mean = sum(values) / len(values)
    std = _stddev(values)
    return ICSummary(
        name=name,
        ic_mean=mean,
        icir=mean / std if len(values) >= 2 and std > 1e-12 else None,
        ic_positive_rate=sum(1 for value in values if value > 0) / len(values),
        observations=len(values),
        missing_observations=missing,
        status="available" if len(values) >= 2 and std > 1e-12 else "insufficient_evidence",
    )


def evaluate_alpha_subset_acceptance(
    summaries,
    quantile_results,
    min_average_ic: float = 0.03,
    min_monotonic_rate: float = 0.60,
    min_factor_count: int = 50,
    max_factor_count: int = 70,
) -> AlphaSubsetAcceptanceReport:
    usable = [
        summary for summary in summaries
        if summary.ic_mean is not None and isfinite(summary.ic_mean)
        and getattr(summary, "status", "available") == "available"
    ]
    average_ic = (
        sum(float(summary.ic_mean) for summary in usable) / len(usable)
        if usable
        else 0.0
    )
    monotonic_rate = (
        sum(1 for result in quantile_results if result.is_monotonic) / len(quantile_results)
        if quantile_results
        else 0.0
    )
    reasons = []
    if len(usable) != len(summaries) or not usable:
        reasons.append("insufficient_ic_evidence")
    if average_ic < min_average_ic:
        reasons.append("average_ic")
    if monotonic_rate < min_monotonic_rate:
        reasons.append("monotonic_rate")
    if len(summaries) < min_factor_count or len(summaries) > max_factor_count:
        reasons.append("factor_count")
    return AlphaSubsetAcceptanceReport(
        passed=not reasons,
        average_ic=average_ic,
        monotonic_rate=monotonic_rate,
        reasons=reasons,
    )


def evaluate_regime_ic_differentiation(
    regime_ic_by_factor: Mapping[str, Mapping[str, float]],
    min_ic_spread: float = 0.04,
) -> RegimeICDifferentiationReport:
    ic_spread_by_factor = {}
    differentiated_factors = []
    for factor_name, regime_values in regime_ic_by_factor.items():
        values = [float(value) for value in regime_values.values()]
        spread = max(values) - min(values) if values else 0.0
        ic_spread_by_factor[factor_name] = spread
        if spread >= min_ic_spread:
            differentiated_factors.append(factor_name)
    reasons = [] if differentiated_factors else ["regime_ic_spread"]
    return RegimeICDifferentiationReport(
        passed=not reasons,
        differentiated_factors=sorted(differentiated_factors),
        ic_spread_by_factor=ic_spread_by_factor,
        reasons=reasons,
    )


def compute_group_ic(
    factor_scores: Mapping[date, Mapping[str, float]],
    forward_returns: Mapping[date, Mapping[str, float]],
    groups: Mapping[str, str],
) -> Dict[str, ICSummary]:
    summaries: Dict[str, ICSummary] = {}
    for group_name in sorted(set(groups.values())):
        group_scores = {
            signal_date: {
                ticker: value
                for ticker, value in scores.items()
                if groups.get(ticker) == group_name
            }
            for signal_date, scores in factor_scores.items()
        }
        group_returns = {
            signal_date: {
                ticker: value
                for ticker, value in returns.items()
                if groups.get(ticker) == group_name
            }
            for signal_date, returns in forward_returns.items()
        }
        summaries[group_name] = summarize_ic(
            group_name,
            compute_ic_series(group_scores, group_returns),
        )
    return summaries


def quintile_backtest(
    factor_scores: Mapping[date, Mapping[str, float]],
    forward_returns: Mapping[date, Mapping[str, float]],
    quantiles: int = 5,
) -> QuantileBacktestResult:
    quantile_sums = {quantile: 0.0 for quantile in range(1, quantiles + 1)}
    quantile_counts = {quantile: 0 for quantile in range(1, quantiles + 1)}

    for signal_date in sorted(set(factor_scores).intersection(forward_returns)):
        ranked = sorted(
            factor_scores[signal_date],
            key=lambda ticker: (-factor_scores[signal_date][ticker], ticker),
        )
        if not ranked:
            continue
        for index, ticker in enumerate(ranked):
            quantile = min(quantiles, int(index * quantiles / len(ranked)) + 1)
            quantile_sums[quantile] += float(forward_returns[signal_date][ticker])
            quantile_counts[quantile] += 1

    quantile_returns = {
        quantile: quantile_sums[quantile] / quantile_counts[quantile]
        if quantile_counts[quantile]
        else 0.0
        for quantile in range(1, quantiles + 1)
    }
    values_from_bottom_to_top = [
        quantile_returns[quantile]
        for quantile in range(quantiles, 0, -1)
    ]
    return QuantileBacktestResult(
        quantile_returns=quantile_returns,
        long_short_return=quantile_returns[1] - quantile_returns[quantiles],
        is_monotonic=all(
            left <= right
            for left, right in zip(values_from_bottom_to_top, values_from_bottom_to_top[1:])
        ),
    )


def turnover_rate(previous_weights: Mapping[str, float], current_weights: Mapping[str, float]) -> float:
    tickers = set(previous_weights).union(current_weights)
    return 0.5 * sum(
        abs(float(current_weights.get(ticker, 0.0)) - float(previous_weights.get(ticker, 0.0)))
        for ticker in tickers
    )


def evaluate_oos_backtest_window(
    train_start: date,
    train_end: date,
    validation_start: date,
    validation_end: date,
    required_train_start: date = date(2015, 1, 1),
    latest_train_end: date = date(2022, 12, 31),
    required_validation_start: date = date(2023, 1, 1),
    required_validation_end: date = date(2024, 12, 31),
) -> OOSBacktestWindowReport:
    reasons = []
    if train_start > required_train_start:
        reasons.append("train_start_after_2015")
    if train_end > latest_train_end:
        reasons.append("train_window_after_2022")
    if validation_start < required_validation_start:
        reasons.append("validation_start_before_2023")
    if validation_end < required_validation_end:
        reasons.append("validation_end_before_2024")
    if train_end >= validation_start:
        reasons.append("lookahead_overlap")
    return OOSBacktestWindowReport(passed=not reasons, reasons=reasons)


def _stddev(values) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5
