"""Pre-registration ledger that bounds strategy-search multiplicity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import prod
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .experiment_dispositions import (
    ExperimentDisposition,
    ExperimentDispositionRecord,
    fixed_experiment_disposition,
)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _components(values, name):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence")
    result = tuple(_text(value, name) for value in values)
    if not result or len(set(result)) != len(result):
        raise ValueError(f"{name} must be non-empty and unique")
    return tuple(sorted(result))


def _values(values, name):
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence of parameter values")
    result = tuple(values)
    if not result:
        raise ValueError(f"{name} must not be empty")
    try:
        unique = len(set(result)) == len(result)
    except TypeError as error:
        raise ValueError(f"{name} values must be immutable and hashable") from error
    if not unique:
        raise ValueError(f"{name} values must be unique")
    return result


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    experiment_id: str
    strategy_family: str
    kind: str
    baseline_components: tuple[str, ...]
    candidate_components: tuple[str, ...]
    added_component: str | None
    parameter_grid: Mapping[str, tuple[object, ...]]
    declared_trial_count: int
    trial_ids: tuple[str, ...]
    preregistered_at: datetime

    def __post_init__(self):
        object.__setattr__(self, "experiment_id", _text(self.experiment_id, "experiment_id"))
        family = _text(self.strategy_family, "strategy_family")
        if family not in {"normal", "daytrade", "ai_stocks", "ai_crypto", "crypto_factors"}:
            raise ValueError("unknown strategy_family")
        object.__setattr__(self, "strategy_family", family)
        if self.kind not in {"single_family", "interaction", "candidate_comparison", "factor_screen"}:
            raise ValueError("unknown experiment kind")
        _aware(self.preregistered_at, "preregistered_at")
        grid = {
            _text(name, "parameter name"): tuple(values)
            for name, values in self.parameter_grid.items()
        }
        object.__setattr__(self, "parameter_grid", MappingProxyType(dict(sorted(grid.items()))))


class ExperimentRegistry:
    def __init__(self):
        self._records: dict[str, ExperimentRecord] = {}
        self._completed_trials: set[tuple[str, str]] = set()
        self._imported_records: dict[str, ExperimentRecord] = {}
        self._imported_completed_trials: set[tuple[str, str]] = set()

    @property
    def records(self) -> tuple[ExperimentRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def total_declared_trials(self) -> int:
        return sum(record.declared_trial_count for record in self._records.values())

    @property
    def completed_trial_count(self) -> int:
        return len(self._completed_trials)

    @property
    def completed_trials(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._completed_trials))

    @property
    def known_records(self) -> tuple[ExperimentRecord, ...]:
        combined = dict(self._imported_records)
        combined.update(self._records)
        return tuple(combined[key] for key in sorted(combined))

    @property
    def known_trial_ids(self) -> tuple[str, ...]:
        return tuple(
            trial_id
            for record in self.known_records
            for trial_id in record.trial_ids
        )

    @property
    def total_known_declared_trials(self) -> int:
        return sum(record.declared_trial_count for record in self.known_records)

    @property
    def known_completed_trials(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(self._imported_completed_trials.union(self._completed_trials))
        )

    @property
    def known_completed_trial_count(self) -> int:
        return len(self.known_completed_trials)

    @property
    def global_declared_trial_count(self) -> int:
        return self.total_known_declared_trials

    @property
    def global_completed_trial_count(self) -> int:
        return self.known_completed_trial_count

    @property
    def selectable_candidate_records(self) -> tuple[ExperimentRecord, ...]:
        """Known records still eligible for their next frozen evaluation.

        This view never changes declared/completed trial accounting.  In
        particular, retired records remain present in ``known_records``.
        """

        return tuple(
            record
            for record in self.known_records
            if self.disposition(record.experiment_id).allows_candidate_selection
        )

    def disposition(self, experiment_id: str) -> ExperimentDisposition:
        """Return the governed state for a known experiment.

        A newly registered experiment without a fixed historical outcome is,
        by definition, preregistered.  Fixed outcomes are code-owned by
        :mod:`quantbot.experiment_dispositions` and cannot be changed through
        a registry instance.
        """

        identifier = _text(experiment_id, "experiment_id")
        self._known_record(identifier)
        fixed = fixed_experiment_disposition(identifier)
        if fixed is None:
            return ExperimentDisposition.PREREGISTERED
        return fixed.disposition

    def disposition_record(
        self, experiment_id: str
    ) -> ExperimentDispositionRecord:
        """Return the immutable disposition record for a known experiment."""

        identifier = _text(experiment_id, "experiment_id")
        self._known_record(identifier)
        fixed = fixed_experiment_disposition(identifier)
        if fixed is not None:
            return fixed
        return ExperimentDispositionRecord(
            experiment_id=identifier,
            disposition=ExperimentDisposition.PREREGISTERED,
            rationale="registered hypothesis has no completed outcome",
        )

    def is_candidate_selectable(self, experiment_id: str) -> bool:
        return self.disposition(experiment_id).allows_candidate_selection

    def require_candidate_selectable(self, experiment_id: str) -> ExperimentRecord:
        """Return a record or reject terminal experiments before selection."""

        identifier = _text(experiment_id, "experiment_id")
        record = self._known_record(identifier)
        disposition = self.disposition(identifier)
        if not disposition.allows_candidate_selection:
            raise ValueError(
                f"experiment {identifier} cannot be selected as a candidate: "
                f"disposition={disposition.value}"
            )
        return record

    def import_registry(self, registry: "ExperimentRegistry") -> int:
        """Import another registry for global multiplicity accounting only."""

        if not isinstance(registry, ExperimentRegistry):
            raise TypeError("registry must be an ExperimentRegistry")
        if registry is self:
            return 0
        return self.import_known_experiments(
            registry.known_records,
            completed_trials=registry.known_completed_trials,
        )

    def import_known_experiments(
        self,
        records: Iterable[ExperimentRecord],
        *,
        completed_trials: Iterable[tuple[str, str]] = (),
    ) -> int:
        """Add immutable prior experiments without changing local-run semantics."""

        incoming = tuple(records)
        staged = dict(self._imported_records)
        local_and_imported = {**staged, **self._records}
        known_trial_ids = {
            trial_id
            for known_record in local_and_imported.values()
            for trial_id in known_record.trial_ids
        }
        added = 0
        for record in incoming:
            if not isinstance(record, ExperimentRecord):
                raise TypeError("records must contain ExperimentRecord values")
            existing = local_and_imported.get(record.experiment_id)
            if existing is not None:
                if existing != record:
                    raise ValueError(
                        "known experiment_id has conflicting preregistration"
                    )
                continue
            if (
                record.declared_trial_count != len(record.trial_ids)
                or len(set(record.trial_ids)) != len(record.trial_ids)
            ):
                raise ValueError("known experiment has an inconsistent trial ledger")
            if known_trial_ids.intersection(record.trial_ids):
                raise ValueError("known experiments must have globally unique trial_ids")
            staged[record.experiment_id] = record
            local_and_imported[record.experiment_id] = record
            known_trial_ids.update(record.trial_ids)
            added += 1

        staged_completed = set(self._imported_completed_trials)
        for value in completed_trials:
            if (
                not isinstance(value, tuple)
                or len(value) != 2
                or not all(isinstance(item, str) for item in value)
            ):
                raise ValueError(
                    "completed_trials must contain (experiment_id, trial_id) tuples"
                )
            experiment_id = _text(value[0], "experiment_id")
            trial_id = _text(value[1], "trial_id")
            record = local_and_imported.get(experiment_id)
            if record is None or trial_id not in record.trial_ids:
                raise ValueError("completed trial is not declared by a known experiment")
            staged_completed.add((experiment_id, trial_id))

        self._imported_records = staged
        self._imported_completed_trials = staged_completed
        return added

    def count_known_trials(self, trial_ids: Iterable[str] | None = None) -> int:
        """Count the global declared ledger, optionally validating an exact subset."""

        known = set(self.known_trial_ids)
        if trial_ids is None:
            return len(known)
        requested = tuple(_text(value, "trial_id") for value in trial_ids)
        if len(set(requested)) != len(requested):
            raise ValueError("trial_ids must be unique")
        unknown = set(requested).difference(known)
        if unknown:
            raise ValueError("trial_ids contain trials that are not globally known")
        return len(requested)

    def register_single_family(
        self,
        *,
        experiment_id: str,
        strategy_family: str,
        baseline_components: Sequence[str],
        candidate_components: Sequence[str],
        parameter_name: str,
        parameter_values: Sequence[object],
        preregistered_at: datetime,
    ) -> ExperimentRecord:
        identifier = self._available_id(experiment_id)
        baseline = _components(baseline_components, "baseline_components")
        candidate = _components(candidate_components, "candidate_components")
        added = set(candidate).difference(baseline)
        removed = set(baseline).difference(candidate)
        if len(added) != 1 or removed:
            raise ValueError("single-family experiment must add exactly one component")
        values = _values(parameter_values, "parameter_values")
        if len(values) > 3:
            raise ValueError("a preregistered hypothesis may use at most three values")
        return self._install(
            ExperimentRecord(
                experiment_id=identifier,
                strategy_family=strategy_family,
                kind="single_family",
                baseline_components=baseline,
                candidate_components=candidate,
                added_component=next(iter(added)),
                parameter_grid={_text(parameter_name, "parameter_name"): values},
                declared_trial_count=len(values),
                trial_ids=_trial_ids(identifier, len(values)),
                preregistered_at=_aware(preregistered_at, "preregistered_at"),
            )
        )

    def register_interaction(
        self,
        *,
        experiment_id: str,
        strategy_family: str,
        components: Sequence[str],
        parameter_grid: Mapping[str, Sequence[object]],
        preregistered_at: datetime,
    ) -> ExperimentRecord:
        identifier = self._available_id(experiment_id)
        component_values = _components(components, "components")
        if not isinstance(parameter_grid, Mapping) or len(parameter_grid) < 2:
            raise ValueError("interaction parameter_grid must contain at least two parameters")
        grid = {}
        for name, values in parameter_grid.items():
            parameter = _text(name, "parameter name")
            if parameter in grid:
                raise ValueError("parameter names must be unique")
            choices = _values(values, f"parameter_grid[{parameter}]")
            if len(choices) > 3:
                raise ValueError("each preregistered parameter may use at most three values")
            grid[parameter] = choices
        count = prod(len(values) for values in grid.values())
        if count > 32:
            raise ValueError("interaction experiments are capped at 32 declared trials")
        return self._install(
            ExperimentRecord(
                experiment_id=identifier,
                strategy_family=strategy_family,
                kind="interaction",
                baseline_components=component_values,
                candidate_components=component_values,
                added_component=None,
                parameter_grid=grid,
                declared_trial_count=count,
                trial_ids=_trial_ids(identifier, count),
                preregistered_at=_aware(preregistered_at, "preregistered_at"),
            )
        )

    def register_candidates(self, *, experiment_id, market, candidate_hashes, preregistered_at):
        """Freeze an explicit candidate set; failed/retired candidates still count as trials."""
        identifier = self._available_id(experiment_id)
        if market not in {"stocks", "crypto"}:
            raise ValueError("candidate market must be stocks or crypto")
        candidates = _values(candidate_hashes, "candidate_hashes")
        if len(candidates) > 6:
            raise ValueError("candidate comparisons are capped at six trials")
        return self._install(ExperimentRecord(
            experiment_id=identifier, strategy_family="ai_" + market, kind="candidate_comparison",
            baseline_components=("cash", "rules"), candidate_components=("frozen_candidates",),
            added_component=None, parameter_grid={"candidate_hash": candidates},
            declared_trial_count=len(candidates), trial_ids=_trial_ids(identifier, len(candidates)),
            preregistered_at=_aware(preregistered_at, "preregistered_at"),
        ))

    def register_factor_screen(self, *, experiment_id, hypothesis_hashes, preregistered_at):
        """Register finite factor diagnostics separately from six-strategy comparisons."""
        identifier = self._available_id(experiment_id)
        hypotheses = _values(hypothesis_hashes, "hypothesis_hashes")
        if len(hypotheses) > 72:
            raise ValueError("factor screens are capped at 72 declared diagnostics")
        return self._install(ExperimentRecord(
            experiment_id=identifier, strategy_family="crypto_factors", kind="factor_screen",
            baseline_components=("unconditional_event_returns",), candidate_components=("frozen_factors",),
            added_component=None, parameter_grid={"hypothesis_hash": hypotheses},
            declared_trial_count=len(hypotheses), trial_ids=_trial_ids(identifier, len(hypotheses)),
            preregistered_at=_aware(preregistered_at, "preregistered_at"),
        ))

    def record_trial(self, experiment_id: str, trial_id: str) -> None:
        identifier = _text(experiment_id, "experiment_id")
        trial = _text(trial_id, "trial_id")
        record = self._records.get(identifier)
        if record is None:
            raise ValueError("experiment is not registered")
        if trial not in record.trial_ids:
            raise ValueError("unknown trial for registered experiment")
        key = (identifier, trial)
        if key in self._completed_trials:
            raise ValueError("trial was already recorded")
        self._completed_trials.add(key)

    def _available_id(self, experiment_id: str) -> str:
        identifier = _text(experiment_id, "experiment_id")
        if identifier in self._records or identifier in self._imported_records:
            raise ValueError("experiment_id is already registered")
        return identifier

    def _install(self, record: ExperimentRecord) -> ExperimentRecord:
        self._records[record.experiment_id] = record
        return record

    def _known_record(self, experiment_id: str) -> ExperimentRecord:
        record = self._records.get(experiment_id)
        if record is None:
            record = self._imported_records.get(experiment_id)
        if record is None:
            raise ValueError("experiment is not registered")
        return record


def _trial_ids(experiment_id: str, count: int) -> tuple[str, ...]:
    width = max(2, len(str(count)))
    return tuple(
        f"{experiment_id}:trial-{index:0{width}d}"
        for index in range(1, count + 1)
    )


__all__ = (
    "ExperimentDisposition",
    "ExperimentDispositionRecord",
    "ExperimentRecord",
    "ExperimentRegistry",
)
