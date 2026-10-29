"""Point-in-time, low-entropy factor sleeves for normal trading.

The module deliberately accepts a vendor-produced total-return index instead
of turning raw closes into "total returns".  Raw closes cannot be adjusted
correctly without point-in-time split, dividend, and adjustment-vintage data.
Each factor remains an independently attributable sleeve; portfolio weighting
and experiment selection live in later layers.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from math import fsum, isfinite, log, sqrt, ulp
from hashlib import sha256
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..data.pit_domain import DataQualityTier, SecurityMasterSnapshot
from ..data.pit_evidence import _canonical_bytes, canonical_security_master_snapshot_hash
from ..data.pit_returns import (
    MonthlyReturnObservation,
    MonthlyTotalReturnIndexObservation,
    PointInTimeMonthlyReturnSnapshot,
)


DEFAULT_RESIDUAL_ESTIMATION_MONTHS = 36
MINIMUM_RESIDUAL_SIGNAL_MONTHS = 3
MINIMUM_RESIDUAL_ESTIMATION_MONTHS = 12
DEFAULT_MINIMUM_PRIOR_ABS_EPS = 0.01
_FACTOR_PROVENANCE_SEAL = object()


def _text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


def _finite(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be finite")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be finite") from error
    if not isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _digest(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _month_end(value: date, field_name: str = "month_end") -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a date")
    if value.day != calendar.monthrange(value.year, value.month)[1]:
        raise ValueError(f"{field_name} must be a calendar month end")
    return value


@dataclass(frozen=True, slots=True)
class FactorSleeve:
    """One factor family tied to one market-date interpretation and universe."""

    name: str
    observed_at: datetime
    effective_date: date
    effective_date_timezone: str
    security_master_hash: str
    scores: Mapping[str, float]
    missing_security_ids: tuple[str, ...] = ()
    input_dataset_hashes: tuple[str, ...] = ()
    data_quality_tier: DataQualityTier = DataQualityTier.DEVELOPMENT_FREE
    provenance_verified: bool = False
    security_master_admission_verified: bool = False
    factor_config_hash: str | None = None
    _provenance_seal: object = field(default=None, init=False, repr=False, compare=False)
    _provenance_hash: str | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "name"))
        _aware(self.observed_at, "observed_at")
        if not isinstance(self.effective_date, date) or isinstance(self.effective_date, datetime):
            raise TypeError("effective_date must be a date")
        timezone_name = _text(self.effective_date_timezone, "effective_date_timezone")
        try:
            effective_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("effective_date_timezone must name an installed IANA timezone") from error
        if self.observed_at.astimezone(effective_zone).date() != self.effective_date:
            raise ValueError("effective_date must match observed_at in effective_date_timezone")
        object.__setattr__(self, "effective_date_timezone", timezone_name)
        _digest(self.security_master_hash, "security_master_hash")
        scores: dict[str, float] = {}
        for security_id, score in self.scores.items():
            normalized = _text(security_id, "security_id")
            if normalized in scores:
                raise ValueError("scores contains a duplicate normalized security_id")
            scores[normalized] = _finite(score, "score")
        missing = _normalized_unique_ids(self.missing_security_ids, "missing security_id")
        if set(scores).intersection(missing):
            raise ValueError("a security cannot be both scored and missing")
        object.__setattr__(self, "scores", MappingProxyType(dict(sorted(scores.items()))))
        if self.factor_config_hash is not None:
            _digest(self.factor_config_hash, "factor_config_hash")
        object.__setattr__(self, "missing_security_ids", tuple(sorted(missing)))
        hashes = tuple(sorted({_digest(item, "input dataset hash") for item in self.input_dataset_hashes}))
        object.__setattr__(self, "input_dataset_hashes", hashes)
        if not isinstance(self.data_quality_tier, DataQualityTier):
            object.__setattr__(self, "data_quality_tier", DataQualityTier(self.data_quality_tier))
        if not isinstance(self.provenance_verified, bool):
            raise ValueError("provenance_verified must be a boolean")
        if not isinstance(self.security_master_admission_verified, bool):
            raise ValueError("security_master_admission_verified must be a boolean")
        if self.provenance_verified and (
            self._provenance_seal is not _FACTOR_PROVENANCE_SEAL
            or self._provenance_hash != _factor_sleeve_content_hash(self)
        ):
            raise ValueError("verified provenance can only be issued for unchanged factor content")

    def require_final_admission(self) -> None:
        if self.missing_security_ids:
            raise ValueError("final admission requires complete active-universe factor coverage")
        if not self.scores:
            raise ValueError("final admission requires non-empty factor scores")
        if self.factor_config_hash is None:
            raise ValueError("final admission requires a factor configuration hash")
        if self.data_quality_tier is not DataQualityTier.POINT_IN_TIME_VERIFIED:
            raise ValueError("final admission requires point-in-time verified factor inputs")
        if not self.provenance_verified:
            raise ValueError("final admission requires verified factor provenance")
        if not self.security_master_admission_verified:
            raise ValueError("final admission requires an admitted security master snapshot")
        if self._provenance_seal is not _FACTOR_PROVENANCE_SEAL:
            raise ValueError("final admission requires sealed factor provenance")
        if self._provenance_hash != _factor_sleeve_content_hash(self):
            raise ValueError("final admission factor content differs from its provenance proof")


def residual_momentum_sleeve(
    returns: PointInTimeMonthlyReturnSnapshot,
    snapshot: SecurityMasterSnapshot,
    *,
    window_months: int = 12,
    skip_recent_months: int = 1,
    estimation_window_months: int = DEFAULT_RESIDUAL_ESTIMATION_MONTHS,
) -> FactorSleeve:
    """Score recent standardized residual returns using one fixed market model.

    Alpha and beta are estimated over a longer window ending with the signal
    window.  The signal is the sum of the most recent ``window_months`` model
    residuals, scaled by estimation-window residual volatility.  This avoids
    the underidentified same-window alpha ratio used by the legacy prototype.
    """

    _require_return_snapshot(returns, snapshot)
    _validate_residual_parameters(
        window_months,
        skip_recent_months,
        estimation_window_months,
    )
    if returns.benchmark_security_id in snapshot.security_ids:
        raise ValueError("benchmark_security_id must be outside the investable snapshot")
    visible = _return_index(returns.observations)
    estimation_months = _required_months(
        snapshot.effective_date,
        estimation_window_months,
        skip_recent_months,
    )
    benchmark = _series_for(visible, returns.benchmark_security_id, estimation_months)
    candidate_ids = snapshot.security_ids
    if benchmark is None:
        return _factor_sleeve(
            snapshot,
            _residual_name(window_months, skip_recent_months),
            {},
            candidate_ids,
            returns=returns,
            configuration={
                "family": "residual_momentum",
                "window_months": window_months,
                "skip_recent_months": skip_recent_months,
                "estimation_window_months": estimation_window_months,
                "benchmark_security_id": returns.benchmark_security_id,
                "formula_version": 2,
            },
        )

    scores = {}
    missing = []
    for security_id in candidate_ids:
        values = _series_for(visible, security_id, estimation_months)
        score = (
            None
            if values is None
            else _standardized_residual_momentum(values, benchmark, window_months)
        )
        if score is None:
            missing.append(security_id)
        else:
            scores[security_id] = score
    return _factor_sleeve(
        snapshot,
        _residual_name(window_months, skip_recent_months),
        scores,
        missing,
        returns=returns,
        configuration={
            "family": "residual_momentum",
            "window_months": window_months,
            "skip_recent_months": skip_recent_months,
            "estimation_window_months": estimation_window_months,
            "benchmark_security_id": returns.benchmark_security_id,
            "formula_version": 2,
        },
    )


def build_residual_momentum_sleeves(
    returns: PointInTimeMonthlyReturnSnapshot,
    *,
    snapshot: SecurityMasterSnapshot,
    windows: Sequence[int] = (12, 6, 3),
    skip_recent_months: int = 1,
    estimation_window_months: int = DEFAULT_RESIDUAL_ESTIMATION_MONTHS,
) -> tuple[FactorSleeve, ...]:
    _require_return_snapshot(returns, snapshot)
    window_values = _validated_windows(windows)
    for window in window_values:
        _validate_residual_parameters(window, skip_recent_months, estimation_window_months)
    return tuple(
        residual_momentum_sleeve(
            returns,
            snapshot,
            window_months=window,
            skip_recent_months=skip_recent_months,
            estimation_window_months=estimation_window_months,
        )
        for window in window_values
    )


def short_term_reversal_sleeve(
    returns: PointInTimeMonthlyReturnSnapshot,
    snapshot: SecurityMasterSnapshot,
) -> FactorSleeve:
    _require_return_snapshot(returns, snapshot)
    visible = _return_index(returns.observations)
    target = _previous_month_end(snapshot.effective_date)
    scores = {}
    missing = []
    for security_id in snapshot.security_ids:
        observation = visible.get((security_id, target))
        if observation is None:
            missing.append(security_id)
        else:
            scores[security_id] = -observation.total_return
    return _factor_sleeve(
        snapshot,
        "short_term_reversal_1m",
        scores,
        missing,
        returns=returns,
        configuration={
            "family": "short_term_reversal",
            "window_months": 1,
            "target_month": target,
            "formula_version": 1,
        },
    )


def quality_sleeve(snapshot: SecurityMasterSnapshot) -> FactorSleeve:
    _require_security_master(snapshot)
    required = ("return_on_equity", "gross_margin", "debt_to_assets", "accrual_ratio")
    scores = {}
    missing = []
    for security_id in snapshot.security_ids:
        values = _fundamental_values(snapshot, security_id, required)
        if values is None:
            missing.append(security_id)
            continue
        score = _scaled_linear_combination(values, (1.0, 1.0, -1.0, -1.0), 4.0)
        if score is None:
            missing.append(security_id)
        else:
            scores[security_id] = score
    return _factor_sleeve(
        snapshot,
        "quality_profitability",
        scores,
        missing,
        configuration={
            "family": "quality_profitability",
            "required_fields": required,
            "coefficients": (1.0, 1.0, -1.0, -1.0),
            "formula_version": 1,
        },
    )


def eps_revision_sleeve(
    snapshot: SecurityMasterSnapshot,
    *,
    minimum_prior_abs_eps: float = DEFAULT_MINIMUM_PRIOR_ABS_EPS,
) -> FactorSleeve:
    _require_security_master(snapshot)
    floor = _finite(minimum_prior_abs_eps, "minimum_prior_abs_eps")
    if floor <= 0:
        raise ValueError("minimum_prior_abs_eps must be positive")
    required = ("eps_estimate_current", "eps_estimate_previous")
    scores = {}
    missing = []
    for security_id in snapshot.security_ids:
        values = _fundamental_values(snapshot, security_id, required)
        if values is None or abs(values[1]) < floor:
            missing.append(security_id)
            continue
        try:
            score = values[0] / abs(values[1]) - values[1] / abs(values[1])
        except (OverflowError, ZeroDivisionError):
            score = float("nan")
        if not isfinite(score):
            missing.append(security_id)
        else:
            scores[security_id] = score
    return _factor_sleeve(
        snapshot,
        "eps_revision",
        scores,
        missing,
        configuration={
            "family": "eps_revision",
            "minimum_prior_abs_eps": floor,
            "formula_version": 1,
        },
    )


def neutralize_sleeve(
    sleeve: FactorSleeve,
    snapshot: SecurityMasterSnapshot,
    *,
    lower_quantile: float = 0.05,
    upper_quantile: float = 0.95,
) -> FactorSleeve:
    if not isinstance(sleeve, FactorSleeve):
        raise TypeError("sleeve must be a FactorSleeve")
    _require_security_master(snapshot)
    _require_same_snapshot_time(sleeve, snapshot)
    lower = _quantile_parameter(lower_quantile, "lower_quantile")
    upper = _quantile_parameter(upper_quantile, "upper_quantile")
    if lower > upper:
        raise ValueError("lower_quantile must not exceed upper_quantile")

    eligible, missing = _neutralization_inputs(sleeve, snapshot)
    if not eligible:
        return _factor_sleeve(
            snapshot,
            sleeve.name + "_neutral",
            {},
            missing,
            inherited=sleeve,
            configuration={
                "transform": "sector_log_market_cap_neutralization",
                "lower_quantile": lower,
                "upper_quantile": upper,
                "formula_version": 2,
            },
        )
    raw = {security_id: sleeve.scores[security_id] for security_id in eligible}
    first_residuals, model_rank, scaled_raw = _project_exposures(raw, snapshot)
    if len(eligible) <= model_rank:
        return _factor_sleeve(
            snapshot,
            sleeve.name + "_neutral",
            {},
            tuple(sorted(set(missing).union(eligible))),
            inherited=sleeve,
            configuration={
                "transform": "sector_log_market_cap_neutralization",
                "lower_quantile": lower,
                "upper_quantile": upper,
                "formula_version": 2,
            },
        )
    first_normalized = _zscore(
        first_residuals,
        reference_values=tuple(scaled_raw.values()),
    )
    if not any(first_normalized.values()):
        normalized = first_normalized
    else:
        clipped = _winsorize(first_residuals, lower, upper)
        second_residuals, _second_rank, scaled_clipped = _project_exposures(
            clipped,
            snapshot,
        )
        normalized = _zscore(
            second_residuals,
            reference_values=tuple(scaled_clipped.values()),
        )
    return _factor_sleeve(
        snapshot,
        sleeve.name + "_neutral",
        normalized,
        missing,
        inherited=sleeve,
        configuration={
            "transform": "sector_log_market_cap_neutralization",
            "lower_quantile": lower,
            "upper_quantile": upper,
            "formula_version": 2,
        },
    )


def _return_index(rows: Sequence[MonthlyReturnObservation]):
    result = {}
    for row in rows:
        key = (row.security_id, row.month_end)
        if key in result:
            raise ValueError("verified monthly return snapshot contains duplicate periods")
        result[key] = row
    return result


def _series_for(visible, security_id: str, months: Sequence[date]):
    values = []
    for month in months:
        row = visible.get((security_id, month))
        if row is None:
            return None
        values.append(row.total_return)
    return tuple(values)


def _standardized_residual_momentum(
    values: Sequence[float],
    benchmark: Sequence[float],
    signal_months: int,
) -> float | None:
    normalized = _stable_market_residuals(values, benchmark)
    if normalized is None:
        return None
    residuals, normalized_values, normalized_benchmark = normalized
    degrees_of_freedom = len(residuals) - 2
    if degrees_of_freedom <= 0:
        return None
    try:
        residual_scale = sqrt(
            fsum(value * value for value in residuals) / degrees_of_freedom
        )
        signal = fsum(residuals[-signal_months:])
    except (OverflowError, ValueError):
        return None
    tolerance = _return_precision_tolerance(
        normalized_values + normalized_benchmark
    )
    if residual_scale <= tolerance:
        return 0.0 if abs(signal) <= tolerance * signal_months else None
    return signal / (residual_scale * sqrt(signal_months))


def _stable_market_residuals(
    values: Sequence[float],
    benchmark: Sequence[float],
) -> tuple[tuple[float, ...], tuple[float, ...], tuple[float, ...]] | None:
    normalized = _normalized_market_series(values, benchmark)
    if normalized is None:
        return None
    y_values, x_values = normalized
    coefficients = _fit_market_model(y_values, x_values)
    if coefficients is None:
        return None
    alpha, beta = coefficients
    residuals = _market_model_residuals(y_values, x_values, alpha, beta)
    if residuals is None:
        return None
    return residuals, y_values, x_values


def _normalized_market_series(
    values: Sequence[float],
    benchmark: Sequence[float],
) -> tuple[tuple[float, ...], tuple[float, ...]] | None:
    if len(values) != len(benchmark) or len(values) < 3:
        return None
    try:
        x_level = max(abs(value) for value in benchmark)
        y_level = max(abs(value) for value in values)
    except (OverflowError, TypeError, ValueError):
        return None
    if x_level == 0:
        return None
    try:
        x_values = tuple(value / x_level for value in benchmark)
        y_values = (
            tuple(value / y_level for value in values)
            if y_level > 0
            else tuple(0.0 for _value in values)
        )
    except (OverflowError, TypeError, ValueError):
        return None
    return y_values, x_values


def _fit_market_model(
    y_values: Sequence[float],
    x_values: Sequence[float],
) -> tuple[float, float] | None:
    count = len(y_values)
    try:
        x_mean = fsum(x_values) / count
        y_mean = fsum(y_values) / count
        x_centered = tuple(value - x_mean for value in x_values)
        y_centered = tuple(value - y_mean for value in y_values)
        denominator = fsum(value * value for value in x_centered)
        benchmark_rms = sqrt(denominator / count)
        if benchmark_rms <= _return_precision_tolerance(x_values):
            return None
        beta = fsum(
            x * y for x, y in zip(x_centered, y_centered)
        ) / denominator
        alpha = fsum(
            value - beta * market for value, market in zip(y_values, x_values)
        ) / count
    except (OverflowError, ValueError):
        return None
    if not isfinite(alpha) or not isfinite(beta):
        return None
    return alpha, beta


def _market_model_residuals(
    y_values: Sequence[float],
    x_values: Sequence[float],
    alpha: float,
    beta: float,
) -> tuple[float, ...] | None:
    try:
        residuals = tuple(
            value - alpha - beta * market
            for value, market in zip(y_values, x_values)
        )
    except (OverflowError, ValueError):
        return None
    if any(not isfinite(item) for item in residuals):
        return None
    return residuals


def _relative_tolerance(values: Sequence[float]) -> float:
    scale = max((abs(value) for value in values), default=0.0)
    if scale == 0:
        return 0.0
    return max(ulp(scale) * max(len(values), 1) * 8, scale * 1e-12)


def _return_precision_tolerance(values: Sequence[float]) -> float:
    """Account for float quantization when returns come from index-level ratios."""

    scale = max((abs(value) for value in values), default=0.0)
    return max(_relative_tolerance(values), scale * 1e-6)


def _fundamental_values(snapshot, security_id, names):
    observation = snapshot.latest_fundamentals.get(security_id)
    if observation is None or any(name not in observation.values for name in names):
        return None
    try:
        return tuple(_finite(observation.values[name], name) for name in names)
    except (OverflowError, TypeError, ValueError):
        return None


def _scaled_linear_combination(values, coefficients, divisor):
    scale = max((abs(value) for value in values), default=0.0)
    if scale == 0:
        return 0.0
    try:
        normalized = fsum(
            coefficient * (value / scale)
            for coefficient, value in zip(coefficients, values)
        ) / divisor
        result = normalized * scale
    except (OverflowError, ValueError):
        return None
    return result if isfinite(result) else None


def _neutralization_inputs(sleeve, snapshot):
    active = set(snapshot.security_ids)
    supplied = set(sleeve.scores).union(sleeve.missing_security_ids)
    if supplied.difference(active):
        raise ValueError("sleeve contains securities outside the snapshot universe")
    eligible = []
    missing = set(snapshot.security_ids).difference(sleeve.scores)
    missing.update(sleeve.missing_security_ids)
    for security_id in sleeve.scores:
        classification = snapshot.classification_by_security_id.get(security_id)
        market_cap = snapshot.latest_market_caps.get(security_id)
        if classification is None or market_cap is None or market_cap.market_cap <= 0:
            missing.add(security_id)
        else:
            eligible.append(security_id)
    return tuple(sorted(eligible)), tuple(sorted(missing))


def _within_sector_center(scores, snapshot):
    by_sector: dict[str, list[str]] = {}
    sizes = {}
    for security_id in scores:
        sector = snapshot.classification_by_security_id[security_id].sector
        by_sector.setdefault(sector, []).append(security_id)
        sizes[security_id] = log(snapshot.latest_market_caps[security_id].market_cap)
    centered_scores = {}
    centered_sizes = {}
    for security_ids in by_sector.values():
        score_mean = fsum(scores[item] for item in security_ids) / len(security_ids)
        size_mean = fsum(sizes[item] for item in security_ids) / len(security_ids)
        for security_id in security_ids:
            centered_scores[security_id] = scores[security_id] - score_mean
            centered_sizes[security_id] = sizes[security_id] - size_mean
    return centered_scores, centered_sizes, len(by_sector)


def _project_exposures(scores, snapshot):
    scale = max((abs(value) for value in scores.values()), default=0.0)
    scaled = (
        {key: value / scale for key, value in scores.items()}
        if scale > 0
        else {key: 0.0 for key in scores}
    )
    sector_centered, size_centered, sector_count = _within_sector_center(
        scaled,
        snapshot,
    )
    denominator = fsum(value * value for value in size_centered.values())
    size_rank = 1 if denominator > 0 else 0
    beta = (
        fsum(sector_centered[key] * size_centered[key] for key in scores)
        / denominator
        if size_rank
        else 0.0
    )
    residuals = {
        security_id: sector_centered[security_id] - beta * size_centered[security_id]
        for security_id in scores
    }
    return residuals, sector_count + size_rank, scaled


def _winsorize(scores, lower_quantile, upper_quantile):
    values = tuple(scores.values())
    lower = _quantile(values, lower_quantile)
    upper = _quantile(values, upper_quantile)
    return {key: min(max(value, lower), upper) for key, value in scores.items()}


def _zscore(scores, *, reference_values=()):
    mean = fsum(scores.values()) / len(scores)
    centered = {key: value - mean for key, value in scores.items()}
    variance = fsum(value * value for value in centered.values()) / len(centered)
    scale = sqrt(variance)
    tolerance = _relative_tolerance(tuple(reference_values))
    if scale <= tolerance:
        return {key: 0.0 for key in scores}
    return {key: value / scale for key, value in centered.items()}


def _quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] * (1 - fraction) + ordered[upper_index] * fraction


def _quantile_parameter(value: float, name: str) -> float:
    result = _finite(value, name)
    if result < 0 or result > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return result


def _required_months(effective_date: date, window: int, skip: int) -> tuple[date, ...]:
    end = _shift_month_end(_previous_month_end(effective_date), -skip)
    return tuple(_shift_month_end(end, offset) for offset in range(-(window - 1), 1))


def _previous_month_end(value: date) -> date:
    if value.month == 1:
        return date(value.year - 1, 12, 31)
    previous_month = value.month - 1
    return date(value.year, previous_month, calendar.monthrange(value.year, previous_month)[1])


def _shift_month_end(value: date, offset: int) -> date:
    ordinal = value.year * 12 + value.month - 1 + offset
    year, month_index = divmod(ordinal, 12)
    month = month_index + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _integer_at_least(value: int, minimum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer at least {minimum}")
    return value


def _non_negative_integer(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


def _validate_residual_parameters(window: int, skip: int, estimation: int) -> None:
    _integer_at_least(window, MINIMUM_RESIDUAL_SIGNAL_MONTHS, "window_months")
    _non_negative_integer(skip, "skip_recent_months")
    _integer_at_least(
        estimation,
        MINIMUM_RESIDUAL_ESTIMATION_MONTHS,
        "estimation_window_months",
    )
    if estimation <= window:
        raise ValueError("estimation_window_months must exceed window_months")


def _validated_windows(windows: Sequence[int]) -> tuple[int, ...]:
    if isinstance(windows, (str, bytes)):
        raise ValueError("windows must be a non-empty sequence of unique integers")
    values = tuple(windows)
    if not values:
        raise ValueError("windows must be a non-empty sequence of unique integers")
    for value in values:
        _integer_at_least(value, MINIMUM_RESIDUAL_SIGNAL_MONTHS, "window")
    if len(set(values)) != len(values):
        raise ValueError("windows must be a non-empty sequence of unique integers")
    return values


def _normalized_unique_ids(values: Iterable[str], field_name: str) -> set[str]:
    result = set()
    for value in values:
        normalized = _text(value, field_name)
        if normalized in result:
            raise ValueError(f"{field_name}s contain a duplicate normalized security_id")
        result.add(normalized)
    return result


def _residual_name(window: int, skip: int) -> str:
    return f"residual_momentum_{window}_{skip}"


def _factor_sleeve(
    snapshot,
    name,
    scores,
    missing,
    *,
    returns: PointInTimeMonthlyReturnSnapshot | None = None,
    inherited: FactorSleeve | None = None,
    configuration: Mapping[str, object] | None = None,
) -> FactorSleeve:
    if inherited is not None:
        input_hashes = inherited.input_dataset_hashes
        quality_tier = inherited.data_quality_tier
        provenance_verified = inherited.provenance_verified
    elif returns is not None:
        input_hashes = (snapshot.source_dataset_hash, returns.dataset_hash)
        quality_tier = (
            DataQualityTier.POINT_IN_TIME_VERIFIED
            if snapshot.source_quality_tier is DataQualityTier.POINT_IN_TIME_VERIFIED
            and returns.quality_tier is DataQualityTier.POINT_IN_TIME_VERIFIED
            else DataQualityTier.DEVELOPMENT_FREE
        )
        provenance_verified = returns.provenance_verified
    else:
        input_hashes = (snapshot.source_dataset_hash,)
        quality_tier = snapshot.source_quality_tier
        provenance_verified = snapshot.admission_verified
    config_payload = dict(configuration or {"family": name, "formula_version": 1})
    if inherited is not None:
        config_payload["parent_factor_config_hash"] = inherited.factor_config_hash
    factor_config_hash = sha256(_canonical_bytes(config_payload)).hexdigest()
    base = FactorSleeve(
        name=name,
        observed_at=snapshot.as_of,
        effective_date=snapshot.effective_date,
        effective_date_timezone=snapshot.effective_date_timezone,
        security_master_hash=canonical_security_master_snapshot_hash(snapshot),
        scores=scores,
        factor_config_hash=factor_config_hash,
        missing_security_ids=tuple(missing),
        input_dataset_hashes=input_hashes,
        data_quality_tier=quality_tier,
        provenance_verified=False,
        security_master_admission_verified=snapshot.admission_verified,
    )
    if not provenance_verified:
        return base
    proof_hash = _factor_sleeve_content_hash(base, provenance_verified=True)
    object.__setattr__(base, "provenance_verified", True)
    object.__setattr__(base, "_provenance_seal", _FACTOR_PROVENANCE_SEAL)
    object.__setattr__(base, "_provenance_hash", proof_hash)
    return base


def _factor_sleeve_content_hash(
    sleeve: FactorSleeve,
    *,
    provenance_verified: bool | None = None,
) -> str:
    payload = {
        value.name: getattr(sleeve, value.name)
        for value in fields(sleeve)
        if not value.name.startswith("_")
    }
    if provenance_verified is not None:
        payload["provenance_verified"] = provenance_verified
    return sha256(_canonical_bytes(payload)).hexdigest()


def _require_same_snapshot_time(sleeve, snapshot) -> None:
    if (
        sleeve.observed_at != snapshot.as_of
        or sleeve.effective_date != snapshot.effective_date
        or sleeve.effective_date_timezone != snapshot.effective_date_timezone
    ):
        raise ValueError("sleeve and snapshot must represent the same point-in-time")
    if sleeve.security_master_hash != canonical_security_master_snapshot_hash(snapshot):
        raise ValueError("sleeve snapshot provenance does not match security master")


def _require_security_master(snapshot: SecurityMasterSnapshot) -> None:
    if not isinstance(snapshot, SecurityMasterSnapshot):
        raise TypeError("snapshot must be a SecurityMasterSnapshot")


def _require_return_snapshot(returns, snapshot) -> None:
    _require_security_master(snapshot)
    if not isinstance(returns, PointInTimeMonthlyReturnSnapshot):
        raise TypeError("returns must be a verified PointInTimeMonthlyReturnSnapshot")
    if not returns.provenance_is_valid:
        raise ValueError("monthly return snapshot provenance proof is invalid")
    if (
        returns.observed_at != snapshot.as_of
        or returns.effective_date != snapshot.effective_date
        or returns.effective_date_timezone != snapshot.effective_date_timezone
        or returns.security_master_dataset_hash != snapshot.source_dataset_hash
        or returns.security_master_hash != canonical_security_master_snapshot_hash(snapshot)
    ):
        raise ValueError("monthly returns and security master snapshot provenance differ")
