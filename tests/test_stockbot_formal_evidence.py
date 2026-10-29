import json
import hashlib
import hmac
import copy
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.audit import CompletionAudit, load_evidence_file
from quantbot.evidence import hash_payload
from quantbot.evidence_auth import FormalEvidenceIntegrityError
from quantbot.experiment_execution import (
    ExperimentExecutionIdentity,
    ExperimentExecutionLedger,
)
from quantbot.experiment_reporting import CostScenario, CostSuite, ExperimentRunReport
from quantbot.exposure import ExposureLedger
from quantbot.formal_artifact import verify_canonical_execution_artifact
from quantbot.formal_evidence import (
    admission_evidence_payload,
    materialize_completed_strategy_admission,
)
from quantbot.strategy_admission import (
    build_normal_admission_evidence,
    evaluate_normal_admission,
)
from quantbot.trading import RunManifest
from quantbot.validation_protocol import ValidationWindowManifest
from quantbot.validation_protocol import formal_sealed_warmup_interval


class FormalStrategyEvidenceTest(unittest.TestCase):
    @staticmethod
    def _normal_series(dates, returns, *, turnover=0.0):
        equity = 100_000.0
        points = []
        weights = {f"security-{index}": 0.1 for index in range(8)}
        for day, value in zip(dates, returns):
            previous = equity
            equity *= 1.0 + value
            security_pnl = {
                key: (equity - previous) / len(weights) for key in weights
            }
            points.append(
                {
                    "date": day.isoformat(),
                    "portfolio_return": value,
                    "benchmark_return": 0.0,
                    "equity": equity,
                    "fees": 0.0,
                    "turnover": turnover / 252.0,
                    "pre_trade_equity": previous,
                    "traded_notional": 0.0,
                    "weights": weights,
                    "security_pnl": security_pnl,
                }
            )
        return {"initial_equity": 100_000.0, "points": points}

    def _completed_normal_run(
        self,
        root: Path,
        *,
        metrics_mutator=None,
        admission_override=None,
        cost_suite=None,
        execution_ledger_path=None,
        include_artifact=True,
        tamper_artifact_after_hash=False,
        forge_raw_pnl=False,
        forge_adaptive_dates=False,
    ):
        released_at = datetime(2028, 8, 1, tzinfo=timezone.utc)
        exposure_path = root / "exposure-ledger.jsonl"
        output_path = root / "normal-result.json"
        evidence_path = root / "evidence.jsonl"
        warmup = formal_sealed_warmup_interval("normal", date(2026, 8, 1))
        cost_model_payload = {
            "execution": "next_open",
            "cost_bps_per_side": 5.0,
            "stress_cost_bps_per_side": 10.0,
            "slippage_bps_per_side": 0.0,
        }
        manifest = ValidationWindowManifest(
            strategy_family="normal",
            mode="sealed",
            request_start=date(2026, 8, 1),
            request_end=date(2028, 7, 31),
            warmup_start=warmup.start,
            warmup_end=warmup.end,
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash=hash_payload(cost_model_payload),
            code_hash="d" * 64,
        )
        permit = ExposureLedger(
            exposure_path,
            _clock=lambda: released_at,
        ).claim(manifest, run_id="normal-sealed-v1")
        identity = ExperimentExecutionIdentity(
            strategy_family="normal",
            experiment_id="normal-h1-quality-diversification",
            access_mode="sealed",
            request_start=manifest.request_start,
            request_end=manifest.request_end,
            dataset_hash=manifest.dataset_hash,
            config_hash=manifest.config_hash,
            cost_model_hash=manifest.cost_model_hash,
            code_hash=manifest.code_hash,
        )
        dates = tuple(
            manifest.request_start + timedelta(days=index)
            for index in range(252)
        )
        candidate_returns = tuple(
            (0.0015, 0.0010, -0.0004)[index % 3]
            for index in range(len(dates))
        )
        baseline_returns = tuple(
            (0.0002, 0.0001, -0.0001)[index % 3]
            for index in range(len(dates))
        )
        stress_returns = tuple(
            (0.0012, 0.0008, -0.0004)[index % 3]
            for index in range(len(dates))
        )
        h2_returns = tuple(
            (0.0001, -0.0005, 0.0001)[index % 3]
            for index in range(len(dates))
        )
        statistical_dates = tuple(
            date(2021, 1, 1) + timedelta(days=index)
            for index in range(len(dates))
        )
        artifact = {
            "artifact_version": 1,
            "experiment_id": identity.experiment_id,
            "cost_model_payload": cost_model_payload,
            "baseline": self._normal_series(dates, baseline_returns),
            "candidate": self._normal_series(dates, candidate_returns),
            "stress_candidate": self._normal_series(dates, stress_returns),
            "statistical_observation_dates": [
                value.isoformat() for value in statistical_dates
            ],
            "statistical_trials": {
                identity.experiment_id: list(candidate_returns),
                "normal-h2-capital-efficiency": list(h2_returns),
            },
        }
        artifact_hash = hash_payload(artifact)
        verified = verify_canonical_execution_artifact(
            artifact,
            artifact_hash=artifact_hash,
            execution_identity=identity,
            permit=permit,
        )
        metrics = dict(verified.report_metric_projection)
        if metrics_mutator is not None:
            metrics_mutator(metrics)
        decision = evaluate_normal_admission(
            verified.admission_metrics,
            _clock=lambda: released_at,
        )
        if tamper_artifact_after_hash:
            artifact["candidate"]["points"][0]["portfolio_return"] = 0.99
        if forge_raw_pnl:
            artifact["candidate"]["points"][0]["security_pnl"] = {
                "security-forged": 0.0
            }
            artifact_hash = hash_payload(artifact)
        if forge_adaptive_dates:
            artifact["statistical_observation_dates"] = [
                value.isoformat() for value in dates
            ]
            artifact_hash = hash_payload(artifact)
        suite = cost_suite or CostSuite(
            (
                CostScenario("baseline", 5.0, "baseline"),
                CostScenario("double_cost_stress", 10.0, "stress"),
            )
        )
        observed_at = released_at
        report = ExperimentRunReport(
            experiment_id=identity.experiment_id,
            run_id="normal-sealed-v1",
            run_type="sealed_backtest",
            observed_at=observed_at,
            dataset_hash=manifest.dataset_hash,
            input_materialization_hash="e" * 64,
            config_hash=manifest.config_hash,
            code_hash=manifest.code_hash,
            cost_model_hash=manifest.cost_model_hash,
            cost_suite=suite,
            output_hash=hash_payload(metrics),
            metrics=metrics,
            admission=admission_override or {
                "passed": decision.passed,
                "formal_admission_eligible": decision.passed,
                "reasons": list(decision.reasons),
            },
        )
        run_manifest = RunManifest(
            run_type="sealed_backtest",
            observed_at=observed_at,
            dataset_hash=manifest.dataset_hash,
            config_hash=manifest.config_hash,
            code_hash=manifest.code_hash,
            policy_version=ACCEPTANCE_POLICY_VERSION,
            cost_model_hash=manifest.cost_model_hash,
            input_materialization_hash=report.input_materialization_hash,
            output_hash=report.output_hash,
            report_hash=report.report_hash,
            cost_suite_hash=suite.suite_hash,
            run_id="normal-sealed-v1",
            experiment_id=identity.experiment_id,
        )
        evidence = build_normal_admission_evidence(
            verified.admission_metrics,
            run_manifest,
            _clock=lambda: released_at,
        )
        payload = {
            "formal_admission_eligible": decision.passed,
            "permit_record_hash": permit.record_hash,
            "dataset_hash": manifest.dataset_hash,
            "config_hash": manifest.config_hash,
            "cost_model_hash": manifest.cost_model_hash,
            "code_hash": manifest.code_hash,
            "input_materialization_hash": report.input_materialization_hash,
            "output_hash": report.output_hash,
            "report_hash": report.report_hash,
            "cost_suite_hash": suite.suite_hash,
            "experiment_run_report": report.to_payload(),
            "admission_evidence": admission_evidence_payload(evidence),
        }
        if include_artifact:
            payload.update(
                {
                    "canonical_execution_artifact": artifact,
                    "canonical_execution_artifact_hash": artifact_hash,
                }
            )
        execution = (
            ExperimentExecutionLedger(exposure_path)
            if execution_ledger_path is None
            else ExperimentExecutionLedger.for_testing(execution_ledger_path)
        )
        execution.begin(
            identity,
            run_id="normal-sealed-v1",
            result_path=output_path,
        )
        execution.complete(
            identity,
            run_id="normal-sealed-v1",
            result_path=output_path,
            payload=payload,
            exit_code=0 if decision.passed else 1,
        )
        return (
            released_at,
            exposure_path,
            output_path,
            evidence_path,
            identity,
            execution,
        )

    def test_completed_result_materializes_one_authenticated_auditable_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(
                    root / "exposure-ledger.jsonl"
                ),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                (
                    released_at,
                    exposure_path,
                    output_path,
                    evidence_path,
                    identity,
                    execution,
                ) = self._completed_normal_run(root)
                first = materialize_completed_strategy_admission(
                    evidence_path=evidence_path,
                    exposure_ledger_path=exposure_path,
                    execution_ledger=execution,
                    execution_identity=identity,
                    run_id="normal-sealed-v1",
                    result_path=output_path,
                    _clock=lambda: released_at,
                )
                second = materialize_completed_strategy_admission(
                    evidence_path=evidence_path,
                    exposure_ledger_path=exposure_path,
                    execution_ledger=execution,
                    execution_identity=identity,
                    run_id="normal-sealed-v1",
                    result_path=output_path,
                    _clock=lambda: released_at,
                )
                report = CompletionAudit(["normal_strategy_admission"]).evaluate(
                    load_evidence_file(evidence_path)
                )

            self.assertTrue(first.authenticated)
            self.assertTrue(first.canonical_verified)
            self.assertEqual(first.formal_record_hash, second.formal_record_hash)
            self.assertTrue(report.complete)
            self.assertEqual(
                len(evidence_path.read_text(encoding="utf-8").splitlines()),
                1,
            )

    def test_unsigned_or_tampered_formal_record_cannot_complete_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_path = root / "evidence.jsonl"
            forged = {
                "requirement_id": "normal_strategy_admission",
                "passed": True,
                "detail": "passed",
                "run_type": "sealed_backtest",
                "observed_at": "2028-08-01T00:00:00+00:00",
                "dataset_hash": "a" * 64,
                "config_hash": "b" * 64,
                "cost_model_hash": "c" * 64,
                "code_hash": "d" * 64,
                "policy_version": ACCEPTANCE_POLICY_VERSION,
                "input_materialization_hash": "e" * 64,
                "output_hash": "f" * 64,
                "report_hash": "1" * 64,
                "cost_suite_hash": "2" * 64,
                "run_id": "forged",
                "experiment_id": "normal-h1-quality-diversification",
            }
            evidence_path.write_text(json.dumps(forged) + "\n", encoding="utf-8")
            report = CompletionAudit(["normal_strategy_admission"]).evaluate(
                load_evidence_file(evidence_path)
            )
            self.assertFalse(report.complete)

            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(
                    root / "exposure-ledger.jsonl"
                ),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                (
                    released_at,
                    exposure_path,
                    output_path,
                    signed_path,
                    identity,
                    execution,
                ) = self._completed_normal_run(root)
                materialize_completed_strategy_admission(
                    evidence_path=signed_path,
                    exposure_ledger_path=exposure_path,
                    execution_ledger=execution,
                    execution_identity=identity,
                    run_id="normal-sealed-v1",
                    result_path=output_path,
                    _clock=lambda: released_at,
                )
                raw = json.loads(
                    signed_path.read_text(encoding="utf-8").splitlines()[-1]
                )
                raw["passed"] = False
                signed_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
                with self.assertRaises(FormalEvidenceIntegrityError):
                    load_evidence_file(signed_path)

    def test_low_level_chain_signature_without_verifier_proof_cannot_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_path = root / "evidence.jsonl"
            key = b"formal-evidence-test-key"
            business = {
                "requirement_id": "normal_strategy_admission",
                "passed": True,
                "detail": "passed",
                "run_type": "sealed_backtest",
                "observed_at": "2028-08-01T00:00:00+00:00",
                "dataset_hash": "a" * 64,
                "config_hash": "b" * 64,
                "cost_model_hash": "c" * 64,
                "code_hash": "d" * 64,
                "policy_version": ACCEPTANCE_POLICY_VERSION,
                "input_materialization_hash": "e" * 64,
                "output_hash": "f" * 64,
                "report_hash": "1" * 64,
                "cost_suite_hash": "2" * 64,
                "run_id": "forged",
                "experiment_id": "normal-h1-quality-diversification",
            }
            unsigned = {
                **business,
                "formal_evidence_version": 2,
                "permit_record_hash": "3" * 64,
                "execution_receipt_hash": "4" * 64,
                "execution_result_hash": "5" * 64,
                "execution_artifact_hash": "6" * 64,
                "canonical_verifier_version": 1,
                # A low-level chain signer has not received a verifier grant.
                "canonical_verification_hash": "7" * 64,
                "canonical_verification_signature": "8" * 64,
                "formal_prev_hash": "0" * 64,
            }
            canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":"))
            record_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            record = {
                **unsigned,
                "formal_record_hash": record_hash,
                "formal_signature": hmac.new(
                    key,
                    record_hash.encode("ascii"),
                    hashlib.sha256,
                ).hexdigest(),
            }
            evidence_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": key.decode("ascii"),
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "missing.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                loaded = load_evidence_file(evidence_path)
                audit = CompletionAudit(["normal_strategy_admission"]).evaluate(
                    loaded
                )
            self.assertTrue(loaded["normal_strategy_admission"].authenticated)
            self.assertFalse(
                loaded["normal_strategy_admission"].canonical_verified
            )
            self.assertFalse(audit.complete)

    def test_wrong_cost_suite_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            wrong_suite = CostSuite(
                (
                    CostScenario("baseline", 4.0, "baseline"),
                    CostScenario("double_cost_stress", 10.0, "stress"),
                )
            )
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(root, cost_suite=wrong_suite)
                with self.assertRaisesRegex(PermissionError, "cost suite"):
                    self._materialize(run)

    def test_forged_summary_metrics_disagreeing_with_raw_artifact_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }

            def forge(metrics):
                candidate = dict(metrics["candidate"])
                candidate["annual_return"] = 9.0
                metrics["candidate"] = candidate

            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(root, metrics_mutator=forge)
                with self.assertRaisesRegex(PermissionError, "raw execution artifact"):
                    self._materialize(run)

    def test_report_admission_inconsistent_with_recalculation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(
                    root,
                    admission_override={
                        "passed": False,
                        "formal_admission_eligible": False,
                        "reasons": ["invented"],
                    },
                )
                with self.assertRaisesRegex(PermissionError, "admission.passed"):
                    self._materialize(run)

    def test_missing_raw_artifact_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(root, include_artifact=False)
                with self.assertRaisesRegex(ValueError, "canonical_execution_artifact"):
                    self._materialize(run)

    def test_tampered_raw_artifact_is_rejected_before_metric_use(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(
                    root,
                    tamper_artifact_after_hash=True,
                )
                with self.assertRaisesRegex(PermissionError, "artifact hash"):
                    self._materialize(run)

    def test_self_hashed_raw_pnl_that_breaks_equity_conservation_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(root, forge_raw_pnl=True)
                with self.assertRaisesRegex(PermissionError, "security_pnl/equity"):
                    self._materialize(run)

    def test_noncanonical_execution_ledger_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            bypass = root / "fresh-execution-ledger.jsonl"
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(
                    root,
                    execution_ledger_path=bypass,
                )
                with self.assertRaisesRegex(PermissionError, "canonical experiment"):
                    self._materialize(run)

    def test_sealed_returns_cannot_be_relabelled_as_adaptive_statistics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                run = self._completed_normal_run(
                    root,
                    forge_adaptive_dates=True,
                )
                with self.assertRaisesRegex(PermissionError, "2021-01-01"):
                    self._materialize(run)

    @staticmethod
    def _materialize(run):
        (
            released_at,
            exposure_path,
            output_path,
            evidence_path,
            identity,
            execution,
        ) = run
        return materialize_completed_strategy_admission(
            evidence_path=evidence_path,
            exposure_ledger_path=exposure_path,
            execution_ledger=execution,
            execution_identity=identity,
            run_id="normal-sealed-v1",
            result_path=output_path,
            _clock=lambda: released_at,
        )


class DayTradeRawArtifactVerifierTest(unittest.TestCase):
    @staticmethod
    def _arm(cost_bps):
        rate = cost_bps / 10_000.0
        specifications = (
            (
                date(2026, 8, 1),
                "long",
                100.0,
                101.0,
                datetime(2026, 8, 1, 14, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 1, 15, 30, tzinfo=timezone.utc),
            ),
            (
                date(2026, 8, 2),
                "short",
                100.0,
                99.0,
                datetime(2026, 8, 2, 14, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 2, 15, 30, tzinfo=timezone.utc),
            ),
        )
        equity = 100_000.0
        daily = []
        trades = []
        for day, side, entry_event, exit_event, entry_time, exit_time in specifications:
            entry = entry_event * (
                1.0 + rate if side == "long" else 1.0 - rate
            )
            exit_price = exit_event * (
                1.0 - rate if side == "long" else 1.0 + rate
            )
            pnl = (
                exit_price - entry
                if side == "long"
                else entry - exit_price
            )
            equity += pnl
            daily.append(
                {
                    "day": day.isoformat(),
                    "pnl": pnl,
                    "ending_equity": equity,
                }
            )
            trades.append(
                {
                    "day": day.isoformat(),
                    "entry_timestamp": entry_time.isoformat(),
                    "exit_timestamp": exit_time.isoformat(),
                    "side": side,
                    "entry_event_price": entry_event,
                    "exit_event_price": exit_event,
                    "entry_price": entry,
                    "exit_price": exit_price,
                    "quantity": 1.0,
                    "notional": entry,
                    "borrow_cost": 0.0,
                    "pnl": pnl,
                    "security_id": "stable-security",
                }
            )
        return {"daily": daily, "trades": trades}

    def _artifact(self, root):
        released_at = datetime(2028, 8, 1, tzinfo=timezone.utc)
        exposure_path = root / "exposure-ledger.jsonl"
        cost_model_payload = {
            "execution": "next_available_minute_event",
            "cost_bps_per_side": 5.0,
            "stress_cost_bps_per_side": 12.0,
            "max_volume_participation": 0.10,
        }
        warmup = formal_sealed_warmup_interval("daytrade", date(2026, 8, 1))
        manifest = ValidationWindowManifest(
            strategy_family="daytrade",
            mode="sealed",
            request_start=date(2026, 8, 1),
            request_end=date(2028, 7, 31),
            warmup_start=warmup.start,
            warmup_end=warmup.end,
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash=hash_payload(cost_model_payload),
            code_hash="d" * 64,
        )
        permit = ExposureLedger(
            exposure_path,
            _clock=lambda: released_at,
        ).claim(manifest, run_id="daytrade-sealed-v1")
        identity = ExperimentExecutionIdentity(
            strategy_family="daytrade",
            experiment_id="daytrade-h9-rvol-reward-risk",
            access_mode="sealed",
            request_start=manifest.request_start,
            request_end=manifest.request_end,
            dataset_hash=manifest.dataset_hash,
            config_hash=manifest.config_hash,
            cost_model_hash=manifest.cost_model_hash,
            code_hash=manifest.code_hash,
        )
        stat_dates = tuple(
            date(2023, 1, 1) + timedelta(days=index) for index in range(16)
        )
        selected = "daytrade-h9-rvol-reward-risk:trial-01"
        trials = {
            selected: [
                (0.002, 0.001, -0.0004)[index % 3]
                for index in range(len(stat_dates))
            ],
            "baseline": [
                (0.0001, -0.0005, 0.0001)[index % 3]
                for index in range(len(stat_dates))
            ],
        }
        for trial_index in range(20):
            trials[f"historical-{trial_index:02d}"] = [
                (
                    -0.0002 - trial_index * 1e-6,
                    0.0001,
                    -0.0001,
                )[index % 3]
                for index in range(len(stat_dates))
            ]
        artifact = {
            "artifact_version": 1,
            "experiment_id": identity.experiment_id,
            "cost_model_payload": cost_model_payload,
            "initial_capital": 100_000.0,
            "observation_dates": ["2026-08-01", "2026-08-02"],
            "baseline_5bps": self._arm(5.0),
            "candidate_5bps": self._arm(5.0),
            "candidate_12bps": self._arm(12.0),
            "statistical_observation_dates": [
                value.isoformat() for value in stat_dates
            ],
            "selected_trial_id": selected,
            "statistical_trials": trials,
        }
        return artifact, identity, permit

    def test_long_short_fills_must_match_exact_5_and_12_bps_costs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                artifact, identity, permit = self._artifact(root)
                cases = (
                    ("baseline_5bps", 0, "entry_price", "entry fill cost"),
                    ("candidate_5bps", 1, "exit_price", "exit fill cost"),
                    ("candidate_12bps", 0, "entry_price", "entry fill cost"),
                    ("candidate_12bps", 1, "exit_price", "exit fill cost"),
                )
                for arm, trade_index, field, message in cases:
                    with self.subTest(arm=arm, trade_index=trade_index, field=field):
                        forged = copy.deepcopy(artifact)
                        forged[arm]["trades"][trade_index][field] += 0.01
                        if field == "entry_price":
                            trade = forged[arm]["trades"][trade_index]
                            trade["notional"] = (
                                trade["entry_price"] * trade["quantity"]
                            )
                        with self.assertRaisesRegex(PermissionError, message):
                            verify_canonical_execution_artifact(
                                forged,
                                artifact_hash=hash_payload(forged),
                                execution_identity=identity,
                                permit=permit,
                            )

    def test_daytrade_adaptive_statistics_cannot_use_sealed_dates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            environment = {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "formal-evidence-test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root / "exposure-ledger.jsonl"),
            }
            with mock.patch.dict(os.environ, environment, clear=False):
                artifact, identity, permit = self._artifact(root)
                forged = copy.deepcopy(artifact)
                forged["statistical_observation_dates"] = [
                    (date(2026, 8, 1) + timedelta(days=index)).isoformat()
                    for index in range(16)
                ]
                with self.assertRaisesRegex(PermissionError, "2023-01-01"):
                    verify_canonical_execution_artifact(
                        forged,
                        artifact_hash=hash_payload(forged),
                        execution_identity=identity,
                        permit=permit,
                    )


if __name__ == "__main__":
    unittest.main()
