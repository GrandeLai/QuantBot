import calendar
import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data import (
    DataQualityTier,
    FundamentalObservation,
    MarketCapObservation,
    MonthlyReturnObservation,
    PointInTimeMonthlyReturnSnapshot,
    SecurityClassification,
    SecurityIdentity,
    SecurityMasterSnapshot,
    canonical_security_master_snapshot_hash,
)
from quantbot.data.pit_returns import (
    _RETURN_PROVENANCE_SEAL,
    _return_snapshot_content_hash,
)
from quantbot.normal import (
    NormalFactorExperiment,
    NormalRegimeContext,
    PointInTimeNormalRequestProvider,
    preregistered_normal_experiments,
)
from quantbot.regime import Regime
from quantbot.trading import PointInTimeSnapshot


UTC = timezone.utc


def _month_end(year, month):
    return date(year, month, calendar.monthrange(year, month)[1])


def _months(start_year=2021, start_month=12, count=40):
    values = []
    ordinal = start_year * 12 + start_month - 1
    for offset in range(count):
        year, month_index = divmod(ordinal + offset, 12)
        values.append(_month_end(year, month_index + 1))
    return tuple(values)


def _master(as_of):
    ids = tuple(f"SEC{index}" for index in range(8))
    known = datetime(2000, 1, 1, tzinfo=UTC)
    identities = {
        security_id: SecurityIdentity(security_id, date(2000, 1, 1), None, known)
        for security_id in ids
    }
    classifications = {
        security_id: SecurityClassification(
            security_id,
            security_id,
            "NYSE",
            "Tech" if index < 4 else "Finance",
            date(2000, 1, 1),
            None,
            known,
        )
        for index, security_id in enumerate(ids)
    }
    fundamentals = {
        security_id: FundamentalObservation(
            security_id,
            date(2024, 12, 31),
            datetime(2025, 2, 15, tzinfo=UTC),
            0,
            {
                "return_on_equity": 0.10 + index * 0.01,
                "gross_margin": 0.20 + (index % 3) * 0.02,
                "debt_to_assets": 0.40 - index * 0.01,
                "accrual_ratio": 0.03 - (index % 2) * 0.01,
            },
        )
        for index, security_id in enumerate(ids)
    }
    caps = {
        security_id: MarketCapObservation(
            security_id,
            date(2025, 4, 14),
            datetime(2025, 4, 14, 21, tzinfo=UTC),
            1_000_000_000.0 * (index + 1),
        )
        for index, security_id in enumerate(ids)
    }
    return SecurityMasterSnapshot(
        as_of=as_of,
        effective_date=as_of.date(),
        effective_date_timezone="UTC",
        source_dataset_hash="a" * 64,
        source_quality_tier=DataQualityTier.DEVELOPMENT_FREE,
        universes=("SP500", "RUSSELL1000"),
        security_ids=ids,
        identity_by_security_id=identities,
        classification_by_security_id=classifications,
        ticker_by_security_id={security_id: security_id for security_id in ids},
        latest_fundamentals=fundamentals,
        latest_market_caps=caps,
        known_corporate_actions=(),
        effective_corporate_actions=(),
    )


class _Port:
    def __init__(self, master):
        months = _months()
        rows = []
        for month_index, month in enumerate(months):
            market = ((month_index % 7) - 3) * 0.006
            rows.append(
                MonthlyReturnObservation(
                    "SPY-ID", month, market, datetime(month.year, month.month, month.day, 22, tzinfo=UTC)
                )
            )
            for security_index, security_id in enumerate(master.security_ids):
                residual = (((month_index + security_index * 2) % 9) - 4) * 0.002
                rows.append(
                    MonthlyReturnObservation(
                        security_id,
                        month,
                        market * (0.7 + security_index * 0.03) + residual,
                        datetime(month.year, month.month, month.day, 22, tzinfo=UTC),
                    )
                )
        self.returns = PointInTimeMonthlyReturnSnapshot(
            observed_at=master.as_of,
            effective_date=master.effective_date,
            effective_date_timezone=master.effective_date_timezone,
            provider="test",
            dataset_hash="b" * 64,
            security_master_dataset_hash=master.source_dataset_hash,
            security_master_hash=canonical_security_master_snapshot_hash(master),
            quality_tier=DataQualityTier.DEVELOPMENT_FREE,
            adjustment_vintage_at=master.as_of,
            benchmark_security_id="SPY-ID",
            reference_security_ids=("SPY-ID",),
            observations=tuple(rows),
            latest_complete_month=months[-1],
            investable_security_ids=master.security_ids,
            missing_latest_security_ids=(),
            security_master_admission_verified=False,
            provenance_verified=False,
        )
        # Provider tests isolate factor/request wiring.  The signed dataset
        # boundary itself is covered exhaustively in test_stockbot_pit_total_returns.
        object.__setattr__(self.returns, "provenance_verified", True)
        object.__setattr__(self.returns, "_provenance_seal", _RETURN_PROVENANCE_SEAL)
        object.__setattr__(
            self.returns,
            "_provenance_hash",
            _return_snapshot_content_hash(self.returns),
        )

    def monthly_returns(self, security_master):
        return self.returns

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {
            security_id: tuple(
                (((index + offset) % 11) - 5) * 0.001
                for index in range(24)
            )
            for offset, security_id in enumerate(security_master.security_ids)
        }

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {
            sleeve.name: tuple(
                (((index + offset) % 7) - 3) * 0.002
                for index in range(24)
            )
            for offset, sleeve in enumerate(sleeves)
        }

    def market_betas(self, security_master, *, lookback_sessions):
        return {security_id: 0.8 for security_id in security_master.security_ids}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {security_id: 20_000_000 for security_id in security_master.security_ids}

    def regime_context(self, security_master):
        return NormalRegimeContext(Regime.R1_BULL_LOW_VOL)


class PointInTimeNormalRequestProviderTest(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2025, 4, 15, 22, tzinfo=UTC)
        self.master = _master(self.as_of)
        self.snapshot = PointInTimeSnapshot(
            observed_at=self.as_of,
            security_master=self.master,
        )

    def test_baseline_and_h1_use_the_same_complete_security_master(self):
        port = _Port(self.master)
        baseline = PointInTimeNormalRequestProvider(port).build_request(
            self.snapshot, {}
        )
        candidate = PointInTimeNormalRequestProvider(
            port,
            experiment=NormalFactorExperiment(include_quality=True),
        ).build_request(self.snapshot, {})

        self.assertEqual(
            tuple(sleeve.name for sleeve in baseline.sleeves),
            ("residual_momentum_12_1_neutral",),
        )
        self.assertEqual(
            tuple(sleeve.name for sleeve in candidate.sleeves),
            (
                "residual_momentum_12_1_neutral",
                "quality_profitability_neutral",
            ),
        )
        self.assertEqual(baseline.security_master, candidate.security_master)
        self.assertEqual(set(baseline.asset_return_history), set(self.master.security_ids))

    def test_quality_missing_for_one_security_fails_closed(self):
        incomplete = replace(
            self.master,
            latest_fundamentals={
                key: value
                for key, value in self.master.latest_fundamentals.items()
                if key != "SEC0"
            },
        )
        snapshot = PointInTimeSnapshot(observed_at=self.as_of, security_master=incomplete)
        port = _Port(incomplete)
        with self.assertRaisesRegex(ValueError, "complete quality"):
            PointInTimeNormalRequestProvider(
                port,
                experiment=NormalFactorExperiment(include_quality=True),
            ).build_request(snapshot, {})

    def test_formal_mode_rejects_unadmitted_inputs(self):
        with self.assertRaisesRegex(ValueError, "admitted security master"):
            PointInTimeNormalRequestProvider(
                _Port(self.master), formal_admission=True
            ).build_request(self.snapshot, {})

    def test_each_normal_experiment_declares_exactly_one_trial(self):
        registry = preregistered_normal_experiments()
        by_id = {record.experiment_id: record for record in registry.records}

        # Every normal declaration counts towards global DSR/PBO multiplicity,
        # so the total must grow with the ledger rather than stay pinned at one.
        self.assertEqual(registry.total_declared_trials, len(by_id))
        self.assertEqual(
            set(by_id),
            {"normal-h1-quality-diversification", "normal-h2-capital-efficiency"},
        )
        self.assertEqual(
            by_id["normal-h1-quality-diversification"].added_component,
            "quality_profitability_neutral",
        )
        self.assertEqual(
            by_id["normal-h2-capital-efficiency"].added_component,
            "capital_efficient_allocation",
        )
        for record in registry.records:
            self.assertEqual(record.declared_trial_count, 1)
            self.assertEqual(record.strategy_family, "normal")


if __name__ == "__main__":
    unittest.main()
