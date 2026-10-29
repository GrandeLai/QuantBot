"""Contracts for the preregistered normal H2 capital-efficiency experiment.

H2 declares exactly one variable: whether the allocator re-deploys the capital
that its own single-stock, sector and liquidity caps free up, and whether
volatility targeting is two-sided.  Both arms share one provider instance, so
signal, universe, sessions, costs and risk limits are identical by
construction.
"""

import calendar
import sys
import unittest
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
from quantbot.experiment_dispositions import (
    ExperimentDisposition,
    fixed_experiment_disposition,
)
from quantbot.normal import (
    NORMAL_H2_BASELINE_CONFIG,
    NORMAL_H2_CANDIDATE_CONFIG,
    NORMAL_H2_EXPERIMENT_ID,
    NormalBacktestSession,
    NormalFactorExperiment,
    NormalRegimeContext,
    PointInTimeNormalRequestProvider,
    preregistered_normal_experiments,
    run_normal_h2_experiment,
)
from quantbot.regime import Regime
from quantbot.trading import PointInTimeSnapshot


UTC = timezone.utc
IDS = tuple("SEC%d" % index for index in range(8))
SECTOR_OF = {sid: ("Tech" if index < 4 else "Finance") for index, sid in enumerate(IDS)}


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
    known = datetime(2000, 1, 1, tzinfo=UTC)
    identities = {
        sid: SecurityIdentity(sid, date(2000, 1, 1), None, known) for sid in IDS
    }
    classifications = {
        sid: SecurityClassification(
            sid, sid, "NYSE", SECTOR_OF[sid], date(2000, 1, 1), None, known
        )
        for sid in IDS
    }
    fundamentals = {
        sid: FundamentalObservation(
            sid,
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
        for index, sid in enumerate(IDS)
    }
    caps = {
        sid: MarketCapObservation(
            sid,
            date(2025, 4, 14),
            datetime(2025, 4, 14, 21, tzinfo=UTC),
            1_000_000_000.0 * (index + 1),
        )
        for index, sid in enumerate(IDS)
    }
    return SecurityMasterSnapshot(
        as_of=as_of,
        effective_date=as_of.date(),
        effective_date_timezone="UTC",
        source_dataset_hash="a" * 64,
        source_quality_tier=DataQualityTier.DEVELOPMENT_FREE,
        universes=("SP500", "RUSSELL1000"),
        security_ids=IDS,
        identity_by_security_id=identities,
        classification_by_security_id=classifications,
        ticker_by_security_id={sid: sid for sid in IDS},
        latest_fundamentals=fundamentals,
        latest_market_caps=caps,
        known_corporate_actions=(),
        effective_corporate_actions=(),
    )


class _Port:
    """One market-data view reused by both arms, keyed by master identity."""

    def __init__(self):
        self._returns = {}

    def monthly_returns(self, security_master):
        key = canonical_security_master_snapshot_hash(security_master)
        if key not in self._returns:
            self._returns[key] = self._build_returns(security_master)
        return self._returns[key]

    @staticmethod
    def _build_returns(master):
        months = _months()
        rows = []
        for month_index, month in enumerate(months):
            market = ((month_index % 7) - 3) * 0.006
            observed = datetime(month.year, month.month, month.day, 22, tzinfo=UTC)
            rows.append(MonthlyReturnObservation("SPY-ID", month, market, observed))
            for security_index, sid in enumerate(master.security_ids):
                residual = (((month_index + security_index * 2) % 9) - 4) * 0.002
                rows.append(
                    MonthlyReturnObservation(
                        sid,
                        month,
                        market * (0.7 + security_index * 0.03) + residual,
                        observed,
                    )
                )
        snapshot = PointInTimeMonthlyReturnSnapshot(
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
        # This suite isolates allocator behaviour; the signed dataset boundary
        # is covered in test_stockbot_pit_total_returns.
        object.__setattr__(snapshot, "provenance_verified", True)
        object.__setattr__(snapshot, "_provenance_seal", _RETURN_PROVENANCE_SEAL)
        object.__setattr__(
            snapshot, "_provenance_hash", _return_snapshot_content_hash(snapshot)
        )
        return snapshot

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {
            sid: tuple((((index + offset) % 11) - 5) * 0.004 for index in range(24))
            for offset, sid in enumerate(security_master.security_ids)
        }

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {
            sleeve.name: tuple((((index + offset) % 7) - 3) * 0.002 for index in range(24))
            for offset, sleeve in enumerate(sleeves)
        }

    def market_betas(self, security_master, *, lookback_sessions):
        return {sid: 0.8 for sid in security_master.security_ids}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {sid: 500_000_000 for sid in security_master.security_ids}

    def regime_context(self, security_master):
        return NormalRegimeContext(Regime.R1_BULL_LOW_VOL)


SESSION_DATES = (
    date(2025, 4, 15),
    date(2025, 4, 16),
    date(2025, 4, 17),
    date(2025, 4, 18),
)


def _sessions():
    result = []
    for index, trading_date in enumerate(SESSION_DATES):
        prices = {sid: 100.0 + position + index for position, sid in enumerate(IDS)}
        closes = {sid: value * 1.001 for sid, value in prices.items()}
        snapshot = None
        if index < len(SESSION_DATES) - 1:
            as_of = datetime(
                trading_date.year, trading_date.month, trading_date.day, 22, tzinfo=UTC
            )
            snapshot = PointInTimeSnapshot(
                observed_at=as_of, security_master=_master(as_of)
            )
        result.append(
            NormalBacktestSession(
                trading_date=trading_date,
                open_prices=prices,
                close_prices=closes,
                benchmark_return=0.0005,
                signal_snapshot=snapshot,
            )
        )
    return tuple(result)


def _provider(*, include_quality=False):
    return PointInTimeNormalRequestProvider(
        _Port(), experiment=NormalFactorExperiment(include_quality=include_quality)
    )


class NormalH2DeclarationTest(unittest.TestCase):
    def test_h2_is_completed_rejected_and_not_selectable(self):
        registry = preregistered_normal_experiments()

        self.assertFalse(registry.is_candidate_selectable(NORMAL_H2_EXPERIMENT_ID))
        self.assertEqual(
            registry.disposition(NORMAL_H2_EXPERIMENT_ID),
            ExperimentDisposition.COMPLETED_REJECTED,
        )
        self.assertEqual(
            fixed_experiment_disposition(NORMAL_H2_EXPERIMENT_ID).disposition,
            ExperimentDisposition.COMPLETED_REJECTED,
        )

    def test_h2_declares_exactly_one_trial_against_the_h1_baseline_sleeve(self):
        registry = preregistered_normal_experiments()
        record = next(
            item for item in registry.records if item.experiment_id == NORMAL_H2_EXPERIMENT_ID
        )

        self.assertEqual(record.declared_trial_count, 1)
        self.assertEqual(record.baseline_components, ("residual_momentum_12_1_neutral",))
        self.assertEqual(record.added_component, "capital_efficient_allocation")

    def test_h2_arm_configurations_are_frozen(self):
        self.assertFalse(NORMAL_H2_BASELINE_CONFIG.redistribute_constrained_capital)
        self.assertEqual(NORMAL_H2_BASELINE_CONFIG.volatility_targeting, "cap_only")
        self.assertEqual(NORMAL_H2_BASELINE_CONFIG.min_funded_names, 0)

        self.assertTrue(NORMAL_H2_CANDIDATE_CONFIG.redistribute_constrained_capital)
        self.assertEqual(NORMAL_H2_CANDIDATE_CONFIG.volatility_targeting, "two_sided")
        self.assertEqual(NORMAL_H2_CANDIDATE_CONFIG.min_funded_names, 20)

        self.assertEqual(NORMAL_H2_BASELINE_CONFIG.top_n, NORMAL_H2_CANDIDATE_CONFIG.top_n)
        for name in (
            "max_single_stock_weight",
            "max_sector_weight",
            "max_market_beta_exposure",
            "target_volatility_annual",
            "max_adv_participation",
            "no_trade_band",
            "turnover_penalty",
            "exit_rank_buffer",
        ):
            self.assertEqual(
                getattr(NORMAL_H2_BASELINE_CONFIG, name),
                getattr(NORMAL_H2_CANDIDATE_CONFIG, name),
                "%s must be identical across H2 arms" % name,
            )


class NormalH2RunnerTest(unittest.TestCase):
    def test_runner_redeploys_capital_relative_to_the_frozen_baseline(self):
        result = run_normal_h2_experiment(
            provider=_provider(),
            sessions=_sessions(),
            window_sessions=2,
            historical_audit_replay=True,
        )

        self.assertGreater(
            result.decision.candidate_capital_utilization,
            result.decision.baseline_capital_utilization,
        )
        self.assertNotIn("no_capital_redeployment", result.decision.reasons)

    def test_runner_reports_both_normal_declarations_as_multiplicity(self):
        result = run_normal_h2_experiment(
            provider=_provider(),
            sessions=_sessions(),
            window_sessions=2,
            historical_audit_replay=True,
        )

        self.assertEqual(result.declared_trials, 2)
        self.assertEqual(result.completed_trials, 1)
        self.assertFalse(result.decision.passed)
        self.assertIn("completed_rejected", result.decision.reasons)

    def test_runner_produces_stressed_arms_for_both_configurations(self):
        result = run_normal_h2_experiment(
            provider=_provider(),
            sessions=_sessions(),
            cost_bps_per_side=5.0,
            stress_cost_bps_per_side=10.0,
            window_sessions=2,
            historical_audit_replay=True,
        )

        self.assertLessEqual(result.stressed_candidate.final_equity, result.candidate.final_equity)
        self.assertLessEqual(result.stressed_baseline.final_equity, result.baseline.final_equity)

    def test_runner_rejects_candidate_selection_without_explicit_audit_mode(self):
        with self.assertRaisesRegex(ValueError, "completed_rejected"):
            run_normal_h2_experiment(
                provider=_provider(),
                sessions=_sessions(),
                window_sessions=2,
            )

    def test_runner_rejects_the_quality_sleeve_arm(self):
        with self.assertRaisesRegex(ValueError, "residual momentum baseline sleeve"):
            run_normal_h2_experiment(
                provider=_provider(include_quality=True), sessions=_sessions()
            )

    def test_runner_rejects_a_non_provider(self):
        with self.assertRaisesRegex(TypeError, "PointInTimeNormalRequestProvider"):
            run_normal_h2_experiment(provider=object(), sessions=_sessions())

    def test_runner_rejects_empty_sessions(self):
        with self.assertRaisesRegex(ValueError, "sessions must not be empty"):
            run_normal_h2_experiment(provider=_provider(), sessions=())

    def test_runner_rejects_a_degenerate_window(self):
        with self.assertRaisesRegex(ValueError, "window_sessions"):
            run_normal_h2_experiment(
                provider=_provider(), sessions=_sessions(), window_sessions=1
            )


if __name__ == "__main__":
    unittest.main()
