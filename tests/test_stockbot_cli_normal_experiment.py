import io
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from tests import test_stockbot_pit_bundle as pit_bundle_fixture


NORMAL_ADAPTER_ISSUER = "normal-adapter-auditor"
NORMAL_ADAPTER_KEY = b"normal-adapter-test-key"


def _write_normal_adapter_attestation(
    path,
    loader,
    bundle,
    *,
    issuer=NORMAL_ADAPTER_ISSUER,
    key=NORMAL_ADAPTER_KEY,
):
    from quantbot.evidence import hash_payload
    from quantbot.normal.adapter_trust import normal_adapter_artifact_identity

    identity = normal_adapter_artifact_identity(loader)
    unsigned = {
        "artifact_version": 1,
        "issuer": issuer,
        "adapter_artifact_hash": identity.adapter_artifact_hash,
        "schema_mapping_hash": identity.schema_mapping_hash,
        "bundle_provider": bundle.manifest.provider,
        "bundle_schema_version": bundle.manifest.schema_version,
        "bundle_id": bundle.manifest.bundle_id,
        "bundle_dataset_hash": bundle.dataset_hash,
    }
    content_hash = hash_payload(unsigned)
    payload = {
        **unsigned,
        "content_hash": content_hash,
        "signature": hmac.new(
            key,
            content_hash.encode("ascii"),
            hashlib.sha256,
        ).hexdigest(),
    }
    Path(path).write_text(json.dumps(payload), encoding="utf-8")
    return identity, payload


def _normal_admission_evidence(payload, *, passed, run_id):
    return {
        "requirement_id": "normal_strategy_admission",
        "passed": passed,
        "detail": "passed" if passed else "failed:sharpe",
        "run_type": "sealed_backtest",
        "observed_at": "2028-08-01T00:00:00+00:00",
        "dataset_hash": payload["dataset_hash"],
        "config_hash": payload["config_hash"],
        "cost_model_hash": payload["cost_model_hash"],
        "code_hash": payload["code_hash"],
        "policy_version": "quantbot.acceptance.v1",
        "input_materialization_hash": payload["input_materialization_hash"],
        "output_hash": payload["output_hash"],
        "report_hash": payload["report_hash"],
        "cost_suite_hash": payload["cost_suite_hash"],
        "run_id": run_id,
        "experiment_id": "normal-h1-quality-diversification",
    }


class _NoReadPort:
    def monthly_returns(self, security_master):
        return None

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {}

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {}

    def market_betas(self, security_master, *, lookback_sessions):
        return {}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {}

    def regime_context(self, security_master):
        return None


class StockBotNormalExperimentCliTest(unittest.TestCase):
    def test_command_is_registered_with_explicit_warmup_and_access_identity(self):
        from quantbot.cli_commands.registry import build_parser

        args = build_parser().parse_args(
            [
                "normal-experiment-run",
                "--exposure-ledger",
                "/tmp/quantbot-ledger.jsonl",
                "--evidence-file",
                "/tmp/quantbot-evidence.jsonl",
                "--run-id",
                "normal-research-h1",
                "--access-mode",
                "research",
                "--start",
                "2010-01-01",
                "--end",
                "2020-12-31",
                "--warmup-start",
                "2007-01-01",
                "--warmup-end",
                "2009-12-31",
            ]
        )

        self.assertEqual(args.command, "normal-experiment-run")
        self.assertEqual(args.cost_bps_per_side, 5.0)
        self.assertEqual(args.stress_cost_bps_per_side, 10.0)
        self.assertEqual(args.warmup_start, "2007-01-01")
        self.assertEqual(args.warmup_end, "2009-12-31")
        self.assertEqual(args.adapter, "")
        self.assertEqual(args.historical_trial_artifact, "")
        self.assertEqual(args.adaptive_validation_artifact, "")
        self.assertFalse(args.historical_audit_replay)
        self.assertEqual(
            args.experiment_id,
            "normal-h1-quality-diversification",
        )

    def test_command_accepts_normal_h2_without_changing_h1_default(self):
        from quantbot.cli_commands.registry import build_parser

        args = build_parser().parse_args(
            [
                "normal-experiment-run",
                "--experiment-id",
                "normal-h2-capital-efficiency",
                "--exposure-ledger",
                "/tmp/quantbot-ledger.jsonl",
                "--evidence-file",
                "/tmp/quantbot-evidence.jsonl",
                "--run-id",
                "normal-research-h2",
                "--access-mode",
                "research",
                "--start",
                "2010-01-01",
                "--end",
                "2020-12-31",
                "--warmup-start",
                "2007-01-01",
                "--warmup-end",
                "2009-12-31",
            ]
        )

        self.assertEqual(args.experiment_id, "normal-h2-capital-efficiency")

    def test_h2_reuses_the_baseline_provider_and_frozen_run_identity(self):
        from quantbot.cli_commands.normal import _run_selected_normal_experiment

        baseline_provider = object()
        quality_provider = object()
        inputs = SimpleNamespace(
            sessions=("shared-session",),
            providers=lambda: (baseline_provider, quality_provider),
        )
        args = SimpleNamespace(
            experiment_id="normal-h2-capital-efficiency",
            initial_equity=125_000.0,
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
        )
        expected = object()
        with patch(
            "quantbot.cli_commands.normal.run_normal_h1_experiment"
        ) as h1, patch(
            "quantbot.cli_commands.normal.run_normal_h2_experiment",
            return_value=expected,
        ) as h2:
            actual = _run_selected_normal_experiment(args, inputs)

        self.assertIs(actual, expected)
        h1.assert_not_called()
        h2.assert_called_once_with(
            provider=baseline_provider,
            sessions=inputs.sessions,
            initial_equity=125_000.0,
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
            historical_audit_replay=True,
        )
        self.assertNotIn("historical_h2_audit", h2.call_args.kwargs)

    def test_rejected_h2_requires_explicit_nonexclusive_audit_mode(self):
        from quantbot.cli_commands.normal import (
            _require_normal_experiment_execution_mode,
        )

        with self.assertRaisesRegex(PermissionError, "completed_rejected"):
            _require_normal_experiment_execution_mode(
                SimpleNamespace(
                    historical_audit_replay=False,
                    access_mode="research",
                ),
                "normal-h2-capital-efficiency",
            )
        with self.assertRaisesRegex(PermissionError, "cannot consume"):
            _require_normal_experiment_execution_mode(
                SimpleNamespace(
                    historical_audit_replay=True,
                    access_mode="sealed",
                ),
                "normal-h2-capital-efficiency",
            )
        _require_normal_experiment_execution_mode(
            SimpleNamespace(
                historical_audit_replay=True,
                access_mode="research",
            ),
            "normal-h2-capital-efficiency",
        )

    def test_h1_requires_signed_h2_audit_before_any_claim(self):
        from quantbot.cli_commands.normal import (
            _resolve_normal_h2_historical_audit,
        )

        with self.assertRaisesRegex(PermissionError, "before claim"):
            _resolve_normal_h2_historical_audit(
                SimpleNamespace(historical_trial_artifact=""),
                environ={},
                experiment_id="normal-h1-quality-diversification",
                mode="research",
                request_start=date(2010, 1, 1),
                request_end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                warmup_end=date(2009, 12, 31),
                dataset_hash="a" * 64,
                cost_model_hash="b" * 64,
                code_hash="c" * 64,
            )

    def test_explicit_h2_artifact_path_precedes_environment_fallback(self):
        from quantbot.cli_commands.normal import (
            _resolve_normal_h2_historical_audit,
        )

        audit = SimpleNamespace(require_compatible=lambda **_kwargs: None)
        with patch(
            "quantbot.cli_commands.normal.trusted_normal_trial_keys",
            return_value={"issuer": b"x" * 16},
        ), patch(
            "quantbot.cli_commands.normal.load_normal_h2_historical_audit",
            return_value=audit,
        ) as loader:
            resolved = _resolve_normal_h2_historical_audit(
                SimpleNamespace(historical_trial_artifact="/explicit/h2.json"),
                environ={
                    "STOCKBOT_NORMAL_H2_AUDIT_ARTIFACT_PATH": "/fallback/h2.json"
                },
                experiment_id="normal-h1-quality-diversification",
                mode="research",
                request_start=date(2010, 1, 1),
                request_end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                warmup_end=date(2009, 12, 31),
                dataset_hash="a" * 64,
                cost_model_hash="b" * 64,
                code_hash="c" * 64,
            )

        self.assertIs(resolved, audit)
        self.assertEqual(loader.call_args.args[0], Path("/explicit/h2.json"))

    def test_sealed_adaptive_artifact_bytes_are_bound_before_verification(self):
        from quantbot.cli_commands.normal import (
            _resolve_pending_normal_adaptive_validation,
        )

        with tempfile.TemporaryDirectory() as root:
            fallback = Path(root) / "fallback.json"
            explicit = Path(root) / "explicit.json"
            fallback.write_text('{"source":"fallback"}', encoding="utf-8")
            explicit.write_text('{"source":"explicit"}', encoding="utf-8")
            pending = _resolve_pending_normal_adaptive_validation(
                SimpleNamespace(
                    adaptive_validation_artifact=str(explicit),
                ),
                environ={
                    "STOCKBOT_NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH": str(
                        fallback
                    )
                },
                experiment_id="normal-h1-quality-diversification",
                mode="sealed",
            )

        self.assertEqual(pending.path, explicit)
        self.assertEqual(
            pending.file_hash,
            hashlib.sha256(b'{"source":"explicit"}').hexdigest(),
        )

    def test_adaptive_artifact_mode_misuse_fails_before_data_access(self):
        from quantbot.cli_commands.normal import (
            _resolve_pending_normal_adaptive_validation,
        )

        with self.assertRaisesRegex(PermissionError, "requires a signed"):
            _resolve_pending_normal_adaptive_validation(
                SimpleNamespace(adaptive_validation_artifact=""),
                environ={},
                experiment_id="normal-h1-quality-diversification",
                mode="sealed",
            )
        with self.assertRaisesRegex(ValueError, "reserved for sealed"):
            _resolve_pending_normal_adaptive_validation(
                SimpleNamespace(adaptive_validation_artifact="/tmp/adaptive.json"),
                environ={},
                experiment_id="normal-h1-quality-diversification",
                mode="adaptive_development",
            )

    def test_strategy_protocol_hash_excludes_adapter_and_data_identity(self):
        from quantbot.cli_commands.normal import _normal_strategy_protocol_hash

        first = _normal_strategy_protocol_hash(
            "normal-h1-quality-diversification"
        )
        second = _normal_strategy_protocol_hash(
            "normal-h1-quality-diversification"
        )
        h2 = _normal_strategy_protocol_hash("normal-h2-capital-efficiency")

        self.assertEqual(first, second)
        self.assertNotEqual(first, h2)

    def test_controlled_result_path_must_be_nonempty_and_absolute(self):
        from quantbot.cli_commands.normal import (
            _require_normal_experiment_result_path,
        )

        for value in ("", "relative/result.json"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError,
                "absolute output file",
            ):
                _require_normal_experiment_result_path(value)
        self.assertEqual(
            _require_normal_experiment_result_path("/tmp/normal-result.json"),
            Path("/tmp/normal-result.json").resolve(),
        )

    def test_h2_payload_reports_selected_experiment_and_utilization(self):
        from quantbot.cli_commands.normal import _normal_experiment_payload

        summary = SimpleNamespace(
            annual_return=0.12,
            benchmark_annual_return=0.05,
            excess_alpha_annual=0.07,
            sharpe=1.3,
            max_drawdown=0.10,
        )
        arm = SimpleNamespace(
            summary=summary,
            annual_turnover=2.0,
            fees=100.0,
            points=(
                SimpleNamespace(
                    trading_date=date(2019, 1, 2),
                    weights={"SEC-1": 0.5},
                ),
            ),
            daily_returns=(0.01,),
        )
        result = SimpleNamespace(
            decision=SimpleNamespace(
                passed=True,
                reasons=(),
                cagr_improvement=0.02,
                sharpe_improvement=0.01,
                positive_window_fraction=0.70,
                top_three_profit_contribution=0.40,
                baseline_capital_utilization=0.65,
                candidate_capital_utilization=0.95,
            ),
            baseline=arm,
            candidate=arm,
            stressed_baseline=arm,
            stressed_candidate=arm,
            declared_trials=2,
            completed_trials=1,
            dsr=None,
            pbo=None,
        )
        permit = SimpleNamespace(
            mode="research",
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash="c" * 64,
            code_hash="d" * 64,
            record_hash="e" * 64,
        )
        inputs = SimpleNamespace(
            provenance_verified=True,
            adapter_code_hash="f" * 64,
            session_hash="1" * 64,
            read_evidence=SimpleNamespace(
                evidence_hash="2" * 64,
                tables=("daily_bars",),
                dataset_hash="a" * 64,
                receipts=(),
                trading_calendar_sessions=(),
            ),
        )

        payload = _normal_experiment_payload(
            SimpleNamespace(
                experiment_id="normal-h2-capital-efficiency",
                access_mode="research",
                run_id="normal-h2-test",
                cost_bps_per_side=5.0,
                stress_cost_bps_per_side=10.0,
            ),
            SimpleNamespace(dataset_hash="a" * 64),
            permit,
            result,
            "b" * 64,
            "c" * 64,
            "d" * 64,
            inputs,
            None,
        )

        self.assertEqual(
            payload["experiment_id"],
            "normal-h2-capital-efficiency",
        )
        self.assertEqual(
            payload["decision"]["baseline_capital_utilization"],
            0.65,
        )
        self.assertEqual(
            payload["decision"]["candidate_capital_utilization"],
            0.95,
        )
        self.assertEqual(
            payload["experiment_run_report"]["dataset_hash"],
            payload["dataset_hash"],
        )
        self.assertEqual(
            payload["experiment_run_report"]["input_materialization_hash"],
            payload["input_materialization_hash"],
        )
        self.assertEqual(
            payload["experiment_run_report"]["report_hash"],
            payload["report_hash"],
        )
        self.assertNotIn("canonical_execution_artifact", payload)
        self.assertNotIn("canonical_execution_artifact_hash", payload)

    def test_sealed_payload_uses_canonical_admission_for_trials_leverage_and_permit(self):
        from quantbot.cli_commands.normal import _normal_experiment_payload
        from quantbot.evidence import hash_payload
        from quantbot.exposure import ExposureLedger
        from quantbot.normal.execution_artifact import normal_cost_model_payload
        from quantbot.normal.historical_trials import NormalAdaptiveValidationArtifact
        from quantbot.validation_protocol import ValidationWindowManifest

        with tempfile.TemporaryDirectory() as root:
            ledger_path = Path(root).resolve() / "exposure.jsonl"
            cost_payload = normal_cost_model_payload(
                cost_bps_per_side=5.0,
                stress_cost_bps_per_side=10.0,
            )
            cost_model_hash = hash_payload(cost_payload)
            manifest = ValidationWindowManifest(
                strategy_family="normal",
                mode="sealed",
                request_start=date(2026, 8, 1),
                request_end=date(2028, 7, 31),
                warmup_start=date(2023, 8, 1),
                warmup_end=date(2026, 7, 31),
                dataset_hash="a" * 64,
                config_hash="b" * 64,
                cost_model_hash=cost_model_hash,
                code_hash="d" * 64,
            )
            with patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "normal-formal-test-key",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                },
                clear=False,
            ):
                permit = ExposureLedger(
                    ledger_path,
                    _clock=lambda: datetime(2028, 8, 1, tzinfo=timezone.utc),
                ).claim(manifest, run_id="normal-sealed-builder-test")
                summary = SimpleNamespace(
                    annual_return=0.13,
                    benchmark_annual_return=0.06,
                    excess_alpha_annual=0.07,
                    sharpe=1.5,
                    max_drawdown=0.10,
                )
                baseline_summary = SimpleNamespace(
                    annual_return=0.10,
                    benchmark_annual_return=0.06,
                    excess_alpha_annual=0.04,
                    sharpe=1.2,
                    max_drawdown=0.10,
                )
                stress_summary = SimpleNamespace(
                    annual_return=0.10,
                    benchmark_annual_return=0.06,
                    excess_alpha_annual=0.04,
                    sharpe=1.3,
                    max_drawdown=0.10,
                )

                def arm(arm_summary, weight, *, cost_bps=5.0):
                    first_return = 0.01
                    second_return = 0.005
                    first_equity = 100_000.0 * (1.0 + first_return)
                    second_equity = first_equity * (1.0 + second_return)
                    first_turnover = 0.25
                    first_traded_notional = 50_000.0
                    second_turnover = 0.10
                    second_traded_notional = 2.0 * second_turnover * first_equity
                    cost_rate = cost_bps / 10_000.0
                    return SimpleNamespace(
                        summary=arm_summary,
                        annual_turnover=2.0,
                        fees=100.0,
                        initial_equity=100_000.0,
                        points=(
                            SimpleNamespace(
                                trading_date=date(2026, 8, 3),
                                portfolio_return=first_return,
                                benchmark_return=0.001,
                                equity=first_equity,
                                fees=first_traded_notional * cost_rate,
                                turnover=first_turnover,
                                pre_trade_equity=100_000.0,
                                traded_notional=first_traded_notional,
                                weights={"SEC-1": weight},
                                security_pnl={"SEC-1": 1_000.0},
                            ),
                            SimpleNamespace(
                                trading_date=date(2026, 8, 4),
                                portfolio_return=second_return,
                                benchmark_return=0.001,
                                equity=second_equity,
                                fees=second_traded_notional * cost_rate,
                                turnover=second_turnover,
                                pre_trade_equity=first_equity,
                                traded_notional=second_traded_notional,
                                weights={"SEC-1": weight},
                                security_pnl={"SEC-1": second_equity - first_equity},
                            ),
                        ),
                        daily_returns=(first_return, second_return),
                    )

                baseline = arm(baseline_summary, 0.8)
                candidate = arm(summary, 1.2)
                stressed_candidate = arm(stress_summary, 1.2, cost_bps=10.0)
                adaptive_validation = create_autospec(
                    NormalAdaptiveValidationArtifact,
                    instance=True,
                )
                adaptive_validation.content_hash = "4" * 64
                adaptive_validation.observation_dates = (
                    date(2021, 1, 4),
                    date(2021, 1, 5),
                    date(2021, 1, 6),
                    date(2021, 1, 7),
                )
                adaptive_validation.h1_daily_returns = (0.01, -0.002, 0.007, 0.003)
                adaptive_validation.h2_daily_returns = (0.004, -0.003, 0.002, 0.001)
                result = SimpleNamespace(
                    decision=SimpleNamespace(
                        passed=True,
                        reasons=(),
                        cagr_improvement=0.03,
                        sharpe_improvement=0.30,
                        positive_window_fraction=0.70,
                        top_three_profit_contribution=0.40,
                    ),
                    baseline=baseline,
                    candidate=candidate,
                    stressed_baseline=baseline,
                    stressed_candidate=stressed_candidate,
                    declared_trials=2,
                    completed_trials=1,
                    dsr=SimpleNamespace(confidence=0.99),
                    pbo=SimpleNamespace(
                        probability=0.01,
                        selected_strategy_ids=(
                            "normal-h1-quality-diversification",
                            "normal-h2-capital-efficiency",
                        ),
                    ),
                    historical_h2_audit_hash=None,
                    adaptive_validation_hash=adaptive_validation.content_hash,
                )
                inputs = SimpleNamespace(
                    provenance_verified=True,
                    adapter_code_hash="f" * 64,
                    session_hash="1" * 64,
                    read_evidence=SimpleNamespace(
                        evidence_hash="2" * 64,
                        tables=("daily_bars",),
                        dataset_hash="a" * 64,
                        receipts=(),
                        trading_calendar_sessions=(date(2026, 8, 3),),
                    ),
                )
                payload = _normal_experiment_payload(
                    SimpleNamespace(
                        experiment_id="normal-h1-quality-diversification",
                        access_mode="sealed",
                        run_id="normal-sealed-builder-test",
                        cost_bps_per_side=5.0,
                        stress_cost_bps_per_side=10.0,
                    ),
                    SimpleNamespace(dataset_hash="a" * 64),
                    permit,
                    result,
                    "b" * 64,
                    cost_model_hash,
                    "d" * 64,
                    inputs,
                    SimpleNamespace(issuer="adapter-auditor", content_hash="3" * 64),
                    adaptive_validation=adaptive_validation,
                )

        self.assertFalse(payload["formal_admission_eligible"])
        self.assertEqual(payload["gross_leverage"], 1.2)
        self.assertIn("incomplete_trial_ledger", payload["admission_evidence"]["detail"])
        self.assertIn("leverage", payload["admission_evidence"]["detail"])
        self.assertEqual(
            payload["admission_evidence"]["report_hash"],
            payload["report_hash"],
        )
        self.assertEqual(
            payload["canonical_execution_artifact_hash"],
            hash_payload(payload["canonical_execution_artifact"]),
        )
        self.assertEqual(
            payload["canonical_execution_artifact"]["cost_model_payload"],
            cost_payload,
        )
        self.assertEqual(
            payload["adaptive_validation_hash"],
            adaptive_validation.content_hash,
        )

    def test_materialization_hash_ignores_permit_record_but_binds_sessions(self):
        from quantbot.cli_commands.normal import _normal_input_materialization_hash

        receipt = SimpleNamespace(
            as_payload=lambda: {
                "table": "daily_bars",
                "start": "2019-01-01",
                "end": "2019-12-31",
                "partitions": [{"path": "daily.parquet", "sha256": "a" * 64}],
            }
        )

        def inputs(session_hash, evidence_hash):
            return SimpleNamespace(
                session_hash=session_hash,
                read_evidence=SimpleNamespace(
                    dataset_hash="b" * 64,
                    receipts=(receipt,),
                    trading_calendar_sessions=(date(2019, 1, 2),),
                    evidence_hash=evidence_hash,
                ),
            )

        first = _normal_input_materialization_hash(inputs("c" * 64, "d" * 64))
        same_data_new_permit = _normal_input_materialization_hash(
            inputs("c" * 64, "e" * 64)
        )
        changed_sessions = _normal_input_materialization_hash(
            inputs("f" * 64, "d" * 64)
        )

        self.assertEqual(first, same_data_new_permit)
        self.assertNotEqual(first, changed_sessions)

    def test_development_evidence_is_idempotent_from_fixed_completed_payload(self):
        from quantbot.cli_commands.normal import (
            _record_normal_evidence_idempotently,
        )
        from quantbot.evidence_auth import EvidenceAppendConflictError

        materialization = {
            "requirement_id": "normal_h1_experiment",
            "passed": False,
            "detail": "experiment_id=normal-h1-quality-diversification",
            "run_type": "historical_backtest",
            "observed_at": "2026-07-31T00:00:00+00:00",
            "dataset_hash": "a" * 64,
            "input_materialization_hash": "b" * 64,
            "output_hash": "c" * 64,
            "report_hash": "d" * 64,
            "cost_suite_hash": "e" * 64,
            "config_hash": "f" * 64,
            "cost_model_hash": "1" * 64,
            "code_hash": "2" * 64,
            "policy_version": None,
            "run_id": "normal-h1-adaptive-v1",
            "experiment_id": "normal-h1-quality-diversification",
        }
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "evidence.jsonl"
            _record_normal_evidence_idempotently(path, materialization)
            _record_normal_evidence_idempotently(path, materialization)
            self.assertEqual(len(path.read_text(encoding="utf-8").splitlines()), 1)

            conflict = {**materialization, "detail": "conflicting"}
            with self.assertRaisesRegex(
                EvidenceAppendConflictError,
                "conflicting payload",
            ):
                _record_normal_evidence_idempotently(path, conflict)

    def test_standard_cli_can_resolve_a_deployment_adapter(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import _resolve_normal_experiment_loader

        def adapter(**_kwargs):
            return None

        module = SimpleNamespace(load_normal_h1=adapter)
        with patch(
            "quantbot.cli_commands.normal.importlib.import_module",
            return_value=module,
        ) as importer:
            resolved = _resolve_normal_experiment_loader(
                SimpleNamespace(adapter="vendor_adapter:load_normal_h1"),
                CLIContext(stdout=io.StringIO()),
            )

        self.assertIs(resolved, adapter)
        importer.assert_called_once_with("vendor_adapter")

    def test_normal_adapter_spec_and_canonical_ledger_fail_closed(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import (
            _require_normal_canonical_ledger,
            _resolve_normal_experiment_loader,
        )

        with self.assertRaisesRegex(ValueError, "module:callable"):
            _resolve_normal_experiment_loader(
                SimpleNamespace(adapter="not-a-loader"),
                CLIContext(stdout=io.StringIO()),
            )
        with patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_LEDGER_PATH": "/tmp/canonical-ledger.jsonl"},
            clear=False,
        ), self.assertRaisesRegex(ValueError, "canonical"):
            _require_normal_canonical_ledger("/tmp/alternate-ledger.jsonl")
        with patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_LEDGER_PATH": "/tmp/normal-ledger-directory"},
            clear=False,
        ):
            _require_normal_canonical_ledger(
                "/tmp/normal-ledger-directory/exposure-ledger.jsonl"
            )

    def test_missing_verified_adapter_returns_without_consuming_exposure(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import handle_normal_experiment_run

        output = io.StringIO()
        args = SimpleNamespace(bundle_root="/external/pit")
        bundle = SimpleNamespace(dataset_hash="a" * 64)
        with patch(
            "quantbot.cli_commands.normal._normal_experiment_bundle",
            return_value=bundle,
        ), patch(
            "quantbot.cli_commands.normal.ExposureLedger",
            side_effect=AssertionError("adapter rejection must precede exposure claim"),
        ):
            exit_code = handle_normal_experiment_run(args, CLIContext(stdout=output))

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(payload["executed"])
        self.assertEqual(
            payload["experiment_id"],
            "normal-h1-quality-diversification",
        )
        self.assertFalse(payload["formal_admission_eligible"])
        self.assertEqual(payload["reason"], "verified_pit_normal_adapter_unavailable")
        self.assertEqual(payload["dataset_hash"], "a" * 64)

    def test_completed_execution_resume_skips_adapter_and_bundle_reads(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import handle_normal_experiment_run
        from quantbot.validation_protocol import ValidationWindowManifest

        adapter_calls = []

        def adapter(**_kwargs):
            adapter_calls.append(True)
            raise AssertionError("completed resume must not invoke adapter")

        window = {
            "start": date(2010, 1, 1),
            "end": date(2020, 12, 31),
            "warmup_start": date(2007, 1, 1),
            "warmup_end": date(2009, 12, 31),
        }
        manifest = ValidationWindowManifest(
            strategy_family="normal",
            mode="research",
            request_start=window["start"],
            request_end=window["end"],
            warmup_start=window["warmup_start"],
            warmup_end=window["warmup_end"],
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash="c" * 64,
            code_hash="d" * 64,
        )
        permit = SimpleNamespace(
            mode="research",
            request_start=window["start"],
            request_end=window["end"],
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash="c" * 64,
            code_hash="d" * 64,
        )
        execution_state = SimpleNamespace(
            completed=True,
            result_payload={"execution_complete": True},
            exit_code=1,
        )
        bundle = SimpleNamespace(
            dataset_hash="a" * 64,
            begin_read_session=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("completed resume must not read the bundle")
            ),
        )
        args = SimpleNamespace(
            experiment_id="normal-h2-capital-efficiency",
            historical_audit_replay=True,
            access_mode="research",
            bundle_root="/external/pit",
            adapter="",
            exposure_ledger="/tmp/exposure-ledger.jsonl",
            evidence_file="/tmp/evidence.jsonl",
            output_file="/tmp/normal-h2-result.json",
            run_id="normal-h2-resume",
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
            start=window["start"].isoformat(),
            end=window["end"].isoformat(),
            warmup_start=window["warmup_start"].isoformat(),
            warmup_end=window["warmup_end"].isoformat(),
        )
        materialize = patch(
            "quantbot.cli_commands.normal._materialize_completed_normal_experiment",
            return_value=1,
        )
        with patch(
            "quantbot.cli_commands.normal.resolve_runtime_config",
            return_value=None,
        ), patch(
            "quantbot.cli_commands.normal._normal_experiment_bundle",
            return_value=bundle,
        ), patch(
            "quantbot.cli_commands.normal._normal_experiment_manifest",
            return_value=(
                manifest,
                (
                    window["start"],
                    window["end"],
                    window["warmup_start"],
                    window["warmup_end"],
                    "b" * 64,
                    "c" * 64,
                    "d" * 64,
                    "e" * 64,
                    "f" * 64,
                ),
            ),
        ), patch(
            "quantbot.cli_commands.normal._require_bundle_coverage",
        ), patch(
            "quantbot.cli_commands.normal._require_normal_canonical_ledger",
        ), patch(
            "quantbot.cli_commands.normal.ExposureLedger",
        ) as exposure, patch(
            "quantbot.cli_commands.normal._begin_normal_experiment_execution",
            return_value=(object(), object(), execution_state),
        ), materialize as materializer:
            exposure.return_value.claim.return_value = permit
            exit_code = handle_normal_experiment_run(
                args,
                CLIContext(
                    stdout=io.StringIO(),
                    environ={},
                    normal_experiment_loader=adapter,
                ),
            )

        self.assertEqual(exit_code, 1)
        self.assertEqual(adapter_calls, [])
        materializer.assert_called_once()

    def test_adapter_cannot_skip_bundle_reads_and_failed_run_consumes_claim(self):
        from quantbot.data import NORMAL_H1_REQUIRED_TABLES, PointInTimeParquetBundle
        from quantbot.evidence import hash_payload
        from quantbot.exposure import ExposureLedger
        from quantbot.validation_protocol import ValidationWindowManifest

        fixture = pit_bundle_fixture.PointInTimeParquetBundleTest()
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as ledger_root:
            root_path = Path(root)
            fixture._build(root_path)
            ledger_path = Path(ledger_root).resolve() / "exposure.jsonl"

            with patch.dict(os.environ, {
                "STOCKBOT_EXPOSURE_HMAC_KEY": "test-key",
                "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
            }, clear=False):
                bundle = PointInTimeParquetBundle(root_path)
                manifest = ValidationWindowManifest(
                    strategy_family="normal",
                    mode="research",
                    request_start=date(2019, 2, 1),
                    request_end=date(2019, 12, 31),
                    warmup_start=date(2019, 1, 1),
                    warmup_end=date(2019, 1, 31),
                    dataset_hash=bundle.dataset_hash,
                    config_hash=hash_payload({"config": "malicious-adapter"}),
                    cost_model_hash=hash_payload({"cost": "malicious-adapter"}),
                    code_hash=hash_payload({"code": "malicious-adapter"}),
                )
                permit = ExposureLedger(ledger_path).claim(
                    manifest,
                    run_id="normal-malicious-adapter",
                )
                read_session = bundle.begin_read_session(
                    permit=permit,
                    strategy_family="normal",
                )
                with self.assertRaisesRegex(PermissionError, "missing required tables"):
                    read_session.seal(
                        required_tables=NORMAL_H1_REQUIRED_TABLES,
                    )

                records = ExposureLedger(ledger_path).verify()
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0].outcome, "granted")
                self.assertEqual(records[0].run_id, "normal-malicious-adapter")

    def test_partial_research_window_is_rejected_before_exposure_claim(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import handle_normal_experiment_run

        def adapter(**_kwargs):
            return None

        args = SimpleNamespace(
            access_mode="research",
            experiment_id="normal-h1-quality-diversification",
            historical_audit_replay=False,
            bundle_root="/external/pit",
            adapter="",
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
            start="2019-01-01",
            end="2019-12-31",
            warmup_start="2018-01-01",
            warmup_end="2018-12-31",
        )
        bundle = SimpleNamespace(dataset_hash="a" * 64)
        with patch(
            "quantbot.cli_commands.normal._normal_experiment_bundle",
            return_value=bundle,
        ), patch(
            "quantbot.cli_commands.normal.ExposureLedger",
            side_effect=AssertionError("partial window must not consume exposure"),
        ):
            with self.assertRaisesRegex(ValueError, "window is frozen"):
                handle_normal_experiment_run(
                    args,
                    CLIContext(
                        stdout=io.StringIO(),
                        normal_experiment_loader=adapter,
                    ),
                )

    def test_adapter_identity_hashes_actual_artifact_and_schema_mapping(self):
        from quantbot.cli_commands.normal import _normal_adapter_code_hash

        def adapter(**_kwargs):
            return None

        actual = _normal_adapter_code_hash(adapter)
        adapter.stockbot_adapter_code_hash = "a" * 64
        self.assertEqual(_normal_adapter_code_hash(adapter), actual)

        adapter.stockbot_schema_mapping = {"daily_bars": {"open": "px_open"}}
        mapped = _normal_adapter_code_hash(adapter)
        self.assertNotEqual(mapped, actual)

        del adapter.stockbot_schema_mapping
        adapter.stockbot_schema_mapping_hash = "b" * 64
        with self.assertRaisesRegex(ValueError, "self-attested"):
            _normal_adapter_code_hash(adapter)

    def test_sealed_adapter_requires_external_trust_before_exposure_claim(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import handle_normal_experiment_run

        def adapter(**_kwargs):
            return None

        bundle = SimpleNamespace(
            dataset_hash="a" * 64,
            manifest=SimpleNamespace(
                provider="vendor",
                schema_version="1",
                bundle_id="bundle-1",
            ),
        )
        with patch(
            "quantbot.cli_commands.normal._normal_experiment_bundle",
            return_value=bundle,
        ), patch(
            "quantbot.cli_commands.normal.ExposureLedger",
            side_effect=AssertionError("untrusted adapter must be rejected before claim"),
        ):
            with self.assertRaisesRegex(
                PermissionError,
                "STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH",
            ):
                handle_normal_experiment_run(
                    SimpleNamespace(access_mode="sealed"),
                    CLIContext(
                        stdout=io.StringIO(),
                        environ={},
                        normal_experiment_loader=adapter,
                    ),
                )

    def test_trusted_attestation_binds_actual_adapter_mapping_and_bundle(self):
        from quantbot.normal.adapter_trust import (
            normal_adapter_artifact_identity,
            verify_normal_adapter_attestation,
        )

        def adapter(**_kwargs):
            return None

        adapter.stockbot_schema_mapping = {"daily_bars": {"open": "px_open"}}
        bundle = SimpleNamespace(
            dataset_hash="a" * 64,
            manifest=SimpleNamespace(
                provider="vendor",
                schema_version="1",
                bundle_id="bundle-1",
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "adapter-attestation.json"
            identity, _payload = _write_normal_adapter_attestation(
                path,
                adapter,
                bundle,
            )
            verified = verify_normal_adapter_attestation(
                path,
                adapter_identity=identity,
                bundle_manifest=bundle.manifest,
                bundle_dataset_hash=bundle.dataset_hash,
                trusted_keys={NORMAL_ADAPTER_ISSUER: NORMAL_ADAPTER_KEY},
            )
            self.assertEqual(verified.adapter_artifact_hash, identity.adapter_artifact_hash)

            adapter.stockbot_schema_mapping = {"daily_bars": {"open": "forged"}}
            with self.assertRaisesRegex(PermissionError, "schema_mapping_hash"):
                verify_normal_adapter_attestation(
                    path,
                    adapter_identity=normal_adapter_artifact_identity(adapter),
                    bundle_manifest=bundle.manifest,
                    bundle_dataset_hash=bundle.dataset_hash,
                    trusted_keys={NORMAL_ADAPTER_ISSUER: NORMAL_ADAPTER_KEY},
                )

            adapter.stockbot_schema_mapping = {"daily_bars": {"open": "px_open"}}
            other_bundle = SimpleNamespace(
                provider="other-vendor",
                schema_version="1",
                bundle_id="bundle-1",
            )
            with self.assertRaisesRegex(PermissionError, "bundle_provider"):
                verify_normal_adapter_attestation(
                    path,
                    adapter_identity=identity,
                    bundle_manifest=other_bundle,
                    bundle_dataset_hash=bundle.dataset_hash,
                    trusted_keys={NORMAL_ADAPTER_ISSUER: NORMAL_ADAPTER_KEY},
                )

            def forged_adapter(**_kwargs):
                return "fabricated-market-data"

            forged_adapter.__module__ = adapter.__module__
            forged_adapter.__qualname__ = adapter.__qualname__
            forged_adapter.stockbot_schema_mapping = adapter.stockbot_schema_mapping
            with self.assertRaisesRegex(PermissionError, "adapter_artifact_hash"):
                verify_normal_adapter_attestation(
                    path,
                    adapter_identity=normal_adapter_artifact_identity(
                        forged_adapter
                    ),
                    bundle_manifest=bundle.manifest,
                    bundle_dataset_hash=bundle.dataset_hash,
                    trusted_keys={NORMAL_ADAPTER_ISSUER: NORMAL_ADAPTER_KEY},
                )

    def test_adapter_attestation_replacement_after_claim_is_rejected(self):
        from quantbot.cli_commands.normal import (
            _normal_adapter_attestation,
            _normal_experiment_code_hash,
            _require_unchanged_normal_adapter_trust,
        )

        def adapter(**_kwargs):
            return None

        bundle = SimpleNamespace(
            dataset_hash="a" * 64,
            manifest=SimpleNamespace(
                provider="vendor",
                schema_version="1",
                bundle_id="bundle-1",
            ),
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "adapter-attestation.json"
            identity, _payload = _write_normal_adapter_attestation(
                path,
                adapter,
                bundle,
            )
            replacement_issuer = "replacement-auditor"
            replacement_key = b"replacement-adapter-key"
            environ = {
                "STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH": str(path),
                "STOCKBOT_TRUSTED_NORMAL_ADAPTER_KEYS_JSON": json.dumps(
                    {
                        NORMAL_ADAPTER_ISSUER: NORMAL_ADAPTER_KEY.hex(),
                        replacement_issuer: replacement_key.hex(),
                    }
                ),
            }
            args = SimpleNamespace(access_mode="sealed")
            original = _normal_adapter_attestation(
                args,
                bundle,
                identity,
                environ=environ,
            )
            code_hash = _normal_experiment_code_hash(
                identity.adapter_artifact_hash,
                original,
            )
            _write_normal_adapter_attestation(
                path,
                adapter,
                bundle,
                issuer=replacement_issuer,
                key=replacement_key,
            )
            with self.assertRaisesRegex(PermissionError, "changed after claim"):
                _require_unchanged_normal_adapter_trust(
                    args,
                    bundle,
                    adapter,
                    identity,
                    original,
                    code_hash=code_hash,
                    environ=environ,
                )

    def test_permit_identity_binds_trusted_adapter_attestation(self):
        from quantbot.cli_commands.normal import _normal_experiment_manifest
        from quantbot.normal.adapter_trust import verify_normal_adapter_attestation

        def adapter(**_kwargs):
            return None

        bundle = SimpleNamespace(
            dataset_hash="a" * 64,
            manifest=SimpleNamespace(
                provider="vendor",
                schema_version="1",
                bundle_id="bundle-1",
            ),
        )
        args = SimpleNamespace(
            access_mode="research",
            start="2010-01-01",
            end="2020-12-31",
            warmup_start="2007-01-01",
            warmup_end="2009-12-31",
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
        )
        with tempfile.TemporaryDirectory() as root:
            path = Path(root).resolve() / "adapter-attestation.json"
            identity, _payload = _write_normal_adapter_attestation(
                path,
                adapter,
                bundle,
            )
            attestation = verify_normal_adapter_attestation(
                path,
                adapter_identity=identity,
                bundle_manifest=bundle.manifest,
                bundle_dataset_hash=bundle.dataset_hash,
                trusted_keys={NORMAL_ADAPTER_ISSUER: NORMAL_ADAPTER_KEY},
            )
            trusted_manifest, _identity = _normal_experiment_manifest(
                args,
                bundle,
                adapter_code_hash=identity.adapter_artifact_hash,
                adapter_attestation=attestation,
            )
            untrusted_manifest, _identity = _normal_experiment_manifest(
                args,
                bundle,
                adapter_code_hash=identity.adapter_artifact_hash,
                adapter_attestation=None,
            )
            self.assertNotEqual(
                trusted_manifest.config_hash,
                untrusted_manifest.config_hash,
            )
            self.assertNotEqual(
                trusted_manifest.code_hash,
                untrusted_manifest.code_hash,
            )

    def test_protocol_costs_are_frozen_before_exposure_claim(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import handle_normal_experiment_run

        def adapter(**_kwargs):
            return None

        base_args = {
            "bundle_root": "/external/pit",
            "exposure_ledger": "/tmp/normal-ledger.jsonl",
            "evidence_file": "/tmp/normal-evidence.jsonl",
            "run_id": "normal-cost-attack",
            "access_mode": "research",
            "start": "2019-01-01",
            "end": "2019-12-31",
            "warmup_start": "2018-01-01",
            "warmup_end": "2018-12-31",
            "initial_equity": 100_000.0,
            "cost_bps_per_side": 5.0,
            "stress_cost_bps_per_side": 10.0,
            "output_file": "",
        }
        bundle = SimpleNamespace(dataset_hash="a" * 64)
        for field, value, message in (
            ("cost_bps_per_side", 4.0, "frozen at 5.0"),
            ("stress_cost_bps_per_side", 9.0, "frozen at 10.0"),
        ):
            with self.subTest(field=field), patch(
                "quantbot.cli_commands.normal._normal_experiment_bundle",
                return_value=bundle,
            ), patch(
                "quantbot.cli_commands.normal.ExposureLedger",
                side_effect=AssertionError("invalid protocol must not claim exposure"),
            ):
                with self.assertRaisesRegex(ValueError, message):
                    handle_normal_experiment_run(
                        SimpleNamespace(**{**base_args, field: value}),
                        CLIContext(
                            stdout=io.StringIO(),
                            normal_experiment_loader=adapter,
                        ),
                    )

    def test_sealed_direct_writer_cannot_bypass_authenticated_execution(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import _write_normal_experiment_payload

        with tempfile.TemporaryDirectory() as root:
            evidence_path = Path(root) / "evidence.jsonl"
            payload = {
                "formal_admission_eligible": False,
                "decision": {"passed": False, "reasons": ["sharpe"]},
                "permit_record_hash": "a" * 64,
                "bundle_read_evidence_hash": "b" * 64,
                "cost_model_hash": "c" * 64,
                "dataset_hash": "d" * 64,
                "config_hash": "e" * 64,
                "code_hash": "f" * 64,
                "input_materialization_hash": "1" * 64,
                "output_hash": "2" * 64,
                "report_hash": "3" * 64,
                "cost_suite_hash": "4" * 64,
                "experiment_id": "normal-h1-quality-diversification",
            }
            payload["admission_evidence"] = _normal_admission_evidence(
                payload,
                passed=False,
                run_id="normal-sealed-rejected",
            )
            with self.assertRaisesRegex(
                ValueError,
                "authenticated completed execution",
            ):
                _write_normal_experiment_payload(
                    SimpleNamespace(
                        access_mode="sealed",
                        evidence_file=str(evidence_path),
                        output_file="",
                        run_id="normal-sealed-rejected",
                    ),
                    CLIContext(stdout=io.StringIO()),
                    payload,
                )
            self.assertFalse(evidence_path.exists())

    def test_sealed_pass_also_cannot_use_legacy_direct_writer(self):
        from quantbot.cli_commands.context import CLIContext
        from quantbot.cli_commands.normal import _write_normal_experiment_payload

        with tempfile.TemporaryDirectory() as root:
            evidence_path = Path(root) / "evidence.jsonl"
            payload = {
                "formal_admission_eligible": True,
                "decision": {"passed": True, "reasons": []},
                "permit_record_hash": "a" * 64,
                "bundle_read_evidence_hash": "b" * 64,
                "cost_model_hash": "c" * 64,
                "dataset_hash": "d" * 64,
                "config_hash": "e" * 64,
                "code_hash": "f" * 64,
                "input_materialization_hash": "1" * 64,
                "output_hash": "2" * 64,
                "report_hash": "3" * 64,
                "cost_suite_hash": "4" * 64,
                "experiment_id": "normal-h1-quality-diversification",
            }
            payload["admission_evidence"] = _normal_admission_evidence(
                payload,
                passed=True,
                run_id="normal-sealed-admitted",
            )
            with self.assertRaisesRegex(
                ValueError,
                "authenticated completed execution",
            ):
                _write_normal_experiment_payload(
                    SimpleNamespace(
                        access_mode="sealed",
                        evidence_file=str(evidence_path),
                        output_file="",
                        run_id="normal-sealed-admitted",
                    ),
                    CLIContext(stdout=io.StringIO()),
                    payload,
                )


if __name__ == "__main__":
    unittest.main()
