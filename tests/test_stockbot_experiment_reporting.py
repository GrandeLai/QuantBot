import math
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.experiment_reporting import (
    CostScenario,
    CostSuite,
    ExperimentRunReport,
)


def cost_suite(*scenarios):
    return CostSuite(
        scenarios
        or (
            CostScenario("five-bps", 5, "baseline"),
            CostScenario("twelve-bps", 12, "stress"),
        )
    )


def report(**changes):
    values = {
        "experiment_id": "daytrade-h9-rvol-reward-risk",
        "run_id": "adaptive-2024-h9",
        "run_type": "adaptive_development",
        "observed_at": datetime(
            2026,
            7,
            31,
            8,
            9,
            10,
            123456,
            tzinfo=timezone(timedelta(hours=8)),
        ),
        "dataset_hash": "e" * 64,
        "input_materialization_hash": "a" * 64,
        "config_hash": "b" * 64,
        "code_hash": "c" * 64,
        "cost_model_hash": "f" * 64,
        "cost_suite": cost_suite(),
        "output_hash": "d" * 64,
        "metrics": {
            "five-bps": {"net_return": 0.03, "trades": 321},
            "windows": ["2023-Q1", "2023-Q2"],
        },
        "admission": {
            "passed": False,
            "reasons": ["insufficient_oos_months"],
        },
    }
    values.update(changes)
    return ExperimentRunReport(**values)


class ExperimentReportingTest(unittest.TestCase):
    def test_cost_suite_is_canonical_and_order_independent(self):
        first = cost_suite(
            CostScenario("twelve-bps", 12, "stress"),
            CostScenario("five-bps", 5, "baseline"),
            CostScenario("zero-bps", 0, "stress"),
        )
        second = cost_suite(
            CostScenario("zero-bps", 0.0, "stress"),
            CostScenario("twelve-bps", 12.0, "stress"),
            CostScenario("five-bps", 5.0, "baseline"),
        )

        self.assertEqual(
            [scenario.name for scenario in first.scenarios],
            ["five-bps", "twelve-bps", "zero-bps"],
        )
        self.assertEqual(first.baseline.name, "five-bps")
        self.assertEqual(
            [scenario.name for scenario in first.stress_scenarios],
            ["twelve-bps", "zero-bps"],
        )
        self.assertEqual(first.suite_hash, second.suite_hash)
        self.assertEqual(first.to_payload(), second.to_payload())
        self.assertEqual(CostSuite.from_payload(first.to_payload()), first)

    def test_cost_suite_rejects_invalid_membership_and_costs(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            CostSuite(())
        with self.assertRaisesRegex(ValueError, "unique"):
            cost_suite(
                CostScenario("same", 5, "baseline"),
                CostScenario("same", 12, "stress"),
            )
        with self.assertRaisesRegex(ValueError, "exactly one baseline"):
            cost_suite(CostScenario("stress-only", 5, "stress"))
        with self.assertRaisesRegex(ValueError, "exactly one baseline"):
            cost_suite(
                CostScenario("first", 5, "baseline"),
                CostScenario("second", 12, "baseline"),
            )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            CostScenario("negative", -0.01, "stress")
        with self.assertRaisesRegex(ValueError, "finite"):
            CostScenario("infinite", math.inf, "stress")
        with self.assertRaisesRegex(TypeError, "finite number"):
            CostScenario("boolean", True, "stress")
        with self.assertRaisesRegex(ValueError, "baseline or stress"):
            CostScenario("invalid-kind", 5, "production")

    def test_cost_suite_from_payload_detects_tampering_and_noncanonical_shape(self):
        payload = cost_suite().to_payload()
        payload["suite_hash"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "does not match"):
            CostSuite.from_payload(payload)

        payload = cost_suite().to_payload()
        payload["unknown"] = True
        with self.assertRaisesRegex(ValueError, "fields are invalid"):
            CostSuite.from_payload(payload)

    def test_report_round_trip_has_deterministic_hash_and_canonical_utc_time(self):
        original = report()
        reordered = report(
            cost_suite=cost_suite(
                CostScenario("twelve-bps", 12, "stress"),
                CostScenario("five-bps", 5, "baseline"),
            ),
            metrics={
                "windows": ["2023-Q1", "2023-Q2"],
                "five-bps": {"trades": 321, "net_return": 0.03},
            },
            admission={
                "reasons": ["insufficient_oos_months"],
                "passed": False,
            },
        )

        self.assertEqual(original.report_hash, reordered.report_hash)
        self.assertEqual(
            original.to_payload()["observed_at"],
            "2026-07-31T00:09:10.123456Z",
        )
        restored = ExperimentRunReport.from_payload(original.to_payload())
        self.assertEqual(restored, original)
        self.assertEqual(restored.to_payload(), original.to_payload())

    def test_report_and_nested_json_are_immutable_and_detached_from_inputs(self):
        metrics = {"nested": {"values": [1, 2]}}
        admission = {"passed": False, "reasons": ["drawdown"]}
        original = report(metrics=metrics, admission=admission)
        expected_hash = original.report_hash

        metrics["nested"]["values"].append(3)
        admission["reasons"].append("sharpe")

        self.assertEqual(original.metrics["nested"]["values"], (1, 2))
        self.assertEqual(original.admission["reasons"], ("drawdown",))
        self.assertEqual(original.report_hash, expected_hash)
        with self.assertRaises(TypeError):
            original.metrics["new"] = 1
        with self.assertRaises(TypeError):
            original.metrics["nested"]["new"] = 1
        with self.assertRaises(FrozenInstanceError):
            original.run_id = "different"

        payload = original.to_payload()
        payload["metrics"]["nested"]["values"].append(99)
        self.assertEqual(original.metrics["nested"]["values"], (1, 2))

    def test_report_rejects_non_json_and_non_finite_nested_values(self):
        with self.assertRaisesRegex(ValueError, "NaN or infinity"):
            report(metrics={"sharpe": math.nan})
        with self.assertRaisesRegex(ValueError, "NaN or infinity"):
            report(admission={"threshold": math.inf})
        with self.assertRaisesRegex(TypeError, "non-JSON"):
            report(metrics={"invalid": {1, 2}})
        with self.assertRaisesRegex(TypeError, "keys must be strings"):
            report(metrics={1: "invalid"})
        with self.assertRaisesRegex(TypeError, "JSON mapping"):
            report(admission=["not", "a", "mapping"])

    def test_report_requires_strict_hashes_and_aware_timestamp(self):
        for field in (
            "dataset_hash",
            "input_materialization_hash",
            "config_hash",
            "code_hash",
            "cost_model_hash",
            "output_hash",
        ):
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "lowercase SHA-256"
            ):
                report(**{field: "A" * 64})
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            report(observed_at=datetime(2026, 7, 31))

    def test_report_from_payload_detects_any_identity_or_output_tampering(self):
        original = report()
        for field, value in (
            ("run_id", "tampered-run"),
            ("output_hash", "e" * 64),
            ("metrics", {"five-bps": {"net_return": 99.0}}),
            ("admission", {"passed": True, "reasons": []}),
        ):
            with self.subTest(field=field):
                payload = original.to_payload()
                payload[field] = value
                with self.assertRaisesRegex(ValueError, "report_hash does not match"):
                    ExperimentRunReport.from_payload(payload)

        payload = original.to_payload()
        payload["observed_at"] = "2026-07-31T00:09:10.123456+00:00"
        with self.assertRaisesRegex(ValueError, "ending in Z"):
            ExperimentRunReport.from_payload(payload)

        payload = original.to_payload()
        payload["schema_version"] = True
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ExperimentRunReport.from_payload(payload)


if __name__ == "__main__":
    unittest.main()
