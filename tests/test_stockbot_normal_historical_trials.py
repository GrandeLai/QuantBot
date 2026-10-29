import hmac
import sys
import unittest
from datetime import date, timedelta
from hashlib import sha256
from math import fsum, sqrt
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.evidence import hash_payload
from quantbot.normal import (
    NORMAL_H1_EXPERIMENT_ID,
    NORMAL_H2_EXPERIMENT_ID,
    NormalFactorExperiment,
    PointInTimeNormalRequestProvider,
    run_normal_h1_experiment,
)
from quantbot.normal.historical_trials import (
    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_VERSION,
    NORMAL_H2_AUDIT_ARTIFACT_VERSION,
    NORMAL_H2_AUDIT_PROOF_VERSION,
    NormalAdaptiveValidationArtifact,
    NormalH2HistoricalAudit,
    normal_trial_return_history_hash,
    verify_normal_adaptive_validation_artifact,
    verify_normal_h2_historical_audit,
)
from quantbot.normal.protocol import (
    NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW,
    NORMAL_RESEARCH_WINDOW,
)
from quantbot.statistical_validation import (
    cscv_partition,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)
from quantbot.validation_protocol import ValidationWindowManifest


ISSUER = "normal-trial-auditor"
KEY = b"normal-trial-auditor-test-key"
OBSERVATION_DATES = (
    date(2010, 1, 4),
    date(2010, 1, 5),
    date(2010, 1, 6),
    date(2010, 1, 7),
    date(2010, 1, 8),
    date(2010, 1, 11),
    date(2010, 1, 12),
    date(2010, 1, 13),
)
H2_RETURNS = (0.01, -0.004, 0.006, -0.002, 0.008, -0.003, 0.004, 0.001)
DATASET_HASH = "a" * 64
CONFIG_HASH = "b" * 64
COST_HASH = "c" * 64
CODE_HASH = "d" * 64
INPUT_HASH = "e" * 64
SESSION_HASH = "f" * 64
STRATEGY_PROTOCOL_HASH = "3" * 64
SOURCE_HASH = "4" * 64
ADAPTER_HASH = "5" * 64
ADAPTIVE_DATES = tuple(
    date(2021, 1, 4) + timedelta(days=index) for index in range(16)
)
ADAPTIVE_H2_RETURNS = tuple(
    (0.004 if index % 2 == 0 else -0.003) + (index % 5) * 0.0001
    for index in range(16)
)
ADAPTIVE_H1_RETURNS = tuple(value + 0.003 for value in ADAPTIVE_H2_RETURNS)


def _signed(payload):
    unsigned = dict(payload)
    unsigned.pop("content_hash", None)
    unsigned.pop("signature", None)
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


def _artifact():
    window = NORMAL_RESEARCH_WINDOW
    manifest = ValidationWindowManifest(
        strategy_family="normal",
        mode="research",
        request_start=window.request_start,
        request_end=window.request_end,
        warmup_start=window.warmup_start,
        warmup_end=window.warmup_end,
        dataset_hash=DATASET_HASH,
        config_hash=CONFIG_HASH,
        cost_model_hash=COST_HASH,
        code_hash=CODE_HASH,
    )
    date_strings = [value.isoformat() for value in OBSERVATION_DATES]
    proof = _signed(
        {
            "proof_version": NORMAL_H2_AUDIT_PROOF_VERSION,
            "issuer": ISSUER,
            "experiment_id": NORMAL_H2_EXPERIMENT_ID,
            "disposition": "completed_rejected",
            "run_id": "normal-h2-research-audit-v1",
            "run_type": "historical_audit_replay",
            "policy_version": ACCEPTANCE_POLICY_VERSION,
            "permit_mode": "research",
            "permit_request_start": window.request_start.isoformat(),
            "permit_request_end": window.request_end.isoformat(),
            "permit_warmup_start": window.warmup_start.isoformat(),
            "permit_warmup_end": window.warmup_end.isoformat(),
            "permit_actual_start": manifest.actual_start.isoformat(),
            "permit_actual_end": manifest.actual_end.isoformat(),
            "permit_claimed_at": "2026-07-30T00:00:00+00:00",
            "permit_manifest_hash": manifest.manifest_hash,
            "permit_record_hash": "1" * 64,
            "permit_dataset_hash": DATASET_HASH,
            "permit_config_hash": CONFIG_HASH,
            "permit_cost_model_hash": COST_HASH,
            "permit_code_hash": CODE_HASH,
            "execution_observed_at": "2026-07-30T01:00:00+00:00",
            "execution_evidence_hash": "2" * 64,
            "input_materialization_hash": INPUT_HASH,
            "session_hash": SESSION_HASH,
            "return_history_hash": normal_trial_return_history_hash(
                date_strings,
                H2_RETURNS,
            ),
        }
    )
    return _signed(
        {
            "artifact_version": NORMAL_H2_AUDIT_ARTIFACT_VERSION,
            "issuer": ISSUER,
            "strategy_family": "normal",
            "policy_version": ACCEPTANCE_POLICY_VERSION,
            "experiment_id": NORMAL_H2_EXPERIMENT_ID,
            "disposition": "completed_rejected",
            "observation_dates": date_strings,
            "daily_returns": list(H2_RETURNS),
            "execution_evidence": proof,
        }
    )


def _sharpe(values):
    mean = fsum(values) / len(values)
    variance = fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return mean / sqrt(variance) * sqrt(252.0)


def _adaptive_artifact():
    window = NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW
    date_strings = [value.isoformat() for value in ADAPTIVE_DATES]
    dsr = deflated_sharpe_ratio(
        ADAPTIVE_H1_RETURNS,
        (_sharpe(ADAPTIVE_H1_RETURNS), _sharpe(ADAPTIVE_H2_RETURNS)),
    )
    partition = cscv_partition(
        {
            NORMAL_H1_EXPERIMENT_ID: ADAPTIVE_H1_RETURNS,
            NORMAL_H2_EXPERIMENT_ID: ADAPTIVE_H2_RETURNS,
        }
    )
    assert partition is not None
    pbo = cscv_probability_of_backtest_overfitting(
        partition.trial_returns,
        folds=partition.folds,
    )
    return _signed(
        {
            "artifact_version": NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_VERSION,
            "issuer": ISSUER,
            "strategy_family": "normal",
            "policy_version": ACCEPTANCE_POLICY_VERSION,
            "experiment_id": NORMAL_H1_EXPERIMENT_ID,
            "source_mode": "adaptive_development",
            "request_start": window.request_start.isoformat(),
            "request_end": window.request_end.isoformat(),
            "warmup_start": window.warmup_start.isoformat(),
            "warmup_end": window.warmup_end.isoformat(),
            "candidate_survived": True,
            "declared_trials": 2,
            "completed_trials": 2,
            "dataset_hash": DATASET_HASH,
            "input_materialization_hash": INPUT_HASH,
            "strategy_protocol_hash": STRATEGY_PROTOCOL_HASH,
            "h1_config_hash": "6" * 64,
            "h2_config_hash": "7" * 64,
            "cost_model_hash": COST_HASH,
            "stockbot_source_hash": SOURCE_HASH,
            "adapter_artifact_hash": ADAPTER_HASH,
            "h1_code_hash": CODE_HASH,
            "h2_code_hash": CODE_HASH,
            "h1_report_hash": "8" * 64,
            "h2_report_hash": "9" * 64,
            "h2_audit_artifact_hash": "0" * 64,
            "observation_dates": date_strings,
            "h1_daily_returns": list(ADAPTIVE_H1_RETURNS),
            "h2_daily_returns": list(ADAPTIVE_H2_RETURNS),
            "h1_return_history_hash": normal_trial_return_history_hash(
                date_strings,
                ADAPTIVE_H1_RETURNS,
            ),
            "h2_return_history_hash": normal_trial_return_history_hash(
                date_strings,
                ADAPTIVE_H2_RETURNS,
            ),
            "dsr_confidence": dsr.confidence,
            "pbo": pbo.probability,
        }
    )


class _NoReadPort:
    def monthly_returns(self, security_master):
        raise AssertionError("backtester is patched")

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {}

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {}

    def market_betas(self, security_master, *, lookback_sessions):
        return {}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {}

    def regime_context(self, security_master):
        raise AssertionError("backtester is patched")


def _arm(returns, *, annual_return, sharpe, excess):
    return SimpleNamespace(
        daily_returns=tuple(returns),
        summary=SimpleNamespace(
            annual_return=annual_return,
            sharpe=sharpe,
            excess_alpha_annual=excess,
            max_drawdown=0.05,
        ),
        annual_turnover=1.0,
        pnl_by_security_id={"SEC-1": 1.0, "SEC-2": 1.0, "SEC-3": 1.0, "SEC-4": 1.0},
        points=tuple(
            SimpleNamespace(trading_date=day, weights={"SEC-1": 0.25})
            for day in OBSERVATION_DATES
        ),
    )


class NormalHistoricalTrialTrustTest(unittest.TestCase):
    def test_signed_h2_audit_is_verified_and_direct_construction_is_forbidden(self):
        audit = verify_normal_h2_historical_audit(
            _artifact(),
            trusted_keys={ISSUER: KEY},
        )

        self.assertEqual(audit.observation_dates, OBSERVATION_DATES)
        self.assertEqual(audit.daily_returns, H2_RETURNS)
        self.assertEqual(
            audit.execution_proof["disposition"],
            "completed_rejected",
        )
        with self.assertRaisesRegex(PermissionError, "signature verification"):
            NormalH2HistoricalAudit(
                OBSERVATION_DATES,
                H2_RETURNS,
                {},
                "a" * 64,
                ISSUER,
            )

    def test_tampered_return_history_fails_even_with_a_resigned_outer_artifact(self):
        artifact = _artifact()
        artifact["daily_returns"][0] = 0.99
        artifact = _signed(artifact)

        with self.assertRaisesRegex(ValueError, "return_history_hash"):
            verify_normal_h2_historical_audit(
                artifact,
                trusted_keys={ISSUER: KEY},
            )

    def test_h2_audit_must_match_current_h1_identity_before_claim(self):
        audit = verify_normal_h2_historical_audit(
            _artifact(),
            trusted_keys={ISSUER: KEY},
        )
        window = NORMAL_RESEARCH_WINDOW
        audit.require_compatible(
            mode="research",
            request_start=window.request_start,
            request_end=window.request_end,
            warmup_start=window.warmup_start,
            warmup_end=window.warmup_end,
            dataset_hash=DATASET_HASH,
            cost_model_hash=COST_HASH,
            code_hash=CODE_HASH,
        )
        with self.assertRaisesRegex(PermissionError, "cost_model_hash"):
            audit.require_compatible(
                mode="research",
                request_start=window.request_start,
                request_end=window.request_end,
                warmup_start=window.warmup_start,
                warmup_end=window.warmup_end,
                dataset_hash=DATASET_HASH,
                cost_model_hash="0" * 64,
                code_hash=CODE_HASH,
            )

    def test_runner_uses_current_h1_and_signed_h2_not_the_baseline(self):
        audit = verify_normal_h2_historical_audit(
            _artifact(),
            trusted_keys={ISSUER: KEY},
        )
        shared = _NoReadPort()
        baseline_provider = PointInTimeNormalRequestProvider(shared)
        candidate_provider = PointInTimeNormalRequestProvider(
            shared,
            experiment=NormalFactorExperiment(include_quality=True),
        )
        baseline = _arm(
            (0.001,) * len(OBSERVATION_DATES),
            annual_return=0.05,
            sharpe=0.5,
            excess=0.0,
        )
        h1_returns = (0.012, -0.003, 0.007, -0.001, 0.009, -0.002, 0.005, 0.002)
        candidate = _arm(
            h1_returns,
            annual_return=0.10,
            sharpe=1.5,
            excess=0.06,
        )
        partition = SimpleNamespace(
            trial_returns={
                NORMAL_H1_EXPERIMENT_ID: h1_returns,
                NORMAL_H2_EXPERIMENT_ID: H2_RETURNS,
            },
            folds=2,
            dropped_observations=0,
        )
        dsr = SimpleNamespace(confidence=0.99)
        pbo = SimpleNamespace(probability=0.01)
        with patch(
            "quantbot.normal.experiment_runner._run",
            side_effect=(baseline, candidate, baseline, candidate),
        ), patch(
            "quantbot.normal.experiment_runner.deflated_sharpe_ratio",
            return_value=dsr,
        ) as dsr_call, patch(
            "quantbot.normal.experiment_runner._normal_cscv_partition",
            return_value=partition,
        ) as partition_call, patch(
            "quantbot.normal.experiment_runner.cscv_probability_of_backtest_overfitting",
            return_value=pbo,
        ):
            result = run_normal_h1_experiment(
                baseline_provider=baseline_provider,
                candidate_provider=candidate_provider,
                sessions=(object(),),
                historical_h2_audit=audit,
                window_sessions=2,
            )

        self.assertEqual(result.completed_trials, 2)
        self.assertEqual(dsr_call.call_args.args[1][0], candidate.summary.sharpe)
        self.assertEqual(len(dsr_call.call_args.args[1]), 2)
        matrix = partition_call.call_args.args[0]
        self.assertEqual(
            set(matrix),
            {NORMAL_H1_EXPERIMENT_ID, NORMAL_H2_EXPERIMENT_ID},
        )
        self.assertNotIn("corrected-residual-momentum-baseline", matrix)

    def test_adaptive_validation_is_recomputed_and_direct_construction_is_forbidden(self):
        artifact = verify_normal_adaptive_validation_artifact(
            _adaptive_artifact(),
            trusted_keys={ISSUER: KEY},
        )

        self.assertGreaterEqual(artifact.dsr.confidence, 0.95)
        self.assertLessEqual(artifact.pbo, 0.05)
        artifact.require_compatible(
            strategy_protocol_hash=STRATEGY_PROTOCOL_HASH,
            cost_model_hash=COST_HASH,
            stockbot_source_hash=SOURCE_HASH,
            adapter_artifact_hash=ADAPTER_HASH,
        )
        with self.assertRaisesRegex(PermissionError, "signature verification"):
            NormalAdaptiveValidationArtifact(
                ADAPTIVE_DATES,
                ADAPTIVE_H1_RETURNS,
                ADAPTIVE_H2_RETURNS,
                artifact.dsr,
                artifact.pbo_result,
                "a" * 64,
                ISSUER,
                STRATEGY_PROTOCOL_HASH,
                COST_HASH,
                SOURCE_HASH,
                ADAPTER_HASH,
            )

    def test_adaptive_validation_rejects_resigned_false_statistics(self):
        payload = _adaptive_artifact()
        payload["dsr_confidence"] = 0.95
        payload = _signed(payload)

        with self.assertRaisesRegex(ValueError, "does not match returns"):
            verify_normal_adaptive_validation_artifact(
                payload,
                trusted_keys={ISSUER: KEY},
            )

    def test_adaptive_validation_identity_mismatch_fails_closed(self):
        artifact = verify_normal_adaptive_validation_artifact(
            _adaptive_artifact(),
            trusted_keys={ISSUER: KEY},
        )

        with self.assertRaisesRegex(PermissionError, "strategy_protocol_hash"):
            artifact.require_compatible(
                strategy_protocol_hash="f" * 64,
                cost_model_hash=COST_HASH,
                stockbot_source_hash=SOURCE_HASH,
                adapter_artifact_hash=ADAPTER_HASH,
            )

    def test_sealed_runner_uses_frozen_adaptive_statistics_without_recomputing(self):
        artifact = verify_normal_adaptive_validation_artifact(
            _adaptive_artifact(),
            trusted_keys={ISSUER: KEY},
        )
        shared = _NoReadPort()
        baseline_provider = PointInTimeNormalRequestProvider(
            shared,
            formal_admission=True,
        )
        candidate_provider = PointInTimeNormalRequestProvider(
            shared,
            experiment=NormalFactorExperiment(include_quality=True),
            formal_admission=True,
        )
        baseline = _arm(
            (0.001,) * len(OBSERVATION_DATES),
            annual_return=0.05,
            sharpe=0.5,
            excess=0.0,
        )
        candidate = _arm(
            (0.012, -0.003, 0.007, -0.001, 0.009, -0.002, 0.005, 0.002),
            annual_return=0.10,
            sharpe=1.5,
            excess=0.06,
        )
        with patch(
            "quantbot.normal.experiment_runner._run",
            side_effect=(baseline, candidate, baseline, candidate),
        ), patch(
            "quantbot.normal.experiment_runner.deflated_sharpe_ratio",
            side_effect=AssertionError("sealed H1 must not recompute DSR"),
        ), patch(
            "quantbot.normal.experiment_runner.cscv_probability_of_backtest_overfitting",
            side_effect=AssertionError("sealed H1 must not recompute PBO"),
        ):
            result = run_normal_h1_experiment(
                baseline_provider=baseline_provider,
                candidate_provider=candidate_provider,
                sessions=(object(),),
                adaptive_validation=artifact,
                window_sessions=2,
            )

        self.assertEqual(result.completed_trials, 2)
        self.assertIs(result.dsr, artifact.dsr)
        self.assertIs(result.pbo, artifact.pbo_result)
        self.assertEqual(result.adaptive_validation_hash, artifact.content_hash)


if __name__ == "__main__":
    unittest.main()
