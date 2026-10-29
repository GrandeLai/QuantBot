import sys
import unittest
from copy import copy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data import (
    ADMISSION_REQUIRED_COMPONENTS,
    PRICE_ADMISSION_REQUIRED_COMPONENTS,
    DataQualityTier,
    DailyPriceObservation,
    PointInTimeDataManifest,
    PointInTimeDataset,
    PointInTimePriceDataset,
    PointInTimePriceManifest,
    PointInTimeTotalReturnDataset,
    PointInTimeTotalReturnManifest,
    RETURN_ADMISSION_REQUIRED_COMPONENTS,
    SecurityClassification,
    SecurityIdentity,
    SecurityMasterSnapshot,
    MonthlyTotalReturnIndexObservation,
    TradingSession,
    UniverseMembership,
    canonical_dataset_hash,
    canonical_price_dataset_hash,
    canonical_security_master_snapshot_hash,
    canonical_total_return_dataset_hash,
    sign_point_in_time_coverage,
    sign_trading_calendar,
)
from quantbot.factors import short_term_reversal_sleeve


UTC = timezone.utc
VERIFIER = "independent-return-validator"
VERIFIER_KEY = b"independent-return-validator-key"
CALENDAR_ISSUER = "independent-month-calendar"
CALENDAR_KEY = b"independent-month-calendar-key"


def instant(day: date, hour: int = 21) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


PERIODS = (
    date(2024, 12, 31),
    date(2025, 1, 31),
    date(2025, 2, 28),
    date(2025, 3, 31),
)
ADJUSTMENT_VINTAGE_AT = datetime(2025, 4, 1, 12, tzinfo=UTC)


CALENDAR = sign_trading_calendar(
    exchange="XNYS",
    effective_date_timezone="UTC",
    issuer=CALENDAR_ISSUER,
    issuer_version="1",
    generated_at=instant(date(2024, 12, 1)),
    coverage_start=PERIODS[0],
    coverage_end=PERIODS[-1],
    missing_intervals=(),
    sessions=tuple(TradingSession(day, instant(day)) for day in PERIODS),
    signing_key=CALENDAR_KEY,
)


ADMISSION_CALENDAR = sign_trading_calendar(
    exchange="XNYS",
    effective_date_timezone="UTC",
    issuer=CALENDAR_ISSUER,
    issuer_version="1",
    generated_at=instant(date(2024, 12, 1)),
    coverage_start=PERIODS[0],
    coverage_end=date(2025, 4, 15),
    missing_intervals=(),
    sessions=tuple(
        TradingSession(day, instant(day))
        for day in (*PERIODS, date(2025, 4, 15))
    ),
    signing_key=CALENDAR_KEY,
)


def master(as_of: datetime, sector: str = "Tech") -> SecurityMasterSnapshot:
    identity = SecurityIdentity("SEC", date(2000, 1, 1), None, instant(date(2000, 1, 1)))
    classification = SecurityClassification(
        "SEC", "SEC", "NYSE", sector, date(2000, 1, 1), None, instant(date(2000, 1, 1))
    )
    membership = UniverseMembership(
        "research",
        "SEC",
        date(2000, 1, 1),
        None,
        instant(date(2000, 1, 1)),
        "verified-index-history",
    )
    digest = canonical_dataset_hash(
        provider="verified-security-master-provider",
        dataset_id="verified-security-master-v1",
        feed="consolidated",
        schema_version="1",
        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        effective_date_timezone="UTC",
        coverage_start=date(2000, 1, 1),
        coverage_end=date(2035, 12, 31),
        identities=(identity,),
        classifications=(classification,),
        memberships=(membership,),
        fundamentals=(),
        market_caps=(),
        corporate_actions=(),
    )
    dataset = PointInTimeDataset(
        manifest=PointInTimeDataManifest(
            provider="verified-security-master-provider",
            dataset_id="verified-security-master-v1",
            feed="consolidated",
            schema_version="1",
            quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
            effective_date_timezone="UTC",
            coverage_start=date(2000, 1, 1),
            coverage_end=date(2035, 12, 31),
            content_hash=digest,
        ),
        identities=(identity,),
        classifications=(classification,),
        memberships=(membership,),
    )
    return dataset.snapshot(as_of, ("research",))


def admitted_union_master(as_of: datetime) -> SecurityMasterSnapshot:
    universes = ("SP500", "RUSSELL1000")
    identities = (
        SecurityIdentity("SEC", date(2000, 1, 1), None, instant(date(2000, 1, 1))),
        SecurityIdentity("SEC2", date(2000, 1, 1), None, instant(date(2000, 1, 1))),
    )
    classifications = (
        SecurityClassification(
            "SEC", "SEC", "NYSE", "Tech", date(2000, 1, 1), None,
            instant(date(2000, 1, 1)),
        ),
        SecurityClassification(
            "SEC2", "SEC2", "NYSE", "Finance", date(2000, 1, 1), None,
            instant(date(2000, 1, 1)),
        ),
    )
    memberships = (
        UniverseMembership(
            "SP500", "SEC", date(2000, 1, 1), None,
            instant(date(2000, 1, 1)), "verified-index-history",
        ),
        UniverseMembership(
            "RUSSELL1000", "SEC", date(2000, 1, 1), None,
            instant(date(2000, 1, 1)), "verified-index-history",
        ),
        UniverseMembership(
            "RUSSELL1000", "SEC2", date(2000, 1, 1), None,
            instant(date(2000, 1, 1)), "verified-index-history",
        ),
    )
    digest = canonical_dataset_hash(
        provider="verified-union-security-master-provider",
        dataset_id="verified-union-security-master-v1",
        feed="consolidated",
        schema_version="1",
        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        effective_date_timezone="UTC",
        coverage_start=date(2000, 1, 1),
        coverage_end=date(2035, 12, 31),
        identities=identities,
        classifications=classifications,
        memberships=memberships,
        fundamentals=(),
        market_caps=(),
        corporate_actions=(),
    )
    record_counts = {
        "identities": len(identities),
        "classifications": len(classifications),
        "memberships": len(memberships),
        "fundamentals": 0,
        "market_caps": 0,
        "corporate_actions": 0,
    }
    master_attestation = sign_point_in_time_coverage(
        verifier=VERIFIER,
        verifier_version="1",
        generated_at=instant(date(2025, 5, 1)),
        provider="verified-union-security-master-provider",
        dataset_id="verified-union-security-master-v1",
        dataset_hash=digest,
        session_calendar_hash=ADMISSION_CALENDAR.content_hash,
        related_dataset_hashes={},
        covered_universes=universes,
        session_security_counts={
            session.session_date: 2 for session in ADMISSION_CALENDAR.sessions
        },
        source_artifact_hashes=("a" * 64,),
        record_counts=record_counts,
        assertions={name: True for name in ADMISSION_REQUIRED_COMPONENTS},
        missing_sessions=(),
        missing_intervals=(),
        signing_key=VERIFIER_KEY,
    )
    dataset = PointInTimeDataset(
        manifest=PointInTimeDataManifest(
            provider="verified-union-security-master-provider",
            dataset_id="verified-union-security-master-v1",
            feed="consolidated",
            schema_version="1",
            quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
            effective_date_timezone="UTC",
            coverage_start=date(2000, 1, 1),
            coverage_end=date(2035, 12, 31),
            content_hash=digest,
            coverage_attestation=master_attestation,
        ),
        identities=identities,
        classifications=classifications,
        memberships=memberships,
    )
    price_rows = tuple(
        DailyPriceObservation(
            security_id=identity.security_id,
            trading_date=session.session_date,
            close=100.0,
            volume=100_000,
            available_at=session.close_at,
        )
        for session in ADMISSION_CALENDAR.sessions
        for identity in identities
    )
    price_hash = canonical_price_dataset_hash(
        provider="verified-union-price-provider",
        dataset_id="verified-union-prices-v1",
        feed="consolidated",
        schema_version="1",
        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        effective_date_timezone="UTC",
        observations=price_rows,
    )
    price_attestation = sign_point_in_time_coverage(
        verifier=VERIFIER,
        verifier_version="1",
        generated_at=instant(date(2025, 5, 1)),
        provider="verified-union-price-provider",
        dataset_id="verified-union-prices-v1",
        dataset_hash=price_hash,
        session_calendar_hash=ADMISSION_CALENDAR.content_hash,
        related_dataset_hashes={"security_master": digest},
        covered_universes=universes,
        session_security_counts={
            session.session_date: 2 for session in ADMISSION_CALENDAR.sessions
        },
        source_artifact_hashes=("b" * 64,),
        record_counts={"prices": len(price_rows)},
        assertions={name: True for name in PRICE_ADMISSION_REQUIRED_COMPONENTS},
        missing_sessions=(),
        missing_intervals=(),
        signing_key=VERIFIER_KEY,
    )
    prices = PointInTimePriceDataset(
        manifest=PointInTimePriceManifest(
            provider="verified-union-price-provider",
            dataset_id="verified-union-prices-v1",
            feed="consolidated",
            schema_version="1",
            quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
            effective_date_timezone="UTC",
            content_hash=price_hash,
            coverage_attestation=price_attestation,
        ),
        observations=price_rows,
    )
    return dataset.admitted_snapshot(
        as_of,
        universes,
        admission_start=PERIODS[0],
        admission_end=date(2025, 4, 15),
        universe=universes,
        trusted_verifier_keys={VERIFIER: VERIFIER_KEY},
        price_dataset=prices,
        trading_calendar=ADMISSION_CALENDAR,
        trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
    )


class PointInTimeTotalReturnDatasetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2025, 4, 15, 12, tzinfo=UTC)
        self.master = master(self.as_of)
        self.rows = (
            MonthlyTotalReturnIndexObservation("SEC", PERIODS[0], 100.0, instant(PERIODS[0])),
            MonthlyTotalReturnIndexObservation("SEC", PERIODS[1], 110.0, instant(PERIODS[1])),
            MonthlyTotalReturnIndexObservation("SEC", PERIODS[2], 121.0, instant(date(2025, 3, 5))),
            MonthlyTotalReturnIndexObservation("SEC", PERIODS[3], 133.1, instant(PERIODS[3])),
            MonthlyTotalReturnIndexObservation("BENCH", PERIODS[0], 100.0, instant(PERIODS[0])),
            MonthlyTotalReturnIndexObservation("BENCH", PERIODS[1], 102.0, instant(PERIODS[1])),
            MonthlyTotalReturnIndexObservation("BENCH", PERIODS[2], 101.0, instant(PERIODS[2])),
            MonthlyTotalReturnIndexObservation("BENCH", PERIODS[3], 103.0, instant(PERIODS[3])),
        )

    def _dataset(
        self,
        *,
        rows=None,
        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        assertions=None,
        signing_key=VERIFIER_KEY,
        adjustment_vintage_at=ADJUSTMENT_VINTAGE_AT,
        security_master=None,
        covered_universes=None,
    ):
        rows = self.rows if rows is None else tuple(rows)
        security_master = security_master or self.master
        covered_universes = (
            security_master.universes
            if covered_universes is None
            else tuple(covered_universes)
        )
        digest = canonical_total_return_dataset_hash(
            provider="verified-total-return-provider",
            dataset_id="monthly-total-return-v1",
            feed="consolidated",
            schema_version="1",
            quality_tier=quality_tier,
            effective_date_timezone="UTC",
            adjustment_vintage_at=adjustment_vintage_at,
            coverage_start=PERIODS[0],
            coverage_end=PERIODS[-1],
            security_master_dataset_hash=security_master.source_dataset_hash,
            reference_security_ids=("BENCH",),
            observations=rows,
        )
        counts = {
            period: len({row.security_id for row in rows if row.month_end == period})
            for period in PERIODS
        }
        attestation = sign_point_in_time_coverage(
            verifier=VERIFIER,
            verifier_version="1",
            generated_at=max(
                instant(date(2025, 5, 1)),
                adjustment_vintage_at + timedelta(days=1),
            ),
            provider="verified-total-return-provider",
            dataset_id="monthly-total-return-v1",
            dataset_hash=digest,
            session_calendar_hash=CALENDAR.content_hash,
            related_dataset_hashes={"security_master": security_master.source_dataset_hash},
            covered_universes=covered_universes,
            session_security_counts=counts,
            source_artifact_hashes=("b" * 64,),
            record_counts={"total_return_index": len(rows)},
            assertions=assertions or {
                name: True for name in RETURN_ADMISSION_REQUIRED_COMPONENTS
            },
            missing_sessions=(),
            missing_intervals=(),
            signing_key=signing_key,
        )
        manifest = PointInTimeTotalReturnManifest(
            provider="verified-total-return-provider",
            dataset_id="monthly-total-return-v1",
            feed="consolidated",
            schema_version="1",
            quality_tier=quality_tier,
            effective_date_timezone="UTC",
            adjustment_vintage_at=adjustment_vintage_at,
            coverage_start=PERIODS[0],
            coverage_end=PERIODS[-1],
            security_master_dataset_hash=security_master.source_dataset_hash,
            reference_security_ids=("BENCH",),
            content_hash=digest,
            coverage_attestation=attestation,
        )
        return PointInTimeTotalReturnDataset(manifest=manifest, observations=rows)

    def _snapshot(self, dataset=None, security_master=None, benchmark="BENCH"):
        return (dataset or self._dataset()).snapshot(
            security_master=security_master or self.master,
            universe="research",
            benchmark_security_id=benchmark,
            trading_calendar=CALENDAR,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
            trusted_verifier_keys={VERIFIER: VERIFIER_KEY},
        )

    def test_signed_snapshot_binds_master_calendar_benchmark_and_visible_revision(self):
        snapshot = self._snapshot()

        returns = {
            (row.security_id, row.month_end): row.total_return
            for row in snapshot.observations
        }
        self.assertAlmostEqual(returns[("SEC", PERIODS[2])], 0.10)
        self.assertAlmostEqual(returns[("SEC", PERIODS[3])], 0.10)
        self.assertTrue(snapshot.provenance_verified)
        self.assertFalse(snapshot.security_master_admission_verified)
        self.assertEqual(snapshot.latest_complete_month, PERIODS[-1])
        self.assertEqual(snapshot.investable_security_ids, ("SEC",))
        self.assertEqual(snapshot.missing_latest_security_ids, ())
        self.assertEqual(snapshot.security_master_hash, canonical_security_master_snapshot_hash(self.master))
        self.assertEqual(snapshot.security_master_dataset_hash, self.master.source_dataset_hash)
        self.assertEqual(snapshot.benchmark_security_id, "BENCH")
        with self.assertRaisesRegex(ValueError, "admitted security master"):
            snapshot.require_final_admission()
        with self.assertRaisesRegex(ValueError, "provenance"):
            replace(snapshot, provider="tampered-provider")
        factor = short_term_reversal_sleeve(snapshot, self.master)
        with self.assertRaisesRegex(ValueError, "admitted security master"):
            factor.require_final_admission()

    def test_manifest_content_tampering_and_wrong_master_are_rejected(self):
        dataset = self._dataset()
        with self.assertRaisesRegex(ValueError, "content_hash"):
            PointInTimeTotalReturnDataset(
                manifest=replace(dataset.manifest, content_hash="c" * 64, coverage_attestation=None),
                observations=self.rows,
            )
        with self.assertRaisesRegex(ValueError, "security master"):
            self._snapshot(dataset, master(self.as_of, "Finance"))

    def test_untrusted_signature_unknown_benchmark_and_failed_adjustment_assertion_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "signature"):
            self._snapshot(self._dataset(signing_key=b"different-signing-key"))
        with self.assertRaisesRegex(ValueError, "reference"):
            self._snapshot(benchmark="FUTURE_BENCH")
        with self.assertRaisesRegex(ValueError, "predates the adjustment vintage"):
            self._snapshot(
                self._dataset(
                    adjustment_vintage_at=datetime(2026, 1, 1, tzinfo=UTC)
                )
            )
        for failed_assertion in ("corporate_actions", "split_adjustments", "cash_dividends"):
            assertions = {name: True for name in RETURN_ADMISSION_REQUIRED_COMPONENTS}
            assertions[failed_assertion] = False
            with self.subTest(assertion=failed_assertion), self.assertRaisesRegex(
                ValueError, failed_assertion
            ):
                self._snapshot(self._dataset(assertions=assertions))

    def test_duplicate_vintage_is_rejected_and_free_data_cannot_final_admit(self):
        duplicate = self.rows + (self.rows[0],)
        with self.assertRaisesRegex(ValueError, "duplicate total-return index period"):
            self._dataset(rows=duplicate)

        missing_february = tuple(
            row for row in self.rows if row.month_end != date(2025, 2, 28)
        )
        with self.assertRaisesRegex(ValueError, "incomplete monthly coverage"):
            self._dataset(rows=missing_february)

        free_snapshot = self._snapshot(
            self._dataset(quality_tier=DataQualityTier.DEVELOPMENT_FREE)
        )
        self.assertTrue(free_snapshot.provenance_verified)
        with self.assertRaisesRegex(ValueError, "point-in-time verified"):
            free_snapshot.require_final_admission()

        benchmark_only = tuple(
            row for row in self.rows if row.security_id == "BENCH"
        )
        uncovered = self._snapshot(self._dataset(rows=benchmark_only))
        self.assertEqual(uncovered.investable_security_ids, ())
        self.assertEqual(uncovered.missing_latest_security_ids, ("SEC",))
        with self.assertRaisesRegex(ValueError, "investable total-return coverage"):
            uncovered.require_final_admission()

    def test_union_admitted_master_produces_final_admissible_private_provenance(self):
        admitted_master = admitted_union_master(self.as_of)
        self.assertTrue(admitted_master.admission_verified)
        union_rows = self.rows + tuple(
            MonthlyTotalReturnIndexObservation(
                "SEC2",
                period,
                100.0 + index,
                instant(period),
            )
            for index, period in enumerate(PERIODS)
        )
        dataset = self._dataset(
            rows=union_rows,
            security_master=admitted_master,
            covered_universes=admitted_master.universes,
        )
        snapshot = dataset.snapshot(
            security_master=admitted_master,
            universe=admitted_master.universes,
            benchmark_security_id="BENCH",
            trading_calendar=CALENDAR,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
            trusted_verifier_keys={VERIFIER: VERIFIER_KEY},
        )

        self.assertTrue(snapshot.provenance_is_valid)
        self.assertTrue(snapshot.security_master_admission_verified)
        self.assertEqual(snapshot.investable_security_ids, ("SEC", "SEC2"))
        snapshot.require_final_admission()

        with self.assertRaisesRegex(ValueError, "exact admitted universe set"):
            dataset.snapshot(
                security_master=admitted_master,
                universe=(admitted_master.universes[0],),
                benchmark_security_id="BENCH",
                trading_calendar=CALENDAR,
                trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
                trusted_verifier_keys={VERIFIER: VERIFIER_KEY},
            )
        return_attestation_missing_one_universe = self._dataset(
            rows=union_rows,
            security_master=admitted_master,
            covered_universes=(admitted_master.universes[0],),
        )
        with self.assertRaisesRegex(ValueError, "requested universe"):
            return_attestation_missing_one_universe.snapshot(
                security_master=admitted_master,
                universe=admitted_master.universes,
                benchmark_security_id="BENCH",
                trading_calendar=CALENDAR,
                trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
                trusted_verifier_keys={VERIFIER: VERIFIER_KEY},
            )

        tampered_master = copy(admitted_master)
        object.__setattr__(tampered_master, "universes", ("SP500",))
        self.assertFalse(tampered_master.admission_verified)
        non_admissible = dataset.snapshot(
            security_master=tampered_master,
            universe="SP500",
            benchmark_security_id="BENCH",
            trading_calendar=CALENDAR,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
            trusted_verifier_keys={VERIFIER: VERIFIER_KEY},
        )
        self.assertTrue(non_admissible.provenance_is_valid)
        with self.assertRaisesRegex(ValueError, "admitted security master"):
            non_admissible.require_final_admission()


if __name__ == "__main__":
    unittest.main()
