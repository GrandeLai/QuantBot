"""Materialize formal admission evidence from authenticated execution facts."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from typing import Mapping

from .acceptance_policy import ACCEPTANCE_POLICY_VERSION
from .audit import RequirementEvidence, load_evidence_records
from .evidence import hash_payload
from .evidence_auth import (
    _append_verified_strategy_evidence,
    _mint_canonical_verification_grant,
)
from .experiment_execution import (
    ExperimentExecutionIdentity,
    ExperimentExecutionLedger,
    canonical_experiment_execution_ledger_path,
)
from .experiment_reporting import (
    CostScenario,
    CostSuite,
    ExperimentRunReport,
)
from .exposure import ExposureLedger
from .formal_artifact import verify_canonical_execution_artifact
from .strategy_admission import (
    build_daytrade_admission_evidence,
    build_normal_admission_evidence,
)
from .trading import RunManifest


_AUTHENTICATION_FIELDS = frozenset(
    {
        "permit_record_hash",
        "execution_receipt_hash",
        "execution_result_hash",
        "execution_artifact_hash",
        "canonical_verifier_version",
        "canonical_verification_hash",
        "formal_record_hash",
    }
)
_EVIDENCE_INPUT_FIELDS = tuple(
    item.name
    for item in fields(RequirementEvidence)
    if item.init and item.name not in _AUTHENTICATION_FIELDS
)


def materialize_completed_strategy_admission(
    *,
    evidence_path: Path | str,
    exposure_ledger_path: Path | str,
    execution_ledger: ExperimentExecutionLedger,
    execution_identity: ExperimentExecutionIdentity,
    run_id: str,
    result_path: Path | str,
    _clock=None,
) -> RequirementEvidence:
    """Verify, re-evaluate, sign, and idempotently index one sealed result.

    The persisted completed result and its execution receipt are the source of
    truth.  The evidence JSONL is only a recoverable authenticated index; a
    caller-provided ``passed`` flag is never trusted.
    """

    if not isinstance(execution_ledger, ExperimentExecutionLedger):
        raise TypeError("execution_ledger must be an ExperimentExecutionLedger")
    if not isinstance(execution_identity, ExperimentExecutionIdentity):
        raise TypeError("execution_identity must be an ExperimentExecutionIdentity")
    if execution_identity.access_mode != "sealed":
        raise ValueError("formal strategy evidence requires a sealed execution")
    canonical_execution_path = canonical_experiment_execution_ledger_path(
        exposure_ledger_path
    )
    if execution_ledger.path != canonical_execution_path:
        raise PermissionError(
            "formal evidence requires the canonical experiment execution ledger"
        )
    state = execution_ledger.verify_completed(
        execution_identity,
        run_id=run_id,
        result_path=result_path,
    )
    if (
        state.result_payload is None
        or state.receipt_hash is None
        or state.result_hash is None
        or state.exit_code is None
    ):
        raise ValueError("completed execution state is incomplete")
    payload = dict(state.result_payload)
    report_payload = payload.get("experiment_run_report")
    report = ExperimentRunReport.from_payload(report_payload)
    _require_report_identity(
        payload,
        report,
        execution_identity=execution_identity,
        run_id=run_id,
    )
    permit = _verified_permit(
        payload,
        exposure_ledger_path=exposure_ledger_path,
        execution_identity=execution_identity,
        run_id=run_id,
        _clock=_clock,
    )
    verified_artifact = verify_canonical_execution_artifact(
        payload.get("canonical_execution_artifact"),
        artifact_hash=payload.get("canonical_execution_artifact_hash"),
        execution_identity=execution_identity,
        permit=permit,
    )
    _require_report_metric_projection(
        report.metrics,
        verified_artifact.report_metric_projection,
    )
    expected = _rebuild_admission_evidence(
        report,
        admission_metrics=verified_artifact.admission_metrics,
        permit=permit,
        _clock=_clock,
    )
    _require_report_admission(report, expected)
    supplied = _evidence_from_payload(payload.get("admission_evidence"))
    if admission_evidence_payload(supplied) != admission_evidence_payload(expected):
        raise PermissionError(
            "completed admission evidence does not match canonical re-evaluation"
        )
    formal_eligible = payload.get("formal_admission_eligible")
    if type(formal_eligible) is not bool or formal_eligible != expected.passed:
        raise PermissionError("completed formal admission flag is inconsistent")
    if state.exit_code != (0 if expected.passed else 1):
        raise PermissionError("execution exit code disagrees with admission decision")
    grant = _mint_canonical_verification_grant(
        admission_evidence_payload(expected),
        permit_record_hash=permit.record_hash,
        execution_receipt_hash=state.receipt_hash,
        execution_result_hash=state.result_hash,
        execution_artifact_hash=verified_artifact.artifact_hash,
    )
    signed = _append_verified_strategy_evidence(
        evidence_path,
        admission_evidence_payload(expected),
        verification_grant=grant,
    )
    for record in reversed(load_evidence_records(Path(evidence_path))):
        if record.formal_record_hash == signed["formal_record_hash"]:
            return record
    raise RuntimeError("authenticated formal evidence was not recoverable")


def _require_report_identity(
    payload: Mapping[str, object],
    report: ExperimentRunReport,
    *,
    execution_identity: ExperimentExecutionIdentity,
    run_id: str,
) -> None:
    if report.run_type != "sealed_backtest":
        raise PermissionError("formal experiment report must be a sealed_backtest")
    if report.run_id != run_id or report.experiment_id != execution_identity.experiment_id:
        raise PermissionError("experiment report run identity is inconsistent")
    if report.output_hash != hash_payload(report.to_payload()["metrics"]):
        raise PermissionError("experiment report output_hash is invalid")
    expected_suite = _expected_cost_suite(report.experiment_id)
    if report.cost_suite != expected_suite:
        raise PermissionError("formal experiment report cost suite is not canonical")
    expected = {
        "dataset_hash": execution_identity.dataset_hash,
        "config_hash": execution_identity.config_hash,
        "cost_model_hash": execution_identity.cost_model_hash,
        "code_hash": execution_identity.code_hash,
    }
    for name, value in expected.items():
        if getattr(report, name) != value or payload.get(name) != value:
            raise PermissionError(f"completed {name} is inconsistent")
    for name, value in (
        ("input_materialization_hash", report.input_materialization_hash),
        ("output_hash", report.output_hash),
        ("report_hash", report.report_hash),
        ("cost_suite_hash", report.cost_suite.suite_hash),
    ):
        if payload.get(name) != value:
            raise PermissionError(f"completed {name} does not match its report")


def _verified_permit(
    payload: Mapping[str, object],
    *,
    exposure_ledger_path: Path | str,
    execution_identity: ExperimentExecutionIdentity,
    run_id: str,
    _clock=None,
):
    record_hash = payload.get("permit_record_hash")
    if record_hash is None:
        raw_permit = payload.get("data_access_permit")
        if isinstance(raw_permit, Mapping):
            record_hash = raw_permit.get("record_hash")
    ledger = ExposureLedger(exposure_ledger_path, _clock=_clock)
    record = next(
        (
            item
            for item in ledger.verify()
            if item.record_hash == record_hash and item.outcome == "granted"
        ),
        None,
    )
    if record is None:
        raise PermissionError("formal result does not reference a granted permit")
    permit = record.to_permit()
    ledger.verify_permit(permit)
    if permit.run_id != run_id or permit.mode != "sealed":
        raise PermissionError("formal result permit run identity is invalid")
    identity = execution_identity
    if not permit.admits(
        strategy_family=identity.strategy_family,
        start=identity.request_start,
        end=identity.request_end,
        dataset_hash=identity.dataset_hash,
        config_hash=identity.config_hash,
        cost_model_hash=identity.cost_model_hash,
        code_hash=identity.code_hash,
        _clock=_clock,
    ):
        raise PermissionError("formal result permit does not admit the execution identity")
    return permit


def _rebuild_admission_evidence(
    report: ExperimentRunReport,
    *,
    admission_metrics,
    permit,
    _clock=None,
) -> RequirementEvidence:
    manifest = RunManifest(
        run_type=report.run_type,
        observed_at=report.observed_at,
        dataset_hash=report.dataset_hash,
        config_hash=report.config_hash,
        code_hash=report.code_hash,
        policy_version=ACCEPTANCE_POLICY_VERSION,
        cost_model_hash=report.cost_model_hash,
        input_materialization_hash=report.input_materialization_hash,
        output_hash=report.output_hash,
        report_hash=report.report_hash,
        cost_suite_hash=report.cost_suite.suite_hash,
        run_id=report.run_id,
        experiment_id=report.experiment_id,
    )
    if report.experiment_id == "normal-h1-quality-diversification":
        return build_normal_admission_evidence(
            admission_metrics,
            manifest,
            _clock=_clock,
        )
    if report.experiment_id == "daytrade-h9-rvol-reward-risk":
        return build_daytrade_admission_evidence(
            admission_metrics,
            manifest,
            _clock=_clock,
        )
    raise PermissionError("experiment is not an active formal strategy hypothesis")


def _expected_cost_suite(experiment_id: str) -> CostSuite:
    if experiment_id == "normal-h1-quality-diversification":
        return CostSuite(
            (
                CostScenario("baseline", 5.0, "baseline"),
                CostScenario("double_cost_stress", 10.0, "stress"),
            )
        )
    if experiment_id == "daytrade-h9-rvol-reward-risk":
        return CostSuite(
            (
                CostScenario("baseline", 5.0, "baseline"),
                CostScenario("stress_12bps", 12.0, "stress"),
            )
        )
    raise PermissionError("experiment is not an active formal strategy hypothesis")


def _require_report_metric_projection(actual, expected, *, path="metrics") -> None:
    if not isinstance(actual, Mapping):
        raise PermissionError(f"formal report {path} must be an object")
    for name, expected_value in expected.items():
        if name not in actual:
            raise PermissionError(f"formal report {path}.{name} is missing")
        actual_value = actual[name]
        selected_path = f"{path}.{name}"
        if isinstance(expected_value, Mapping):
            _require_report_metric_projection(
                actual_value,
                expected_value,
                path=selected_path,
            )
        elif isinstance(expected_value, float):
            if (
                isinstance(actual_value, bool)
                or not isinstance(actual_value, (int, float))
                or abs(float(actual_value) - expected_value)
                > max(1.0, abs(expected_value)) * 1e-10
            ):
                raise PermissionError(
                    f"formal report {selected_path} disagrees with raw execution artifact"
                )
        elif actual_value != expected_value:
            raise PermissionError(
                f"formal report {selected_path} disagrees with raw execution artifact"
            )


def _require_report_admission(
    report: ExperimentRunReport,
    expected: RequirementEvidence,
) -> None:
    admission = report.admission
    for name in ("passed", "formal_admission_eligible"):
        value = admission.get(name)
        if type(value) is not bool or value != expected.passed:
            raise PermissionError(
                f"formal report admission.{name} disagrees with canonical admission"
            )
    raw_reasons = admission.get("reasons")
    if not isinstance(raw_reasons, (list, tuple)):
        raise PermissionError("formal report admission.reasons must be an array")
    expected_reasons = (
        ()
        if expected.passed
        else tuple(expected.detail.removeprefix("failed:").split(","))
    )
    if tuple(raw_reasons) != expected_reasons:
        raise PermissionError(
            "formal report admission.reasons disagree with canonical admission"
        )


def _evidence_from_payload(payload: object) -> RequirementEvidence:
    if not isinstance(payload, Mapping):
        raise ValueError("completed result lacks canonical admission_evidence")
    values = {name: payload.get(name) for name in _EVIDENCE_INPUT_FIELDS}
    return RequirementEvidence(**values)


def admission_evidence_payload(
    evidence: RequirementEvidence,
) -> dict[str, object]:
    """Serialize canonical business fields without recorder-owned auth state."""

    if not isinstance(evidence, RequirementEvidence):
        raise TypeError("evidence must be RequirementEvidence")
    return {name: getattr(evidence, name) for name in _EVIDENCE_INPUT_FIELDS}


__all__ = (
    "admission_evidence_payload",
    "materialize_completed_strategy_admission",
)
