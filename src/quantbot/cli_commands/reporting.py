"""Verification and comparability checks for canonical experiment reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..experiment_reporting import ExperimentRunReport
from .context import CLIContext


def _load_report(path: str) -> ExperimentRunReport:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "experiment_run_report" in payload:
        payload = payload["experiment_run_report"]
    return ExperimentRunReport.from_payload(payload)


def _comparison_reasons(
    report: ExperimentRunReport,
    baseline: ExperimentRunReport,
) -> tuple[list[str], bool]:
    reasons: list[str] = []

    if report.experiment_id != baseline.experiment_id:
        reasons.append("experiment_id_mismatch")
    if report.dataset_hash != baseline.dataset_hash:
        reasons.append("dataset_hash_mismatch")
    if (
        report.input_materialization_hash
        != baseline.input_materialization_hash
    ):
        reasons.append("input_materialization_hash_mismatch")
    if report.config_hash != baseline.config_hash:
        reasons.append("config_hash_mismatch")
    if report.code_hash != baseline.code_hash:
        reasons.append("code_hash_mismatch")
    if report.cost_model_hash != baseline.cost_model_hash:
        reasons.append("cost_model_hash_mismatch")
    if report.cost_suite.suite_hash != baseline.cost_suite.suite_hash:
        reasons.append("cost_suite_hash_mismatch")
    return reasons, True


def _result_payload(
    *,
    report_valid: bool,
    admission_passed: bool,
    baseline_comparable: bool | None,
    reasons: list[str],
    dataset_hash_checked: bool,
    report: ExperimentRunReport | None = None,
) -> dict[str, Any]:
    payload = {
        "report_valid": report_valid,
        "admission_passed": admission_passed,
        "baseline_comparable": baseline_comparable,
        "dataset_hash_checked": dataset_hash_checked,
        "reasons": reasons,
    }
    if report is not None:
        payload.update(
            {
                "experiment_id": report.experiment_id,
                "run_id": report.run_id,
                "report_hash": report.report_hash,
                "dataset_hash": report.dataset_hash,
                "input_materialization_hash": (
                    report.input_materialization_hash
                ),
                "cost_suite_hash": report.cost_suite.suite_hash,
            }
        )
    return payload


def _validate_admission(report: ExperimentRunReport) -> list[str]:
    reasons: list[str] = []
    if type(report.admission.get("passed")) is not bool:
        reasons.append("admission_passed_not_boolean")
    formal = report.admission.get("formal_admission_eligible")
    if formal is not None and type(formal) is not bool:
        reasons.append("formal_admission_eligible_not_boolean")
    admission_reasons = report.admission.get("reasons")
    if (
        not isinstance(admission_reasons, tuple)
        or any(not isinstance(item, str) for item in admission_reasons)
    ):
        reasons.append("admission_reasons_not_string_array")
    return reasons


def _emit_result(
    payload: dict[str, Any],
    output_file: str,
    context: CLIContext,
) -> bool:
    output = json.dumps(payload, sort_keys=True) + "\n"
    if output_file:
        try:
            Path(output_file).write_text(output, encoding="utf-8")
        except OSError as error:
            payload["reasons"].append(
                f"output_file_error:{type(error).__name__}:{error}"
            )
            context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
            return False
    context.stdout.write(output)
    return True


def handle_experiment_acceptance_report(
    args: Any,
    context: CLIContext,
) -> int:
    """Verify one report and, when supplied, its baseline comparability."""

    try:
        report = _load_report(args.report_file)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        payload = _result_payload(
            report_valid=False,
            admission_passed=False,
            baseline_comparable=None,
            dataset_hash_checked=False,
            reasons=[f"report_invalid:{type(error).__name__}:{error}"],
        )
        _emit_result(payload, args.output_file, context)
        return 1

    admission_passed = report.admission.get("passed") is True
    reasons = _validate_admission(report)
    if reasons:
        payload = _result_payload(
            report_valid=False,
            admission_passed=False,
            baseline_comparable=None,
            dataset_hash_checked=True,
            reasons=reasons,
            report=report,
        )
        _emit_result(payload, args.output_file, context)
        return 1
    if not admission_passed:
        reasons.append("admission_not_passed")

    baseline_comparable: bool | None = None
    dataset_hash_checked = False
    if args.baseline_report_file:
        try:
            baseline = _load_report(args.baseline_report_file)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            baseline_comparable = False
            reasons.append(
                f"baseline_report_invalid:{type(error).__name__}:{error}"
            )
        else:
            comparison_reasons, dataset_hash_checked = _comparison_reasons(
                report,
                baseline,
            )
            reasons.extend(comparison_reasons)
            baseline_comparable = not comparison_reasons

    payload = _result_payload(
        report_valid=True,
        admission_passed=admission_passed,
        baseline_comparable=baseline_comparable,
        reasons=reasons,
        dataset_hash_checked=dataset_hash_checked,
        report=report,
    )
    emitted = _emit_result(payload, args.output_file, context)
    accepted = (
        admission_passed
        and baseline_comparable is not False
        and emitted
    )
    return 0 if accepted else 1


__all__ = ["handle_experiment_acceptance_report"]
