import copy
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.evidence import hash_payload
from quantbot.experiment_execution import ExperimentExecutionIdentity
from quantbot.exposure import ExposureLedger
from quantbot.formal_artifact import verify_canonical_execution_artifact
from quantbot.normal.execution_artifact import (
    build_normal_canonical_execution_artifact,
    normal_cost_model_payload,
)
from quantbot.normal.experiments import (
    NORMAL_H1_EXPERIMENT_ID,
    NORMAL_H2_EXPERIMENT_ID,
)
from quantbot.normal.historical_trials import NormalAdaptiveValidationArtifact
from quantbot.validation_protocol import ValidationWindowManifest


def _arm(*, first_return=0.01, second_return=0.02, cost_bps=5.0):
    initial = 100_000.0
    first_equity = initial * (1.0 + first_return)
    second_equity = first_equity * (1.0 + second_return)
    first_turnover = 0.25
    first_traded_notional = 2.0 * first_turnover * initial
    second_turnover = 0.10
    second_traded_notional = 2.0 * second_turnover * first_equity
    cost_rate = cost_bps / 10_000.0
    first_pnl = first_equity - initial
    second_pnl = second_equity - first_equity
    return SimpleNamespace(
        initial_equity=initial,
        # A deliberately unrelated summary proves the raw artifact is sourced
        # from ledger points rather than reverse-filled from report metrics.
        summary=SimpleNamespace(annual_return=99.0),
        points=(
            SimpleNamespace(
                trading_date=date(2026, 8, 3),
                portfolio_return=first_return,
                benchmark_return=0.003,
                equity=first_equity,
                fees=first_traded_notional * cost_rate,
                turnover=first_turnover,
                pre_trade_equity=initial,
                traded_notional=first_traded_notional,
                weights={"SEC-B": 0.3, "SEC-A": 0.6},
                security_pnl={"SEC-B": -10.0, "SEC-A": first_pnl + 10.0},
            ),
            SimpleNamespace(
                trading_date=date(2026, 8, 4),
                portfolio_return=second_return,
                benchmark_return=-0.002,
                equity=second_equity,
                fees=second_traded_notional * cost_rate,
                turnover=second_turnover,
                pre_trade_equity=first_equity,
                traded_notional=second_traded_notional,
                weights={"SEC-A": 0.7},
                security_pnl={"SEC-A": second_pnl},
            ),
        ),
    )


def _adaptive_validation():
    artifact = create_autospec(NormalAdaptiveValidationArtifact, instance=True)
    artifact.content_hash = "a" * 64
    artifact.observation_dates = tuple(
        date(2021, 1, day) for day in range(4, 8)
    )
    artifact.h1_daily_returns = (0.01, -0.002, 0.007, 0.003)
    artifact.h2_daily_returns = (0.004, -0.003, 0.002, 0.001)
    return artifact


class NormalExecutionArtifactTest(unittest.TestCase):
    def test_raw_series_and_adaptive_trials_are_serialized_from_sources(self):
        cost_payload = normal_cost_model_payload(
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
        )
        adaptive = _adaptive_validation()
        artifact = build_normal_canonical_execution_artifact(
            experiment_id=NORMAL_H1_EXPERIMENT_ID,
            baseline=_arm(first_return=0.001, second_return=0.002),
            candidate=_arm(),
            stress_candidate=_arm(
                first_return=0.008,
                second_return=0.015,
                cost_bps=10.0,
            ),
            adaptive_validation=adaptive,
            cost_model_payload=cost_payload,
            expected_cost_model_hash=hash_payload(cost_payload),
        )

        self.assertEqual(
            set(artifact),
            {
                "artifact_version",
                "experiment_id",
                "cost_model_payload",
                "baseline",
                "candidate",
                "stress_candidate",
                "statistical_observation_dates",
                "statistical_trials",
            },
        )
        first = artifact["candidate"]["points"][0]
        self.assertEqual(
            set(first),
            {
                "date",
                "portfolio_return",
                "benchmark_return",
                "equity",
                "fees",
                "turnover",
                "pre_trade_equity",
                "traded_notional",
                "weights",
                "security_pnl",
            },
        )
        self.assertEqual(first["portfolio_return"], 0.01)
        self.assertEqual(first["equity"], 101_000.0)
        self.assertEqual(first["fees"], 25.0)
        self.assertEqual(first["turnover"], 0.25)
        self.assertEqual(first["pre_trade_equity"], 100_000.0)
        self.assertEqual(first["traded_notional"], 50_000.0)
        self.assertEqual(first["weights"], {"SEC-A": 0.6, "SEC-B": 0.3})
        self.assertEqual(
            first["security_pnl"], {"SEC-A": 1_010.0, "SEC-B": -10.0}
        )
        self.assertNotIn("summary", artifact["candidate"])
        self.assertEqual(
            artifact["statistical_observation_dates"],
            [value.isoformat() for value in adaptive.observation_dates],
        )
        self.assertEqual(
            artifact["statistical_trials"],
            {
                NORMAL_H1_EXPERIMENT_ID: list(adaptive.h1_daily_returns),
                NORMAL_H2_EXPERIMENT_ID: list(adaptive.h2_daily_returns),
            },
        )

    def test_cost_payload_must_match_the_permit_identity(self):
        with self.assertRaisesRegex(PermissionError, "does not match"):
            build_normal_canonical_execution_artifact(
                experiment_id=NORMAL_H1_EXPERIMENT_ID,
                baseline=_arm(),
                candidate=_arm(),
                stress_candidate=_arm(cost_bps=10.0),
                adaptive_validation=_adaptive_validation(),
                cost_model_payload=normal_cost_model_payload(
                    cost_bps_per_side=5.0,
                    stress_cost_bps_per_side=10.0,
                ),
                expected_cost_model_hash="f" * 64,
            )

    def test_artifact_hash_changes_when_one_raw_point_changes(self):
        cost_payload = normal_cost_model_payload(
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
        )
        artifact = build_normal_canonical_execution_artifact(
            experiment_id=NORMAL_H1_EXPERIMENT_ID,
            baseline=_arm(),
            candidate=_arm(),
            stress_candidate=_arm(cost_bps=10.0),
            adaptive_validation=_adaptive_validation(),
            cost_model_payload=cost_payload,
            expected_cost_model_hash=hash_payload(cost_payload),
        )
        changed = copy.deepcopy(artifact)
        changed["candidate"]["points"][1]["portfolio_return"] += 0.000001

        self.assertNotEqual(hash_payload(artifact), hash_payload(changed))

    def test_generated_artifact_matches_the_formal_exact_schema(self):
        cost_payload = normal_cost_model_payload(
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
        )
        cost_hash = hash_payload(cost_payload)
        with tempfile.TemporaryDirectory() as root, patch.dict(
            os.environ,
            {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "normal-artifact-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(
                    Path(root).resolve() / "exposure.jsonl"
                ),
            },
            clear=False,
        ):
            manifest = ValidationWindowManifest(
                strategy_family="normal",
                mode="sealed",
                request_start=date(2026, 8, 1),
                request_end=date(2028, 7, 31),
                warmup_start=date(2023, 8, 1),
                warmup_end=date(2026, 7, 31),
                dataset_hash="b" * 64,
                config_hash="c" * 64,
                cost_model_hash=cost_hash,
                code_hash="d" * 64,
            )
            permit = ExposureLedger(
                Path(root).resolve() / "exposure.jsonl",
                _clock=lambda: datetime(2028, 8, 1, tzinfo=timezone.utc),
            ).claim(manifest, run_id="normal-artifact-schema")
            identity = ExperimentExecutionIdentity(
                strategy_family="normal",
                experiment_id=NORMAL_H1_EXPERIMENT_ID,
                access_mode="sealed",
                request_start=manifest.request_start,
                request_end=manifest.request_end,
                dataset_hash=manifest.dataset_hash,
                config_hash=manifest.config_hash,
                cost_model_hash=manifest.cost_model_hash,
                code_hash=manifest.code_hash,
            )
            artifact = build_normal_canonical_execution_artifact(
                experiment_id=NORMAL_H1_EXPERIMENT_ID,
                baseline=_arm(first_return=0.001, second_return=0.002),
                candidate=_arm(),
                stress_candidate=_arm(
                    first_return=0.008,
                    second_return=0.015,
                    cost_bps=10.0,
                ),
                adaptive_validation=_adaptive_validation(),
                cost_model_payload=cost_payload,
                expected_cost_model_hash=permit.cost_model_hash,
            )

            verified = verify_canonical_execution_artifact(
                artifact,
                artifact_hash=hash_payload(artifact),
                execution_identity=identity,
                permit=permit,
            )

        self.assertEqual(verified.artifact_hash, hash_payload(artifact))


if __name__ == "__main__":
    unittest.main()
