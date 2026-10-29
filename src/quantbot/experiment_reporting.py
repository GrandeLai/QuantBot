"""Canonical, immutable reports for comparable strategy experiments."""

from __future__ import annotations

import hmac
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

from .evidence import hash_payload


REPORT_SCHEMA_VERSION = 1
COST_SUITE_SCHEMA_VERSION = 1
_COST_KINDS = frozenset({"baseline", "stress"})
_SHA256_LENGTH = 64


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty, trimmed string")
    return value


def _sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    if normalized < 0.0:
        raise ValueError(f"{name} must be non-negative")
    return 0.0 if normalized == 0.0 else normalized


def _exact_mapping(
    payload: object,
    expected_fields: frozenset[str],
    name: str,
) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if set(payload) != expected_fields:
        raise ValueError(f"{name} fields are invalid")
    return payload


def _schema_version(value: object, name: str) -> None:
    if type(value) is not int or value != 1:
        raise ValueError(f"unsupported {name} schema version")


def _freeze_json(value: object, path: str) -> object:
    """Copy a JSON value into an immutable, canonical in-memory form."""

    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        return value
    if isinstance(value, Mapping):
        keys = tuple(value)
        if any(not isinstance(key, str) for key in keys):
            raise TypeError(f"{path} mapping keys must be strings")
        return MappingProxyType(
            {
                key: _freeze_json(value[key], f"{path}.{key}")
                for key in sorted(keys)
            }
        )
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return tuple(
            _freeze_json(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    raise TypeError(
        f"{path} contains a non-JSON value of type {type(value).__name__}"
    )


def _freeze_json_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON mapping")
    frozen = _freeze_json(value, name)
    if not isinstance(frozen, Mapping):  # pragma: no cover - defensive
        raise TypeError(f"{name} must be a JSON mapping")
    return frozen


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: _thaw_json(value[key])
            for key in sorted(value)
        }
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _timestamp_payload(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_from_payload(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("observed_at must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("observed_at must be a valid ISO-8601 timestamp") from error
    if _timestamp_payload(parsed) != value:
        raise ValueError("observed_at must use the canonical UTC representation")
    return parsed


@dataclass(frozen=True, slots=True)
class CostScenario:
    """One named per-side transaction-cost assumption."""

    name: str
    per_side_bps: float
    kind: str = "stress"

    def __post_init__(self) -> None:
        _text(self.name, "name")
        if self.kind not in _COST_KINDS:
            raise ValueError("kind must be baseline or stress")
        object.__setattr__(
            self,
            "per_side_bps",
            _nonnegative_number(self.per_side_bps, "per_side_bps"),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "per_side_bps": self.per_side_bps,
        }

    def to_payload(self) -> dict[str, object]:
        return self.canonical_payload()

    @classmethod
    def from_payload(cls, payload: object) -> "CostScenario":
        item = _exact_mapping(
            payload,
            frozenset({"kind", "name", "per_side_bps"}),
            "cost scenario",
        )
        return cls(
            name=item["name"],  # type: ignore[arg-type]
            per_side_bps=item["per_side_bps"],  # type: ignore[arg-type]
            kind=item["kind"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CostSuite:
    """A canonical set containing one baseline and zero or more stresses."""

    scenarios: tuple[CostScenario, ...]

    def __post_init__(self) -> None:
        if isinstance(self.scenarios, (str, bytes, bytearray)):
            raise TypeError("scenarios must be an iterable of CostScenario values")
        try:
            scenarios = tuple(self.scenarios)
        except TypeError as error:
            raise TypeError(
                "scenarios must be an iterable of CostScenario values"
            ) from error
        if not scenarios:
            raise ValueError("cost suite must contain at least one scenario")
        if any(not isinstance(scenario, CostScenario) for scenario in scenarios):
            raise TypeError("every cost scenario must be a CostScenario")
        names = [scenario.name for scenario in scenarios]
        if len(set(names)) != len(names):
            raise ValueError("cost scenario names must be unique")
        baselines = [
            scenario for scenario in scenarios if scenario.kind == "baseline"
        ]
        if len(baselines) != 1:
            raise ValueError("cost suite must contain exactly one baseline scenario")
        object.__setattr__(
            self,
            "scenarios",
            tuple(sorted(scenarios, key=lambda scenario: scenario.name)),
        )

    @property
    def baseline(self) -> CostScenario:
        return next(
            scenario for scenario in self.scenarios if scenario.kind == "baseline"
        )

    @property
    def stress_scenarios(self) -> tuple[CostScenario, ...]:
        return tuple(
            scenario for scenario in self.scenarios if scenario.kind == "stress"
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": COST_SUITE_SCHEMA_VERSION,
            "scenarios": [
                scenario.canonical_payload() for scenario in self.scenarios
            ],
        }

    @property
    def suite_hash(self) -> str:
        return hash_payload(self.canonical_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            **self.canonical_payload(),
            "suite_hash": self.suite_hash,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "CostSuite":
        item = _exact_mapping(
            payload,
            frozenset({"schema_version", "scenarios", "suite_hash"}),
            "cost suite",
        )
        _schema_version(item["schema_version"], "cost suite")
        scenarios_payload = item["scenarios"]
        if not isinstance(scenarios_payload, list):
            raise TypeError("cost suite scenarios must be a list")
        suite = cls(
            tuple(
                CostScenario.from_payload(scenario)
                for scenario in scenarios_payload
            )
        )
        supplied_hash = _sha256(item["suite_hash"], "suite_hash")
        if not hmac.compare_digest(supplied_hash, suite.suite_hash):
            raise ValueError("suite_hash does not match the cost suite")
        return suite


@dataclass(frozen=True, slots=True)
class ExperimentRunReport:
    """Versioned identity, metrics, and admission result for one experiment."""

    experiment_id: str
    run_id: str
    run_type: str
    observed_at: datetime
    dataset_hash: str
    input_materialization_hash: str
    config_hash: str
    code_hash: str
    cost_model_hash: str
    cost_suite: CostSuite
    output_hash: str
    metrics: Mapping[str, object]
    admission: Mapping[str, object]

    def __post_init__(self) -> None:
        for name in ("experiment_id", "run_id", "run_type"):
            _text(getattr(self, name), name)
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at must be a timezone-aware datetime")
        object.__setattr__(
            self,
            "observed_at",
            self.observed_at.astimezone(timezone.utc),
        )
        for name in (
            "dataset_hash",
            "input_materialization_hash",
            "config_hash",
            "code_hash",
            "cost_model_hash",
            "output_hash",
        ):
            _sha256(getattr(self, name), name)
        if not isinstance(self.cost_suite, CostSuite):
            raise TypeError("cost_suite must be a CostSuite")
        object.__setattr__(
            self,
            "metrics",
            _freeze_json_mapping(self.metrics, "metrics"),
        )
        object.__setattr__(
            self,
            "admission",
            _freeze_json_mapping(self.admission, "admission"),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "run_id": self.run_id,
            "run_type": self.run_type,
            "observed_at": _timestamp_payload(self.observed_at),
            "dataset_hash": self.dataset_hash,
            "input_materialization_hash": self.input_materialization_hash,
            "config_hash": self.config_hash,
            "code_hash": self.code_hash,
            "cost_model_hash": self.cost_model_hash,
            "cost_suite": self.cost_suite.to_payload(),
            "output_hash": self.output_hash,
            "metrics": _thaw_json(self.metrics),
            "admission": _thaw_json(self.admission),
        }

    @property
    def report_hash(self) -> str:
        return hash_payload(self.canonical_payload())

    def to_payload(self) -> dict[str, object]:
        return {
            **self.canonical_payload(),
            "report_hash": self.report_hash,
        }

    @classmethod
    def from_payload(cls, payload: object) -> "ExperimentRunReport":
        item = _exact_mapping(
            payload,
            frozenset(
                {
                    "schema_version",
                    "experiment_id",
                    "run_id",
                    "run_type",
                    "observed_at",
                    "dataset_hash",
                    "input_materialization_hash",
                    "config_hash",
                    "code_hash",
                    "cost_model_hash",
                    "cost_suite",
                    "output_hash",
                    "metrics",
                    "admission",
                    "report_hash",
                }
            ),
            "experiment run report",
        )
        _schema_version(item["schema_version"], "experiment report")
        report = cls(
            experiment_id=item["experiment_id"],  # type: ignore[arg-type]
            run_id=item["run_id"],  # type: ignore[arg-type]
            run_type=item["run_type"],  # type: ignore[arg-type]
            observed_at=_timestamp_from_payload(item["observed_at"]),
            dataset_hash=item["dataset_hash"],  # type: ignore[arg-type]
            input_materialization_hash=item["input_materialization_hash"],  # type: ignore[arg-type]
            config_hash=item["config_hash"],  # type: ignore[arg-type]
            code_hash=item["code_hash"],  # type: ignore[arg-type]
            cost_model_hash=item["cost_model_hash"],  # type: ignore[arg-type]
            cost_suite=CostSuite.from_payload(item["cost_suite"]),
            output_hash=item["output_hash"],  # type: ignore[arg-type]
            metrics=item["metrics"],  # type: ignore[arg-type]
            admission=item["admission"],  # type: ignore[arg-type]
        )
        supplied_hash = _sha256(item["report_hash"], "report_hash")
        if not hmac.compare_digest(supplied_hash, report.report_hash):
            raise ValueError("report_hash does not match the experiment report")
        return report


__all__ = [
    "COST_SUITE_SCHEMA_VERSION",
    "REPORT_SCHEMA_VERSION",
    "CostScenario",
    "CostSuite",
    "ExperimentRunReport",
]
