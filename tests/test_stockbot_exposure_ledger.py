import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.exposure import (
    DEFAULT_EXPOSURE_LEDGER_PATH_ENV,
    ExposureDeniedError,
    ExposureLedger,
    ExposureLedgerIntegrityError,
    ValidationWindowManifest,
    canonical_exposure_ledger_path,
)
from quantbot.validation_protocol import formal_sealed_warmup_interval


UTC = timezone.utc
HMAC_KEY_ENV = "STOCKBOT_EXPOSURE_HMAC_KEY"
TEST_KEY = "unit-test-exposure-ledger-key"


def _signed_legacy_record(window, *, run_id, claimed_at, outcome="granted", denial_reason=None):
    manifest_payload = window.payload_for_ledger_version(1)
    canonical = lambda value: json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    manifest_hash = hashlib.sha256(
        canonical(manifest_payload).encode("utf-8")
    ).hexdigest()
    unsigned = {
        "ledger_version": 1,
        "run_id": run_id,
        **manifest_payload,
        "manifest_hash": manifest_hash,
        "claimed_at": claimed_at.isoformat(),
        "outcome": outcome,
        "denial_reason": denial_reason,
        "prev_hash": "0" * 64,
    }
    record_hash = hashlib.sha256(canonical(unsigned).encode("utf-8")).hexdigest()
    return {
        **unsigned,
        "record_hash": record_hash,
        "signature": hmac.new(
            TEST_KEY.encode("utf-8"),
            record_hash.encode("ascii"),
            hashlib.sha256,
        ).hexdigest(),
    }


def manifest(
    *,
    mode="adaptive_development",
    request_start=date(2023, 1, 3),
    request_end=date(2023, 1, 31),
    warmup_start=date(2022, 11, 1),
    warmup_end=date(2023, 1, 2),
    strategy_family="daytrade",
    dataset_hash="a" * 64,
):
    return ValidationWindowManifest(
        strategy_family=strategy_family,
        mode=mode,
        request_start=request_start,
        request_end=request_end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
        dataset_hash=dataset_hash,
        config_hash="b" * 64,
        cost_model_hash="c" * 64,
        code_hash="d" * 64,
    )


class ExposureLedgerTest(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(
            os.environ,
            {
                HMAC_KEY_ENV: TEST_KEY,
                DEFAULT_EXPOSURE_LEDGER_PATH_ENV: "",
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_manifest_and_permit_include_the_complete_warmup_read_interval(self):
        window = manifest()
        self.assertEqual(window.actual_start, date(2022, 11, 1))
        self.assertEqual(window.actual_end, date(2023, 1, 31))
        self.assertEqual(window.read_interval.start, date(2022, 11, 1))
        self.assertEqual(window.read_interval.end, date(2023, 1, 31))
        self.assertEqual(
            window.protected_holdout_interval.start,
            date(2023, 1, 3),
        )
        self.assertEqual(
            window.as_payload()["read_interval"],
            {"start": "2022-11-01", "end": "2023-01-31"},
        )
        self.assertEqual(
            window.as_payload()["protected_holdout_interval"],
            {"start": "2023-01-03", "end": "2023-01-31"},
        )

        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {DEFAULT_EXPOSURE_LEDGER_PATH_ENV: tmpdir},
            clear=False,
        ):
            ledger = ExposureLedger(Path(tmpdir))
            permit = ledger.claim(
                window,
                run_id="adaptive-one",
                claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
            )
            self.assertEqual(ledger.verify_permit(permit).record_hash, permit.record_hash)
            permit.assert_allows(
                dataset_hash="a" * 64,
                strategy_family="daytrade",
                start=date(2022, 11, 1),
                end=date(2023, 1, 31),
            )
            with self.assertRaisesRegex(PermissionError, "outside"):
                permit.assert_allows(
                    dataset_hash="a" * 64,
                    strategy_family="daytrade",
                    start=date(2022, 10, 31),
                    end=date(2023, 1, 31),
                )
            with self.assertRaisesRegex(PermissionError, "dataset_hash"):
                permit.assert_allows(
                    dataset_hash="e" * 64,
                    strategy_family="daytrade",
                    start=date(2023, 1, 3),
                    end=date(2023, 1, 31),
                )
            with self.assertRaisesRegex(PermissionError, "strategy_family is required"):
                permit.assert_allows(
                    dataset_hash="a" * 64,
                    strategy_family=None,
                    start=date(2023, 1, 3),
                    end=date(2023, 1, 31),
                )

    def test_adaptive_window_cannot_be_relabeled_as_sealed(self):
        adaptive = manifest(
            mode="adaptive_development",
            request_start=date(2023, 1, 1),
            request_end=date(2024, 12, 31),
            warmup_start=None,
            warmup_end=None,
        )
        self.assertEqual(adaptive.mode, "adaptive_development")
        with self.assertRaisesRegex(ValueError, "active formal"):
            manifest(
                mode="sealed",
                request_start=date(2023, 1, 1),
                request_end=date(2024, 12, 31),
                warmup_start=None,
                warmup_end=None,
            )
        with self.assertRaisesRegex(ValueError, "legacy.*exposed"):
            manifest(
                mode="sealed",
                request_start=date(2025, 1, 1),
                request_end=date(2026, 6, 30),
                warmup_start=None,
                warmup_end=None,
            )
        with self.assertRaisesRegex(ValueError, "warmup_end must precede"):
            manifest(
                mode="adaptive_development",
                request_start=date(2023, 1, 1),
                request_end=date(2024, 12, 31),
                warmup_start=date(2026, 8, 1),
                warmup_end=date(2028, 7, 31),
            )

    def test_exclusive_overlap_cannot_be_bypassed_by_run_id_and_denial_is_chained(self):
        first = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 1, 1),
            request_end=date(2024, 3, 31),
            warmup_start=date(2023, 12, 1),
            warmup_end=date(2023, 12, 31),
        )
        overlapping = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 3, 31),
            request_end=date(2024, 6, 30),
            warmup_start=None,
            warmup_end=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ExposureLedger(Path(tmpdir))
            granted = ledger.claim(
                first,
                run_id="first-directory-run",
                claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
            )
            with self.assertRaises(ExposureDeniedError) as denied:
                ledger.claim(
                    overlapping,
                    run_id="different-directory-run",
                    claimed_at=datetime(2026, 7, 17, 0, 1, tzinfo=UTC),
                )
            records = ledger.verify()

        self.assertEqual(len(records), 2)
        self.assertEqual(records[1].outcome, "denied")
        self.assertEqual(records[1].prev_hash, granted.record_hash)
        self.assertEqual(denied.exception.record.record_hash, records[1].record_hash)

    def test_warmup_read_overlap_does_not_consume_a_protected_holdout(self):
        adaptive = manifest(
            request_start=date(2024, 1, 1),
            request_end=date(2024, 1, 31),
            warmup_start=None,
            warmup_end=None,
        )
        fixed = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 3, 1),
            request_end=date(2024, 3, 31),
            warmup_start=date(2024, 1, 31),
            warmup_end=date(2024, 2, 29),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ExposureLedger(Path(tmpdir))
            ledger.claim(
                adaptive,
                run_id="adaptive-observation",
                claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
            )
            permit = ledger.claim(
                fixed,
                run_id="later-fixed-label",
                claimed_at=datetime(2026, 7, 17, 0, 1, tzinfo=UTC),
            )
            with self.assertRaisesRegex(ExposureDeniedError, "already exposed"):
                ledger.claim(
                    manifest(
                        request_start=date(2024, 3, 15),
                        request_end=date(2024, 4, 15),
                        warmup_start=None,
                        warmup_end=None,
                    ),
                    run_id="adaptive-protected-overlap",
                    claimed_at=datetime(2026, 7, 17, 0, 2, tzinfo=UTC),
                )

        self.assertEqual(permit.read_interval.start, date(2024, 1, 31))
        self.assertEqual(
            permit.protected_holdout_interval.start,
            date(2024, 3, 1),
        )

    def test_exact_retry_is_idempotent_and_run_id_drift_is_denied(self):
        window = manifest()
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ExposureLedger(Path(tmpdir))
            first = ledger.claim(
                window,
                run_id="recoverable-run",
                claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
            )
            retried = ledger.claim(
                window,
                run_id="recoverable-run",
                claimed_at=datetime(2026, 7, 17, 0, 1, tzinfo=UTC),
            )
            self.assertEqual(first, retried)
            self.assertEqual(len(ledger.records), 1)

            with self.assertRaisesRegex(ExposureDeniedError, "run_id"):
                ledger.claim(
                    manifest(dataset_hash="f" * 64),
                    run_id="recoverable-run",
                    claimed_at=datetime(2026, 7, 17, 0, 2, tzinfo=UTC),
                )

            records = ledger.records

        self.assertEqual(len(records), 2)
        self.assertEqual(records[-1].outcome, "denied")
        self.assertEqual(records[-1].prev_hash, first.record_hash)

    def test_denied_retry_returns_the_original_denial_without_appending(self):
        first = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 1, 1),
            request_end=date(2024, 3, 31),
            warmup_start=None,
            warmup_end=None,
        )
        overlapping = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 3, 1),
            request_end=date(2024, 6, 30),
            warmup_start=None,
            warmup_end=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ExposureLedger(Path(tmpdir))
            ledger.claim(first, run_id="first-protected-window")
            with self.assertRaises(ExposureDeniedError) as initial:
                ledger.claim(overlapping, run_id="recover-denied-run")
            count_after_initial = len(ledger.records)
            with self.assertRaises(ExposureDeniedError) as retried:
                ledger.claim(
                    overlapping,
                    run_id="recover-denied-run",
                    claimed_at=datetime(2099, 1, 1, tzinfo=UTC),
                )

            records = ledger.records

        self.assertEqual(len(records), count_after_initial)
        self.assertEqual(
            retried.exception.record.record_hash,
            initial.exception.record.record_hash,
        )
        self.assertEqual(retried.exception.reason, initial.exception.reason)

    def test_legacy_chain_verifies_and_keeps_conservative_overlap_semantics(self):
        legacy_manifest = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 4, 1),
            request_end=date(2024, 4, 30),
            warmup_start=date(2024, 1, 1),
            warmup_end=date(2024, 3, 31),
        )
        claimed_at = datetime(2026, 7, 17, tzinfo=UTC)
        canonical = lambda value: json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        payload = _signed_legacy_record(
            legacy_manifest,
            run_id="legacy-fixed",
            claimed_at=claimed_at,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "exposure-ledger.jsonl"
            path.write_text(canonical(payload) + "\n", encoding="utf-8")
            ledger = ExposureLedger(Path(tmpdir))
            records = ledger.verify()
            self.assertEqual(records[0].ledger_version, 1)
            self.assertEqual(ledger.verify_permit(records[0].to_permit()), records[0])

            with self.assertRaisesRegex(ExposureDeniedError, "already exposed"):
                ledger.claim(
                    manifest(
                        mode="fixed_holdout",
                        request_start=date(2024, 1, 15),
                        request_end=date(2024, 1, 31),
                        warmup_start=None,
                        warmup_end=None,
                    ),
                    run_id="new-fixed",
                    claimed_at=datetime(2026, 7, 17, 0, 1, tzinfo=UTC),
                )
            migrated = ledger.verify()

        self.assertEqual([record.ledger_version for record in migrated], [1, 2])
        self.assertEqual(migrated[-1].outcome, "denied")

    def test_legacy_exposed_sealed_record_is_verifiable_but_cannot_become_a_permit(self):
        legacy_window = ValidationWindowManifest(
            strategy_family="normal",
            mode="sealed",
            request_start=date(2025, 1, 1),
            request_end=date(2026, 6, 30),
            dataset_hash="a" * 64,
            config_hash="b" * 64,
            cost_model_hash="c" * 64,
            code_hash="d" * 64,
            _allow_legacy_exposed_sealed=True,
        )
        payload = _signed_legacy_record(
            legacy_window,
            run_id="legacy-exposed-sealed",
            claimed_at=datetime(2026, 7, 1, tzinfo=UTC),
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "exposure-ledger.jsonl"
            path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            ledger = ExposureLedger(path)
            records = ledger.verify()

            self.assertEqual(len(records), 1)
            with self.assertRaisesRegex(PermissionError, "read-only"):
                records[0].to_permit()
            with self.assertRaisesRegex(ValueError, "cannot create"):
                ledger.claim(records[0].manifest, run_id="legacy-reclaim")
            self.assertEqual(len(ledger.records), 1)

    def test_public_canonical_ledger_path_normalizes_directories_and_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir) / "ledger"
            expected = (directory / "exposure-ledger.jsonl").resolve()
            self.assertEqual(canonical_exposure_ledger_path(directory), expected)
            self.assertEqual(canonical_exposure_ledger_path(expected), expected)

    def test_concurrent_exclusive_claims_produce_one_permit_and_one_denial(self):
        window = manifest(
            mode="fixed_holdout",
            request_start=date(2024, 7, 1),
            request_end=date(2024, 9, 30),
            warmup_start=None,
            warmup_end=None,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir)

            def attempt(run_id):
                try:
                    ExposureLedger(path).claim(
                        window,
                        run_id=run_id,
                        claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
                    )
                except ExposureDeniedError:
                    return "denied"
                return "granted"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = tuple(pool.map(attempt, ("parallel-one", "parallel-two")))
            records = ExposureLedger(path).verify()

        self.assertEqual(sorted(outcomes), ["denied", "granted"])
        self.assertEqual([record.outcome for record in records].count("granted"), 1)
        self.assertEqual([record.outcome for record in records].count("denied"), 1)

    def test_sealed_claim_is_utc_complete_and_bound_to_the_formal_window(self):
        sealed = manifest(
            mode="sealed",
            request_start=date(2026, 8, 1),
            request_end=date(2028, 7, 31),
            warmup_start=None,
            warmup_end=None,
        )
        plus_fourteen = timezone(timedelta(hours=14))
        with tempfile.TemporaryDirectory() as tmpdir:
            premature_time = datetime(2028, 8, 1, 0, 30, tzinfo=plus_fourteen)
            premature_ledger = ExposureLedger(
                Path(tmpdir) / "premature",
                _clock=lambda: premature_time,
            )
            with self.assertRaisesRegex(ExposureDeniedError, "not complete"):
                premature_ledger.claim(
                    sealed,
                    run_id="sealed-too-early",
                    claimed_at=premature_time,
                )
            self.assertEqual(premature_ledger.records[0].outcome, "denied")

            released_time = datetime(2028, 8, 1, tzinfo=UTC)
            permit = ExposureLedger(
                Path(tmpdir) / "complete",
                _clock=lambda: released_time,
            ).claim(
                sealed,
                run_id="sealed-formal",
                claimed_at=released_time,
            )

        self.assertEqual(permit.mode, "sealed")
        self.assertTrue(permit.is_authentic())

    def test_forged_future_claimed_at_is_denied_by_the_internal_clock(self):
        sealed = manifest(
            mode="sealed",
            request_start=date(2026, 8, 1),
            request_end=date(2028, 7, 31),
            warmup_start=None,
            warmup_end=None,
        )
        trusted_now = datetime(2026, 7, 20, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir:
            ledger = ExposureLedger(Path(tmpdir), _clock=lambda: trusted_now)
            with self.assertRaisesRegex(ExposureDeniedError, "in the future"):
                ledger.claim(
                    sealed,
                    run_id="forged-future-claim",
                    claimed_at=datetime(2028, 8, 1, tzinfo=UTC),
                )

            record = ledger.records[0]

        self.assertEqual(record.outcome, "denied")
        self.assertEqual(record.claimed_at, trusted_now)

    def test_sealed_permit_cannot_be_replayed_before_release(self):
        warmup = formal_sealed_warmup_interval("daytrade", date(2026, 8, 1))
        sealed = manifest(
            mode="sealed",
            request_start=date(2026, 8, 1),
            request_end=date(2028, 7, 31),
            warmup_start=warmup.start,
            warmup_end=warmup.end,
        )
        released_time = datetime(2028, 8, 1, tzinfo=UTC)
        premature_time = datetime(2026, 7, 20, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {DEFAULT_EXPOSURE_LEDGER_PATH_ENV: tmpdir},
            clear=False,
        ):
            permit = ExposureLedger(
                Path(tmpdir),
                _clock=lambda: released_time,
            ).claim(sealed, run_id="sealed-replay", claimed_at=released_time)

            with self.assertRaisesRegex(PermissionError, "not released"):
                permit.assert_allows(
                    dataset_hash="a" * 64,
                    strategy_family="daytrade",
                    start=date(2026, 8, 1),
                    end=date(2028, 7, 31),
                    _clock=lambda: premature_time,
                )
            self.assertFalse(
                permit.admits(
                    strategy_family="daytrade",
                    start=date(2026, 8, 1),
                    end=date(2028, 7, 31),
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    cost_model_hash="c" * 64,
                    code_hash="d" * 64,
                    _clock=lambda: premature_time,
                )
            )
            self.assertTrue(
                permit.admits(
                    strategy_family="daytrade",
                    start=date(2026, 8, 1),
                    end=date(2028, 7, 31),
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    cost_model_hash="c" * 64,
                    code_hash="d" * 64,
                    _clock=lambda: released_time,
                )
            )

    def test_research_reads_are_not_blocked_by_the_sealed_release_gate(self):
        trusted_now = datetime(2026, 7, 20, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {DEFAULT_EXPOSURE_LEDGER_PATH_ENV: tmpdir},
            clear=False,
        ):
            permit = ExposureLedger(
                Path(tmpdir),
                _clock=lambda: trusted_now,
            ).claim(
                manifest(
                    mode="research",
                    request_start=date(2019, 1, 1),
                    request_end=date(2019, 12, 31),
                    warmup_start=None,
                    warmup_end=None,
                ),
                run_id="ordinary-research-read",
            )
            permit.assert_allows(
                dataset_hash="a" * 64,
                strategy_family="daytrade",
                start=date(2019, 1, 1),
                end=date(2019, 12, 31),
                _clock=lambda: trusted_now,
            )

    def test_admits_rejects_a_signed_permit_absent_from_the_canonical_ledger(self):
        sealed = manifest(
            mode="sealed",
            request_start=date(2026, 8, 1),
            request_end=date(2028, 7, 31),
            warmup_start=None,
            warmup_end=None,
        )
        released_time = datetime(2028, 8, 1, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as tmpdir, patch.dict(
            os.environ,
            {
                DEFAULT_EXPOSURE_LEDGER_PATH_ENV: str(
                    Path(tmpdir) / "canonical.jsonl"
                )
            },
            clear=False,
        ):
            permit = ExposureLedger(
                Path(tmpdir) / "alternate.jsonl",
                path_env="",
                _clock=lambda: released_time,
            ).claim(sealed, run_id="noncanonical-sealed", claimed_at=released_time)

            self.assertFalse(
                permit.admits(
                    strategy_family="daytrade",
                    start=date(2026, 8, 1),
                    end=date(2028, 7, 31),
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    cost_model_hash="c" * 64,
                    code_hash="d" * 64,
                    _clock=lambda: released_time,
                )
            )

    def test_tampering_or_switching_the_canonical_ledger_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            canonical = Path(tmpdir) / "canonical.jsonl"
            with patch.dict(
                os.environ,
                {DEFAULT_EXPOSURE_LEDGER_PATH_ENV: str(canonical)},
                clear=False,
            ):
                ledger = ExposureLedger()
                ledger.claim(
                    manifest(),
                    run_id="canonical-run",
                    claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
                )
                with self.assertRaisesRegex(ValueError, "canonical"):
                    ExposureLedger(Path(tmpdir) / "alternate")
                alternate = ExposureLedger(
                    Path(tmpdir) / "alternate",
                    path_env="",
                ).claim(
                    manifest(),
                    run_id="alternate-run",
                    claimed_at=datetime(2026, 7, 17, 0, 1, tzinfo=UTC),
                )
                with self.assertRaisesRegex(PermissionError, "canonical"):
                    alternate.assert_allows(
                        dataset_hash="a" * 64,
                        strategy_family="daytrade",
                        start=date(2023, 1, 3),
                        end=date(2023, 1, 31),
                    )

            payload = json.loads(canonical.read_text(encoding="utf-8"))
            payload["dataset_hash"] = "f" * 64
            canonical.write_text(json.dumps(payload) + "\n", encoding="utf-8")
            with self.assertRaises(ExposureLedgerIntegrityError):
                ledger.verify()

            with patch.dict(
                os.environ,
                {DEFAULT_EXPOSURE_LEDGER_PATH_ENV: "relative-ledger"},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "absolute path"):
                    ExposureLedger()


if __name__ == "__main__":
    unittest.main()
