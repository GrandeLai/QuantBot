import sys
import unittest
from copy import copy
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data.point_in_time import (
    ADMISSION_REQUIRED_COMPONENTS,
    CorporateAction,
    CorporateActionType,
    DataQualityTier,
    DailyPriceObservation,
    FundamentalObservation,
    MarketCapObservation,
    PointInTimeCoverageAttestation,
    PointInTimeDataManifest,
    PointInTimeDataset,
    PointInTimePriceDataset,
    PointInTimePriceManifest,
    PointInTimeTradingCalendar,
    SecurityMasterSnapshot,
    SecurityClassification,
    SecurityIdentity,
    UniverseMembership,
    canonical_dataset_hash,
    canonical_price_dataset_hash,
    sign_point_in_time_coverage,
    sign_trading_calendar,
    TradingSession,
)
from quantbot.trading.domain import PointInTimeSnapshot as TradingPointInTimeSnapshot
from quantbot.universe import PointInTimeUniverseBuilder, TradingSuspension


UTC = timezone.utc
TRUSTED_VERIFIER = "independent-test-validator"
TRUSTED_KEY = b"point-in-time-test-signing-key"
CALENDAR_ISSUER = "independent-calendar-authority"
CALENDAR_KEY = b"point-in-time-calendar-signing-key"
PROVIDER = "test-pit-provider"
DATASET_ID = "test-security-master-v1"


def known_on(value: date, hour: int = 12) -> datetime:
    return datetime(value.year, value.month, value.day, hour, tzinfo=UTC)


TEST_TRADING_CALENDAR = sign_trading_calendar(
    exchange="XNYS",
    effective_date_timezone="America/New_York",
    issuer=CALENDAR_ISSUER,
    issuer_version="1",
    generated_at=known_on(date(2023, 12, 31)),
    coverage_start=date(2024, 1, 1),
    coverage_end=date(2024, 1, 7),
    missing_intervals=(),
    sessions=tuple(
        TradingSession(day, known_on(day, 21))
        for day in (
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 4),
            date(2024, 1, 5),
        )
    ),
    signing_key=CALENDAR_KEY,
)


class PointInTimeDataTest(unittest.TestCase):
    def _records(self):
        identities = (
            SecurityIdentity(
                security_id="sec-old",
                listed_on=date(2010, 1, 1),
                delisted_on=date(2025, 1, 1),
                available_at=known_on(date(2010, 1, 1)),
                delisted_on_available_at=known_on(date(2025, 1, 1)),
            ),
        )
        classifications = (
            SecurityClassification(
                security_id="sec-old",
                ticker="OLD",
                exchange="NYSE",
                sector="Industrials",
                effective_from=date(2010, 1, 1),
                effective_to=date(2025, 1, 1),
                available_at=known_on(date(2010, 1, 1)),
                effective_to_available_at=known_on(date(2025, 1, 1)),
            ),
        )
        memberships = (
            UniverseMembership(
                universe="research",
                security_id="sec-old",
                effective_from=date(2010, 1, 1),
                effective_to=date(2025, 1, 1),
                available_at=known_on(date(2010, 1, 1)),
                source="verified-index-history",
                effective_to_available_at=known_on(date(2025, 1, 1)),
            ),
        )
        return identities, classifications, memberships

    def _dataset(
        self,
        *,
        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
        identities=None,
        classifications=None,
        memberships=None,
        fundamentals=(),
        market_caps=None,
        corporate_actions=(),
        coverage_start=date(2010, 1, 1),
        coverage_end=date(2025, 12, 31),
        covered_universes=("research",),
    ):
        defaults = self._records()
        identities = defaults[0] if identities is None else tuple(identities)
        classifications = defaults[1] if classifications is None else tuple(classifications)
        memberships = defaults[2] if memberships is None else tuple(memberships)
        market_caps = (
            tuple(
                MarketCapObservation(
                    security_id=identity.security_id,
                    effective_on=identity.listed_on,
                    available_at=identity.available_at,
                    market_cap=1_000_000_000,
                )
                for identity in identities
            )
            if market_caps is None
            else tuple(market_caps)
        )
        digest = canonical_dataset_hash(
            provider=PROVIDER,
            dataset_id=DATASET_ID,
            feed="test-pit-feed",
            schema_version="1",
            quality_tier=quality_tier,
            effective_date_timezone="America/New_York",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            fundamentals=fundamentals,
            market_caps=market_caps,
            corporate_actions=corporate_actions,
        )
        coverage_sessions = tuple(
            session.session_date
            for session in TEST_TRADING_CALENDAR.sessions
            if coverage_start <= session.session_date <= coverage_end
        )
        record_counts = {
            "identities": len(identities),
            "classifications": len(classifications),
            "memberships": len(memberships),
            "fundamentals": len(fundamentals),
            "market_caps": len(market_caps),
            "corporate_actions": len(corporate_actions),
        }
        attestation = None
        if quality_tier is DataQualityTier.POINT_IN_TIME_VERIFIED:
            attestation = sign_point_in_time_coverage(
                verifier=TRUSTED_VERIFIER,
                verifier_version="1",
                generated_at=known_on(date(2026, 1, 2)),
                provider=PROVIDER,
                dataset_id=DATASET_ID,
                dataset_hash=digest,
                session_calendar_hash=TEST_TRADING_CALENDAR.content_hash,
                related_dataset_hashes={},
                covered_universes=covered_universes,
                session_security_counts={day: max(1, len(identities)) for day in coverage_sessions},
                source_artifact_hashes=("a" * 64,),
                record_counts=record_counts,
                assertions={name: True for name in ADMISSION_REQUIRED_COMPONENTS},
                missing_sessions=(),
                missing_intervals=(),
                signing_key=TRUSTED_KEY,
            )
        manifest = PointInTimeDataManifest(
            provider=PROVIDER,
            dataset_id=DATASET_ID,
            feed="test-pit-feed",
            schema_version="1",
            quality_tier=quality_tier,
            effective_date_timezone="America/New_York",
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            content_hash=digest,
            coverage_attestation=attestation,
        )
        return PointInTimeDataset(
            manifest=manifest,
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            fundamentals=fundamentals,
            market_caps=market_caps,
            corporate_actions=corporate_actions,
        )

    def _price_dataset(
        self,
        rows,
        quality_tier=DataQualityTier.DEVELOPMENT_FREE,
        security_master_hash=None,
        covered_universes=("research",),
    ):
        observations = tuple(
            row if isinstance(row, DailyPriceObservation) else DailyPriceObservation(**row)
            for row in rows
        )
        digest = canonical_price_dataset_hash(
            provider=PROVIDER,
            dataset_id="test-daily-prices-v1",
            feed="test-raw-daily-bars",
            schema_version="1",
            quality_tier=quality_tier,
            effective_date_timezone="America/New_York",
            observations=observations,
        )
        session_security_counts = {
            session: len(
                {item.security_id for item in observations if item.trading_date == session}
            )
            for session in sorted({item.trading_date for item in observations})
        }
        attestation = None
        if quality_tier is DataQualityTier.POINT_IN_TIME_VERIFIED:
            attestation = sign_point_in_time_coverage(
                verifier=TRUSTED_VERIFIER,
                verifier_version="1",
                generated_at=known_on(date(2026, 1, 2)),
                provider=PROVIDER,
                dataset_id="test-daily-prices-v1",
                dataset_hash=digest,
                session_calendar_hash=TEST_TRADING_CALENDAR.content_hash,
                related_dataset_hashes={
                    "security_master": security_master_hash or self._dataset().manifest.content_hash
                },
                covered_universes=covered_universes,
                session_security_counts=session_security_counts,
                source_artifact_hashes=("b" * 64,),
                record_counts={"prices": len(observations)},
                assertions={
                    "available_at": True,
                    "raw_close": True,
                    "raw_volume": True,
                    "session_coverage": True,
                    "stable_security_ids": True,
                },
                missing_sessions=(),
                missing_intervals=(),
                signing_key=TRUSTED_KEY,
            )
        manifest = PointInTimePriceManifest(
            provider=PROVIDER,
            dataset_id="test-daily-prices-v1",
            feed="test-raw-daily-bars",
            schema_version="1",
            quality_tier=quality_tier,
            effective_date_timezone="America/New_York",
            content_hash=digest,
            coverage_attestation=attestation,
        )
        return PointInTimePriceDataset(manifest=manifest, observations=observations)

    def _build_universe(
        self,
        builder,
        as_of,
        prices,
        trading_calendar=TEST_TRADING_CALENDAR,
    ):
        return builder.build(
            as_of,
            prices,
            trading_calendar=trading_calendar,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
        )

    def test_membership_effective_early_but_available_late_is_not_backfilled(self):
        identities, classifications, _ = self._records()
        late_membership = UniverseMembership(
            universe="research",
            security_id="sec-old",
            effective_from=date(2010, 1, 1),
            effective_to=date(2025, 1, 1),
            available_at=known_on(date(2020, 1, 3)),
            source="late-vendor-file",
            effective_to_available_at=known_on(date(2025, 1, 1)),
        )
        dataset = self._dataset(
            identities=identities,
            classifications=classifications,
            memberships=(late_membership,),
        )

        before = dataset.snapshot(known_on(date(2020, 1, 3), hour=11), ("research",))
        boundary = dataset.snapshot(known_on(date(2020, 1, 3), hour=12), ("research",))

        self.assertEqual(before.security_ids, ())
        self.assertEqual(boundary.security_ids, ("sec-old",))

    def test_active_known_membership_missing_available_classification_fails_closed(self):
        identities, _, memberships = self._records()
        late_classification = SecurityClassification(
            security_id="sec-old",
            ticker="OLD",
            exchange="NYSE",
            sector="Industrials",
            effective_from=date(2010, 1, 1),
            effective_to=date(2025, 1, 1),
            available_at=known_on(date(2020, 1, 4)),
            effective_to_available_at=known_on(date(2025, 1, 1)),
        )
        dataset = self._dataset(
            identities=identities,
            classifications=(late_classification,),
            memberships=memberships,
        )

        with self.assertRaisesRegex(ValueError, "classification"):
            dataset.snapshot(known_on(date(2020, 1, 3)), ("research",))

    def test_later_delisted_security_remains_in_history_then_disappears(self):
        dataset = self._dataset()

        historical = dataset.snapshot(known_on(date(2024, 12, 31)), ("research",))
        after_delisting = dataset.snapshot(known_on(date(2025, 1, 1)), ("research",))

        self.assertEqual(historical.security_ids, ("sec-old",))
        self.assertIsNone(historical.identity_by_security_id["sec-old"].delisted_on)
        self.assertIsNone(
            historical.classification_by_security_id["sec-old"].effective_to
        )
        self.assertEqual(after_delisting.security_ids, ())

    def test_late_termination_facts_are_not_backfilled(self):
        termination_known = known_on(date(2025, 1, 3))
        identity = SecurityIdentity(
            "sec-old",
            date(2010, 1, 1),
            date(2025, 1, 1),
            known_on(date(2010, 1, 1)),
            termination_known,
        )
        classification = SecurityClassification(
            "sec-old",
            "OLD",
            "NYSE",
            "Industrials",
            date(2010, 1, 1),
            date(2025, 1, 1),
            known_on(date(2010, 1, 1)),
            termination_known,
        )
        membership = UniverseMembership(
            "research",
            "sec-old",
            date(2010, 1, 1),
            date(2025, 1, 1),
            known_on(date(2010, 1, 1)),
            "history",
            termination_known,
        )
        dataset = self._dataset(
            identities=(identity,),
            classifications=(classification,),
            memberships=(membership,),
        )

        before_file = dataset.snapshot(known_on(date(2025, 1, 2)), ("research",))
        after_file = dataset.snapshot(termination_known, ("research",))

        self.assertEqual(before_file.security_ids, ("sec-old",))
        self.assertIsNone(before_file.identity_by_security_id["sec-old"].delisted_on)
        self.assertEqual(after_file.security_ids, ())

    def test_effective_date_uses_manifest_timezone_for_same_instant(self):
        dataset = self._dataset()
        instant_utc = datetime(2025, 1, 1, 0, 30, tzinfo=UTC)
        instant_new_york = instant_utc.astimezone(ZoneInfo("America/New_York"))

        utc_view = dataset.snapshot(instant_utc, ("research",))
        local_view = dataset.snapshot(instant_new_york, ("research",))

        self.assertEqual(utc_view.effective_date, date(2024, 12, 31))
        self.assertEqual(utc_view.security_ids, ("sec-old",))
        self.assertEqual(utc_view, local_view)

    def test_market_cap_is_an_available_at_observation_not_static_classification(self):
        market_caps = (
            MarketCapObservation(
                "sec-old", date(2024, 1, 1), known_on(date(2024, 1, 2)), 100.0,
            ),
            MarketCapObservation(
                "sec-old", date(2024, 1, 2), known_on(date(2024, 1, 4)), 200.0,
            ),
        )
        dataset = self._dataset(market_caps=market_caps)

        before_revision = dataset.snapshot(known_on(date(2024, 1, 3)), ("research",))
        after_revision = dataset.snapshot(known_on(date(2024, 1, 4)), ("research",))

        self.assertEqual(before_revision.latest_market_caps["sec-old"].market_cap, 100.0)
        self.assertEqual(after_revision.latest_market_caps["sec-old"].market_cap, 200.0)

    def test_security_master_snapshot_composes_with_shared_trading_snapshot(self):
        observed_at = known_on(date(2024, 1, 3))
        security_master = self._dataset().snapshot(observed_at, ("research",))

        trading_snapshot = TradingPointInTimeSnapshot(
            observed_at=observed_at,
            security_master=security_master,
        )

        self.assertIsInstance(security_master, SecurityMasterSnapshot)
        self.assertIs(trading_snapshot.security_master, security_master)
        with self.assertRaisesRegex(ValueError, "future"):
            TradingPointInTimeSnapshot(
                observed_at=known_on(date(2024, 1, 2)),
                security_master=security_master,
            )

    def test_ticker_change_and_reuse_are_isolated_by_stable_security_id(self):
        identities = (
            SecurityIdentity(
                "sec-one", date(2010, 1, 1), date(2020, 1, 1),
                known_on(date(2010, 1, 1)), known_on(date(2020, 1, 1)),
            ),
            SecurityIdentity("sec-two", date(2020, 1, 1), None, known_on(date(2020, 1, 1))),
        )
        classifications = (
            SecurityClassification(
                "sec-one", "SHARED", "NYSE", "Tech", date(2010, 1, 1),
                date(2020, 1, 1), known_on(date(2010, 1, 1)), known_on(date(2020, 1, 1)),
            ),
            SecurityClassification(
                "sec-two", "SHARED", "NASDAQ", "Health", date(2020, 1, 1),
                date(2021, 1, 1), known_on(date(2020, 1, 1)), known_on(date(2020, 12, 1)),
            ),
            SecurityClassification(
                "sec-two", "NEW", "NASDAQ", "Health", date(2021, 1, 1),
                None, known_on(date(2020, 12, 1)),
            ),
        )
        memberships = (
            UniverseMembership(
                "research", "sec-one", date(2010, 1, 1), date(2020, 1, 1),
                known_on(date(2010, 1, 1)), "history", known_on(date(2020, 1, 1)),
            ),
            UniverseMembership("research", "sec-two", date(2020, 1, 1), None, known_on(date(2020, 1, 1)), "history"),
        )
        dataset = self._dataset(
            identities=identities,
            classifications=classifications,
            memberships=memberships,
        )

        old = dataset.snapshot(known_on(date(2019, 12, 31)), ("research",))
        reused = dataset.snapshot(known_on(date(2020, 6, 1)), ("research",))
        changed = dataset.snapshot(known_on(date(2021, 6, 1)), ("research",))

        self.assertEqual(old.ticker_by_security_id, {"sec-one": "SHARED"})
        self.assertEqual(reused.ticker_by_security_id, {"sec-two": "SHARED"})
        self.assertEqual(changed.ticker_by_security_id, {"sec-two": "NEW"})

    def test_future_fundamentals_and_actions_are_not_visible(self):
        fundamental = FundamentalObservation(
            security_id="sec-old",
            period_end=date(2023, 12, 31),
            available_at=known_on(date(2024, 2, 1)),
            revision=0,
            values={"eps": 2.5},
        )
        action = CorporateAction(
            action_type=CorporateActionType.SYMBOL_CHANGE,
            action_id="action-1",
            security_id="sec-old",
            effective_on=date(2024, 3, 1),
            available_at=known_on(date(2024, 2, 1)),
            new_ticker="NEW",
        )
        classifications = (
            SecurityClassification(
                "sec-old", "OLD", "NYSE", "Industrials", date(2010, 1, 1),
                date(2024, 3, 1), known_on(date(2010, 1, 1)), known_on(date(2024, 2, 1)),
            ),
            SecurityClassification(
                "sec-old", "NEW", "NYSE", "Industrials", date(2024, 3, 1),
                date(2025, 1, 1), known_on(date(2024, 2, 1)), known_on(date(2025, 1, 1)),
            ),
        )
        dataset = self._dataset(
            classifications=classifications,
            fundamentals=(fundamental,),
            corporate_actions=(action,),
        )

        before = dataset.snapshot(known_on(date(2024, 1, 31)), ("research",))
        announced = dataset.snapshot(known_on(date(2024, 2, 1)), ("research",))
        effective = dataset.snapshot(known_on(date(2024, 3, 1)), ("research",))

        self.assertEqual(before.latest_fundamentals, {})
        self.assertEqual(before.known_corporate_actions, ())
        self.assertEqual(announced.latest_fundamentals["sec-old"], fundamental)
        self.assertEqual(announced.known_corporate_actions, (action,))
        self.assertEqual(announced.effective_corporate_actions, ())
        self.assertEqual(effective.effective_corporate_actions, (action,))
        self.assertEqual(effective.ticker_by_security_id["sec-old"], "NEW")

    def test_delisting_action_remains_visible_on_effective_date(self):
        action = CorporateAction(
            action_type=CorporateActionType.DELISTING,
            action_id="delist-sec-old",
            security_id="sec-old",
            effective_on=date(2025, 1, 1),
            available_at=known_on(date(2024, 12, 31)),
        )
        dataset = self._dataset(corporate_actions=(action,))

        effective = dataset.snapshot(known_on(date(2025, 1, 1)), ("research",))
        held_security_actions = dataset.actions_as_of(
            known_on(date(2025, 1, 1)), ("sec-old",)
        )

        self.assertEqual(effective.security_ids, ())
        self.assertEqual(effective.effective_corporate_actions, (action,))
        self.assertEqual(held_security_actions.effective_actions, (action,))

    def test_corporate_action_payload_is_type_checked(self):
        with self.assertRaisesRegex(ValueError, "new_ticker"):
            CorporateAction(
                CorporateActionType.SYMBOL_CHANGE,
                "bad-symbol",
                "sec-old",
                date(2024, 1, 1),
                known_on(date(2023, 12, 1)),
            )
        with self.assertRaisesRegex(ValueError, "ratio"):
            CorporateAction(
                CorporateActionType.SPLIT,
                "bad-split",
                "sec-old",
                date(2024, 1, 1),
                known_on(date(2023, 12, 1)),
                ratio=0,
            )
        with self.assertRaisesRegex(ValueError, "cash_amount"):
            CorporateAction(
                CorporateActionType.CASH_DIVIDEND,
                "bad-dividend",
                "sec-old",
                date(2024, 1, 1),
                known_on(date(2023, 12, 1)),
                cash_amount=-1,
            )
        inconsistent = CorporateAction(
            CorporateActionType.SYMBOL_CHANGE,
            "inconsistent-symbol",
            "sec-old",
            date(2024, 1, 1),
            known_on(date(2023, 12, 1)),
            new_ticker="NOT-IN-HISTORY",
        )
        with self.assertRaisesRegex(ValueError, "classification history"):
            self._dataset(corporate_actions=(inconsistent,))

    def test_constructor_rejects_duplicate_overlap_and_cross_security_ticker_collision(self):
        identities, classifications, memberships = self._records()
        with self.assertRaisesRegex(ValueError, "duplicate.*security"):
            self._dataset(identities=identities + identities)

        overlap = UniverseMembership(
            "research", "sec-old", date(2024, 1, 1), None,
            known_on(date(2020, 1, 1)), "other",
        )
        with self.assertRaisesRegex(ValueError, "overlap"):
            self._dataset(memberships=memberships + (overlap,))

        second_identity = SecurityIdentity(
            "sec-two", date(2020, 1, 1), None, known_on(date(2020, 1, 1)),
        )
        collision = SecurityClassification(
            "sec-two", "OLD", "NASDAQ", "Health",
            date(2020, 1, 1), None, known_on(date(2020, 1, 1)),
        )
        with self.assertRaisesRegex(ValueError, "ticker"):
            self._dataset(
                identities=identities + (second_identity,),
                classifications=classifications + (collision,),
            )

        with self.assertRaisesRegex(ValueError, "delisted_on_available_at"):
            SecurityIdentity(
                "bad-end", date(2020, 1, 1), date(2021, 1, 1),
                known_on(date(2020, 1, 1)),
            )

    def test_constructor_rejects_records_outside_listing_or_without_classification_coverage(self):
        identity = SecurityIdentity(
            "sec-new", date(2020, 1, 1), None, known_on(date(2020, 1, 1))
        )
        classification = SecurityClassification(
            "sec-new", "NEW", "NYSE", "Tech", date(2020, 1, 1), None,
            known_on(date(2020, 1, 1)),
        )
        bad_membership = UniverseMembership(
            "research", "sec-new", date(2019, 1, 1), None,
            known_on(date(2019, 1, 1)), "bad-history",
        )
        with self.assertRaisesRegex(ValueError, "listing lifecycle"):
            self._dataset(
                identities=(identity,),
                classifications=(classification,),
                memberships=(bad_membership,),
            )

        uncovered_membership = UniverseMembership(
            "research", "sec-new", date(2020, 1, 1), None,
            known_on(date(2020, 1, 1)), "bad-history",
        )
        short_classification = SecurityClassification(
            "sec-new", "NEW", "NYSE", "Tech", date(2020, 1, 1),
            date(2021, 1, 1), known_on(date(2020, 1, 1)), known_on(date(2021, 1, 1)),
        )
        with self.assertRaisesRegex(ValueError, "classification coverage"):
            self._dataset(
                identities=(identity,),
                classifications=(short_classification,),
                memberships=(uncovered_membership,),
            )

    def test_values_are_deeply_immutable_and_datetime_must_be_aware(self):
        values = {"reported": {"eps": 2.5}, "tags": ["gaap"]}
        observation = FundamentalObservation(
            "sec-old", date(2023, 12, 31), known_on(date(2024, 2, 1)), 0, values,
        )
        values["reported"]["eps"] = 99.0
        values["tags"].append("mutated")

        self.assertEqual(observation.values["reported"]["eps"], 2.5)
        self.assertEqual(observation.values["tags"], ("gaap",))
        with self.assertRaises(TypeError):
            observation.values["new"] = 1
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            SecurityIdentity("bad", date(2020, 1, 1), None, datetime(2020, 1, 1))

        dataset = self._dataset(fundamentals=(observation,))
        with self.assertRaises((AttributeError, TypeError)):
            dataset.identities = ()
        with self.assertRaises(TypeError):
            dataset.snapshot(known_on(date(2024, 2, 2)), ("research",)).ticker_by_security_id["sec-old"] = "MUTATED"

    def test_hash_is_order_invariant_sensitive_and_verified_quality_is_required(self):
        identities, classifications, memberships = self._records()
        market_caps = (
            MarketCapObservation(
                "sec-old", date(2010, 1, 1), known_on(date(2010, 1, 1)), 1_000_000_000,
            ),
        )
        kwargs = dict(
            provider=PROVIDER,
            dataset_id=DATASET_ID,
            feed="test-pit-feed",
            schema_version="1",
            quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
            effective_date_timezone="America/New_York",
            coverage_start=date(2010, 1, 1),
            coverage_end=date(2025, 12, 31),
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            fundamentals=(),
            market_caps=market_caps,
            corporate_actions=(),
        )
        first = canonical_dataset_hash(**kwargs)
        reordered = canonical_dataset_hash(
            **{**kwargs, "identities": tuple(reversed(identities))}
        )
        changed = canonical_dataset_hash(**{**kwargs, "feed": "different-feed"})

        self.assertEqual(first, reordered)
        self.assertNotEqual(first, changed)
        verified_master = self._dataset()
        admission_price_rows = tuple(
            {
                "security_id": "sec-old",
                "trading_date": session.session_date,
                "close": 100.0,
                "volume": 100_000,
                "available_at": session.close_at,
            }
            for session in TEST_TRADING_CALENDAR.sessions
            if date(2024, 1, 2) <= session.session_date <= date(2024, 1, 5)
        )
        admission_kwargs = dict(
            universe="research",
            trusted_verifier_keys={TRUSTED_VERIFIER: TRUSTED_KEY},
            trading_calendar=TEST_TRADING_CALENDAR,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
            price_dataset=self._price_dataset(
                admission_price_rows,
                quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
                security_master_hash=verified_master.manifest.content_hash,
            ),
        )
        verified_master.require_admission_quality(
            date(2024, 1, 2), date(2024, 1, 5), **admission_kwargs
        )
        admitted = verified_master.admitted_snapshot(
            known_on(date(2024, 1, 5), 23),
            ("research",),
            admission_start=date(2024, 1, 2),
            admission_end=date(2024, 1, 5),
            **admission_kwargs,
        )
        self.assertTrue(admitted.admission_verified)
        self.assertFalse(replace(admitted, universes=("ex-post",)).admission_verified)

        future_membership = UniverseMembership(
            universe="research",
            security_id="sec-old",
            effective_from=date(2024, 1, 2),
            effective_to=date(2025, 1, 1),
            available_at=known_on(date(2024, 1, 2)),
            source="verified-index-history",
            effective_to_available_at=known_on(date(2025, 1, 1)),
        )
        future_master = self._dataset(memberships=(future_membership,))
        future_admission_kwargs = {
            **admission_kwargs,
            "price_dataset": self._price_dataset(
                admission_price_rows,
                quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
                security_master_hash=future_master.manifest.content_hash,
            ),
        }
        with self.assertRaisesRegex(ValueError, "non-empty materialized universe"):
            future_master.admitted_snapshot(
                known_on(date(2024, 1, 1), 23),
                ("research",),
                admission_start=date(2024, 1, 1),
                admission_end=date(2024, 1, 5),
                **future_admission_kwargs,
            )
        with self.assertRaisesRegex(ValueError, "inside the admission window"):
            verified_master.admitted_snapshot(
                known_on(date(2024, 12, 31), 23),
                ("research",),
                admission_start=date(2024, 1, 2),
                admission_end=date(2024, 1, 5),
                **admission_kwargs,
            )
        with self.assertRaisesRegex(ValueError, "exactly the admitted universe"):
            verified_master.admitted_snapshot(
                known_on(date(2024, 1, 5), 23),
                ("research", "unverified-extra"),
                admission_start=date(2024, 1, 2),
                admission_end=date(2024, 1, 5),
                **admission_kwargs,
            )
        with self.assertRaisesRegex(ValueError, "trading calendar coverage"):
            verified_master.require_admission_quality(
                date(2015, 1, 1), date(2024, 12, 31), **admission_kwargs
            )
        with self.assertRaisesRegex(ValueError, "point-in-time verified"):
            self._dataset(quality_tier=DataQualityTier.DEVELOPMENT_FREE).require_admission_quality(
                date(2024, 1, 2), date(2024, 1, 5), **admission_kwargs
            )
        with self.assertRaisesRegex(ValueError, "coverage"):
            verified_master.require_admission_quality(
                date(2009, 1, 1), date(2024, 1, 5), **admission_kwargs
            )
        with self.assertRaisesRegex(ValueError, "trusted verifier signature"):
            verified_master.require_admission_quality(
                date(2024, 1, 2),
                date(2024, 1, 5),
                **{**admission_kwargs, "trusted_verifier_keys": {TRUSTED_VERIFIER: b"wrong"}},
            )
        with self.assertRaisesRegex(ValueError, "session coverage|security IDs"):
            verified_master.require_admission_quality(
                date(2024, 1, 2),
                date(2024, 1, 5),
                **{
                    **admission_kwargs,
                    "price_dataset": self._price_dataset(
                        admission_price_rows[:-1],
                        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
                        security_master_hash=verified_master.manifest.content_hash,
                    ),
                },
            )
        empty = self._dataset(
            identities=(), classifications=(), memberships=(), market_caps=()
        )
        with self.assertRaisesRegex(ValueError, "non-empty"):
            empty.require_admission_quality(
                date(2024, 1, 2), date(2024, 1, 5), **admission_kwargs
            )
        with self.assertRaisesRegex(ValueError, "price data.*point-in-time verified"):
            verified_master.require_admission_quality(
                date(2024, 1, 2),
                date(2024, 1, 5),
                **{
                    **admission_kwargs,
                    "price_dataset": self._price_dataset(
                        admission_kwargs["price_dataset"].observations
                    ),
                },
            )
        wrong_security_rows = tuple(
            {**row, "security_id": "not-in-security-master"}
            for row in admission_price_rows
        )
        with self.assertRaisesRegex(ValueError, "security IDs"):
            verified_master.require_admission_quality(
                date(2024, 1, 2),
                date(2024, 1, 5),
                **{
                    **admission_kwargs,
                    "price_dataset": self._price_dataset(
                        wrong_security_rows,
                        quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
                        security_master_hash=verified_master.manifest.content_hash,
                    ),
                },
            )

    def test_admitted_snapshot_seals_exact_multi_universe_union(self):
        identities = (
            SecurityIdentity(
                "sec-alpha",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
            ),
            SecurityIdentity(
                "sec-beta",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
            ),
        )
        classifications = tuple(
            SecurityClassification(
                identity.security_id,
                "ALPHA" if identity.security_id == "sec-alpha" else "BETA",
                "NYSE",
                "Industrials",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
            )
            for identity in identities
        )
        memberships = (
            UniverseMembership(
                "SP500",
                "sec-alpha",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
                "verified-index-history",
            ),
            UniverseMembership(
                "RUSSELL1000",
                "sec-alpha",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
                "verified-index-history",
            ),
            UniverseMembership(
                "RUSSELL1000",
                "sec-beta",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
                "verified-index-history",
            ),
        )
        admitted_universes = ("SP500", "RUSSELL1000")
        dataset = self._dataset(
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            covered_universes=admitted_universes,
        )
        price_rows = tuple(
            {
                "security_id": identity.security_id,
                "trading_date": session.session_date,
                "close": 100.0,
                "volume": 100_000,
                "available_at": session.close_at,
            }
            for session in TEST_TRADING_CALENDAR.sessions
            if date(2024, 1, 2) <= session.session_date <= date(2024, 1, 5)
            for identity in identities
        )

        def verified_prices(*, covered=admitted_universes, rows=price_rows):
            return self._price_dataset(
                rows,
                quality_tier=DataQualityTier.POINT_IN_TIME_VERIFIED,
                security_master_hash=dataset.manifest.content_hash,
                covered_universes=covered,
            )

        admission_kwargs = dict(
            admission_start=date(2024, 1, 2),
            admission_end=date(2024, 1, 5),
            universe=admitted_universes,
            trusted_verifier_keys={TRUSTED_VERIFIER: TRUSTED_KEY},
            price_dataset=verified_prices(),
            trading_calendar=TEST_TRADING_CALENDAR,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
        )
        admitted = dataset.admitted_snapshot(
            known_on(date(2024, 1, 5), 23),
            admitted_universes,
            **admission_kwargs,
        )

        self.assertEqual(admitted.universes, ("RUSSELL1000", "SP500"))
        self.assertEqual(admitted.security_ids, ("sec-alpha", "sec-beta"))
        self.assertTrue(admitted.admission_verified)

        tampered_union = copy(admitted)
        object.__setattr__(tampered_union, "universes", ("SP500",))
        self.assertFalse(tampered_union.admission_verified)
        tampered_content = copy(admitted)
        object.__setattr__(
            tampered_content,
            "ticker_by_security_id",
            {"sec-alpha": "ALTERED", "sec-beta": "BETA"},
        )
        self.assertFalse(tampered_content.admission_verified)
        tampered_claim = copy(admitted)
        object.__setattr__(tampered_claim, "_admission_universes", ("SP500",))
        self.assertFalse(tampered_claim.admission_verified)

        with self.assertRaisesRegex(ValueError, "exactly the admitted universe set"):
            dataset.admitted_snapshot(
                known_on(date(2024, 1, 5), 23),
                ("SP500",),
                **admission_kwargs,
            )
        with self.assertRaisesRegex(ValueError, "requested universe"):
            dataset.admitted_snapshot(
                known_on(date(2024, 1, 5), 23),
                admitted_universes,
                **{
                    **admission_kwargs,
                    "price_dataset": verified_prices(covered=("SP500",)),
                },
            )
        master_missing_union_attestation = self._dataset(
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            covered_universes=("SP500",),
        )
        with self.assertRaisesRegex(ValueError, "requested universe"):
            master_missing_union_attestation.admitted_snapshot(
                known_on(date(2024, 1, 5), 23),
                admitted_universes,
                **admission_kwargs,
            )
        with self.assertRaisesRegex(ValueError, "price security IDs"):
            dataset.admitted_snapshot(
                known_on(date(2024, 1, 5), 23),
                admitted_universes,
                **{
                    **admission_kwargs,
                    "price_dataset": verified_prices(
                        rows=tuple(
                            row for row in price_rows if row["security_id"] == "sec-alpha"
                        )
                    ),
                },
            )

    def test_point_in_time_universe_builder_does_not_use_future_price_rows(self):
        dataset = self._dataset()
        builder = PointInTimeUniverseBuilder(
            dataset=dataset,
            universes=("research",),
            min_avg_dollar_volume=5_000_000,
            min_history_days=2,
            lookback_days=2,
        )
        rows = [
            {"security_id": "sec-old", "trading_date": date(2024, 1, 2), "close": 100.0, "volume": 1_000, "available_at": known_on(date(2024, 1, 2), 21)},
            {"security_id": "sec-old", "trading_date": date(2024, 1, 3), "close": 100.0, "volume": 1_000, "available_at": known_on(date(2024, 1, 3), 21)},
            {"security_id": "sec-old", "trading_date": date(2024, 1, 4), "close": 100.0, "volume": 100_000, "available_at": known_on(date(2024, 1, 4), 21)},
            {"security_id": "sec-old", "trading_date": date(2024, 1, 5), "close": 100.0, "volume": 100_000, "available_at": known_on(date(2024, 1, 5), 21)},
        ]
        prices = self._price_dataset(rows)

        historical = self._build_universe(
            builder, known_on(date(2024, 1, 3), 20), prices
        )
        later = self._build_universe(
            builder, known_on(date(2024, 1, 5), 23), prices
        )

        self.assertEqual(historical, [])
        self.assertEqual(later, ["sec-old"])

    def test_point_in_time_universe_builder_uses_stable_ids_and_active_snapshot(self):
        identities = (
            SecurityIdentity(
                "sec-old", date(2010, 1, 1), date(2020, 1, 1),
                known_on(date(2010, 1, 1)), known_on(date(2020, 1, 1)),
            ),
            SecurityIdentity("sec-new", date(2020, 1, 1), None, known_on(date(2020, 1, 1))),
        )
        classifications = (
            SecurityClassification(
                "sec-old", "SAME", "NYSE", "Tech", date(2010, 1, 1),
                date(2020, 1, 1), known_on(date(2010, 1, 1)), known_on(date(2020, 1, 1)),
            ),
            SecurityClassification(
                "sec-new", "SAME", "NASDAQ", "Health", date(2020, 1, 1),
                None, known_on(date(2020, 1, 1)),
            ),
        )
        memberships = (
            UniverseMembership(
                "research", "sec-old", date(2010, 1, 1), date(2020, 1, 1),
                known_on(date(2010, 1, 1)), "history", known_on(date(2020, 1, 1)),
            ),
            UniverseMembership("research", "sec-new", date(2020, 1, 1), None, known_on(date(2020, 1, 1)), "history"),
        )
        dataset = self._dataset(
            identities=identities,
            classifications=classifications,
            memberships=memberships,
        )
        rows = [
            {
                "security_id": security_id,
                "trading_date": date(2021, 1, day),
                "close": 100.0,
                "volume": 100_000,
                "available_at": known_on(date(2021, 1, day), 21),
            }
            for security_id in ("sec-old", "sec-new")
            for day in (1, 2)
        ]
        builder = PointInTimeUniverseBuilder(
            dataset=dataset,
            universes=("research",),
            min_avg_dollar_volume=5_000_000,
            min_history_days=2,
            lookback_days=2,
        )
        stable_id_calendar = sign_trading_calendar(
            exchange="XNYS",
            effective_date_timezone="America/New_York",
            issuer=CALENDAR_ISSUER,
            issuer_version="1",
            generated_at=known_on(date(2020, 12, 31)),
            coverage_start=date(2021, 1, 1),
            coverage_end=date(2021, 1, 2),
            missing_intervals=(),
            sessions=tuple(
                TradingSession(day, known_on(day, 21))
                for day in (date(2021, 1, 1), date(2021, 1, 2))
            ),
            signing_key=CALENDAR_KEY,
        )

        self.assertEqual(
            self._build_universe(
                builder,
                known_on(date(2021, 1, 2), 23),
                self._price_dataset(rows),
                trading_calendar=stable_id_calendar,
            ),
            ["sec-new"],
        )

    def test_daytrade_snapshot_ranks_prior_close_adv_and_uses_stable_id_tie_break(self):
        identities = tuple(
            SecurityIdentity(item, date(2010, 1, 1), None, known_on(date(2010, 1, 1)))
            for item in ("sec-a", "sec-b")
        )
        classifications = tuple(
            SecurityClassification(
                item,
                ticker,
                "NYSE",
                "Tech",
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
            )
            for item, ticker in (("sec-a", "AAA"), ("sec-b", "BBB"))
        )
        memberships = tuple(
            UniverseMembership(
                "research",
                item,
                date(2010, 1, 1),
                None,
                known_on(date(2010, 1, 1)),
                "history",
            )
            for item in ("sec-a", "sec-b")
        )
        dataset = self._dataset(
            identities=identities,
            classifications=classifications,
            memberships=memberships,
        )
        rows = tuple(
            {
                "security_id": security_id,
                "trading_date": trading_day,
                "close": 10.0,
                "volume": volume,
                "available_at": known_on(trading_day, 21),
            }
            for security_id, volume in (("sec-a", 600_000), ("sec-b", 600_000))
            for trading_day in (date(2024, 1, 2), date(2024, 1, 3))
        )
        builder = PointInTimeUniverseBuilder(
            dataset,
            ("research",),
            min_avg_dollar_volume=5_000_000,
            min_history_days=2,
            lookback_days=2,
        )

        daily = builder.build_daytrade_snapshot(
            date(2024, 1, 4),
            self._price_dataset(rows),
            trading_calendar=TEST_TRADING_CALENDAR,
            trusted_calendar_keys={CALENDAR_ISSUER: CALENDAR_KEY},
            top_n=1,
        )

        self.assertEqual(daily.ticker_by_security_id, {"sec-a": "AAA"})

    def test_daytrade_snapshot_rejects_unexplained_missing_bar_and_accepts_halt_evidence(self):
        builder = PointInTimeUniverseBuilder(
            self._dataset(),
            ("research",),
            min_avg_dollar_volume=1.0,
            min_history_days=2,
            lookback_days=1,
        )
        row = {
            "security_id": "sec-old",
            "trading_date": date(2024, 1, 2),
            "close": 10.0,
            "volume": 1_000.0,
            "available_at": known_on(date(2024, 1, 2), 21),
        }
        prices = self._price_dataset((row,))
        kwargs = {
            "trading_calendar": TEST_TRADING_CALENDAR,
            "trusted_calendar_keys": {CALENDAR_ISSUER: CALENDAR_KEY},
        }

        with self.assertRaisesRegex(ValueError, "without suspension evidence"):
            builder.build_daytrade_snapshot(date(2024, 1, 4), prices, **kwargs)
        accepted = builder.build_daytrade_snapshot(
            date(2024, 1, 4),
            prices,
            suspensions=(
                TradingSuspension(
                    "sec-old",
                    date(2024, 1, 3),
                    known_on(date(2024, 1, 3), 20),
                    "exchange-halt-feed",
                ),
            ),
            **kwargs,
        )
        self.assertEqual(accepted.security_ids, ())

    def test_point_in_time_universe_builder_rejects_ambiguous_or_future_available_rows(self):
        builder = PointInTimeUniverseBuilder(
            dataset=self._dataset(),
            universes=("research",),
            min_avg_dollar_volume=1.0,
            min_history_days=1,
            lookback_days=1,
        )
        as_of = known_on(date(2024, 1, 2))
        future_available = {
            "security_id": "sec-old",
            "trading_date": date(2024, 1, 2),
            "close": 100.0,
            "volume": 100_000,
            "available_at": known_on(date(2024, 1, 3)),
        }

        self.assertEqual(
            self._build_universe(builder, as_of, self._price_dataset((future_available,))),
            [],
        )
        with self.assertRaisesRegex(ValueError, "security_id"):
            self._price_dataset(({
                "ticker": "OLD",
                "trading_date": date(2024, 1, 2),
                "close": 100.0,
                "volume": 1,
                "available_at": known_on(date(2024, 1, 2), 21),
            },))
        with self.assertRaisesRegex(ValueError, "available_at"):
            self._price_dataset(({
                "security_id": "sec-old",
                "trading_date": date(2024, 1, 2),
                "close": 100.0,
                "volume": 1,
            },))
        with self.assertRaisesRegex(ValueError, "raw close"):
            self._price_dataset(({
                "security_id": "sec-old",
                "trading_date": date(2024, 1, 2),
                "adjusted_close": 100.0,
                "volume": 1,
                "available_at": known_on(date(2024, 1, 2), 21),
            },))
        duplicate = {
            "security_id": "sec-old",
            "trading_date": date(2024, 1, 2),
            "close": 100.0,
            "volume": 1,
            "available_at": known_on(date(2024, 1, 2), 21),
        }
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self._price_dataset((duplicate, dict(duplicate)))

        sparse_builder = PointInTimeUniverseBuilder(
            dataset=self._dataset(),
            universes=("research",),
            min_avg_dollar_volume=1.0,
            min_history_days=1,
            lookback_days=2,
        )
        self.assertEqual(
            self._build_universe(
                sparse_builder, as_of, self._price_dataset((duplicate,))
            ),
            [],
        )

        incomplete_sessions = self._price_dataset(
            (
                {**duplicate, "trading_date": date(2024, 1, 2)},
                {
                    **duplicate,
                    "trading_date": date(2024, 1, 4),
                    "available_at": known_on(date(2024, 1, 4), 21),
                },
            )
        )
        with self.assertRaisesRegex(ValueError, "calendar session coverage"):
            self._build_universe(
                sparse_builder, known_on(date(2024, 1, 4), 23), incomplete_sessions
            )

        before_close = self._price_dataset(
            ({**duplicate, "available_at": known_on(date(2024, 1, 2), 12)},)
        )
        with self.assertRaisesRegex(ValueError, "session close"):
            self._build_universe(
                builder, known_on(date(2024, 1, 2), 23), before_close
            )

    def test_universe_builder_strict_config_and_action_query_coverage(self):
        with self.assertRaisesRegex(ValueError, "min_avg_dollar_volume"):
            PointInTimeUniverseBuilder(
                dataset=self._dataset(),
                universes=("research",),
                min_avg_dollar_volume=True,
            )
        with self.assertRaisesRegex(ValueError, "min_avg_dollar_volume"):
            PointInTimeUniverseBuilder(
                dataset=self._dataset(),
                universes=("research",),
                min_avg_dollar_volume=10**400,
            )
        with self.assertRaisesRegex(ValueError, "min_history_days"):
            PointInTimeUniverseBuilder(
                dataset=self._dataset(),
                universes=("research",),
                min_history_days=True,
            )
        with self.assertRaisesRegex(ValueError, "coverage"):
            self._dataset().actions_as_of(
                known_on(date(2030, 1, 1)), ("sec-old",)
            )

        overflow_builder = PointInTimeUniverseBuilder(
            dataset=self._dataset(),
            universes=("research",),
            min_avg_dollar_volume=1e300,
            min_history_days=1,
            lookback_days=1,
        )
        overflow_row = {
            "security_id": "sec-old",
            "trading_date": date(2024, 1, 2),
            "close": 1e308,
            "volume": 1e308,
            "available_at": known_on(date(2024, 1, 2), 21),
        }
        self.assertEqual(
            self._build_universe(
                overflow_builder,
                known_on(date(2024, 1, 2), 23),
                self._price_dataset((overflow_row,)),
            ),
            [],
        )

    def test_corporate_actions_cannot_fall_outside_listing_lifecycle(self):
        pre_listing_split = CorporateAction(
            CorporateActionType.SPLIT,
            "pre-listing-split",
            "sec-old",
            date(2005, 1, 1),
            known_on(date(2005, 1, 1)),
            ratio=2.0,
        )
        post_delisting_dividend = CorporateAction(
            CorporateActionType.CASH_DIVIDEND,
            "post-delisting-dividend",
            "sec-old",
            date(2025, 1, 2),
            known_on(date(2025, 1, 2)),
            cash_amount=1.0,
        )

        for action in (pre_listing_split, post_delisting_dividend):
            with self.subTest(action=action.action_id), self.assertRaisesRegex(
                ValueError, "listing"
            ):
                self._dataset(corporate_actions=(action,))

    def test_security_master_snapshot_public_constructor_rejects_future_facts(self):
        dataset = self._dataset()
        future_identity = dataset.identities[0]
        future_classification = dataset.classifications[0]

        with self.assertRaisesRegex(ValueError, "future"):
            SecurityMasterSnapshot(
                as_of=known_on(date(2011, 1, 3)),
                effective_date=date(2011, 1, 3),
                effective_date_timezone="America/New_York",
                source_dataset_hash=dataset.manifest.content_hash,
                source_quality_tier=dataset.manifest.quality_tier,
                universes=("research",),
                security_ids=("sec-old",),
                identity_by_security_id={"sec-old": future_identity},
                classification_by_security_id={"sec-old": future_classification},
                ticker_by_security_id={"sec-old": "OLD"},
                latest_fundamentals={},
                latest_market_caps={},
                known_corporate_actions=(),
                effective_corporate_actions=(),
            )

        safe = dataset.snapshot(known_on(date(2011, 1, 3)), ("research",))
        known_action = CorporateAction(
            CorporateActionType.CASH_DIVIDEND,
            "duplicate-action-id",
            "sec-old",
            date(2011, 1, 1),
            known_on(date(2010, 12, 31)),
            cash_amount=1.0,
        )
        future_payload = CorporateAction(
            CorporateActionType.CASH_DIVIDEND,
            "duplicate-action-id",
            "sec-old",
            date(2011, 1, 1),
            known_on(date(2025, 1, 1)),
            cash_amount=999.0,
        )
        with self.assertRaisesRegex(ValueError, "effective corporate actions"):
            SecurityMasterSnapshot(
                as_of=safe.as_of,
                effective_date=safe.effective_date,
                effective_date_timezone=safe.effective_date_timezone,
                source_dataset_hash=safe.source_dataset_hash,
                source_quality_tier=safe.source_quality_tier,
                universes=safe.universes,
                security_ids=safe.security_ids,
                identity_by_security_id=safe.identity_by_security_id,
                classification_by_security_id=safe.classification_by_security_id,
                ticker_by_security_id=safe.ticker_by_security_id,
                latest_fundamentals=safe.latest_fundamentals,
                latest_market_caps=safe.latest_market_caps,
                known_corporate_actions=(known_action,),
                effective_corporate_actions=(future_payload,),
            )


if __name__ == "__main__":
    unittest.main()
