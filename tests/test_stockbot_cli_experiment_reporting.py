import io
import json
import sys
import tempfile
import unittest
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.cli_commands.context import CLIContext
from quantbot.cli_commands.reporting import handle_experiment_acceptance_report
from quantbot.experiment_reporting import (
    CostScenario,
    CostSuite,
    ExperimentRunReport,
)


def _cost_suite(stress_bps=12.0):
    return CostSuite(
        (
            CostScenario("baseline-five-bps", 5.0, "baseline"),
            CostScenario("stress", stress_bps, "stress"),
        )
    )


def _report(**changes):
    values = {
        "experiment_id": "normal-h1-quality-diversification",
        "run_id": "candidate-run",
        "run_type": "adaptive_development",
        "observed_at": datetime(2026, 7, 31, tzinfo=timezone.utc),
        "input_materialization_hash": "a" * 64,
        "config_hash": "b" * 64,
        "code_hash": "c" * 64,
        "cost_model_hash": "f" * 64,
        "cost_suite": _cost_suite(),
        "output_hash": "d" * 64,
        "metrics": {"net_return": 0.08},
        "admission": {"passed": True, "reasons": []},
    }
    if "dataset_hash" in {field.name for field in fields(ExperimentRunReport)}:
        values["dataset_hash"] = "e" * 64
    values.update(changes)
    return ExperimentRunReport(**values)


def _write_report(path, report):
    Path(path).write_text(
        json.dumps(report.to_payload(), sort_keys=True),
        encoding="utf-8",
    )


class ExperimentAcceptanceReportCliTest(unittest.TestCase):
    def _run(self, report_file, *, baseline="", output_file=""):
        stdout = io.StringIO()
        code = handle_experiment_acceptance_report(
            SimpleNamespace(
                report_file=str(report_file),
                baseline_report_file=str(baseline) if baseline else "",
                output_file=str(output_file) if output_file else "",
            ),
            CLIContext(stdout=stdout),
        )
        return code, json.loads(stdout.getvalue())

    def test_valid_admitted_report_without_baseline_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            _write_report(report_file, _report())

            code, payload = self._run(report_file)

        self.assertEqual(code, 0)
        self.assertTrue(payload["report_valid"])
        self.assertTrue(payload["admission_passed"])
        self.assertIsNone(payload["baseline_comparable"])
        self.assertFalse(payload["dataset_hash_checked"])
        self.assertEqual(payload["reasons"], [])

    def test_failed_admission_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            _write_report(
                report_file,
                _report(admission={"passed": False, "reasons": ["sharpe"]}),
            )

            code, payload = self._run(report_file)

        self.assertEqual(code, 1)
        self.assertTrue(payload["report_valid"])
        self.assertFalse(payload["admission_passed"])
        self.assertEqual(payload["reasons"], ["admission_not_passed"])

    def test_baseline_comparison_allows_output_differences(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            baseline_file = Path(directory) / "baseline.json"
            _write_report(report_file, _report())
            _write_report(
                baseline_file,
                _report(
                    run_id="baseline-run",
                    output_hash="2" * 64,
                    metrics={"net_return": 0.04},
                    admission={"passed": False, "reasons": ["baseline"]},
                ),
            )

            code, payload = self._run(
                report_file,
                baseline=baseline_file,
            )

        self.assertEqual(code, 0)
        self.assertTrue(payload["baseline_comparable"])
        self.assertTrue(payload["dataset_hash_checked"])
        self.assertEqual(payload["reasons"], [])

    def test_identity_mismatches_make_baseline_incomparable(self):
        cases = (
            (
                "experiment_id",
                {"experiment_id": "normal-h2-capital-efficiency"},
                "experiment_id_mismatch",
            ),
            (
                "dataset_hash",
                {"dataset_hash": "0" * 64},
                "dataset_hash_mismatch",
            ),
            (
                "input_materialization_hash",
                {"input_materialization_hash": "1" * 64},
                "input_materialization_hash_mismatch",
            ),
            (
                "config_hash",
                {"config_hash": "4" * 64},
                "config_hash_mismatch",
            ),
            (
                "code_hash",
                {"code_hash": "2" * 64},
                "code_hash_mismatch",
            ),
            (
                "cost_model_hash",
                {"cost_model_hash": "3" * 64},
                "cost_model_hash_mismatch",
            ),
            (
                "cost_suite",
                {"cost_suite": _cost_suite(stress_bps=20.0)},
                "cost_suite_hash_mismatch",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            _write_report(report_file, _report())
            for name, changes, expected_reason in cases:
                with self.subTest(name=name):
                    baseline_file = Path(directory) / f"{name}.json"
                    _write_report(
                        baseline_file,
                        _report(run_id=f"{name}-baseline", **changes),
                    )

                    code, payload = self._run(
                        report_file,
                        baseline=baseline_file,
                    )

                    self.assertEqual(code, 1)
                    self.assertFalse(payload["baseline_comparable"])
                    self.assertIn(expected_reason, payload["reasons"])

    def test_tampered_report_is_rejected_by_strict_report_hash_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            payload = _report().to_payload()
            payload["output_hash"] = "9" * 64
            report_file.write_text(json.dumps(payload), encoding="utf-8")

            code, result = self._run(report_file)

        self.assertEqual(code, 1)
        self.assertFalse(result["report_valid"])
        self.assertFalse(result["admission_passed"])
        self.assertIn("report_hash does not match", result["reasons"][0])

    def test_wrapped_run_output_is_accepted_and_admission_schema_is_strict(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "wrapped.json"
            report_file.write_text(
                json.dumps(
                    {"experiment_run_report": _report().to_payload()}
                ),
                encoding="utf-8",
            )
            code, payload = self._run(report_file)
            self.assertEqual(code, 0)
            self.assertEqual(
                payload["experiment_id"],
                "normal-h1-quality-diversification",
            )

            invalid = _report(
                admission={"passed": 1, "reasons": ["not strict"]}
            )
            _write_report(report_file, invalid)
            code, payload = self._run(report_file)

        self.assertEqual(code, 1)
        self.assertFalse(payload["report_valid"])
        self.assertIn("admission_passed_not_boolean", payload["reasons"])

    def test_invalid_baseline_is_reported_without_invalidating_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            baseline_file = Path(directory) / "baseline.json"
            _write_report(report_file, _report())
            baseline_file.write_text("not json", encoding="utf-8")

            code, payload = self._run(
                report_file,
                baseline=baseline_file,
            )

        self.assertEqual(code, 1)
        self.assertTrue(payload["report_valid"])
        self.assertFalse(payload["baseline_comparable"])
        self.assertIn("baseline_report_invalid", payload["reasons"][0])

    def test_output_file_contains_the_same_structured_result(self):
        with tempfile.TemporaryDirectory() as directory:
            report_file = Path(directory) / "report.json"
            output_file = Path(directory) / "acceptance.json"
            _write_report(report_file, _report())

            code, stdout_payload = self._run(
                report_file,
                output_file=output_file,
            )
            file_payload = json.loads(output_file.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(file_payload, stdout_payload)

    def test_missing_report_returns_structured_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            code, payload = self._run(Path(directory) / "missing.json")

        self.assertEqual(code, 1)
        self.assertFalse(payload["report_valid"])
        self.assertIn("report_invalid:FileNotFoundError", payload["reasons"][0])


if __name__ == "__main__":
    unittest.main()
