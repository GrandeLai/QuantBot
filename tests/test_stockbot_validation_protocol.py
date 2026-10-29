import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.validation_protocol import (
    DAYTRADE_VALIDATION_PROTOCOL,
    FixedHoldoutGate,
    NORMAL_VALIDATION_PROTOCOL,
    SealedHoldoutGate,
    assert_validation_clock_released,
    assert_validation_data_released,
    purged_walk_forward_split,
)


UTC = timezone.utc


class ValidationProtocolTest(unittest.TestCase):
    def test_protocols_have_fixed_research_walk_forward_and_sealed_dates(self):
        self.assertEqual(NORMAL_VALIDATION_PROTOCOL.research_start, date(2010, 1, 1))
        self.assertEqual(NORMAL_VALIDATION_PROTOCOL.research_end, date(2020, 12, 31))
        self.assertEqual(NORMAL_VALIDATION_PROTOCOL.walk_forward_start, date(2021, 1, 1))
        self.assertEqual(DAYTRADE_VALIDATION_PROTOCOL.research_start, date(2019, 1, 1))
        for protocol in (NORMAL_VALIDATION_PROTOCOL, DAYTRADE_VALIDATION_PROTOCOL):
            self.assertEqual(protocol.walk_forward_end, date(2024, 12, 31))
            self.assertEqual(protocol.sealed_start, date(2026, 8, 1))
            self.assertEqual(protocol.sealed_end, date(2028, 7, 31))
            self.assertEqual(protocol.phase(date(2027, 6, 1)), "sealed")
            self.assertEqual(protocol.phase(date(2025, 6, 1)), "outside")

    def test_purge_and_embargo_remove_max_label_horizon_neighbors(self):
        dates = tuple(date(2025, 1, 5) + timedelta(days=index) for index in range(11))
        split = purged_walk_forward_split(
            dates,
            validation_start=date(2025, 1, 10),
            validation_end=date(2025, 1, 11),
            purge_days=2,
            embargo_days=2,
        )

        self.assertEqual(split.train_dates, tuple(date(2025, 1, day) for day in (5, 6, 7)))
        self.assertEqual(split.purged_dates, tuple(date(2025, 1, day) for day in (8, 9)))
        self.assertEqual(split.validation_dates, tuple(date(2025, 1, day) for day in (10, 11)))
        self.assertEqual(split.embargoed_dates, tuple(date(2025, 1, day) for day in (12, 13)))

    def test_sealed_holdout_can_be_claimed_only_once_per_strategy_family(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gate = SealedHoldoutGate(Path(tmpdir))
            claim = gate.claim(
                strategy_family="normal",
                dataset_hash="a" * 64,
                config_hash="b" * 64,
                code_hash="c" * 64,
                claimed_at=datetime(2028, 8, 1, tzinfo=UTC),
            )
            with self.assertRaisesRegex(ValueError, "already claimed"):
                gate.claim(
                    strategy_family="normal",
                    dataset_hash="a" * 64,
                    config_hash="d" * 64,
                    code_hash="c" * 64,
                    claimed_at=datetime(2028, 8, 2, tzinfo=UTC),
                )
            payload = json.loads((Path(tmpdir) / "normal.json").read_text(encoding="utf-8"))

        self.assertEqual(claim.strategy_family, "normal")
        self.assertEqual(payload["dataset_hash"], "a" * 64)

    def test_holdout_cannot_be_claimed_before_it_is_complete_or_with_bad_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gate = SealedHoldoutGate(Path(tmpdir))
            with self.assertRaisesRegex(ValueError, "not complete"):
                gate.claim(
                    strategy_family="daytrade",
                    dataset_hash="a" * 64,
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                    claimed_at=datetime(2028, 7, 31, 23, tzinfo=UTC),
                )
            with self.assertRaisesRegex(ValueError, "dataset_hash"):
                gate.claim(
                    strategy_family="daytrade",
                    dataset_hash="bad",
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                    claimed_at=datetime(2028, 8, 1, tzinfo=UTC),
                )

    def test_fixed_holdout_access_is_claimed_atomically_once_per_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            gate = FixedHoldoutGate(Path(tmpdir))
            claim = gate.claim(
                run_id="daytrade-h5-2024q4",
                strategy_family="daytrade",
                holdout_start=date(2024, 9, 1),
                holdout_end=date(2024, 12, 31),
                manifest_hash="a" * 64,
                config_hash="b" * 64,
                code_hash="c" * 64,
                claimed_at=datetime(2026, 7, 17, tzinfo=UTC),
            )
            with self.assertRaisesRegex(ValueError, "already claimed"):
                gate.claim(
                    run_id="daytrade-h5-2024q4",
                    strategy_family="daytrade",
                    holdout_start=date(2024, 9, 1),
                    holdout_end=date(2024, 12, 31),
                    manifest_hash="a" * 64,
                    config_hash="b" * 64,
                    code_hash="c" * 64,
                    claimed_at=datetime(2026, 7, 17, 0, 1, tzinfo=UTC),
                )
            payload = json.loads(
                (Path(tmpdir) / "daytrade-h5-2024q4.json").read_text(encoding="utf-8")
            )

        self.assertEqual(claim.run_id, "daytrade-h5-2024q4")
        self.assertEqual(payload["holdout_start"], "2024-09-01")
        self.assertEqual(payload["manifest_hash"], "a" * 64)

    def test_shared_release_gate_blocks_sealed_overlap_before_cutoff(self):
        with self.assertRaisesRegex(PermissionError, "not released"):
            assert_validation_data_released(
                strategy_family="daytrade",
                start=date(2028, 7, 1),
                end=date(2028, 8, 1),
                _clock=lambda: datetime(2026, 7, 20, tzinfo=UTC),
            )

    def test_shared_release_gate_requires_canonical_permit_after_cutoff(self):
        assert_validation_clock_released(
            strategy_family="normal",
            start=date(2026, 8, 1),
            end=date(2028, 7, 31),
            _clock=lambda: datetime(2028, 8, 1, tzinfo=UTC),
        )
        with self.assertRaisesRegex(PermissionError, "canonical sealed permit"):
            assert_validation_data_released(
                strategy_family="normal",
                start=date(2026, 8, 1),
                end=date(2028, 7, 31),
                _clock=lambda: datetime(2028, 8, 1, tzinfo=UTC),
            )

    def test_shared_release_gate_is_noop_for_research_interval(self):
        assert_validation_data_released(
            strategy_family="normal",
            start=date(2019, 1, 1),
            end=date(2019, 12, 31),
            _clock=lambda: datetime(2026, 7, 20, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
