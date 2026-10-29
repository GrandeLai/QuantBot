import io
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.research_cli import main
from quantbot.data import canonical_pit_bundle_manifest_hash
from quantbot.daytrade_validation_artifact import (
    DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    DAYTRADE_EXPERIMENT_KEYS_ENV,
)
from quantbot.normal.historical_trials import (
    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    NORMAL_TRIAL_KEYS_ENV,
)
from quantbot.readiness import evaluate_strategy_readiness
from tests import test_stockbot_pit_bundle as pit_bundle_fixture
from tests.test_stockbot_daytrade_validation_artifact import (
    ISSUER as DAYTRADE_ADAPTIVE_ISSUER,
    KEY as DAYTRADE_ADAPTIVE_KEY,
    _signed_artifact as signed_daytrade_adaptive_artifact,
)


def _readiness_adapter(**_kwargs):
    return None


def _build_bundle(root, *, coverage_start="2007-01-01", coverage_end="2020-12-31"):
    fixture = pit_bundle_fixture.PointInTimeParquetBundleTest()
    payload, reader = fixture._build(root)
    payload["coverage_start"] = coverage_start
    payload["coverage_end"] = coverage_end
    for table in payload["tables"].values():
        for partition in table["partitions"]:
            partition["coverage_start"] = coverage_start
            partition["coverage_end"] = coverage_end
    payload["dataset_hash"] = canonical_pit_bundle_manifest_hash(payload)
    (root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    return payload, reader


class StrategyReadinessTest(unittest.TestCase):
    def test_research_normal_can_be_ready_without_opening_partition_contents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            _build_bundle(root)
            ledger = Path(tmpdir) / "ledger.jsonl"
            report = evaluate_strategy_readiness(
                strategy="normal-h1",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                warmup_end=date(2009, 12, 31),
                bundle_root=str(root),
                adapter_loader=_readiness_adapter,
                exposure_ledger=str(ledger),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                },
                import_checker=lambda _name: True,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertTrue(report.ready_to_claim, report.blocked_reasons)
            self.assertEqual(report.blocked_reasons, ())
            self.assertEqual(
                report.as_payload()["requested_interval"],
                {"start": "2010-01-01", "end": "2020-12-31"},
            )
            self.assertEqual(
                report.as_payload()["read_interval"],
                {"start": "2007-01-01", "end": "2020-12-31"},
            )
            self.assertFalse(ledger.exists())

    def test_directory_and_explicit_ledger_file_are_the_same_canonical_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            _build_bundle(root)
            ledger_dir = Path(tmpdir) / "ledger"
            ledger_dir.mkdir()
            report = evaluate_strategy_readiness(
                strategy="normal-h1",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                warmup_end=date(2009, 12, 31),
                bundle_root=str(root),
                adapter_loader=_readiness_adapter,
                exposure_ledger=str(ledger_dir / "exposure-ledger.jsonl"),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger_dir),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                },
                import_checker=lambda _name: True,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertNotIn("canonical_ledger_mismatch", report.blocked_reasons)
            self.assertTrue(report.ready_to_claim, report.blocked_reasons)
            self.assertFalse((ledger_dir / "exposure-ledger.jsonl").exists())

    def test_bundle_metadata_must_cover_the_complete_warmup_read_interval(self):
        fixture = pit_bundle_fixture.PointInTimeParquetBundleTest()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            fixture._build(root)
            ledger = Path(tmpdir) / "ledger.jsonl"
            report = evaluate_strategy_readiness(
                strategy="normal-h1",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                warmup_end=date(2009, 12, 31),
                bundle_root=str(root),
                adapter_loader=_readiness_adapter,
                exposure_ledger=str(ledger),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                },
                import_checker=lambda _name: True,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertIn(
                "pit_manifest_coverage_incomplete",
                report.blocked_reasons,
            )
            self.assertIn(
                "pit_partition_coverage_incomplete",
                report.blocked_reasons,
            )
            self.assertFalse(ledger.exists())

    def test_partition_gap_is_reported_without_opening_parquet_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            payload, _reader = _build_bundle(root)
            first_table = next(iter(payload["tables"].values()))
            first_table["partitions"][0]["coverage_start"] = "2008-01-01"
            payload["dataset_hash"] = canonical_pit_bundle_manifest_hash(payload)
            (root / "manifest.json").write_text(
                json.dumps(payload),
                encoding="utf-8",
            )
            ledger = Path(tmpdir) / "ledger.jsonl"
            original_read_bytes = Path.read_bytes

            def guarded_read_bytes(path):
                if path.suffix == ".parquet":
                    raise AssertionError("Parquet content was opened")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", guarded_read_bytes):
                report = evaluate_strategy_readiness(
                    strategy="normal-h1",
                    access_mode="research",
                    start=date(2010, 1, 1),
                    end=date(2020, 12, 31),
                    warmup_start=date(2007, 1, 1),
                    warmup_end=date(2009, 12, 31),
                    bundle_root=str(root),
                    adapter_loader=_readiness_adapter,
                    exposure_ledger=str(ledger),
                    environ={
                        "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                        "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                    },
                    import_checker=lambda _name: True,
                    _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
                )

            self.assertIn(
                "pit_partition_coverage_incomplete",
                report.blocked_reasons,
            )
            self.assertFalse(ledger.exists())

    def test_existing_ledger_is_verified_without_appending_a_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            _build_bundle(root)
            ledger = Path(tmpdir) / "ledger.jsonl"
            ledger.write_text("not-json\n", encoding="utf-8")
            before = ledger.read_bytes()
            report = evaluate_strategy_readiness(
                strategy="normal-h1",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                warmup_end=date(2009, 12, 31),
                bundle_root=str(root),
                adapter_loader=_readiness_adapter,
                exposure_ledger=str(ledger),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                },
                import_checker=lambda _name: True,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertIn("canonical_ledger_invalid", report.blocked_reasons)
            self.assertEqual(ledger.read_bytes(), before)

    def test_warmup_pair_and_writable_ledger_parent_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "quantbot.readiness.os.access",
            return_value=False,
        ):
            ledger = Path(tmpdir) / "ledger.jsonl"
            report = evaluate_strategy_readiness(
                strategy="normal-h1",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                warmup_start=date(2007, 1, 1),
                exposure_ledger=str(ledger),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                },
                import_checker=lambda _name: True,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertIn("warmup_interval_incomplete", report.blocked_reasons)
            self.assertIn(
                "canonical_ledger_parent_not_writable",
                report.blocked_reasons,
            )
            self.assertFalse(ledger.exists())

    def test_normal_h2_shares_the_h1_bundle_adapter_and_windows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            _build_bundle(root)
            ledger = Path(tmpdir) / "ledger.jsonl"
            reports = {}
            for strategy in ("normal-h1", "normal-h2"):
                reports[strategy] = evaluate_strategy_readiness(
                    strategy=strategy,
                    access_mode="research",
                    start=date(2010, 1, 1),
                    end=date(2020, 12, 31),
                    warmup_start=date(2007, 1, 1),
                    warmup_end=date(2009, 12, 31),
                    bundle_root=str(root),
                    adapter_loader=_readiness_adapter,
                    exposure_ledger=str(ledger),
                    environ={
                        "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                        "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                    },
                    import_checker=lambda _name: True,
                    _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
                )

            self.assertFalse(reports["normal-h2"].ready_to_claim)
            self.assertEqual(
                reports["normal-h2"].blocked_reasons,
                ("experiment_completed_rejected",),
            )
            self.assertEqual(
                reports["normal-h2"].deferred_checks,
                reports["normal-h1"].deferred_checks,
            )
            self.assertEqual(reports["normal-h2"].strategy, "normal-h2")
            self.assertFalse(ledger.exists())

    def test_normal_h2_requires_a_normal_adapter_like_h1(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "never-created.jsonl"
            report = evaluate_strategy_readiness(
                strategy="normal-h2",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                exposure_ledger=str(ledger),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "secret",
                },
                import_checker=lambda _name: False,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertFalse(report.ready_to_claim)
            self.assertIn("normal_adapter_missing", report.blocked_reasons)
            self.assertFalse(ledger.exists())

    def test_unsupported_readiness_strategy_still_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported readiness strategy"):
            evaluate_strategy_readiness(
                strategy="normal-h3",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
            )

    def test_normal_subwindow_cannot_report_ready(self):
        report = evaluate_strategy_readiness(
            strategy="normal-h1",
            access_mode="research",
            start=date(2019, 1, 1),
            end=date(2019, 12, 31),
            environ={},
            import_checker=lambda _name: True,
            _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

        self.assertIn("normal_window_identity_mismatch", report.blocked_reasons)

    def test_normal_adaptive_and_sealed_warmups_are_frozen(self):
        windows = (
            (
                "adaptive_development",
                date(2021, 1, 1),
                date(2024, 12, 31),
                date(2018, 1, 1),
                date(2020, 12, 31),
            ),
            (
                "sealed",
                date(2026, 8, 1),
                date(2028, 7, 31),
                date(2023, 8, 1),
                date(2026, 7, 31),
            ),
        )
        for mode, start, end, warmup_start, warmup_end in windows:
            with self.subTest(mode=mode):
                exact = evaluate_strategy_readiness(
                    strategy="normal-h1",
                    access_mode=mode,
                    start=start,
                    end=end,
                    warmup_start=warmup_start,
                    warmup_end=warmup_end,
                    environ={},
                    import_checker=lambda _name: True,
                    _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
                )
                self.assertNotIn(
                    "normal_warmup_identity_mismatch",
                    exact.blocked_reasons,
                )
                self.assertNotIn("normal_warmup_required", exact.blocked_reasons)

        drifted = evaluate_strategy_readiness(
            strategy="normal-h1",
            access_mode="adaptive_development",
            start=date(2021, 1, 1),
            end=date(2024, 12, 31),
            warmup_start=date(2018, 1, 2),
            warmup_end=date(2020, 12, 31),
            environ={},
            import_checker=lambda _name: True,
            _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
        self.assertIn("normal_warmup_identity_mismatch", drifted.blocked_reasons)

    def test_daytrade_window_and_sixty_day_warmup_are_frozen(self):
        common = {
            "strategy": "daytrade-h3",
            "access_mode": "research",
            "start": date(2019, 1, 1),
            "end": date(2022, 12, 31),
            "environ": {},
            "import_checker": lambda _name: True,
            "_clock": lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        }
        exact = evaluate_strategy_readiness(
            **common,
            warmup_start=date(2018, 11, 2),
            warmup_end=date(2018, 12, 31),
        )
        missing = evaluate_strategy_readiness(**common)
        drifted = evaluate_strategy_readiness(
            **common,
            warmup_start=date(2018, 11, 1),
            warmup_end=date(2018, 12, 31),
        )

        self.assertNotIn("daytrade_window_identity_mismatch", exact.blocked_reasons)
        self.assertNotIn("daytrade_warmup_required", exact.blocked_reasons)
        self.assertNotIn("daytrade_warmup_identity_mismatch", exact.blocked_reasons)
        self.assertIn("daytrade_warmup_required", missing.blocked_reasons)
        self.assertIn(
            "daytrade_warmup_identity_mismatch",
            drifted.blocked_reasons,
        )

    def test_h3_readiness_rejects_non_research_modes(self):
        report = evaluate_strategy_readiness(
            strategy="daytrade-h3",
            access_mode="adaptive_development",
            start=date(2023, 1, 1),
            end=date(2024, 12, 31),
            warmup_start=date(2022, 11, 2),
            warmup_end=date(2022, 12, 31),
            environ={},
            import_checker=lambda _name: True,
            _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

        self.assertIn("h3_research_only", report.blocked_reasons)

    def test_adapter_spec_is_inspected_without_importing_the_module(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "pit"
            root.mkdir()
            _build_bundle(root)
            ledger = Path(tmpdir) / "ledger.jsonl"
            marker = Path(tmpdir) / "adapter-imported"
            module_name = "_stockbot_readiness_static_adapter"
            module = Path(tmpdir) / f"{module_name}.py"
            module.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('imported')\n"
                "def load_normal_h1(**kwargs):\n"
                "    return None\n",
                encoding="utf-8",
            )
            with patch.object(sys, "path", [tmpdir, *sys.path]):
                report = evaluate_strategy_readiness(
                    strategy="normal-h1",
                    access_mode="research",
                    start=date(2010, 1, 1),
                    end=date(2020, 12, 31),
                    warmup_start=date(2007, 1, 1),
                    warmup_end=date(2009, 12, 31),
                    bundle_root=str(root),
                    adapter_spec=f"{module_name}:load_normal_h1",
                    exposure_ledger=str(ledger),
                    environ={
                        "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                        "STOCKBOT_EXPOSURE_HMAC_KEY": "present-only",
                    },
                    import_checker=lambda _name: True,
                    _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
                )

            self.assertTrue(report.ready_to_claim, report.blocked_reasons)
            self.assertFalse(marker.exists())
            self.assertNotIn(module_name, sys.modules)

    def test_sealed_h1_readiness_hooks_signed_adaptive_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "normal-adaptive.json"
            artifact.write_text("not-json", encoding="utf-8")
            common = {
                "strategy": "normal-h1",
                "access_mode": "sealed",
                "start": date(2026, 8, 1),
                "end": date(2028, 7, 31),
                "warmup_start": date(2023, 8, 1),
                "warmup_end": date(2026, 7, 31),
                "import_checker": lambda _name: True,
                "_clock": lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            }
            missing_keys = evaluate_strategy_readiness(
                **common,
                environ={
                    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV: str(artifact),
                },
            )
            invalid_artifact = evaluate_strategy_readiness(
                **common,
                environ={
                    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV: str(artifact),
                    NORMAL_TRIAL_KEYS_ENV: json.dumps(
                        {"issuer": (b"trusted-adaptive-key").hex()}
                    ),
                },
            )

            self.assertIn(
                "trusted_normal_trial_keys_missing_or_invalid",
                missing_keys.blocked_reasons,
            )
            self.assertIn(
                "normal_adaptive_validation_artifact_invalid",
                invalid_artifact.blocked_reasons,
            )
            self.assertIn(
                "normal_adaptive_validation_identity_compatibility_before_claim",
                invalid_artifact.deferred_checks,
            )

    def test_missing_resources_return_stable_reasons_without_creating_ledger(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = Path(tmpdir) / "never-created.jsonl"
            report = evaluate_strategy_readiness(
                strategy="normal-h1",
                access_mode="research",
                start=date(2010, 1, 1),
                end=date(2020, 12, 31),
                exposure_ledger=str(ledger),
                environ={
                    "STOCKBOT_EXPOSURE_LEDGER_PATH": str(ledger),
                    "STOCKBOT_EXPOSURE_HMAC_KEY": "secret-not-for-output",
                },
                import_checker=lambda _name: False,
                _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
            )

            self.assertFalse(report.ready_to_claim)
            self.assertIn("data_root_missing", report.blocked_reasons)
            self.assertIn("normal_adapter_missing", report.blocked_reasons)
            self.assertIn("missing_data_package:pyarrow", report.blocked_reasons)
            self.assertFalse(ledger.exists())

    def test_unreleased_h9_does_not_open_the_universe_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            universe = Path(tmpdir) / "universe.json"
            universe.write_text("protected-body", encoding="utf-8")
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("protected body was opened"),
            ):
                report = evaluate_strategy_readiness(
                    strategy="daytrade-h9",
                    access_mode="sealed",
                    start=date(2026, 8, 1),
                    end=date(2028, 7, 31),
                    warmup_start=date(2026, 6, 2),
                    warmup_end=date(2026, 7, 31),
                    universe_manifest=str(universe),
                    environ={},
                    import_checker=lambda _name: True,
                    _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
                )

            self.assertFalse(report.formal_time_eligible)
            self.assertIn("sealed_window_not_released", report.blocked_reasons)

    def test_sealed_h9_uses_independent_signed_adaptive_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            adaptive = root / "daytrade-adaptive.json"
            adaptive.write_text(
                json.dumps(signed_daytrade_adaptive_artifact()),
                encoding="utf-8",
            )
            universe_payload = {
                "dataset_hash": "1" * 64,
                "minute_data_hash": "2" * 64,
                "selection_policy": {
                    "as_of": "prior_close",
                    "history_sessions": 252,
                    "minimum_price": 5.0,
                    "adv_window_sessions": 20,
                    "minimum_adv_dollars": 5_000_000.0,
                    "limit": 50,
                    "tie_break": "stable_security_id",
                },
                "snapshots": [{"trading_day": "2026-08-03"}],
                "trading_calendar": {},
                "construction_evidence": {},
            }
            universe = root / "universe.json"
            universe.write_text(json.dumps(universe_payload), encoding="utf-8")
            environment = {
                DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV: str(adaptive),
                DAYTRADE_EXPERIMENT_KEYS_ENV: json.dumps(
                    {
                        DAYTRADE_ADAPTIVE_ISSUER: (
                            DAYTRADE_ADAPTIVE_KEY.hex()
                        )
                    }
                ),
                "STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON": "configured",
                "STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON": "configured",
            }
            common = {
                "strategy": "daytrade-h9",
                "access_mode": "sealed",
                "start": date(2026, 8, 1),
                "end": date(2028, 7, 31),
                "warmup_start": date(2026, 6, 2),
                "warmup_end": date(2026, 7, 31),
                "universe_manifest": str(universe),
                "cache_dir": str(root),
                "environ": environment,
                "import_checker": lambda _name: True,
                "_clock": lambda: datetime(
                    2028,
                    8,
                    1,
                    tzinfo=timezone.utc,
                ),
            }

            independent = evaluate_strategy_readiness(**common)
            universe_payload["historical_trial_artifact"] = {
                "content_hash": "3" * 64
            }
            universe.write_text(json.dumps(universe_payload), encoding="utf-8")
            contaminated = evaluate_strategy_readiness(**common)
            universe_payload.pop("historical_trial_artifact")
            universe.write_text(json.dumps(universe_payload), encoding="utf-8")
            tampered_adaptive = signed_daytrade_adaptive_artifact()
            tampered_adaptive["pbo"] = 0.9
            adaptive.write_text(
                json.dumps(tampered_adaptive),
                encoding="utf-8",
            )
            invalid_adaptive = evaluate_strategy_readiness(**common)

            self.assertNotIn(
                "historical_trial_artifact_missing",
                independent.blocked_reasons,
            )
            self.assertNotIn(
                "daytrade_adaptive_validation_artifact_missing",
                independent.blocked_reasons,
            )
            self.assertNotIn(
                "daytrade_adaptive_validation_artifact_invalid",
                independent.blocked_reasons,
            )
            self.assertNotIn(
                "trusted_daytrade_experiment_keys_missing_or_invalid",
                independent.blocked_reasons,
            )
            self.assertIn(
                "sealed_universe_historical_trial_artifact_forbidden",
                contaminated.blocked_reasons,
            )
            self.assertIn(
                "daytrade_adaptive_validation_artifact_invalid",
                invalid_adaptive.blocked_reasons,
            )

    def test_cli_output_never_contains_paths_or_secret_values(self):
        stdout = io.StringIO()
        secret = "do-not-leak-this-hmac-key"
        private_path = "/private/vendor/quantbot-pit"
        exit_code = main(
            [
                "strategy-readiness",
                "--strategy",
                "normal-h1",
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
            ],
            stdout=stdout,
            environ={
                "STOCKBOT_DATA_ROOT": private_path,
                "STOCKBOT_EXPOSURE_HMAC_KEY": secret,
            },
            import_checker=lambda _name: False,
        )

        payload = json.loads(stdout.getvalue())
        serialized = stdout.getvalue()
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ready_to_claim"])
        self.assertEqual(
            payload["requested_interval"],
            {"start": "2010-01-01", "end": "2020-12-31"},
        )
        self.assertEqual(
            payload["read_interval"],
            {"start": "2007-01-01", "end": "2020-12-31"},
        )
        self.assertNotIn(secret, serialized)
        self.assertNotIn(private_path, serialized)

    def test_sealed_window_identity_is_checked_without_data_access(self):
        report = evaluate_strategy_readiness(
            strategy="daytrade-h9",
            access_mode="sealed",
            start=date(2026, 8, 2),
            end=date(2028, 7, 31),
            environ={},
            import_checker=lambda _name: True,
            _clock=lambda: datetime(2026, 7, 20, tzinfo=timezone.utc),
        )

        self.assertIn("sealed_window_identity_mismatch", report.blocked_reasons)
        self.assertIn("sealed_window_not_released", report.blocked_reasons)


if __name__ == "__main__":
    unittest.main()
