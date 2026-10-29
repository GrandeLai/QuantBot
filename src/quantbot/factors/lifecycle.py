"""Factor lifecycle review and RD-Agent replacement triggers."""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence


class FactorLifecycleStatus(str, Enum):
    ACTIVE = "ACTIVE"
    WATCHLIST = "WATCHLIST"
    RETIRED = "RETIRED"


@dataclass(frozen=True)
class QuarterlyIC:
    quarter: str
    ic_mean: float


@dataclass(frozen=True)
class FactorLifecycleDecision:
    factor_name: str
    status: FactorLifecycleStatus
    weak_quarters: int
    request_replacement: bool


@dataclass(frozen=True)
class FactorDecayAlert:
    factor_name: str
    ic_slope: float
    is_decaying: bool
    recommended_action: str


def review_factor_lifecycle(
    factor_name: str,
    quarterly_ic: Sequence[QuarterlyIC],
    weak_ic_threshold: float = 0.01,
) -> FactorLifecycleDecision:
    weak_quarters = 0
    for item in reversed(list(quarterly_ic)):
        if item.ic_mean < weak_ic_threshold:
            weak_quarters += 1
        else:
            break

    if weak_quarters >= 3:
        status = FactorLifecycleStatus.RETIRED
    elif weak_quarters >= 2:
        status = FactorLifecycleStatus.WATCHLIST
    else:
        status = FactorLifecycleStatus.ACTIVE

    return FactorLifecycleDecision(
        factor_name=factor_name,
        status=status,
        weak_quarters=weak_quarters,
        request_replacement=status == FactorLifecycleStatus.RETIRED,
    )


def detect_ic_decay(
    factor_name: str,
    quarterly_ic: Sequence[QuarterlyIC],
    slope_threshold: float = -0.005,
) -> FactorDecayAlert:
    values = [item.ic_mean for item in quarterly_ic]
    slope = _linear_slope(values)
    is_decaying = slope < slope_threshold
    return FactorDecayAlert(
        factor_name=factor_name,
        ic_slope=slope,
        is_decaying=is_decaying,
        recommended_action="reduce_weight_and_review" if is_decaying else "keep_active",
    )


def apply_decay_weight_adjustment(
    weights: Mapping[str, float],
    alerts: Sequence[FactorDecayAlert],
    reduction: float = 0.50,
):
    decaying = {alert.factor_name for alert in alerts if alert.is_decaying}
    return {
        factor_name: float(weight) * reduction if factor_name in decaying else float(weight)
        for factor_name, weight in weights.items()
    }


def _linear_slope(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    x_mean = (len(values) - 1) / 2
    y_mean = sum(values) / len(values)
    numerator = sum((index - x_mean) * (value - y_mean) for index, value in enumerate(values))
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    return numerator / denominator if denominator else 0.0
