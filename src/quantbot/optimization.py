"""Walk-forward window generation."""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Sequence


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date


@dataclass(frozen=True)
class QuarterlyRetrainWindow:
    train_start: date
    train_end: date
    prediction_start: date
    prediction_end: date


@dataclass(frozen=True)
class OptimizedFactorWeights:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    weights: Dict[str, float]
    training_dates: Sequence[date]


def generate_walk_forward_windows(
    train_start: date,
    first_validation_start: date,
    final_validation_end: date,
    validation_months: int = 3,
    step_months: int = 3,
) -> List[WalkForwardWindow]:
    windows = []
    validation_start = first_validation_start

    while validation_start <= final_validation_end:
        validation_end = min(
            _add_months(validation_start, validation_months) - timedelta(days=1),
            final_validation_end,
        )
        windows.append(
            WalkForwardWindow(
                train_start=train_start,
                train_end=validation_start - timedelta(days=1),
                validation_start=validation_start,
                validation_end=validation_end,
            )
        )
        validation_start = _add_months(validation_start, step_months)

    return windows


def optimize_walk_forward_factor_weights(
    factor_ic_by_date: Mapping[date, Mapping[str, float]],
    windows: Sequence[WalkForwardWindow],
) -> List[OptimizedFactorWeights]:
    optimized = []
    for window in windows:
        training_rows = {
            signal_date: factor_values
            for signal_date, factor_values in factor_ic_by_date.items()
            if window.train_start <= signal_date <= window.train_end
        }
        factor_names = sorted(
            {
                factor_name
                for factor_values in training_rows.values()
                for factor_name in factor_values
            }
        )
        mean_ic = {
            factor_name: _mean(
                [
                    float(factor_values[factor_name])
                    for factor_values in training_rows.values()
                    if factor_name in factor_values
                ]
            )
            for factor_name in factor_names
        }
        positive_scores = {
            factor_name: max(0.0, value)
            for factor_name, value in mean_ic.items()
        }
        weights = _normalize(positive_scores, include_zero_keys=factor_names)
        if factor_names and not any(weights.values()):
            weights = {factor_name: 1.0 / len(factor_names) for factor_name in factor_names}
        optimized.append(
            OptimizedFactorWeights(
                train_start=window.train_start,
                train_end=window.train_end,
                validation_start=window.validation_start,
                validation_end=window.validation_end,
                weights=weights,
                training_dates=tuple(sorted(training_rows)),
            )
        )
    return optimized


def generate_quarterly_retrain_windows(
    first_prediction_start: date,
    final_prediction_end: date,
    training_years: int = 4,
    prediction_months: int = 3,
    step_months: int = 3,
) -> List[QuarterlyRetrainWindow]:
    windows = []
    prediction_start = first_prediction_start
    training_months = training_years * 12

    while prediction_start <= final_prediction_end:
        prediction_end = min(
            _add_months(prediction_start, prediction_months) - timedelta(days=1),
            final_prediction_end,
        )
        windows.append(
            QuarterlyRetrainWindow(
                train_start=_add_months(prediction_start, -training_months),
                train_end=prediction_start - timedelta(days=1),
                prediction_start=prediction_start,
                prediction_end=prediction_end,
            )
        )
        prediction_start = _add_months(prediction_start, step_months)

    return windows


def inverse_volatility_weights(volatilities: Mapping[str, float]) -> Dict[str, float]:
    scores = {
        ticker: (1.0 / float(volatility))
        for ticker, volatility in volatilities.items()
        if float(volatility) > 0
    }
    return _normalize(scores, include_zero_keys=volatilities)


def mean_variance_score_weights(
    expected_returns: Mapping[str, float],
    variances: Mapping[str, float],
) -> Dict[str, float]:
    tickers = set(expected_returns).union(variances)
    scores = {}
    for ticker in tickers:
        variance = float(variances.get(ticker, 0.0))
        expected_return = float(expected_returns.get(ticker, 0.0))
        scores[ticker] = max(expected_return / variance, 0.0) if variance > 0 else 0.0
    return _normalize(scores, include_zero_keys=tickers)


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


def _normalize(scores: Mapping[str, float], include_zero_keys) -> Dict[str, float]:
    total = sum(scores.values())
    if total <= 0:
        return {ticker: 0.0 for ticker in include_zero_keys}
    return {
        ticker: float(scores.get(ticker, 0.0)) / total
        for ticker in include_zero_keys
    }


def _mean(values) -> float:
    return sum(values) / len(values) if values else 0.0
