import hmac
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from hashlib import sha256
from math import fsum, sqrt
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.daytrade_validation_artifact import (
    load_daytrade_adaptive_validation_artifact,
    verify_daytrade_adaptive_validation_artifact,
)
from quantbot.evidence import hash_payload
from quantbot.experiment_reporting import CostScenario, CostSuite, ExperimentRunReport
from quantbot.statistical_validation import (
    cscv_partition,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)


ISSUER = "daytrade-adaptive-test"
KEY = b"daytrade-adaptive-test-signing-key"


def _signed_artifact(**overrides):
    observation_dates = [
        f"{year}-{month:02d}-02"
        for year in (2023, 2024)
        for month in range(1, 13)
    ]
    selected_trial_id = "daytrade-h9-rvol-reward-risk:trial-01"
    trial_daily_returns = {
        "daytrade-h3-pre-entry-confirmation:baseline": [
            0.001 + ((index % 3) - 1) * 0.0004
            for index in range(len(observation_dates))
        ],
        selected_trial_id: [
            0.012 + ((index % 4) - 1.5) * 0.001
            for index in range(len(observation_dates))
        ],
    }
    trial_daily_returns.update(
        {
            f"adaptive-prior-trial-{trial:02d}": [
                -0.003
                + trial * 0.0001
                + ((index + trial) % 5) * 0.0002
                for index in range(len(observation_dates))
            ]
            for trial in range(1, 21)
        }
    )
    def annual_sharpe(values):
        mean = fsum(values) / len(values)
        deviation = sqrt(
            fsum((value - mean) ** 2 for value in values)
            / len(values)
        )
        return mean / deviation * sqrt(252)

    dsr_confidence = deflated_sharpe_ratio(
        trial_daily_returns[selected_trial_id],
        [annual_sharpe(values) for values in trial_daily_returns.values()],
    ).confidence
    partition = cscv_partition(
        trial_daily_returns,
        fold_preference=(8, 6, 4, 2),
    )
    if partition is None:  # pragma: no cover - fixture invariant
        raise AssertionError("adaptive fixture cannot form CSCV folds")
    pbo = cscv_probability_of_backtest_overfitting(
        partition.trial_returns,
        folds=partition.folds,
    ).probability
    statistics = {
        "available": True,
        "evaluation_scope": "full_request_interval",
        "request_start": "2023-01-01",
        "request_end": "2024-12-31",
        "observation_dates": observation_dates,
        "selected_daily_returns": trial_daily_returns[selected_trial_id],
        "trial_daily_returns": trial_daily_returns,
    }
    fixed_statistics = {
        "available": True,
        "deflated_sharpe_ratio": {
            "available": True,
            "confidence": dsr_confidence,
        },
        "probability_of_backtest_overfitting": {
            "available": True,
            "probability": pbo,
        },
    }
    metrics = {
        "declared_trial_count": 21,
        "completed_trial_count": 21,
        "executed_trial_ids": ["daytrade-h9-rvol-reward-risk:trial-01"],
        "statistical_validation": statistics,
        "fixed_candidate_evaluation": {
            "passed": True,
            "statistical_validation": fixed_statistics,
        },
        "rankings": [],
        "windows": [],
    }
    cost_suite = CostSuite(
        (
            CostScenario("baseline", 5.0, "baseline"),
            CostScenario("stress_12bps", 12.0, "stress"),
        )
    )
    report = ExperimentRunReport(
        experiment_id="daytrade-h9-rvol-reward-risk",
        run_id="adaptive-h9-run",
        run_type="historical_optimization",
        observed_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
        dataset_hash="1" * 64,
        input_materialization_hash="2" * 64,
        config_hash="3" * 64,
        code_hash="4" * 64,
        cost_model_hash="5" * 64,
        cost_suite=cost_suite,
        output_hash="6" * 64,
        metrics=metrics,
        admission={
            "passed": False,
            "formal_admission_eligible": False,
            "candidate_survived": True,
            "reasons": ["access_mode_not_sealed:adaptive_development"],
            "status": "development_not_proven",
        },
    )
    unsigned = {
        "artifact_version": 1,
        "issuer": ISSUER,
        "strategy_family": "daytrade",
        "policy_version": ACCEPTANCE_POLICY_VERSION,
        "experiment_id": "daytrade-h9-rvol-reward-risk",
        "source_mode": "adaptive_development",
        "request_start": "2023-01-01",
        "request_end": "2024-12-31",
        "candidate_survived": True,
        "declared_trial_count": 21,
        "completed_trial_count": 21,
        "dsr_confidence": dsr_confidence,
        "pbo": pbo,
        "dataset_hash": report.dataset_hash,
        "input_materialization_hash": report.input_materialization_hash,
        "config_hash": report.config_hash,
        "cost_model_hash": report.cost_model_hash,
        "code_hash": report.code_hash,
        "cost_suite_hash": report.cost_suite.suite_hash,
        "output_hash": report.output_hash,
        "report_hash": report.report_hash,
        "experiment_run_report": report.to_payload(),
        **overrides,
    }
    content_hash = hash_payload(unsigned)
    return {
        **unsigned,
        "content_hash": content_hash,
        "signature": hmac.new(
            KEY,
            content_hash.encode("ascii"),
            sha256,
        ).hexdigest(),
    }


class DayTradeAdaptiveValidationArtifactTest(unittest.TestCase):
    def test_valid_signed_adaptive_report_is_frozen_for_sealed_h9(self):
        artifact = verify_daytrade_adaptive_validation_artifact(
            _signed_artifact(),
            trusted_keys={ISSUER: KEY},
        )

        self.assertEqual(artifact.completed_trial_count, 21)
        self.assertGreaterEqual(artifact.dsr_confidence, 0.95)
        self.assertLessEqual(artifact.pbo, 0.05)
        self.assertEqual(
            artifact.identity_hashes["report_hash"],
            artifact.report.report_hash,
        )

    def test_tamper_unknown_issuer_and_failed_candidate_are_rejected(self):
        tampered = _signed_artifact()
        tampered["pbo"] = 0.9
        with self.assertRaisesRegex(ValueError, "content_hash mismatch"):
            verify_daytrade_adaptive_validation_artifact(
                tampered,
                trusted_keys={ISSUER: KEY},
            )

        with self.assertRaisesRegex(ValueError, "issuer key is missing"):
            verify_daytrade_adaptive_validation_artifact(
                _signed_artifact(),
                trusted_keys={"other": KEY},
            )

        failed = _signed_artifact(candidate_survived=False)
        with self.assertRaisesRegex(ValueError, "candidate did not survive"):
            verify_daytrade_adaptive_validation_artifact(
                failed,
                trusted_keys={ISSUER: KEY},
            )

    def test_report_identity_and_thresholds_cannot_be_relaxed(self):
        mismatched = _signed_artifact(report_hash="f" * 64)
        with self.assertRaisesRegex(ValueError, "report identity mismatch"):
            verify_daytrade_adaptive_validation_artifact(
                mismatched,
                trusted_keys={ISSUER: KEY},
            )

        weak = _signed_artifact(dsr_confidence=0.90)
        with self.assertRaisesRegex(ValueError, "do not match the report"):
            verify_daytrade_adaptive_validation_artifact(
                weak,
                trusted_keys={ISSUER: KEY},
            )

    def test_loader_returns_only_signature_verified_type(self):
        import json

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "adaptive.json"
            path.write_text(json.dumps(_signed_artifact()), encoding="utf-8")
            artifact = load_daytrade_adaptive_validation_artifact(
                path,
                trusted_keys={ISSUER: KEY},
            )

        self.assertEqual(artifact.issuer, ISSUER)


if __name__ == "__main__":
    unittest.main()
