import calendar
import math
import sys
import unittest
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data import (
    DataQualityTier,
    FundamentalObservation,
    MarketCapObservation,
    PointInTimeDataManifest,
    PointInTimeDataset,
    PointInTimeTotalReturnDataset,
    PointInTimeTotalReturnManifest,
    RETURN_ADMISSION_REQUIRED_COMPONENTS,
    SecurityClassification,
    SecurityIdentity,
    SecurityMasterSnapshot,
    TradingSession,
    UniverseMembership,
    canonical_dataset_hash,
    canonical_total_return_dataset_hash,
    canonical_security_master_snapshot_hash,
    sign_point_in_time_coverage,
    sign_trading_calendar,
)
from quantbot.factors.low_entropy import (
    FactorSleeve,
    MonthlyReturnObservation,
    MonthlyTotalReturnIndexObservation,
    build_residual_momentum_sleeves,
    eps_revision_sleeve,
    neutralize_sleeve,
    quality_sleeve,
    residual_momentum_sleeve,
    short_term_reversal_sleeve,
)


UTC = timezone.utc


def month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def at(day: date, hour: int = 23) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def shift_month(year: int, month: int, offset: int) -> tuple[int, int]:
    ordinal = year * 12 + month - 1 + offset
    return ordinal // 12, ordinal % 12 + 1


def return_history(
    security_id: str,
    values: list[float],
    *,
    start_year: int = 2021,
    start_month: int = 1,
) -> tuple[MonthlyReturnObservation, ...]:
    rows = []
    for offset, value in enumerate(values):
        year, month = shift_month(start_year, start_month, offset)
        day = month_end(year, month)
        rows.append(MonthlyReturnObservation(security_id, day, value, at(day)))
    return tuple(rows)


def security_master(
    as_of: datetime,
    fundamentals: dict[str, FundamentalObservation],
    sectors: dict[str, str],
    market_caps: dict[str, float],
    *,
    effective_date_timezone: str = "UTC",
) -> SecurityMasterSnapshot:
    effective_date = as_of.astimezone(ZoneInfo(effective_date_timezone)).date()
    security_ids = tuple(sorted(sectors))
    identities = {
        security_id: SecurityIdentity(
            security_id=security_id,
            listed_on=date(2000, 1, 1),
            delisted_on=None,
            available_at=at(date(2000, 1, 1)),
        )
        for security_id in security_ids
    }
    classifications = {
        security_id: SecurityClassification(
            security_id=security_id,
            ticker="T" + str(index),
            exchange="NYSE",
            sector=sectors[security_id],
            effective_from=date(2000, 1, 1),
            effective_to=None,
            available_at=at(date(2000, 1, 1)),
        )
        for index, security_id in enumerate(security_ids)
    }
    caps = {
        security_id: MarketCapObservation(
            security_id=security_id,
            effective_on=effective_date,
            available_at=as_of,
            market_cap=market_caps[security_id],
        )
        for security_id in security_ids
        if security_id in market_caps
    }
    memberships = tuple(
        UniverseMembership(
            "research",
            security_id,
            date(2000, 1, 1),
            None,
            at(date(2000, 1, 1)),
            "factor-test-point-in-time-universe",
        )
        for security_id in security_ids
    )
    coverage_start = date(1990, 1, 1)
    coverage_end = date(2035, 12, 31)
    digest = canonical_dataset_hash(
        provider="factor-test-security-master-provider",
        dataset_id="factor-test-security-master-v1",
        feed="verified-test-feed",
        schema_version="1",
        quality_tier=DataQualityTier.DEVELOPMENT_FREE,
        effective_date_timezone=effective_date_timezone,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        identities=identities.values(),
        classifications=classifications.values(),
        memberships=memberships,
        fundamentals=fundamentals.values(),
        market_caps=caps.values(),
        corporate_actions=(),
    )
    dataset = PointInTimeDataset(
        manifest=PointInTimeDataManifest(
            provider="factor-test-security-master-provider",
            dataset_id="factor-test-security-master-v1",
            feed="verified-test-feed",
            schema_version="1",
            quality_tier=DataQualityTier.DEVELOPMENT_FREE,
            effective_date_timezone=effective_date_timezone,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            content_hash=digest,
        ),
        identities=identities.values(),
        classifications=classifications.values(),
        memberships=memberships,
        fundamentals=fundamentals.values(),
        market_caps=caps.values(),
    )
    return dataset.snapshot(as_of, ("research",))


def research_master(as_of: datetime, *security_ids: str, timezone_name: str = "UTC"):
    return security_master(
        as_of,
        {},
        {security_id: "Research" for security_id in security_ids},
        {},
        effective_date_timezone=timezone_name,
    )


RETURN_VERIFIER = "factor-test-return-verifier"
RETURN_VERIFIER_KEY = b"factor-test-return-verifier-key"
RETURN_CALENDAR_ISSUER = "factor-test-calendar-issuer"
RETURN_CALENDAR_KEY = b"factor-test-calendar-issuer-key"


def trusted_returns(
    snapshot: SecurityMasterSnapshot,
    rows: tuple[MonthlyReturnObservation, ...],
    benchmark: str = "BENCH",
):
    rows = tuple(row for row in rows if row.available_at <= snapshot.as_of)
    if not any(row.security_id == benchmark for row in rows):
        rows = rows + tuple(
            MonthlyReturnObservation(benchmark, period, 0.0, at(period))
            for period in sorted({row.month_end for row in rows})
        )
    grouped: dict[str, dict[date, list[MonthlyReturnObservation]]] = {}
    for row in rows:
        grouped.setdefault(row.security_id, {}).setdefault(row.month_end, []).append(row)
    index_rows = []
    for security_id, by_month in grouped.items():
        months = sorted(by_month)
        base_month = month_end(*shift_month(months[0].year, months[0].month, -1))
        base_available = at(base_month)
        index_rows.append(
            MonthlyTotalReturnIndexObservation(security_id, base_month, 100.0, base_available)
        )
        canonical_levels = {base_month: 100.0}
        for current_month in months:
            previous_month = month_end(
                *shift_month(current_month.year, current_month.month, -1)
            )
            previous_level = canonical_levels.get(previous_month)
            if previous_level is None:
                raise ValueError("return rows must be contiguous before signing")
            revisions = sorted(by_month[current_month], key=lambda row: row.available_at)
            for revision in revisions:
                index_rows.append(
                    MonthlyTotalReturnIndexObservation(
                        security_id,
                        current_month,
                        previous_level * (1.0 + revision.total_return),
                        revision.available_at,
                    )
                )
            canonical_levels[current_month] = previous_level * (
                1.0 + revisions[0].total_return
            )

    index_rows = tuple(index_rows)
    adjustment_vintage_at = max(row.available_at for row in index_rows)
    periods = tuple(sorted({row.month_end for row in index_rows}))
    calendar = sign_trading_calendar(
        exchange="XNYS",
        effective_date_timezone=snapshot.effective_date_timezone,
        issuer=RETURN_CALENDAR_ISSUER,
        issuer_version="1",
        generated_at=min(row.available_at for row in index_rows),
        coverage_start=periods[0],
        coverage_end=periods[-1],
        missing_intervals=(),
        sessions=tuple(TradingSession(day, at(day)) for day in periods),
        signing_key=RETURN_CALENDAR_KEY,
    )
    digest = canonical_total_return_dataset_hash(
        provider="factor-test-total-return-provider",
        dataset_id="factor-test-total-return-v1",
        feed="verified-test-feed",
        schema_version="1",
        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        effective_date_timezone=snapshot.effective_date_timezone,
        adjustment_vintage_at=adjustment_vintage_at,
        coverage_start=periods[0],
        coverage_end=periods[-1],
        security_master_dataset_hash=snapshot.source_dataset_hash,
        reference_security_ids=(benchmark,),
        observations=index_rows,
    )
    generated_at = max(row.available_at for row in index_rows) + timedelta(days=1)
    attestation = sign_point_in_time_coverage(
        verifier=RETURN_VERIFIER,
        verifier_version="1",
        generated_at=generated_at,
        provider="factor-test-total-return-provider",
        dataset_id="factor-test-total-return-v1",
        dataset_hash=digest,
        session_calendar_hash=calendar.content_hash,
        related_dataset_hashes={"security_master": snapshot.source_dataset_hash},
        covered_universes=snapshot.universes,
        session_security_counts={
            period: len({row.security_id for row in index_rows if row.month_end == period})
            for period in periods
        },
        source_artifact_hashes=("e" * 64,),
        record_counts={"total_return_index": len(index_rows)},
        assertions={name: True for name in RETURN_ADMISSION_REQUIRED_COMPONENTS},
        missing_sessions=(),
        missing_intervals=(),
        signing_key=RETURN_VERIFIER_KEY,
    )
    dataset = PointInTimeTotalReturnDataset(
        manifest=PointInTimeTotalReturnManifest(
            provider="factor-test-total-return-provider",
            dataset_id="factor-test-total-return-v1",
            feed="verified-test-feed",
            schema_version="1",
            quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
            effective_date_timezone=snapshot.effective_date_timezone,
            adjustment_vintage_at=adjustment_vintage_at,
            coverage_start=periods[0],
            coverage_end=periods[-1],
            security_master_dataset_hash=snapshot.source_dataset_hash,
            reference_security_ids=(benchmark,),
            content_hash=digest,
            coverage_attestation=attestation,
        ),
        observations=index_rows,
    )
    return dataset.snapshot(
        security_master=snapshot,
        universe=snapshot.universes[0],
        benchmark_security_id=benchmark,
        trading_calendar=calendar,
        trusted_calendar_keys={RETURN_CALENDAR_ISSUER: RETURN_CALENDAR_KEY},
        trusted_verifier_keys={RETURN_VERIFIER: RETURN_VERIFIER_KEY},
    )


def raw_sleeve(
    snapshot: SecurityMasterSnapshot,
    scores: dict[str, float],
    missing: tuple[str, ...] = (),
) -> FactorSleeve:
    return FactorSleeve(
        name="test",
        observed_at=snapshot.as_of,
        effective_date=snapshot.effective_date,
        effective_date_timezone=snapshot.effective_date_timezone,
        security_master_hash=canonical_security_master_snapshot_hash(snapshot),
        scores=scores,
        missing_security_ids=missing,
    )


class LowEntropyFactorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.as_of = datetime(2025, 4, 15, 12, tzinfo=UTC)
        benchmark_values = [
            (-0.025, -0.010, 0.005, 0.020, 0.030, -0.015)[index % 6]
            for index in range(51)
        ]
        alpha_values = [
            1.4 * value
            + 0.002 * ((index % 3) - 1)
            + (0.012 if index >= 39 else 0.0)
            for index, value in enumerate(benchmark_values)
        ]
        self.base_returns = (
            return_history("BENCH", benchmark_values)
            + return_history("ALPHA", alpha_values)
        )
        self.master = research_master(self.as_of, "ALPHA")
        self.returns = trusted_returns(self.master, self.base_returns)

    def test_monthly_inputs_are_immutable_and_total_return_index_is_explicit(self):
        day = month_end(2025, 1)
        row = MonthlyReturnObservation("sec-1", day, 0.02, at(day))
        index = MonthlyTotalReturnIndexObservation("sec-1", day, 101.0, at(day))

        with self.assertRaises(FrozenInstanceError):
            row.total_return = 0.50
        self.assertEqual(index.index_level, 101.0)
        with self.assertRaisesRegex(ValueError, "security_id"):
            MonthlyReturnObservation(" ", day, 0.02, at(day))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            MonthlyReturnObservation("sec-1", day, 0.02, datetime(2025, 1, 31))
        with self.assertRaisesRegex(ValueError, "finite"):
            MonthlyReturnObservation("sec-1", day, True, at(day))
        with self.assertRaisesRegex(ValueError, "at least -1"):
            MonthlyReturnObservation("sec-1", day, -1.01, at(day))

    def test_factor_sleeve_rejects_normalized_security_id_collision(self):
        with self.assertRaisesRegex(ValueError, "duplicate normalized"):
            FactorSleeve(
                "collision",
                self.as_of,
                self.master.effective_date,
                "UTC",
                canonical_security_master_snapshot_hash(self.master),
                {"SEC": 1.0, " SEC ": 2.0},
            )

        with self.assertRaisesRegex(ValueError, "effective_date"):
            FactorSleeve(
                "wrong-clock",
                self.as_of,
                date(2025, 4, 14),
                "UTC",
                canonical_security_master_snapshot_hash(self.master),
                {"SEC": 1.0},
            )

        with self.assertRaisesRegex(ValueError, "verified provenance"):
            FactorSleeve(
                "forged",
                self.as_of,
                self.master.effective_date,
                "UTC",
                canonical_security_master_snapshot_hash(self.master),
                {"SEC": 1.0},
                provenance_verified=True,
            )

        with self.assertRaisesRegex(ValueError, "complete active-universe"):
            raw_sleeve(self.master, {}, ("ALPHA",)).require_final_admission()

        with self.assertRaisesRegex(ValueError, "non-empty factor scores"):
            FactorSleeve(
                "empty",
                self.as_of,
                self.master.effective_date,
                "UTC",
                canonical_security_master_snapshot_hash(self.master),
                {},
            ).require_final_admission()

    def test_non_universe_ids_cannot_change_residual_momentum(self):
        baseline = residual_momentum_sleeve(
            self.returns,
            snapshot=self.master,
            window_months=6,
        )
        with_future = residual_momentum_sleeve(
            trusted_returns(
                self.master,
                self.base_returns
                + return_history("DELISTED", [0.01] * 51),
            ),
            snapshot=self.master,
            window_months=6,
        )

        self.assertEqual(with_future.scores, baseline.scores)
        self.assertEqual(with_future.missing_security_ids, baseline.missing_security_ids)
        self.assertEqual(set(with_future.scores) | set(with_future.missing_security_ids), {"ALPHA"})

    def test_residual_momentum_skips_the_most_recent_complete_month(self):
        march = month_end(2025, 3)
        changed = tuple(
            MonthlyReturnObservation(
                row.security_id,
                row.month_end,
                5.0 if row.security_id == "ALPHA" and row.month_end == march else row.total_return,
                row.available_at,
            )
            for row in self.base_returns
        )

        original = residual_momentum_sleeve(
            self.returns, self.master, window_months=6
        )
        modified = residual_momentum_sleeve(
            trusted_returns(self.master, changed), self.master, window_months=6
        )

        self.assertEqual(modified.scores, original.scores)
        self.assertEqual(modified.missing_security_ids, original.missing_security_ids)

    def test_market_model_uses_long_estimation_window_and_is_scale_invariant(self):
        tiny_benchmark = [
            1e-10 * value for value in (-3, -2, -1, 1, 2, 3, -2, 2, -1, 1, -3, 3)
        ]
        beta_only = [2.0 * value for value in tiny_benchmark]
        instant = datetime(2025, 1, 15, tzinfo=UTC)
        rows = return_history("BENCH", tiny_benchmark, start_year=2024) + return_history(
            "BETA", beta_only, start_year=2024
        )

        master = research_master(instant, "BETA")
        sleeve = residual_momentum_sleeve(
            trusted_returns(master, rows),
            master,
            window_months=3,
            skip_recent_months=0,
            estimation_window_months=12,
        )

        self.assertAlmostEqual(sleeve.scores["BETA"], 0.0, places=12)

    def test_factor_provenance_hash_binds_every_strategy_parameter(self):
        thirty_six = residual_momentum_sleeve(
            self.returns,
            self.master,
            window_months=6,
            estimation_window_months=36,
        )
        forty_eight = residual_momentum_sleeve(
            self.returns,
            self.master,
            window_months=6,
            estimation_window_months=48,
        )

        self.assertIsNotNone(thirty_six.factor_config_hash)
        self.assertNotEqual(
            thirty_six.factor_config_hash,
            forty_eight.factor_config_hash,
        )

    def test_recent_idiosyncratic_strength_is_positive_after_fixed_market_model(self):
        sleeve = residual_momentum_sleeve(
            self.returns,
            self.master,
            window_months=6,
            estimation_window_months=36,
        )

        self.assertGreater(sleeve.scores["ALPHA"], 0.0)
        self.assertTrue(math.isfinite(sleeve.scores["ALPHA"]))
        with self.assertRaisesRegex(ValueError, "unchanged factor content"):
            replace(sleeve, scores={"ALPHA": 999.0})

    def test_degenerate_benchmark_and_incomplete_history_fail_closed(self):
        constant_benchmark = return_history("BENCH", [0.01] * 12, start_year=2024)
        asset = return_history("ASSET", [0.01 * index for index in range(12)], start_year=2024)
        instant = datetime(2025, 1, 15, tzinfo=UTC)
        master = research_master(instant, "ASSET", "NEW_LISTING")

        sleeve = residual_momentum_sleeve(
            trusted_returns(master, constant_benchmark + asset),
            master,
            window_months=3,
            skip_recent_months=0,
            estimation_window_months=12,
        )

        self.assertEqual(sleeve.scores, {})
        self.assertEqual(sleeve.missing_security_ids, ("ASSET", "NEW_LISTING"))

        near_constant_benchmark = return_history(
            "BENCH",
            [0.01 if index % 2 == 0 else 0.0100000000000002 for index in range(12)],
            start_year=2024,
        )
        near_constant = residual_momentum_sleeve(
            trusted_returns(master, near_constant_benchmark + asset),
            master,
            window_months=3,
            skip_recent_months=0,
            estimation_window_months=12,
        )
        self.assertEqual(near_constant.scores, {})
        self.assertEqual(
            near_constant.missing_security_ids,
            ("ASSET", "NEW_LISTING"),
        )

        extreme_rows = return_history(
            "BENCH", [1e-320 * ((index % 3) - 1) for index in range(12)], start_year=2024
        ) + return_history(
            "ASSET", [0.1 * ((index % 3) - 1) for index in range(12)], start_year=2024
        )
        extreme = residual_momentum_sleeve(
            trusted_returns(research_master(instant, "ASSET"), extreme_rows),
            research_master(instant, "ASSET"),
            window_months=3,
            skip_recent_months=0,
            estimation_window_months=12,
        )
        self.assertEqual(extreme.scores, {})
        self.assertEqual(extreme.missing_security_ids, ("ASSET",))

    def test_residual_builder_validates_hypothesis_family_before_execution(self):
        sleeves = build_residual_momentum_sleeves(
            self.returns,
            snapshot=self.master,
        )

        self.assertEqual(
            tuple(sleeve.name for sleeve in sleeves),
            (
                "residual_momentum_12_1",
                "residual_momentum_6_1",
                "residual_momentum_3_1",
            ),
        )
        self.assertEqual(len({id(sleeve.scores) for sleeve in sleeves}), 3)
        for windows in ((), (3, 3), (2, 3)):
            with self.subTest(windows=windows), self.assertRaises(ValueError):
                build_residual_momentum_sleeves(
                    self.returns,
                    snapshot=self.master,
                    windows=windows,
                )
        with self.assertRaises(ValueError):
            build_residual_momentum_sleeves(
                self.returns,
                snapshot=self.master,
                windows=(),
                skip_recent_months=-1,
            )

    def test_reversal_uses_snapshot_universe_and_availability_boundary(self):
        march = month_end(2025, 3)
        visible_at = datetime(2025, 4, 15, 13, tzinfo=UTC)
        rows = (
            MonthlyReturnObservation("VISIBLE", march, 0.20, at(march)),
            MonthlyReturnObservation("LATE", march, -0.30, visible_at),
            MonthlyReturnObservation("DELISTED", march, 0.90, at(march)),
        )

        before_master = research_master(self.as_of, "VISIBLE", "LATE", "NEW_LISTING")
        boundary_master = research_master(visible_at, "VISIBLE", "LATE", "NEW_LISTING")
        before = short_term_reversal_sleeve(
            trusted_returns(before_master, rows), before_master
        )
        boundary = short_term_reversal_sleeve(
            trusted_returns(boundary_master, rows), boundary_master
        )

        self.assertAlmostEqual(before.scores["VISIBLE"], -0.20)
        self.assertEqual(before.missing_security_ids, ("LATE", "NEW_LISTING"))
        self.assertAlmostEqual(boundary.scores["LATE"], 0.30)
        self.assertNotIn("DELISTED", boundary.scores)

    def test_factor_month_boundary_is_invariant_to_datetime_representation(self):
        instant = datetime(2025, 5, 1, 0, 30, tzinfo=UTC)
        ny_time = instant.astimezone(ZoneInfo("America/New_York"))
        utc_snapshot = research_master(
            instant, "ASSET", timezone_name="America/New_York"
        )
        ny_snapshot = research_master(
            ny_time, "ASSET", timezone_name="America/New_York"
        )
        values = [0.01 * ((index % 5) - 2) for index in range(18)]
        rows = return_history("BENCH", values, start_year=2023, start_month=10) + return_history(
            "ASSET", [1.2 * value + (0.02 if index >= 15 else 0.0) for index, value in enumerate(values)],
            start_year=2023,
            start_month=10,
        )

        self.assertEqual(
            short_term_reversal_sleeve(trusted_returns(utc_snapshot, rows), utc_snapshot),
            short_term_reversal_sleeve(trusted_returns(ny_snapshot, rows), ny_snapshot),
        )
        self.assertEqual(
            residual_momentum_sleeve(
                trusted_returns(utc_snapshot, rows),
                utc_snapshot,
                window_months=3,
                estimation_window_months=12,
            ),
            residual_momentum_sleeve(
                trusted_returns(ny_snapshot, rows),
                ny_snapshot,
                window_months=3,
                estimation_window_months=12,
            ),
        )

    def test_naked_returns_and_conflicting_duplicate_vintage_are_rejected(self):
        march = month_end(2025, 3)
        first = MonthlyReturnObservation("ALPHA", march, 0.10, at(march))
        conflict = MonthlyReturnObservation("ALPHA", march, 0.90, at(march))

        with self.assertRaisesRegex(TypeError, "verified"):
            short_term_reversal_sleeve((first,), self.master)
        for rows in ((first, conflict), (conflict, first)):
            with self.subTest(rows=rows), self.assertRaisesRegex(
                ValueError, "duplicate total-return index period"
            ):
                trusted_returns(self.master, rows)

    def test_quality_and_eps_revision_use_complete_visible_fundamentals(self):
        observations = {
            "complete": FundamentalObservation(
                "complete",
                date(2024, 12, 31),
                self.as_of,
                0,
                {
                    "return_on_equity": 0.20,
                    "gross_margin": 0.50,
                    "debt_to_assets": 0.25,
                    "accrual_ratio": 0.05,
                    "eps_estimate_current": 2.20,
                    "eps_estimate_previous": 2.00,
                },
            ),
            "partial": FundamentalObservation(
                "partial",
                date(2024, 12, 31),
                self.as_of,
                0,
                {
                    "return_on_equity": 0.10,
                    "gross_margin": 0.30,
                    "debt_to_assets": 0.20,
                    "eps_estimate_current": 1.50,
                },
            ),
        }
        master = security_master(
            self.as_of,
            observations,
            sectors={"complete": "Tech", "partial": "Tech"},
            market_caps={"complete": 100.0, "partial": 200.0},
        )

        quality = quality_sleeve(master)
        revision = eps_revision_sleeve(master)

        self.assertAlmostEqual(quality.scores["complete"], (0.20 + 0.50 - 0.25 - 0.05) / 4)
        self.assertEqual(quality.missing_security_ids, ("partial",))
        self.assertAlmostEqual(revision.scores["complete"], 0.10)
        self.assertEqual(revision.missing_security_ids, ("partial",))

    def test_eps_revision_handles_negative_and_zero_prior_estimates(self):
        observations = {
            "improving": FundamentalObservation(
                "improving", date(2024, 12, 31), self.as_of, 0,
                {"eps_estimate_current": -1.0, "eps_estimate_previous": -2.0},
            ),
            "zero": FundamentalObservation(
                "zero", date(2024, 12, 31), self.as_of, 0,
                {"eps_estimate_current": 1.0, "eps_estimate_previous": 0.0},
            ),
        }
        master = security_master(
            self.as_of,
            observations,
            sectors={"improving": "Tech", "zero": "Tech"},
            market_caps={},
        )

        sleeve = eps_revision_sleeve(master)

        self.assertEqual(sleeve.scores["improving"], 0.5)
        self.assertEqual(sleeve.missing_security_ids, ("zero",))

    def test_extreme_fundamental_arithmetic_is_isolated_per_security(self):
        observations = {
            "extreme": FundamentalObservation(
                "extreme",
                date(2024, 12, 31),
                self.as_of,
                0,
                {
                    "return_on_equity": 1e308,
                    "gross_margin": 1e308,
                    "debt_to_assets": -1e308,
                    "accrual_ratio": -1e308,
                    "eps_estimate_current": 1e308,
                    "eps_estimate_previous": -1e308,
                },
            ),
            "normal": FundamentalObservation(
                "normal",
                date(2024, 12, 31),
                self.as_of,
                0,
                {
                    "return_on_equity": 0.2,
                    "gross_margin": 0.4,
                    "debt_to_assets": 0.1,
                    "accrual_ratio": 0.05,
                    "eps_estimate_current": 2.2,
                    "eps_estimate_previous": 2.0,
                },
            ),
        }
        master = security_master(
            self.as_of,
            observations,
            sectors={"extreme": "Tech", "normal": "Tech"},
            market_caps={},
        )

        quality = quality_sleeve(master)
        revision = eps_revision_sleeve(master)

        self.assertIn("normal", quality.scores)
        self.assertIn("normal", revision.scores)
        self.assertTrue(all(math.isfinite(value) for value in quality.scores.values()))
        self.assertTrue(all(math.isfinite(value) for value in revision.scores.values()))

    def test_huge_integer_fundamentals_are_isolated_per_security(self):
        observations = {
            "huge": FundamentalObservation(
                "huge",
                date(2024, 12, 31),
                self.as_of,
                0,
                {
                    "return_on_equity": 10**400,
                    "gross_margin": 10**400,
                    "debt_to_assets": -(10**400),
                    "accrual_ratio": -(10**400),
                    "eps_estimate_current": 10**400,
                    "eps_estimate_previous": -(10**400),
                },
            ),
            "normal": FundamentalObservation(
                "normal",
                date(2024, 12, 31),
                self.as_of,
                0,
                {
                    "return_on_equity": 0.2,
                    "gross_margin": 0.4,
                    "debt_to_assets": 0.1,
                    "accrual_ratio": 0.05,
                    "eps_estimate_current": 2.2,
                    "eps_estimate_previous": 2.0,
                },
            ),
        }
        master = security_master(
            self.as_of,
            observations,
            sectors={"huge": "Tech", "normal": "Tech"},
            market_caps={},
        )

        quality = quality_sleeve(master)
        revision = eps_revision_sleeve(master)

        self.assertEqual(set(quality.scores), {"normal"})
        self.assertEqual(set(revision.scores), {"normal"})
        self.assertEqual(quality.missing_security_ids, ("huge",))
        self.assertEqual(revision.missing_security_ids, ("huge",))

    def test_winsorized_neutralization_removes_sector_and_log_size_exposure(self):
        sectors = {
            "t1": "Tech", "t2": "Tech", "t3": "Tech", "t4": "Tech",
            "f1": "Finance", "f2": "Finance", "f3": "Finance", "f4": "Finance",
        }
        caps = {
            security_id: float(10 ** (index + 2))
            for index, security_id in enumerate(sorted(sectors))
        }
        residual_pattern = {
            "t1": -1.0, "t2": 1.0, "t3": -1.0, "t4": 1.0,
            "f1": 1.0, "f2": -1.0, "f3": 1.0, "f4": -1.0,
        }
        raw_scores = {
            security_id: (5.0 if sector == "Tech" else -3.0)
            + 0.7 * math.log(caps[security_id])
            + residual_pattern[security_id]
            for security_id, sector in sectors.items()
        }
        master = security_master(self.as_of, {}, sectors, caps)

        neutral = neutralize_sleeve(
            raw_sleeve(master, raw_scores), master, lower_quantile=0.0, upper_quantile=1.0
        )

        for sector in set(sectors.values()):
            self.assertAlmostEqual(
                sum(neutral.scores[sid] for sid in sectors if sectors[sid] == sector),
                0.0,
                places=9,
            )
        self.assertAlmostEqual(
            sum(neutral.scores[sid] * math.log(caps[sid]) for sid in sectors),
            0.0,
            places=9,
        )
        values = tuple(neutral.scores.values())
        self.assertTrue(all(math.isfinite(value) for value in values))
        self.assertAlmostEqual(sum(value * value for value in values) / len(values), 1.0)

    def test_neutralization_requires_same_snapshot_time_and_covers_active_universe(self):
        master = security_master(
            self.as_of,
            {},
            {"A": "Tech", "B": "Tech", "C": "Tech"},
            {"A": 100.0, "B": 200.0},
        )
        incomplete = raw_sleeve(master, {"A": 1.0, "B": 2.0})
        future = security_master(
            datetime(2025, 4, 16, 12, tzinfo=UTC),
            {},
            {"A": "Tech", "B": "Tech", "C": "Tech"},
            {"A": 100.0, "B": 200.0, "C": 300.0},
        )

        with self.assertRaisesRegex(ValueError, "same point-in-time"):
            neutralize_sleeve(incomplete, future)

        altered_same_time = security_master(
            self.as_of,
            {},
            {"A": "Finance", "B": "Tech", "C": "Tech"},
            {"A": 150.0, "B": 200.0, "C": 300.0},
        )
        with self.assertRaisesRegex(ValueError, "snapshot provenance"):
            neutralize_sleeve(incomplete, altered_same_time)

        neutral = neutralize_sleeve(incomplete, master)
        self.assertEqual(neutral.scores, {})
        self.assertEqual(neutral.missing_security_ids, ("A", "B", "C"))

    def test_neutralization_without_residual_degrees_of_freedom_fails_closed(self):
        master = security_master(
            self.as_of,
            {},
            {"A": "Tech", "B": "Tech"},
            {"A": 100.0, "B": 200.0},
        )

        neutral = neutralize_sleeve(raw_sleeve(master, {"A": 1.0, "B": 2.0}), master)

        self.assertEqual(neutral.scores, {})
        self.assertEqual(neutral.missing_security_ids, ("A", "B"))

    def test_neutralization_does_not_amplify_floating_point_residue(self):
        sectors = {"A": "Tech", "B": "Tech", "C": "Finance", "D": "Finance"}
        caps = {"A": 100.0, "B": 1_000.0, "C": 200.0, "D": 2_000.0}
        master = security_master(self.as_of, {}, sectors, caps)
        scores = {
            security_id: (3.0 if sectors[security_id] == "Tech" else -2.0)
            + 0.7 * math.log(caps[security_id])
            for security_id in sectors
        }

        neutral = neutralize_sleeve(
            raw_sleeve(master, scores),
            master,
        )

        self.assertEqual(neutral.scores, {key: 0.0 for key in sectors})


if __name__ == "__main__":
    unittest.main()
