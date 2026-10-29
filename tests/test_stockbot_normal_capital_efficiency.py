"""Capital-efficiency contracts for the normal-trading allocator.

The legacy allocator only ever shrinks weights: the single-stock, sector and
liquidity caps truncate a name and the freed budget goes to cash permanently,
and volatility targeting de-levers but never re-levers.  A 20-name book with a
5% single-name cap therefore settles far below its own gross-exposure target
and runs at a fraction of its configured volatility budget, which caps the
return the strategy can earn from any signal.

These tests pin both the legacy behaviour (still the default, so frozen
experiment identities stay reproducible) and the opt-in capital-efficient
behaviour, including the risk limits it must never breach.
"""

import sys
import unittest
from datetime import date, datetime, timezone
from math import fsum
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config_loader import NormalRuntimeConfig
from quantbot.data import (
    DataQualityTier,
    SecurityClassification,
    SecurityIdentity,
    SecurityMasterSnapshot,
    canonical_security_master_snapshot_hash,
)
from quantbot.factors import FactorSleeve
from quantbot.normal import (
    NormalAllocationRequest,
    NormalPortfolioAllocator,
    NormalPortfolioConfig,
    NormalRegimeContext,
)
from quantbot.regime import Regime


UTC = timezone.utc
AS_OF = datetime(2025, 4, 15, 12, tzinfo=UTC)
SECTORS = ("Tech", "Finance", "Health", "Energy")
NAMES = tuple("S%02d" % index for index in range(24))
SECTOR_OF = {name: SECTORS[index % len(SECTORS)] for index, name in enumerate(NAMES)}
PATTERN = (-1.0, -0.4, 0.2, 0.8, 1.3, -0.7, 0.5, -0.9)


def _master():
    identities = {
        name: SecurityIdentity(name, date(2000, 1, 1), None, datetime(2000, 1, 1, tzinfo=UTC))
        for name in NAMES
    }
    classifications = {
        name: SecurityClassification(
            name,
            name,
            "NYSE",
            SECTOR_OF[name],
            date(2000, 1, 1),
            None,
            datetime(2000, 1, 1, tzinfo=UTC),
        )
        for name in NAMES
    }
    return SecurityMasterSnapshot(
        as_of=AS_OF,
        effective_date=AS_OF.date(),
        effective_date_timezone="UTC",
        source_dataset_hash="a" * 64,
        source_quality_tier=DataQualityTier.DEVELOPMENT_FREE,
        universes=("research",),
        security_ids=NAMES,
        identity_by_security_id=identities,
        classification_by_security_id=classifications,
        ticker_by_security_id={name: name for name in NAMES},
        latest_fundamentals={},
        latest_market_caps={},
        known_corporate_actions=(),
        effective_corporate_actions=(),
    )


MASTER = _master()


def _history(scale, phase, count=36):
    return tuple(
        scale * PATTERN[(index + phase) % len(PATTERN)] for index in range(count)
    )


def _sleeve(scores):
    return FactorSleeve(
        name="residual_momentum_12_1_neutral",
        observed_at=MASTER.as_of,
        effective_date=MASTER.effective_date,
        effective_date_timezone=MASTER.effective_date_timezone,
        security_master_hash=canonical_security_master_snapshot_hash(MASTER),
        scores=scores,
        missing_security_ids=(),
        input_dataset_hashes=(MASTER.source_dataset_hash,),
    )


def _request(
    *,
    asset_scale=0.010,
    current_weights=None,
    regime=Regime.R1_BULL_LOW_VOL,
    confidence=1.0,
    adv_dollars=None,
    scores=None,
):
    sleeve = _sleeve(
        scores
        if scores is not None
        else {name: 1.0 - index * 0.05 for index, name in enumerate(NAMES)}
    )
    return NormalAllocationRequest(
        security_master=MASTER,
        sleeves=(sleeve,),
        sleeve_confidences={sleeve.name: 1.0},
        sleeve_return_history={sleeve.name: _history(0.02, 0, 24)},
        asset_return_history={
            name: _history(asset_scale, index) for index, name in enumerate(NAMES)
        },
        current_weights=current_weights or {},
        market_betas={name: 1.0 for name in NAMES},
        adv_dollars=adv_dollars or {name: 500_000_000.0 for name in NAMES},
        capital=100_000.0,
        regime=NormalRegimeContext(regime, confidence=confidence),
    )


LEGACY = NormalPortfolioConfig(top_n=20)
EFFICIENT = NormalPortfolioConfig(
    top_n=20,
    redistribute_constrained_capital=True,
    volatility_targeting="two_sided",
    min_funded_names=20,
)


def _settle(config, *, rebalances=30, **kwargs):
    """Run the allocator to its steady state, as a live book would reach it."""

    allocator = NormalPortfolioAllocator(config)
    weights = kwargs.pop("current_weights", None) or {}
    report = None
    for _ in range(rebalances):
        report = allocator.allocate(_request(current_weights=weights, **kwargs))
        weights = dict(report.targets.weights)
    return report, weights


class LegacyCapitalLeakTest(unittest.TestCase):
    def test_default_config_settles_far_below_its_own_exposure_target(self):
        report, weights = _settle(LEGACY)

        self.assertAlmostEqual(report.target_exposure, 1.0)
        self.assertLess(fsum(weights.values()), 0.75)
        self.assertLess(report.capital_utilization, 0.75)

    def test_default_config_leaves_most_of_the_volatility_budget_unused(self):
        report, _weights = _settle(LEGACY)

        self.assertLess(report.annualized_volatility, LEGACY.target_volatility_annual)
        self.assertLess(report.risk_utilization, 0.75)

    def test_report_exposes_utilization_so_the_leak_is_never_silent(self):
        report, weights = _settle(LEGACY)

        self.assertAlmostEqual(
            report.capital_utilization,
            fsum(weights.values()) / report.target_exposure,
        )
        self.assertAlmostEqual(
            report.risk_utilization,
            report.annualized_volatility / LEGACY.target_volatility_annual,
        )


class RedistributedCapitalTest(unittest.TestCase):
    def test_redistribution_reaches_the_permitted_gross_exposure(self):
        legacy_report, legacy_weights = _settle(LEGACY)
        report, weights = _settle(EFFICIENT)

        self.assertGreater(fsum(weights.values()), 0.95)
        self.assertGreater(report.capital_utilization, 0.95)
        self.assertGreater(
            fsum(weights.values()), 1.25 * fsum(legacy_weights.values())
        )
        self.assertGreater(
            report.capital_utilization, legacy_report.capital_utilization
        )

    def test_redistribution_never_breaches_the_single_stock_cap(self):
        _report, weights = _settle(EFFICIENT)

        self.assertTrue(weights)
        for weight in weights.values():
            self.assertLessEqual(weight, EFFICIENT.max_single_stock_weight + 1e-12)

    def test_redistribution_never_breaches_the_sector_cap(self):
        _report, weights = _settle(EFFICIENT)

        totals = {}
        for security_id, weight in weights.items():
            totals[SECTOR_OF[security_id]] = totals.get(SECTOR_OF[security_id], 0.0) + weight
        self.assertTrue(totals)
        for total in totals.values():
            self.assertLessEqual(total, EFFICIENT.max_sector_weight + 1e-12)

    def test_redistribution_never_breaches_the_liquidity_cap(self):
        thin = {name: 20_000_000.0 for name in NAMES}
        _report, weights = _settle(EFFICIENT, adv_dollars=thin)

        cap = 20_000_000.0 * EFFICIENT.max_adv_participation / 100_000.0
        for weight in weights.values():
            self.assertLessEqual(weight, cap + 1e-12)

    def test_redistribution_still_respects_regime_de_risking(self):
        bull, bull_weights = _settle(EFFICIENT)
        bear, bear_weights = _settle(EFFICIENT, regime=Regime.R4_BEAR)

        self.assertLess(bear.target_exposure, bull.target_exposure)
        self.assertLess(fsum(bear_weights.values()), fsum(bull_weights.values()))
        self.assertLessEqual(
            fsum(bear_weights.values()), bear.target_exposure + 1e-12
        )

    def test_redistribution_does_not_defeat_the_turnover_control(self):
        """The post-smoothing enforcement pass must shrink only, never expand."""

        allocator = NormalPortfolioAllocator(EFFICIENT)
        first = allocator.allocate(_request(current_weights={}))
        cold_gross = fsum(first.targets.weights.values())
        settled, settled_weights = _settle(EFFICIENT)

        self.assertLess(cold_gross, fsum(settled_weights.values()))
        self.assertGreater(first.expected_turnover, 0.0)
        self.assertLess(settled.expected_turnover, first.expected_turnover)


class TwoSidedVolatilityTargetTest(unittest.TestCase):
    def test_two_sided_targeting_uses_the_volatility_budget(self):
        cap_only, _ = _settle(
            NormalPortfolioConfig(top_n=20, redistribute_constrained_capital=True)
        )
        two_sided, _ = _settle(
            NormalPortfolioConfig(
                top_n=20,
                redistribute_constrained_capital=True,
                volatility_targeting="two_sided",
            )
        )

        self.assertGreaterEqual(
            two_sided.annualized_volatility, cap_only.annualized_volatility
        )
        self.assertLessEqual(
            two_sided.annualized_volatility,
            two_sided.risk_utilization * EFFICIENT.target_volatility_annual + 1e-12,
        )

    def test_two_sided_targeting_never_exceeds_the_volatility_target(self):
        _report, weights = _settle(EFFICIENT)
        report, _ = _settle(EFFICIENT)

        self.assertLessEqual(
            report.annualized_volatility,
            EFFICIENT.target_volatility_annual + 1e-12,
        )
        self.assertLessEqual(report.risk_utilization, 1.0 + 1e-12)
        self.assertTrue(weights)

    def test_two_sided_targeting_still_de_levers_a_high_volatility_book(self):
        report, weights = _settle(EFFICIENT, asset_scale=0.060)

        self.assertLessEqual(
            report.annualized_volatility,
            EFFICIENT.target_volatility_annual + 1e-12,
        )
        self.assertLess(fsum(weights.values()), report.target_exposure)

    def test_two_sided_targeting_never_leverages_past_gross_exposure(self):
        report, weights = _settle(EFFICIENT, asset_scale=0.0005)

        self.assertLessEqual(fsum(weights.values()), report.target_exposure + 1e-12)


class SparseMeanVarianceFundingTest(unittest.TestCase):
    def test_min_funded_names_funds_the_whole_selection(self):
        _sparse_report, sparse_weights = _settle(
            NormalPortfolioConfig(top_n=20, redistribute_constrained_capital=True)
        )
        _full_report, full_weights = _settle(EFFICIENT)

        self.assertGreaterEqual(len(full_weights), len(sparse_weights))
        self.assertGreaterEqual(len(full_weights), 20)

    def test_zero_min_funded_names_keeps_the_sparse_solution(self):
        allocator = NormalPortfolioAllocator(NormalPortfolioConfig(top_n=20))
        report = allocator.allocate(_request())

        self.assertLessEqual(len(report.targets.weights), 20)


class CapitalEfficiencyConfigTest(unittest.TestCase):
    def test_defaults_preserve_the_legacy_allocator(self):
        config = NormalPortfolioConfig()

        self.assertFalse(config.redistribute_constrained_capital)
        self.assertEqual(config.volatility_targeting, "cap_only")
        self.assertEqual(config.min_funded_names, 0)

    def test_invalid_volatility_targeting_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "volatility_targeting"):
            NormalPortfolioConfig(volatility_targeting="always_on")

    def test_non_boolean_redistribution_flag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "redistribute_constrained_capital"):
            NormalPortfolioConfig(redistribute_constrained_capital="yes")

    def test_negative_min_funded_names_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "min_funded_names"):
            NormalPortfolioConfig(min_funded_names=-1)

    def test_runtime_config_defaults_are_behaviour_preserving(self):
        runtime = NormalRuntimeConfig()

        self.assertFalse(runtime.redistribute_constrained_capital)
        self.assertEqual(runtime.volatility_targeting, "cap_only")
        self.assertEqual(runtime.min_funded_names, 0)

    def test_runtime_config_rejects_an_unknown_targeting_mode(self):
        with self.assertRaisesRegex(ValueError, "normal.volatility_targeting"):
            NormalRuntimeConfig(volatility_targeting="martingale")

    def test_runtime_config_flows_into_the_portfolio_config(self):
        runtime = NormalRuntimeConfig(
            redistribute_constrained_capital=True,
            volatility_targeting="two_sided",
            min_funded_names=20,
        )
        risk = _RiskStub()
        config = NormalPortfolioConfig.from_runtime(runtime, risk)

        self.assertTrue(config.redistribute_constrained_capital)
        self.assertEqual(config.volatility_targeting, "two_sided")
        self.assertEqual(config.min_funded_names, 20)


class FrozenH1IsolationTest(unittest.TestCase):
    """The H1 protocol hash does not cover these knobs, so the runner must."""

    def setUp(self):
        from quantbot.normal import (
            NormalFactorExperiment,
            PointInTimeNormalRequestProvider,
        )

        self.baseline = PointInTimeNormalRequestProvider(_Port())
        self.candidate = PointInTimeNormalRequestProvider(
            _Port(), experiment=NormalFactorExperiment(include_quality=True)
        )

    def _run(self, config):
        from quantbot.normal import run_normal_h1_experiment

        return run_normal_h1_experiment(
            baseline_provider=self.baseline,
            candidate_provider=self.candidate,
            sessions=(object(),),
            portfolio_config=config,
        )

    def test_h1_rejects_redistributed_capital(self):
        with self.assertRaisesRegex(
            ValueError, "redistribute_constrained_capital is frozen"
        ):
            self._run(
                NormalPortfolioConfig(top_n=20, redistribute_constrained_capital=True)
            )

    def test_h1_rejects_two_sided_volatility_targeting(self):
        with self.assertRaisesRegex(ValueError, "volatility_targeting is frozen"):
            self._run(
                NormalPortfolioConfig(top_n=20, volatility_targeting="two_sided")
            )

    def test_h1_rejects_a_minimum_funded_name_count(self):
        with self.assertRaisesRegex(ValueError, "min_funded_names is frozen"):
            self._run(NormalPortfolioConfig(top_n=20, min_funded_names=20))


class _Port:
    def monthly_returns(self, security_master):
        raise AssertionError("not reached")

    def asset_return_history(self, security_master, *, lookback_sessions):
        return {}

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        return {}

    def market_betas(self, security_master, *, lookback_sessions):
        return {}

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        return {}

    def regime_context(self, security_master):
        raise AssertionError("not reached")


class _RiskStub:
    max_single_stock_weight = 0.05
    max_sector_weight = 0.25
    max_market_beta_exposure = 1.0
    max_portfolio_vol_annual = 0.15


if __name__ == "__main__":
    unittest.main()
