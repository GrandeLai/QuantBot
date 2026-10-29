import io
import hmac
import json
import os
import sys
import tempfile
import unittest
from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.research_cli import main
import quantbot.cli_commands.daytrade as cli_daytrade_module
import quantbot.cli_commands.daytrade_support as daytrade_support_module
from quantbot.daytrade import DayTradeConfig, DayTradeMinuteCache, minute_bar_from_row
from quantbot.daytrade_strategy import DailyUniverseSnapshot
from quantbot.data.pit_calendar import TradingSession, sign_trading_calendar
from quantbot.evidence import hash_payload
from quantbot.audit import (
    CompletionAudit,
    RequirementEvidence,
    load_evidence_file,
    load_evidence_records,
)
from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.exposure import ExposureDeniedError, ExposureLedger
from quantbot.validation_protocol import (
    ValidationWindowManifest,
    formal_sealed_warmup_interval,
)
from quantbot.cli_commands.daytrade_support import (
    DAYTRADE_TOP50_SELECTION_POLICY,
    DayTradeDataAccess,
    DayTradeUniverseManifest,
    daytrade_data_access_payload,
    load_daytrade_bars_for_symbols_from_args,
    minute_bars_hash,
    prepare_daytrade_data_access,
)
from tests.historical_trial_fixtures import (
    sign_historical_trial_artifact_payload,
    signed_historical_trial_artifact,
)
from tests.test_stockbot_daytrade_validation_artifact import (
    ISSUER as ADAPTIVE_ISSUER,
    KEY as ADAPTIVE_KEY,
    _signed_artifact as signed_adaptive_validation_artifact,
)


CALENDAR_ISSUER = "daytrade-test-calendar"
CALENDAR_KEY = b"daytrade-test-calendar-key"
UNIVERSE_ISSUER = "daytrade-test-universe-builder"
UNIVERSE_KEY = b"daytrade-test-universe-key"


def _signed_calendar_payload(sessions):
    calendar = sign_trading_calendar(
        exchange="XNYS",
        effective_date_timezone="America/New_York",
        issuer=CALENDAR_ISSUER,
        issuer_version="1",
        generated_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
        coverage_start=sessions[0],
        coverage_end=sessions[-1],
        missing_intervals=(),
        sessions=tuple(
            TradingSession(
                session_date=item,
                close_at=datetime(
                    item.year,
                    item.month,
                    item.day,
                    21,
                    tzinfo=timezone.utc,
                ),
            )
            for item in sessions
        ),
        signing_key=CALENDAR_KEY,
    )
    return {
        "exchange": calendar.exchange,
        "effective_date_timezone": calendar.effective_date_timezone,
        "issuer": calendar.issuer,
        "issuer_version": calendar.issuer_version,
        "generated_at": calendar.generated_at.isoformat(),
        "coverage_start": calendar.coverage_start.isoformat(),
        "coverage_end": calendar.coverage_end.isoformat(),
        "missing_intervals": list(calendar.missing_intervals),
        "sessions": [
            {
                "session_date": item.session_date.isoformat(),
                "close_at": item.close_at.isoformat(),
            }
            for item in calendar.sessions
        ],
        "content_hash": calendar.content_hash,
        "signature": calendar.signature,
    }


def _construction_proof(
    snapshots,
    *,
    source_dataset_hash,
    calendar_hash,
):
    proof = {
        "proof_type": "stockbot_pit_bundle_core_v1",
        "issuer": UNIVERSE_ISSUER,
        "source_dataset_hash": source_dataset_hash,
        "calendar_hash": calendar_hash,
        "selection_policy_hash": hash_payload(
            dict(DAYTRADE_TOP50_SELECTION_POLICY)
        ),
        "input_rows_hash": "f" * 64,
        "ranked_snapshots_hash": hash_payload(snapshots),
    }
    content_hash = hash_payload(proof)
    return {
        **proof,
        "content_hash": content_hash,
        "signature": hmac.new(
            UNIVERSE_KEY,
            content_hash.encode("ascii"),
            sha256,
        ).hexdigest(),
    }
from tests.test_stockbot_cli import (
    _synthetic_daytrade_rows_for_symbols_and_dates,
    _synthetic_feature_daytrade_loader,
)


class StockBotDynamicDayTradeCliTest(unittest.TestCase):
    def test_sealed_daytrade_interval_is_reserved_for_the_single_h9_run(self):
        cases = (
            ("daytrade-cache-fill", ""),
            ("daytrade-backtest-run", ""),
            ("daytrade-compare-configs", ""),
            ("daytrade-optimize-run", "h3-rvol"),
        )
        for command, experiment_set in cases:
            with self.subTest(command=command, experiment_set=experiment_set):
                args = SimpleNamespace(
                    command=command,
                    experiment_set=experiment_set,
                    access_mode="sealed",
                    universe_manifest="protected-universe.json",
                )
                with mock.patch.object(
                    daytrade_support_module,
                    "load_daytrade_universe_manifest",
                ) as manifest_loader, self.assertRaisesRegex(
                    PermissionError,
                    "reserved.*H3-vs-H9",
                ):
                    prepare_daytrade_data_access(args, DayTradeConfig())
                manifest_loader.assert_not_called()

    def test_unreleased_h9_rejects_before_opening_universe_body(self):
        args = SimpleNamespace(
            command="daytrade-optimize-run",
            experiment_set="h9-rvol-reward-risk",
            access_mode="sealed",
            universe_manifest="protected-universe.json",
            start="2026-08-01",
            end="2028-07-31",
        )
        with mock.patch(
            "quantbot.validation_protocol._utc_now",
            return_value=datetime(2026, 7, 20, tzinfo=timezone.utc),
        ), mock.patch.object(
            daytrade_support_module,
            "load_daytrade_universe_manifest",
        ) as manifest_loader, self.assertRaisesRegex(
            PermissionError,
            "not released",
        ):
            prepare_daytrade_data_access(args, DayTradeConfig())
        manifest_loader.assert_not_called()

    def test_released_h9_claims_permit_before_full_sealed_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger_path = root / "exposure-ledger.jsonl"
            universe = DayTradeUniverseManifest(
                path=root / "universe.json",
                dataset_hash="a" * 64,
                manifest_hash="b" * 64,
                snapshots=(
                    DailyUniverseSnapshot(
                        date(2026, 8, 1),
                        {"security-1": "AAPL"},
                    ),
                ),
                suspended_sessions=frozenset(),
                selection_policy=DAYTRADE_TOP50_SELECTION_POLICY,
                minute_data_hash="c" * 64,
                calendar_hash="d" * 64,
                construction_evidence={"content_hash": "e" * 64},
            )
            args = SimpleNamespace(
                command="daytrade-optimize-run",
                experiment_set="h9-rvol-reward-risk",
                access_mode="sealed",
                universe_manifest=str(universe.path),
                start="2026-08-01",
                end="2028-07-31",
                warmup_start="",
                exposure_ledger=str(root),
                run_id="released-h9",
                cache_dir=str(root / "cache"),
                dataset_hash="",
                config_hash="",
                cost_model_hash="",
                code_hash="",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "released-test-key",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root),
                },
            ), mock.patch(
                "quantbot.validation_protocol._utc_now",
                return_value=datetime(2028, 8, 1, tzinfo=timezone.utc),
            ), mock.patch.object(
                daytrade_support_module,
                "load_daytrade_universe_manifest",
                return_value=universe,
            ) as loader, mock.patch.object(
                daytrade_support_module,
                "_require_frozen_dynamic_identity",
            ), mock.patch.object(
                daytrade_support_module,
                "_require_external_dynamic_cache",
            ):
                access = prepare_daytrade_data_access(
                    args,
                    DayTradeConfig(),
                    warmup_start=date(2026, 6, 2),
                    config_identity={"sealed": "h9"},
                    cost_identity={"cost_bps": 5.0},
                )

            loader.assert_called_once()
            self.assertEqual(access.permit.mode, "sealed")
            self.assertEqual(access.permit.run_id, "released-h9")

    def test_history_artifact_loader_rejects_tampering_and_unknown_issuer(self):
        issuer = "history-loader-test"
        key = b"history-loader-test-key"
        identity = {
            "source_dataset_hash": "a" * 64,
            "minute_data_hash": "b" * 64,
            "calendar_hash": "c" * 64,
            "construction_evidence_hash": "d" * 64,
        }
        artifact, payload, historical_dataset_hash = (
            signed_historical_trial_artifact(
                issuer=issuer,
                signing_key=key,
                historical_identity=identity,
                observation_dates=(date(2024, 1, 2),),
                trial_returns={"daytrade-h1:trial-01": (0.001,)},
                experiment_by_trial={"daytrade-h1:trial-01": "daytrade-h1"},
            )
        )
        content_hash = artifact["content_hash"]
        kwargs = {
            "source_dataset_hash": identity["source_dataset_hash"],
            "minute_data_hash": identity["minute_data_hash"],
            "calendar_hash": identity["calendar_hash"],
            "construction_evidence": {
                "content_hash": identity["construction_evidence_hash"]
            },
        }
        with mock.patch.dict(
            os.environ,
            {
                "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON": json.dumps(
                    {issuer: key.hex()}
                )
            },
        ):
            verified = daytrade_support_module._historical_trial_artifact(
                artifact,
                **kwargs,
            )
            self.assertEqual(verified["content_hash"], content_hash)

            tampered = {
                **artifact,
                "trial_daily_returns": {"daytrade-h1:trial-01": [0.9]},
            }
            with self.assertRaisesRegex(ValueError, "content_hash mismatch"):
                daytrade_support_module._historical_trial_artifact(
                    tampered,
                    **kwargs,
                )

            unknown_payload = {**payload, "issuer": "unknown-history-loader"}
            unknown = sign_historical_trial_artifact_payload(
                unknown_payload,
                signing_key=key,
            )
            with self.assertRaisesRegex(ValueError, "issuer key is missing"):
                daytrade_support_module._historical_trial_artifact(
                    unknown,
                    **kwargs,
                )

    def test_sealed_universe_rejects_same_period_historical_trial_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "sealed-universe.json"
            path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "a" * 64,
                        "snapshots": [
                            {
                                "trading_day": "2026-08-01",
                                "ticker_by_security_id": {
                                    "security-1": "AAPL"
                                },
                            }
                        ],
                        "historical_trial_artifact": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "must not contain.*historical_trial_artifact",
            ):
                daytrade_support_module.load_daytrade_universe_manifest(
                    path,
                    request_start=date(2026, 8, 1),
                    request_end=date(2028, 7, 31),
                    allow_historical_trial_artifact=False,
                )

    def test_historical_artifact_hash_is_part_of_composite_dataset_identity(self):
        snapshot = DailyUniverseSnapshot(
            date(2024, 1, 2),
            {"security-1": "AAPL"},
        )
        common = {
            "path": Path("/tmp/universe.json"),
            "dataset_hash": "a" * 64,
            "manifest_hash": "b" * 64,
            "snapshots": (snapshot,),
            "suspended_sessions": frozenset(),
            "minute_data_hash": "c" * 64,
            "calendar_hash": "d" * 64,
            "construction_evidence": {"content_hash": "e" * 64},
        }
        first = DayTradeUniverseManifest(
            **common,
            historical_trial_artifact={"content_hash": "f" * 64},
        )
        second = DayTradeUniverseManifest(
            **common,
            historical_trial_artifact={"content_hash": "0" * 64},
        )
        missing = DayTradeUniverseManifest(**common)

        first_hash = daytrade_support_module._dynamic_evaluation_dataset_hash(first)
        second_hash = daytrade_support_module._dynamic_evaluation_dataset_hash(second)
        missing_hash = daytrade_support_module._dynamic_evaluation_dataset_hash(missing)

        self.assertNotEqual(first_hash, second_hash)
        self.assertNotEqual(first_hash, missing_hash)
        self.assertNotEqual(second_hash, missing_hash)

    def test_h9_development_modes_can_only_report_candidate_survival(self):
        windows = {
            "research": (date(2019, 1, 2), date(2019, 1, 3)),
            "adaptive_development": (date(2023, 1, 3), date(2023, 1, 4)),
            "fixed_holdout": (date(2024, 1, 2), date(2024, 1, 3)),
        }
        for mode, (start, end) in windows.items():
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmpdir:
                ledger_path = Path(tmpdir) / "exposure.jsonl"
                with mock.patch.dict(
                    os.environ,
                    {
                        "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                        "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                    },
                ):
                    permit = ExposureLedger(ledger_path).claim(
                        ValidationWindowManifest(
                            strategy_family="daytrade",
                            mode=mode,
                            request_start=start,
                            request_end=end,
                            dataset_hash="a" * 64,
                            config_hash="b" * 64,
                            cost_model_hash="c" * 64,
                            code_hash="d" * 64,
                        ),
                        run_id=f"h9-{mode}",
                        claimed_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
                    )
                    admission = cli_daytrade_module._h9_admission_semantics(
                        {"passed": True, "reasons": []},
                        DayTradeDataAccess(symbols=("AAPL",), permit=permit),
                    )

                self.assertTrue(admission["candidate_survived"])
                self.assertFalse(admission["passed"])
                self.assertFalse(admission["formal_admission_eligible"])
                self.assertEqual(admission["decision"], "candidate_survived")
                self.assertEqual(admission["status"], "development_not_proven")
                self.assertIn(
                    f"access_mode_not_sealed:{mode}",
                    admission["admission_reasons"],
                )

    def test_h9_semantics_reuses_unified_gate_and_rejects_leverage_above_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "exposure.jsonl"
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                },
            ):
                permit = ExposureLedger(ledger_path).claim(
                    ValidationWindowManifest(
                        strategy_family="daytrade",
                        mode="adaptive_development",
                        request_start=date(2023, 1, 1),
                        request_end=date(2024, 12, 31),
                        dataset_hash="a" * 64,
                        config_hash="b" * 64,
                        cost_model_hash="c" * 64,
                        code_hash="d" * 64,
                    ),
                    run_id="h9-unified-leverage",
                    claimed_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
                )
                metrics = {
                    "oos_months": 24,
                    "oos_trades": 300,
                    "net_return_after_5bps": 0.10,
                    "profit_factor": 1.20,
                    "sharpe": 1.0,
                    "max_drawdown": 0.04,
                    "annual_net_return": 0.12,
                    "baseline_annual_net_return": 0.10,
                    "baseline_sharpe": 0.75,
                    "baseline_max_drawdown": 0.04,
                    "stressed_12bps_cumulative_return": -0.02,
                    "stressed_12bps_max_drawdown": 0.07,
                    "positive_quarter_fraction": 0.60,
                    "top_symbol_profit_contribution": 0.25,
                    "top_two_profit_contribution": 0.50,
                    "dsr_confidence": 0.95,
                    "pbo": 0.05,
                    "gross_leverage": 1.01,
                    "declared_trials": 21,
                    "completed_trials": 21,
                }
                rejected = cli_daytrade_module._h9_admission_semantics(
                    {
                        "passed": True,
                        "admission_metrics": metrics,
                        "protocol_reasons": [],
                    },
                    DayTradeDataAccess(symbols=("AAPL",), permit=permit),
                )
                survived = cli_daytrade_module._h9_admission_semantics(
                    {
                        "passed": True,
                        "admission_metrics": {**metrics, "gross_leverage": 1.0},
                        "protocol_reasons": [],
                    },
                    DayTradeDataAccess(symbols=("AAPL",), permit=permit),
                )

            self.assertFalse(rejected["candidate_survived"])
            self.assertIn("leverage", rejected["admission_reasons"])
            self.assertTrue(survived["candidate_survived"])
            self.assertEqual(
                survived["admission_reasons"],
                ["access_mode_not_sealed:adaptive_development"],
            )

    def test_h9_admits_only_a_completed_canonical_sealed_permit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "exposure.jsonl"
            released_at = datetime(2028, 8, 1, tzinfo=timezone.utc)
            warmup = formal_sealed_warmup_interval(
                "daytrade", date(2026, 8, 1)
            )
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                },
            ):
                permit = ExposureLedger(
                    ledger_path,
                    _clock=lambda: released_at,
                ).claim(
                    ValidationWindowManifest(
                        strategy_family="daytrade",
                        mode="sealed",
                        request_start=date(2026, 8, 1),
                        request_end=date(2028, 7, 31),
                        warmup_start=warmup.start,
                        warmup_end=warmup.end,
                        dataset_hash="a" * 64,
                        config_hash="b" * 64,
                        cost_model_hash="c" * 64,
                        code_hash="d" * 64,
                    ),
                    run_id="h9-formal-sealed",
                )
                admission = cli_daytrade_module._h9_admission_semantics(
                    {"passed": True, "reasons": []},
                    DayTradeDataAccess(symbols=("AAPL",), permit=permit),
                    _clock=lambda: released_at,
                )

            self.assertTrue(admission["passed"])
            self.assertTrue(admission["formal_admission_eligible"])
            self.assertEqual(admission["decision"], "admit")
            self.assertEqual(admission["status"], "admitted_candidate")
            self.assertEqual(admission["admission_reasons"], [])

    def test_h9_sealed_claim_is_denied_before_the_formal_window_is_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger_path = Path(tmpdir) / "exposure.jsonl"
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                },
            ):
                with self.assertRaisesRegex(ExposureDeniedError, "not complete"):
                    ExposureLedger(ledger_path).claim(
                        ValidationWindowManifest(
                            strategy_family="daytrade",
                            mode="sealed",
                            request_start=date(2026, 8, 1),
                            request_end=date(2028, 7, 31),
                            dataset_hash="a" * 64,
                            config_hash="b" * 64,
                            cost_model_hash="c" * 64,
                            code_hash="d" * 64,
                        ),
                        run_id="h9-premature-sealed",
                        claimed_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
                    )
                self.assertEqual(ExposureLedger(ledger_path).records[0].outcome, "denied")

    def test_h9_walk_forward_protocol_is_frozen_into_config_identity(self):
        args = SimpleNamespace(
            experiment_set="h9-rvol-reward-risk",
            max_candidates=None,
            train_months=3,
            validation_months=1,
            feature_warmup_days=60,
            feature_observation_end="",
            sector_proxies_json="",
        )
        effective = cli_daytrade_module._effective_daytrade_max_candidates(args)
        protocol = cli_daytrade_module._daytrade_optimization_protocol(
            args,
            effective_max_candidates=effective,
        )
        baseline = cli_daytrade_module._canonical_h9_baseline_config()
        candidates = cli_daytrade_module.daytrade_h9_candidate_configs(baseline)
        identity = cli_daytrade_module._daytrade_optimization_config_identity(
            args,
            optimization_base_config=baseline,
            candidate_configs=candidates,
            feature_requirements=None,
            optimization_protocol=protocol,
        )

        self.assertEqual(effective, 2)
        self.assertEqual(
            identity["optimization_protocol"],
            {
                "train_months": 3,
                "validation_months": 1,
                "max_candidates": 2,
                "purge_days": 1,
                "embargo_days": 1,
                "feature_warmup_days": 60,
                "feature_observation_end": None,
                "sector_proxies": None,
            },
        )
        self.assertEqual(
            set(identity["candidate_configs"]),
            {"daytrade-h9-rvol-reward-risk:trial-01"},
        )
        self.assertEqual(
            cli_daytrade_module._daytrade_optimization_cost_identity(
                args,
                baseline,
            ),
            {
                "execution": "next_available_minute_event",
                "cost_bps_per_side": 5.0,
                "max_volume_participation": 0.10,
                "stress_cost_bps_per_side": 12.0,
            },
        )

        args.max_candidates = 3
        with self.assertRaisesRegex(ValueError, "frozen at 2"):
            cli_daytrade_module._effective_daytrade_max_candidates(args)
        args.max_candidates = None
        for name, value in (
            ("train_months", 4),
            ("validation_months", 2),
            ("feature_warmup_days", 30),
            ("feature_observation_end", "10:30"),
            ("sector_proxies_json", json.dumps({"AAPL": "XLK"})),
        ):
            drifted_args = SimpleNamespace(**vars(args))
            setattr(drifted_args, name, value)
            with self.subTest(protocol_field=name), self.assertRaisesRegex(
                ValueError,
                name.replace("_json", ""),
            ):
                cli_daytrade_module._daytrade_optimization_protocol(
                    drifted_args,
                    effective_max_candidates=2,
                )

    def test_h3_runtime_requires_research_window_and_exact_60_day_warmup(self):
        args = SimpleNamespace(
            experiment_set="h3-rvol",
            train_months=3,
            validation_months=1,
            feature_warmup_days=30,
            feature_observation_end="",
            sector_proxies_json="",
        )
        with self.assertRaisesRegex(ValueError, "frozen at 60"):
            cli_daytrade_module._daytrade_optimization_protocol(
                args,
                effective_max_candidates=3,
            )
        args.feature_warmup_days = 60
        protocol = cli_daytrade_module._daytrade_optimization_protocol(
            args,
            effective_max_candidates=3,
        )
        self.assertEqual(protocol["feature_warmup_days"], 60)

    def test_canonical_exposure_ledger_directory_and_jsonl_are_equivalent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger_file = root / "exposure-ledger.jsonl"
            with mock.patch.dict(
                os.environ,
                {"STOCKBOT_EXPOSURE_LEDGER_PATH": str(root)},
            ):
                daytrade_support_module._require_canonical_ledger(
                    str(ledger_file)
                )
            with mock.patch.dict(
                os.environ,
                {"STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_file)},
            ):
                daytrade_support_module._require_canonical_ledger(str(root))

    def test_sealed_h9_requires_and_loads_signed_adaptive_validation_artifact(self):
        args = SimpleNamespace(
            experiment_set="h9-rvol-reward-risk",
            access_mode="sealed",
            adaptive_validation_artifact="",
        )
        with self.assertRaisesRegex(ValueError, "requires.*adaptive-validation"):
            cli_daytrade_module._resolve_h9_adaptive_validation_artifact(
                args,
                {},
            )
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "adaptive.json"
            artifact_path.write_text(
                json.dumps(signed_adaptive_validation_artifact()),
                encoding="utf-8",
            )
            artifact = cli_daytrade_module._resolve_h9_adaptive_validation_artifact(
                args,
                {
                    "STOCKBOT_DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH": str(
                        artifact_path
                    ),
                    "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON": json.dumps(
                        {ADAPTIVE_ISSUER: ADAPTIVE_KEY.hex()}
                    ),
                },
            )
        self.assertEqual(artifact.completed_trial_count, 21)

    def test_h3_h9_request_windows_are_exact_and_reject_before_prepare(self):
        accepted = (
            ("h3-rvol", "research", "2019-01-01", "2022-12-31"),
            (
                "h9-rvol-reward-risk",
                "research",
                "2019-01-01",
                "2022-12-31",
            ),
            (
                "h9-rvol-reward-risk",
                "adaptive_development",
                "2023-01-01",
                "2024-12-31",
            ),
            (
                "h9-rvol-reward-risk",
                "sealed",
                "2026-08-01",
                "2028-07-31",
            ),
        )
        for experiment_set, mode, start, end in accepted:
            with self.subTest(experiment_set=experiment_set, mode=mode):
                cli_daytrade_module._require_exact_daytrade_experiment_window(
                    SimpleNamespace(
                        experiment_set=experiment_set,
                        access_mode=mode,
                        start=start,
                        end=end,
                    )
                )

        with mock.patch.object(
            cli_daytrade_module,
            "prepare_daytrade_data_access",
        ) as prepare, self.assertRaisesRegex(ValueError, "window is frozen"):
            main(
                [
                    "daytrade-optimize-run",
                    "--experiment-set",
                    "h9-rvol-reward-risk",
                    "--start",
                    "2023-02-01",
                    "--end",
                    "2024-12-31",
                    "--access-mode",
                    "adaptive_development",
                    "--universe-manifest",
                    "/not-read.json",
                    "--exposure-ledger",
                    "/not-claimed.jsonl",
                    "--run-id",
                    "window-subset-attack",
                    "--cost-bps",
                    "5",
                    "--max-volume-participation",
                    "0.10",
                    "--output-file",
                    "/not-written.json",
                    "--evidence-file",
                    "/not-written.jsonl",
                ],
                stdout=io.StringIO(),
            )
        prepare.assert_not_called()

        for args, message in (
            (
                SimpleNamespace(
                    experiment_set="h3-rvol",
                    access_mode="adaptive_development",
                    start="2019-01-01",
                    end="2022-12-31",
                ),
                "requires access_mode research",
            ),
            (
                SimpleNamespace(
                    experiment_set="h9-rvol-reward-risk",
                    access_mode="fixed_holdout",
                    start="2023-01-01",
                    end="2024-12-31",
                ),
                "must be research",
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                ValueError,
                message,
            ):
                cli_daytrade_module._require_exact_daytrade_experiment_window(args)

    def test_h9_candidate_diff_rejects_any_change_beyond_reward_risk(self):
        baseline = cli_daytrade_module._canonical_h9_baseline_config()
        name, candidate = cli_daytrade_module.daytrade_h9_candidate_configs(
            baseline
        )[0]
        cli_daytrade_module._require_h9_candidate_diff(
            baseline,
            [(name, candidate)],
        )

        with self.assertRaisesRegex(ValueError, "only reward_risk"):
            cli_daytrade_module._require_h9_candidate_diff(
                baseline,
                [(name, replace(candidate, per_trade_risk_fraction=0.01))],
            )

    def test_h9_formal_evidence_cannot_bypass_completed_execution_receipt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger_path = root / "exposure.jsonl"
            evidence_path = root / "evidence.jsonl"
            released_at = datetime(2028, 8, 1, tzinfo=timezone.utc)
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                },
            ):
                permit = ExposureLedger(
                    ledger_path,
                    _clock=lambda: released_at,
                ).claim(
                    ValidationWindowManifest(
                        strategy_family="daytrade",
                        mode="sealed",
                        request_start=date(2026, 8, 1),
                        request_end=date(2028, 7, 31),
                        dataset_hash="a" * 64,
                        config_hash="b" * 64,
                        cost_model_hash="c" * 64,
                        code_hash="d" * 64,
                    ),
                    run_id="h9-formal-evidence",
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "authenticated completed execution receipt",
                ):
                    cli_daytrade_module._record_daytrade_optimization_evidence(
                        SimpleNamespace(
                            evidence_file=str(evidence_path),
                            run_id="h9-formal-evidence",
                        ),
                        DayTradeDataAccess(symbols=("AAPL",), permit=permit),
                        RequirementEvidence(
                            "daytrade_strategy_admission",
                            True,
                            "canonical gate passed",
                        ),
                        source_name="frozen_cache",
                        run_type="sealed_backtest",
                        actual_dataset_hash="e" * 64,
                        actual_config_hash="f" * 64,
                        formal_sealed=True,
                        output_hash="1" * 64,
                        report_hash="2" * 64,
                        cost_suite_hash="3" * 64,
                        experiment_id="daytrade-h9-rvol-reward-risk",
                        _clock=lambda: released_at,
                    )
                self.assertFalse(evidence_path.exists())

    def test_h9_development_evidence_cannot_complete_strategy_admission(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            ledger_path = root / "exposure.jsonl"
            evidence_path = root / "evidence.jsonl"
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                },
            ):
                permit = ExposureLedger(ledger_path).claim(
                    ValidationWindowManifest(
                        strategy_family="daytrade",
                        mode="adaptive_development",
                        request_start=date(2023, 1, 3),
                        request_end=date(2024, 12, 31),
                        dataset_hash="a" * 64,
                        config_hash="b" * 64,
                        cost_model_hash="c" * 64,
                        code_hash="d" * 64,
                    ),
                    run_id="h9-development-evidence",
                    claimed_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
                )
                cli_daytrade_module._record_daytrade_optimization_evidence(
                    SimpleNamespace(evidence_file=str(evidence_path)),
                    DayTradeDataAccess(symbols=("AAPL",), permit=permit),
                    RequirementEvidence(
                        "daytrade_h9_fixed_candidate_survival",
                        True,
                        "candidate survived",
                    ),
                    source_name="frozen_cache",
                    run_type="historical_optimization",
                    actual_dataset_hash="e" * 64,
                    actual_config_hash="f" * 64,
                    formal_sealed=False,
                )

                raw = json.loads(evidence_path.read_text(encoding="utf-8"))
                report = CompletionAudit(["daytrade_strategy_admission"]).evaluate(
                    load_evidence_file(evidence_path)
                )

            self.assertFalse(report.complete)
            self.assertEqual(
                raw["requirement_id"],
                "daytrade_h9_fixed_candidate_survival",
            )
            self.assertEqual(raw["run_type"], "historical_optimization")

    def test_h9_frozen_protocol_drift_is_rejected_before_data_claim(self):
        frozen = cli_daytrade_module._canonical_h9_baseline_config()
        cli_daytrade_module._require_h9_frozen_protocol(frozen)
        for item in fields(DayTradeConfig):
            current = getattr(frozen, item.name)
            if isinstance(current, bool):
                drifted = not current
            elif isinstance(current, int):
                drifted = current + 1
            elif isinstance(current, float):
                drifted = current + 0.123
            else:
                drifted = current + "-drift"
            with self.subTest(name=item.name), self.assertRaisesRegex(
                ValueError,
                item.name,
            ):
                cli_daytrade_module._require_h9_frozen_protocol(
                    replace(frozen, **{item.name: drifted})
                )

    def test_transaction_costs_are_not_part_of_strategy_config_identity(self):
        from quantbot.evidence import hash_payload

        baseline = cli_daytrade_module._canonical_h9_baseline_config()
        stressed = replace(
            baseline,
            cost_bps=12.0,
            max_volume_participation=0.25,
        )

        self.assertEqual(
            hash_payload(
                cli_daytrade_module._daytrade_strategy_config_payload(baseline)
            ),
            hash_payload(
                cli_daytrade_module._daytrade_strategy_config_payload(stressed)
            ),
        )
        self.assertNotEqual(
            hash_payload(
                cli_daytrade_module._daytrade_optimization_cost_identity(
                    SimpleNamespace(
                        experiment_set="h9-rvol-reward-risk"
                    ),
                    baseline,
                )
            ),
            hash_payload(
                cli_daytrade_module._daytrade_optimization_cost_identity(
                    SimpleNamespace(
                        experiment_set="h9-rvol-reward-risk"
                    ),
                    stressed,
                )
            ),
        )

    def test_input_materialization_binds_dynamic_security_id_schedule(self):
        bars_hash = "a" * 64
        static = DayTradeDataAccess(symbols=("AAPL",))
        first = DayTradeDataAccess(
            symbols=("AAPL",),
            universe_schedule=(
                DailyUniverseSnapshot(
                    date(2024, 1, 2),
                    {"SEC-1": "AAPL"},
                ),
            ),
        )
        renamed = DayTradeDataAccess(
            symbols=("AAPL", "XYZ"),
            universe_schedule=(
                DailyUniverseSnapshot(
                    date(2024, 1, 2),
                    {"SEC-1": "XYZ"},
                ),
            ),
        )

        self.assertEqual(
            cli_daytrade_module._daytrade_input_materialization_hash(
                bars_hash,
                static,
            ),
            bars_hash,
        )
        self.assertNotEqual(
            cli_daytrade_module._daytrade_input_materialization_hash(
                bars_hash,
                first,
            ),
            cli_daytrade_module._daytrade_input_materialization_hash(
                bars_hash,
                renamed,
            ),
        )

    def test_formal_dynamic_calendar_cannot_choose_sparse_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions = (date(2024, 1, 2), date(2024, 1, 10))
            snapshots = [
                {
                    "trading_day": item.isoformat(),
                    "ticker_by_security_id": {"security-1": "AAPL"},
                }
                for item in sessions
            ]
            calendar = _signed_calendar_payload(sessions)
            manifest_path = root / "universe.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "a" * 64,
                        "minute_data_hash": "b" * 64,
                        "selection_policy": dict(DAYTRADE_TOP50_SELECTION_POLICY),
                        "expected_sessions": [item.isoformat() for item in sessions],
                        "trading_calendar": calendar,
                        "construction_evidence": _construction_proof(
                            snapshots,
                            source_dataset_hash="a" * 64,
                            calendar_hash=calendar["content_hash"],
                        ),
                        "snapshots": snapshots,
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                command="daytrade-optimize-run",
                experiment_set="h9-rvol-reward-risk",
                universe_manifest=str(manifest_path),
                symbols="AAPL",
                start="2024-01-02",
                end="2024-01-10",
                warmup_start="",
                exposure_ledger=str(root / "ledger.jsonl"),
                run_id="sparse-calendar-attack",
                access_mode="sealed",
                cache_dir=str(root / "cache"),
            )
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON": json.dumps(
                        {CALENDAR_ISSUER: CALENDAR_KEY.hex()}
                    ),
                    "STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON": json.dumps(
                        {UNIVERSE_ISSUER: UNIVERSE_KEY.hex()}
                    ),
                },
            ), self.assertRaisesRegex(ValueError, "implausible session gap"):
                prepare_daytrade_data_access(
                    args,
                    DayTradeConfig(),
                    config_identity={"experiment_set": "h9-rvol-reward-risk"},
                )

    def test_formal_calendar_signed_coverage_must_include_feature_warmup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions = (date(2024, 1, 2), date(2024, 1, 3))
            snapshots = [
                {
                    "trading_day": item.isoformat(),
                    "ticker_by_security_id": {"security-1": "AAPL"},
                }
                for item in sessions
            ]
            calendar = _signed_calendar_payload(sessions)
            manifest_path = root / "universe.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "a" * 64,
                        "minute_data_hash": "b" * 64,
                        "selection_policy": dict(DAYTRADE_TOP50_SELECTION_POLICY),
                        "expected_sessions": [item.isoformat() for item in sessions],
                        "trading_calendar": calendar,
                        "construction_evidence": _construction_proof(
                            snapshots,
                            source_dataset_hash="a" * 64,
                            calendar_hash=calendar["content_hash"],
                        ),
                        "snapshots": snapshots,
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                command="daytrade-optimize-run",
                experiment_set="h9-rvol-reward-risk",
                universe_manifest=str(manifest_path),
                symbols="AAPL",
                start="2024-01-02",
                end="2024-01-03",
                warmup_start="2024-01-01",
                exposure_ledger=str(root / "ledger.jsonl"),
                run_id="calendar-warmup-coverage-attack",
                access_mode="adaptive_development",
                cache_dir=str(root / "cache"),
            )
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON": json.dumps(
                        {CALENDAR_ISSUER: CALENDAR_KEY.hex()}
                    ),
                    "STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON": json.dumps(
                        {UNIVERSE_ISSUER: UNIVERSE_KEY.hex()}
                    ),
                },
            ), self.assertRaisesRegex(ValueError, "coverage does not contain"):
                prepare_daytrade_data_access(
                    args,
                    DayTradeConfig(),
                    config_identity={"experiment_set": "h9-rvol-reward-risk"},
                )

    def test_formal_dynamic_rejects_forged_core_construction_proof(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sessions = (date(2024, 1, 2),)
            snapshots = [
                {
                    "trading_day": "2024-01-02",
                    "ticker_by_security_id": {"security-1": "AAPL"},
                }
            ]
            calendar = _signed_calendar_payload(sessions)
            proof = _construction_proof(
                snapshots,
                source_dataset_hash="a" * 64,
                calendar_hash=calendar["content_hash"],
            )
            proof["signature"] = "0" * 64
            manifest_path = root / "universe.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "a" * 64,
                        "minute_data_hash": "b" * 64,
                        "selection_policy": dict(DAYTRADE_TOP50_SELECTION_POLICY),
                        "expected_sessions": ["2024-01-02"],
                        "trading_calendar": calendar,
                        "construction_evidence": proof,
                        "snapshots": snapshots,
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                command="daytrade-optimize-run",
                experiment_set="h9-rvol-reward-risk",
                universe_manifest=str(manifest_path),
                symbols="AAPL",
                start="2024-01-02",
                end="2024-01-02",
                warmup_start="",
                exposure_ledger=str(root / "ledger.jsonl"),
                run_id="forged-proof-attack",
                access_mode="sealed",
                cache_dir=str(root / "cache"),
            )
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON": json.dumps(
                        {CALENDAR_ISSUER: CALENDAR_KEY.hex()}
                    ),
                    "STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON": json.dumps(
                        {UNIVERSE_ISSUER: UNIVERSE_KEY.hex()}
                    ),
                },
            ), self.assertRaisesRegex(ValueError, "signature is invalid"):
                prepare_daytrade_data_access(
                    args,
                    DayTradeConfig(),
                    config_identity={"experiment_set": "h9-rvol-reward-risk"},
                )

    def test_h9_rejects_dynamic_manifest_without_exact_top50_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            manifest_path = root / "universe.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "a" * 64,
                        "minute_data_hash": "b" * 64,
                        "snapshots": [
                            {
                                "trading_day": "2024-01-02",
                                "ticker_by_security_id": {"security-1": "AAPL"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = SimpleNamespace(
                universe_manifest=str(manifest_path),
                symbols="AAPL",
                start="2024-01-02",
                end="2024-01-02",
                warmup_start="",
                exposure_ledger=str(root / "ledger.jsonl"),
                run_id="h9-policy-attack",
                access_mode="adaptive_development",
                cache_dir=str(root / "cache"),
            )

            with self.assertRaisesRegex(ValueError, "exact Top50 selection policy"):
                prepare_daytrade_data_access(
                    args,
                    DayTradeConfig(),
                    config_identity={"experiment_set": "h9-rvol-reward-risk"},
                )

    def test_sealed_access_is_reported_but_never_implies_formal_admission(self):
        permit = SimpleNamespace(mode="sealed", as_payload=lambda: {"mode": "sealed"})

        payload = daytrade_data_access_payload(
            DayTradeDataAccess(symbols=("AAPL",), permit=permit)
        )

        self.assertTrue(payload["sealed_data_access"])
        self.assertFalse(payload["formal_admission_eligible"])

    def test_frozen_dynamic_hash_mismatch_fails_before_evaluation_or_api_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trading_day = date(2024, 1, 2)
            bars = [
                minute_bar_from_row(row)
                for row in _synthetic_daytrade_rows_for_symbols_and_dates(
                    ("AAPL",),
                    (trading_day,),
                )
            ]
            cache = DayTradeMinuteCache(root / "cache", "injected")
            cache.write_all(
                bars,
                request_start=trading_day,
                request_end=trading_day,
                expected_sessions=(trading_day,),
            )
            snapshot = DailyUniverseSnapshot(
                trading_day,
                {"security-1": "AAPL"},
            )
            manifest = DayTradeUniverseManifest(
                path=root / "universe.json",
                dataset_hash="a" * 64,
                manifest_hash="b" * 64,
                snapshots=(snapshot,),
                suspended_sessions=frozenset(),
                selection_policy=DAYTRADE_TOP50_SELECTION_POLICY,
                minute_data_hash="c" * 64,
            )
            access = DayTradeDataAccess(
                symbols=("AAPL",),
                universe_schedule=(snapshot,),
                universe_manifest=manifest,
                frozen_cache_required=True,
                declared_minute_data_hash=manifest.minute_data_hash,
            )
            args = SimpleNamespace(
                universe_manifest=str(manifest.path),
                cache_dir=str(root / "cache"),
                expected_sessions_json=None,
            )

            with self.assertRaisesRegex(ValueError, "minute_data_hash"):
                load_daytrade_bars_for_symbols_from_args(
                    args,
                    {},
                    lambda **_kwargs: self.fail("API fallback must not run"),
                    ("AAPL",),
                    trading_day,
                    trading_day,
                    data_access=access,
                )

    def test_frozen_dynamic_cache_miss_never_falls_back_to_loader(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trading_day = date(2024, 1, 2)
            snapshot = DailyUniverseSnapshot(
                trading_day,
                {"security-1": "AAPL"},
            )
            manifest = DayTradeUniverseManifest(
                path=root / "universe.json",
                dataset_hash="a" * 64,
                manifest_hash="b" * 64,
                snapshots=(snapshot,),
                suspended_sessions=frozenset(),
                selection_policy=DAYTRADE_TOP50_SELECTION_POLICY,
                minute_data_hash="c" * 64,
            )
            access = DayTradeDataAccess(
                symbols=("AAPL",),
                universe_schedule=(snapshot,),
                universe_manifest=manifest,
                frozen_cache_required=True,
                declared_minute_data_hash=manifest.minute_data_hash,
            )
            args = SimpleNamespace(
                universe_manifest=str(manifest.path),
                cache_dir=str(root / "cache"),
                expected_sessions_json=None,
            )
            calls = []

            with self.assertRaisesRegex(ValueError, "API fallback forbidden"):
                load_daytrade_bars_for_symbols_from_args(
                    args,
                    {},
                    lambda **kwargs: calls.append(kwargs),
                    ("AAPL",),
                    trading_day,
                    trading_day,
                    data_access=access,
                )
            self.assertEqual(calls, [])

    def test_dynamic_universe_claims_before_loading_and_follows_ticker_change(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe_path = root / "universe.json"
            universe_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "a" * 64,
                        "expected_sessions": ["2024-01-02", "2024-01-03"],
                        "snapshots": [
                            {
                                "trading_day": "2024-01-02",
                                "ticker_by_security_id": {"security-1": "AAPL"},
                            },
                            {
                                "trading_day": "2024-01-03",
                                "ticker_by_security_id": {"security-1": "XYZ"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def loader(*, symbols, start, end):
                calls.append((symbols, start, end))
                return _synthetic_daytrade_rows_for_symbols_and_dates(
                    symbols,
                    (date(2024, 1, 2), date(2024, 1, 3)),
                )

            stdout = io.StringIO()
            ledger_path = root / "exposure.jsonl"
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                    "STOCKBOT_DATA_ROOT": str(root),
                },
            ):
                exit_code = main(
                    [
                        "daytrade-backtest-run",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-03",
                        "--universe-manifest",
                        str(universe_path),
                        "--exposure-ledger",
                        str(ledger_path),
                        "--run-id",
                        "dynamic-rename-test",
                        "--access-mode",
                        "adaptive_development",
                        "--trades-log",
                        str(root / "trades.jsonl"),
                        "--daily-log",
                        str(root / "daily.jsonl"),
                        "--evidence-file",
                        str(root / "evidence.jsonl"),
                    ],
                    stdout=stdout,
                    daytrade_minute_loader=loader,
                )

            payload = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(calls[0][0], ("AAPL", "XYZ"))
            self.assertTrue(payload["dynamic_universe"])
            self.assertEqual(payload["universe_snapshot_count"], 2)
            self.assertEqual(
                payload["data_access_permit"]["mode"],
                "adaptive_development",
            )
            self.assertFalse(payload["formal_admission_eligible"])

    def test_dynamic_universe_rejects_missing_permit_flags_before_loader(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe_path = root / "universe.json"
            universe_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "b" * 64,
                        "snapshots": [
                            {
                                "trading_day": "2024-01-02",
                                "ticker_by_security_id": {"security-1": "AAPL"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            calls = []
            with self.assertRaisesRegex(ValueError, "dynamic universe access requires"):
                main(
                    [
                        "daytrade-cache-fill",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-02",
                        "--universe-manifest",
                        str(universe_path),
                        "--cache-dir",
                        str(root / "cache"),
                    ],
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: calls.append(True),
                )
            self.assertEqual(calls, [])

    def test_universe_manifest_declared_session_gap_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            universe_path = root / "universe.json"
            universe_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "c" * 64,
                        "expected_sessions": ["2024-01-02", "2024-01-03"],
                        "snapshots": [
                            {
                                "trading_day": "2024-01-02",
                                "ticker_by_security_id": {"security-1": "AAPL"},
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "missing a declared daily snapshot"):
                main(
                    [
                        "daytrade-backtest-run",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-03",
                        "--universe-manifest",
                        str(universe_path),
                        "--exposure-ledger",
                        str(root / "exposure.jsonl"),
                        "--run-id",
                        "gap-test",
                        "--access-mode",
                        "adaptive_development",
                        "--trades-log",
                        str(root / "trades.jsonl"),
                        "--daily-log",
                        str(root / "daily.jsonl"),
                        "--evidence-file",
                        str(root / "evidence.jsonl"),
                    ],
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: self.fail(
                        "minute loader must not run"
                    ),
                )

    def test_h9_cli_runs_one_2r_trial_with_global_21_trial_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            start = date(2023, 1, 1)
            end = date(2024, 12, 31)
            warmup_start = start - timedelta(days=60)
            sessions = []
            day = warmup_start
            while day <= end:
                if day.weekday() < 5:
                    sessions.append(day)
                day += timedelta(days=1)
            universe_path = root / "universe.json"
            cache_dir = root / "cache"
            frozen_bars = [
                minute_bar_from_row(row)
                for row in _synthetic_feature_daytrade_loader(
                    symbols=("AAPL",),
                    start=warmup_start,
                    end=end,
                )
            ]
            cache = DayTradeMinuteCache(cache_dir, "injected")
            cache.write_all(
                frozen_bars,
                request_start=warmup_start,
                request_end=end,
                expected_sessions=sessions,
            )
            frozen_hash = minute_bars_hash(
                cache.read(("AAPL",), warmup_start, end)
            )
            snapshot_payload = [
                {
                    "trading_day": item.isoformat(),
                    "ticker_by_security_id": {"security-1": "AAPL"},
                }
                for item in sessions
            ]
            calendar = _signed_calendar_payload(sessions)
            universe_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "e" * 64,
                        "selection_policy": dict(DAYTRADE_TOP50_SELECTION_POLICY),
                        "minute_data_hash": frozen_hash,
                        "expected_sessions": [item.isoformat() for item in sessions],
                        "trading_calendar": calendar,
                        "construction_evidence": _construction_proof(
                            snapshot_payload,
                            source_dataset_hash="e" * 64,
                            calendar_hash=calendar["content_hash"],
                        ),
                        "snapshots": snapshot_payload,
                    }
                ),
                encoding="utf-8",
            )
            output_path = root / "h9.json"
            ledger_path = root / "exposure.jsonl"
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "test-only-secret",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_path),
                    "STOCKBOT_DATA_ROOT": str(root),
                    "STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON": json.dumps(
                        {CALENDAR_ISSUER: CALENDAR_KEY.hex()}
                    ),
                    "STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON": json.dumps(
                        {UNIVERSE_ISSUER: UNIVERSE_KEY.hex()}
                    ),
                },
            ):
                command = [
                        "daytrade-optimize-run",
                        "--experiment-set",
                        "h9-rvol-reward-risk",
                        "--start",
                        start.isoformat(),
                        "--end",
                        end.isoformat(),
                        "--universe-manifest",
                        str(universe_path),
                        "--exposure-ledger",
                        str(ledger_path),
                        "--run-id",
                        "daytrade-h9-test",
                        "--access-mode",
                        "adaptive_development",
                        "--cache-dir",
                        str(cache_dir),
                        "--cost-bps",
                        "5",
                        "--max-volume-participation",
                        "0.10",
                        "--train-months",
                        "3",
                        "--validation-months",
                        "1",
                        "--output-file",
                        str(output_path),
                        "--evidence-file",
                        str(root / "evidence.jsonl"),
                    ]
                exit_code = main(
                    command,
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: self.fail(
                        "H9 must not use API fallback"
                    ),
                )
                with mock.patch.object(
                    cli_daytrade_module,
                    "load_daytrade_feature_inputs_from_args",
                    side_effect=AssertionError(
                        "completed H9 recovery must not reread minute data"
                    ),
                ):
                    replay_output = io.StringIO()
                    replay_exit_code = main(command, stdout=replay_output)

                rotated = list(command)
                rotated[rotated.index("daytrade-h9-test")] = "daytrade-h9-rotated"
                with self.assertRaisesRegex(
                    ValueError,
                    "different run_id",
                ):
                    main(rotated, stdout=io.StringIO())

            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertIn(exit_code, (0, 1))
            self.assertEqual(replay_exit_code, exit_code)
            self.assertEqual(json.loads(replay_output.getvalue()), payload)
            self.assertTrue(payload["execution_complete"])
            self.assertNotIn("canonical_execution_artifact", payload)
            self.assertNotIn("canonical_execution_artifact_hash", payload)
            self.assertNotIn(
                "canonical_execution_inputs",
                payload["statistical_validation"],
            )
            self.assertEqual(payload["declared_trial_count"], 21)
            self.assertEqual(payload["completed_trial_count"], 1)
            self.assertEqual(
                payload["executed_trial_ids"],
                ["daytrade-h9-rvol-reward-risk:trial-01"],
            )
            trial_configs = payload["statistical_validation"]["trial_configs"]
            baseline = trial_configs["daytrade-h9-rvol-reward-risk:baseline"]
            candidate = trial_configs["daytrade-h9-rvol-reward-risk:trial-01"]
            changed = {
                name
                for name in baseline
                if baseline[name] != candidate[name]
            }
            self.assertEqual(changed, {"reward_risk"})
            self.assertEqual(baseline["min_relative_volume"], 2.0)
            self.assertEqual(baseline["entry_end"], "10:30")
            self.assertEqual(baseline["stop_basis"], "fixed_pct")
            self.assertEqual(baseline["stop_loss_pct"], 0.006)
            self.assertEqual(baseline["reward_risk"], 1.5)
            self.assertEqual(candidate["reward_risk"], 2.0)
            self.assertEqual(payload["feature_config"]["required_symbols"], ["AAPL"])
            self.assertFalse(payload["passed"])
            self.assertEqual(payload["decision"], "reject")
            self.assertFalse(payload["formal_admission_eligible"])
            self.assertFalse(payload["sealed_data_access"])
            fixed = payload["fixed_candidate_evaluation"]
            self.assertTrue(fixed["walk_forward_best_is_diagnostic"])
            self.assertIn("global_trial_histories", fixed["reasons"])
            self.assertTrue(fixed["stress_12bps"]["available"])
            self.assertEqual(fixed["stress_12bps"]["cost_bps"], 12.0)
            self.assertTrue(fixed["quarter_consistency"]["available"])
            self.assertIn("2024-Q2", fixed["quarter_consistency"]["quarter_returns"])
            self.assertFalse(fixed["profit_concentration"]["available"])
            self.assertIn("profit_concentration_unavailable", fixed["reasons"])
            self.assertIn(
                "immutable aligned H1-H8 return histories are missing",
                payload["statistical_validation"]["reason"],
            )
            evidence_record = json.loads(
                (root / "evidence.jsonl").read_text(encoding="utf-8").splitlines()[-1]
            )
            self.assertEqual(
                len(
                    (root / "evidence.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                ),
                1,
            )
            self.assertEqual(
                evidence_record["dataset_hash"],
                payload["data_access_permit"]["dataset_hash"],
            )
            self.assertEqual(
                evidence_record["requirement_id"],
                "daytrade_h9_fixed_candidate_survival",
            )
            self.assertEqual(
                payload["data_access_permit"]["warmup_start"],
                warmup_start.isoformat(),
            )
            self.assertEqual(evidence_record["run_type"], "historical_optimization")
            for name in (
                "config_hash",
                "cost_model_hash",
                "code_hash",
            ):
                self.assertEqual(
                    evidence_record[name],
                    payload["data_access_permit"][name],
                )

    def test_sealed_h9_consumes_adaptive_artifact_and_materializes_authenticated_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            start = date(2026, 8, 1)
            end = date(2028, 7, 31)
            warmup_start = start - timedelta(days=60)
            sessions = []
            day = warmup_start
            while day <= end:
                if day.weekday() < 5:
                    sessions.append(day)
                day += timedelta(days=1)
            cache_dir = root / "cache"
            frozen_bars = [
                minute_bar_from_row(row)
                for row in _synthetic_feature_daytrade_loader(
                    symbols=("AAPL",),
                    start=warmup_start,
                    end=end,
                )
            ]
            cache = DayTradeMinuteCache(cache_dir, "injected")
            cache.write_all(
                frozen_bars,
                request_start=warmup_start,
                request_end=end,
                expected_sessions=sessions,
            )
            frozen_hash = minute_bars_hash(
                cache.read(("AAPL",), warmup_start, end)
            )
            snapshots = [
                {
                    "trading_day": item.isoformat(),
                    "ticker_by_security_id": {"security-1": "AAPL"},
                }
                for item in sessions
            ]
            calendar = _signed_calendar_payload(sessions)
            universe_path = root / "universe.json"
            universe_path.write_text(
                json.dumps(
                    {
                        "dataset_hash": "9" * 64,
                        "selection_policy": dict(DAYTRADE_TOP50_SELECTION_POLICY),
                        "minute_data_hash": frozen_hash,
                        "expected_sessions": [item.isoformat() for item in sessions],
                        "trading_calendar": calendar,
                        "construction_evidence": _construction_proof(
                            snapshots,
                            source_dataset_hash="9" * 64,
                            calendar_hash=calendar["content_hash"],
                        ),
                        "snapshots": snapshots,
                    }
                ),
                encoding="utf-8",
            )
            adaptive_path = root / "adaptive.json"
            adaptive_path.write_text(
                json.dumps(signed_adaptive_validation_artifact()),
                encoding="utf-8",
            )
            ledger_path = root / "exposure-ledger.jsonl"
            output_path = root / "sealed-h9.json"
            evidence_path = root / "evidence.jsonl"
            command = [
                "daytrade-optimize-run",
                "--experiment-set",
                "h9-rvol-reward-risk",
                "--start",
                start.isoformat(),
                "--end",
                end.isoformat(),
                "--universe-manifest",
                str(universe_path),
                "--adaptive-validation-artifact",
                str(adaptive_path),
                "--exposure-ledger",
                str(root),
                "--run-id",
                "daytrade-h9-sealed-test",
                "--access-mode",
                "sealed",
                "--cache-dir",
                str(cache_dir),
                "--cost-bps",
                "5",
                "--max-volume-participation",
                "0.10",
                "--output-file",
                str(output_path),
                "--evidence-file",
                str(evidence_path),
            ]
            with mock.patch.dict(
                os.environ,
                {
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "sealed-h9-test-key",
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(root),
                    "STOCKBOT_DATA_ROOT": str(root),
                    "STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON": json.dumps(
                        {CALENDAR_ISSUER: CALENDAR_KEY.hex()}
                    ),
                    "STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON": json.dumps(
                        {UNIVERSE_ISSUER: UNIVERSE_KEY.hex()}
                    ),
                    "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON": json.dumps(
                        {ADAPTIVE_ISSUER: ADAPTIVE_KEY.hex()}
                    ),
                },
            ), mock.patch(
                "quantbot.validation_protocol._utc_now",
                return_value=datetime(2028, 8, 1, tzinfo=timezone.utc),
            ):
                exit_code = main(
                    command,
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: self.fail(
                        "sealed H9 must not use API fallback"
                    ),
                )
                with mock.patch.object(
                    cli_daytrade_module,
                    "load_daytrade_feature_inputs_from_args",
                    side_effect=AssertionError(
                        "completed sealed H9 recovery must not reread minute data"
                    ),
                ):
                    replay_exit_code = main(command, stdout=io.StringIO())

                payload = json.loads(output_path.read_text(encoding="utf-8"))
                records = load_evidence_records(evidence_path)

            self.assertIn(exit_code, (0, 1))
            self.assertEqual(replay_exit_code, exit_code)
            self.assertTrue(payload["execution_complete"])
            artifact = payload["canonical_execution_artifact"]
            self.assertEqual(
                set(artifact),
                {
                    "artifact_version",
                    "experiment_id",
                    "cost_model_payload",
                    "initial_capital",
                    "observation_dates",
                    "baseline_5bps",
                    "candidate_5bps",
                    "candidate_12bps",
                    "statistical_observation_dates",
                    "selected_trial_id",
                    "statistical_trials",
                },
            )
            self.assertEqual(
                artifact["cost_model_payload"],
                {
                    "execution": "next_available_minute_event",
                    "cost_bps_per_side": 5.0,
                    "max_volume_participation": 0.10,
                    "stress_cost_bps_per_side": 12.0,
                },
            )
            self.assertEqual(len(artifact["statistical_trials"]), 22)
            self.assertIn(
                artifact["selected_trial_id"],
                artifact["statistical_trials"],
            )
            self.assertEqual(
                {
                    len(values)
                    for values in artifact["statistical_trials"].values()
                },
                {len(artifact["statistical_observation_dates"])},
            )
            self.assertEqual(
                payload["canonical_execution_artifact_hash"],
                hash_payload(artifact),
            )
            tampered = json.loads(json.dumps(artifact))
            tampered["candidate_12bps"]["daily"][0]["pnl"] += 1.0
            self.assertNotEqual(
                payload["canonical_execution_artifact_hash"],
                hash_payload(tampered),
            )
            self.assertEqual(payload["completed_trial_count"], 21)
            self.assertEqual(
                payload["statistical_validation"]["statistical_source"],
                "signed_adaptive_validation_artifact",
            )
            self.assertEqual(
                payload["admission_evidence"]["passed"],
                payload["formal_admission_eligible"],
            )
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0].authenticated)
            self.assertEqual(
                records[0].requirement_id,
                "daytrade_strategy_admission",
            )


if __name__ == "__main__":
    unittest.main()
