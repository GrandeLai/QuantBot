"""Factor evaluation gates and cross-sectional IC helpers."""

from dataclasses import dataclass
from math import isfinite
from typing import Mapping


@dataclass(frozen=True)
class FactorMetrics:
    name: str
    ic_mean: float
    icir: float
    ic_positive_rate: float
    long_short_return_annual: float
    decay_days: int
    oos_ic: float
    turnover_annual: float = 0.0


def passes_admission(
    metrics: FactorMetrics | None,
    min_ic_mean: float = 0.025,
    min_icir: float = 0.35,
    min_ic_positive_rate: float = 0.54,
    min_long_short_return_annual: float = 0.08,
    min_decay_days: int = 3,
    min_oos_ic: float = 0.02,
    max_turnover_annual: float = 10.0,
) -> bool:
    """Apply the US daily-factor admission standards from the design."""

    if metrics is None:
        return False
    values = (
        metrics.ic_mean, metrics.icir, metrics.ic_positive_rate,
        metrics.long_short_return_annual, metrics.decay_days,
        metrics.oos_ic, metrics.turnover_annual,
    )
    if any(isinstance(value, bool) or not isfinite(value) for value in values):
        return False
    return (
        -1 <= metrics.ic_mean <= 1
        and -1 <= metrics.oos_ic <= 1
        and 0 <= metrics.ic_positive_rate <= 1
        and metrics.turnover_annual >= 0
        and isinstance(metrics.decay_days, int)
        and metrics.ic_mean >= min_ic_mean
        and metrics.icir >= min_icir
        and metrics.ic_positive_rate >= min_ic_positive_rate
        and metrics.long_short_return_annual >= min_long_short_return_annual
        and metrics.decay_days >= min_decay_days
        and metrics.oos_ic >= min_oos_ic
        and metrics.turnover_annual <= max_turnover_annual
    )


def pearson_ic(scores: Mapping[str, float], future_returns: Mapping[str, float]) -> float | None:
    """Cross-sectional IC; fewer than three assets or undefined correlation is unavailable."""

    tickers = sorted(set(scores).intersection(future_returns))
    if len(tickers) < 3:
        return None

    x_values = [float(scores[ticker]) for ticker in tickers]
    y_values = [float(future_returns[ticker]) for ticker in tickers]
    if not all(isfinite(value) for value in x_values + y_values):
        return None
    return _pearson(x_values, y_values)


def _pearson(x_values, y_values) -> float | None:
    x_mean = sum(x_values) / len(x_values)
    y_mean = sum(y_values) / len(y_values)
    numerator = sum(
        (x_value - x_mean) * (y_value - y_mean)
        for x_value, y_value in zip(x_values, y_values)
    )
    x_var = sum((x_value - x_mean) ** 2 for x_value in x_values)
    y_var = sum((y_value - y_mean) ** 2 for y_value in y_values)
    denominator = (x_var * y_var) ** 0.5
    if not denominator or not isfinite(denominator):
        return None
    return max(-1.0, min(1.0, numerator / denominator))
