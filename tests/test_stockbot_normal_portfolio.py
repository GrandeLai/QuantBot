import math
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data import (
    DataQualityTier,
    SecurityClassification,
    SecurityIdentity,
    SecurityMasterSnapshot,
    canonical_security_master_snapshot_hash,
)
from quantbot.factors import FactorSleeve
from quantbot.config_loader import NormalRuntimeConfig, RiskRuntimeConfig
from quantbot.normal import (
    NormalAllocationRequest,
    NormalPortfolioAllocator,
    NormalPortfolioConfig,
    NormalRegimeContext,
    PointInTimeNormalStrategy,
)
from quantbot.regime import Regime
from quantbot.trading import PointInTimeSnapshot, TargetPortfolio


UTC = timezone.utc


def master(as_of: datetime, sectors: dict[str, str]) -> SecurityMasterSnapshot:
    identities = {
        security_id: SecurityIdentity(
            security_id, date(2000, 1, 1), None, datetime(2000, 1, 1, tzinfo=UTC)
        )
        for security_id in sectors
    }
    classifications = {
        security_id: SecurityClassification(
            security_id,
            security_id,
            "NYSE",
            sector,
            date(2000, 1, 1),
            None,
            datetime(2000, 1, 1, tzinfo=UTC),
        )
        for security_id, sector in sectors.items()
    }
    return SecurityMasterSnapshot(
        as_of=as_of,
        effective_date=as_of.date(),
        effective_date_timezone="UTC",
        source_dataset_hash="a" * 64,
        source_quality_tier=DataQualityTier.DEVELOPMENT_FREE,
        universes=("research",),
        security_ids=tuple(sectors),
        identity_by_security_id=identities,
        classification_by_security_id=classifications,
        ticker_by_security_id={key: key for key in sectors},
        latest_fundamentals={},
        latest_market_caps={},
        known_corporate_actions=(),
        effective_corporate_actions=(),
    )


def sleeve(snapshot, name, scores):
    return FactorSleeve(
        name=name,
        observed_at=snapshot.as_of,
        effective_date=snapshot.effective_date,
        effective_date_timezone=snapshot.effective_date_timezone,
        security_master_hash=canonical_security_master_snapshot_hash(snapshot),
        scores=scores,
        missing_security_ids=tuple(sorted(set(snapshot.security_ids).difference(scores))),
        input_dataset_hashes=(snapshot.source_dataset_hash,),
    )


def history(scale: float, phase: int = 0, count: int = 24):
    pattern = (-1.0, -0.4, 0.2, 0.8, 1.3, -0.7)
    return tuple(scale * pattern[(index + phase) % len(pattern)] for index in range(count))


class NormalPortfolioAllocatorTest(unittest.TestCase):
    def setUp(self):
        self.as_of = datetime(2025, 4, 15, 12, tzinfo=UTC)
        self.master = master(
            self.as_of,
            {"A": "Tech", "B": "Tech", "C": "Finance", "D": "Finance"},
        )
        self.momentum = sleeve(
            self.master,
            "residual_momentum_12_1_neutral",
            {"A": 2.0, "B": 1.0, "C": -1.0, "D": -2.0},
        )
        self.quality = sleeve(
            self.master,
            "quality_profitability_neutral",
            {"A": -1.0, "B": 0.0, "C": 2.0, "D": 1.0},
        )
        self.asset_history = {
            "A": history(0.020, 0),
            "B": history(0.015, 1),
            "C": history(0.010, 2),
            "D": history(0.012, 3),
        }

    def request(self, **changes):
        values = dict(
            security_master=self.master,
            sleeves=(self.momentum, self.quality),
            sleeve_confidences={
                self.momentum.name: 0.8,
                self.quality.name: 0.6,
            },
            sleeve_return_history={
                self.momentum.name: history(0.010, 0),
                self.quality.name: history(0.020, 2),
            },
            asset_return_history=self.asset_history,
            current_weights={},
            market_betas={key: 1.0 for key in self.master.security_ids},
            adv_dollars={key: 10_000_000.0 for key in self.master.security_ids},
            capital=100_000.0,
            regime=NormalRegimeContext(Regime.R1_BULL_LOW_VOL),
        )
        values.update(changes)
        return NormalAllocationRequest(**values)

    def test_sleeve_risk_allocation_uses_confidence_inverse_vol_and_shrinkage(self):
        config = NormalPortfolioConfig(
            top_n=4,
            target_gross_exposure=0.8,
            max_single_stock_weight=1.0,
            max_sector_weight=1.0,
            max_market_beta_exposure=10.0,
            target_volatility_annual=10.0,
            max_adv_participation=1.0,
            covariance_shrinkage=0.5,
        )

        report = NormalPortfolioAllocator(config).allocate(self.request())

        self.assertGreater(
            report.sleeve_weights[self.momentum.name],
            report.sleeve_weights[self.quality.name],
        )
        self.assertAlmostEqual(sum(report.sleeve_weights.values()), 1.0)
        self.assertLessEqual(sum(report.targets.weights.values()), 0.8 + 1e-12)
        self.assertEqual(set(report.combined_scores), set(self.master.security_ids))

    def test_portfolio_config_is_derived_from_canonical_runtime_sections(self):
        config = NormalPortfolioConfig.from_runtime(
            NormalRuntimeConfig(
                top_n=7,
                covariance_shrinkage=0.35,
                no_trade_band=0.004,
                max_adv_participation=0.02,
            ),
            RiskRuntimeConfig(
                max_single_stock_weight=0.04,
                max_sector_weight=0.20,
                max_portfolio_vol_annual=0.12,
                max_market_beta_exposure=0.8,
            ),
        )

        self.assertEqual(config.top_n, 7)
        self.assertEqual(config.covariance_shrinkage, 0.35)
        self.assertEqual(config.max_single_stock_weight, 0.04)
        self.assertEqual(config.max_market_beta_exposure, 0.8)

    def test_high_volatility_rebound_reduces_momentum_and_total_exposure(self):
        config = NormalPortfolioConfig(
            top_n=4,
            max_single_stock_weight=1.0,
            max_sector_weight=1.0,
            max_market_beta_exposure=10.0,
            target_volatility_annual=10.0,
            max_adv_participation=1.0,
        )
        allocator = NormalPortfolioAllocator(config)

        normal = allocator.allocate(self.request())
        rebound = allocator.allocate(
            self.request(
                regime=NormalRegimeContext(
                    Regime.R2_BULL_HIGH_VOL,
                    high_volatility_rebound=True,
                )
            )
        )

        self.assertLess(
            rebound.sleeve_weights[self.momentum.name],
            normal.sleeve_weights[self.momentum.name],
        )
        self.assertLess(
            sum(rebound.targets.weights.values()),
            sum(normal.targets.weights.values()),
        )

    def test_all_position_constraints_scale_down_without_hidden_leverage(self):
        config = NormalPortfolioConfig(
            top_n=4,
            target_gross_exposure=1.0,
            max_single_stock_weight=0.25,
            max_sector_weight=0.30,
            max_market_beta_exposure=0.40,
            target_volatility_annual=0.15,
            max_adv_participation=0.01,
            periods_per_year=252,
        )
        request = self.request(
            market_betas={"A": 1.5, "B": 1.2, "C": 0.8, "D": 0.6},
            adv_dollars={"A": 100_000.0, "B": 3_000_000.0, "C": 3_000_000.0, "D": 3_000_000.0},
        )

        report = NormalPortfolioAllocator(config).allocate(request)
        weights = report.targets.weights

        self.assertLessEqual(max(weights.values(), default=0.0), 0.25 + 1e-12)
        self.assertLessEqual(weights.get("A", 0.0), 0.01 + 1e-12)
        for sector in {"Tech", "Finance"}:
            self.assertLessEqual(
                sum(
                    weights.get(security_id, 0.0)
                    for security_id in self.master.security_ids
                    if self.master.classification_by_security_id[security_id].sector == sector
                ),
                0.30 + 1e-12,
            )
        beta = sum(weights.get(key, 0.0) * request.market_betas[key] for key in weights)
        self.assertLessEqual(abs(beta), 0.40 + 1e-12)
        self.assertLessEqual(report.annualized_volatility, 0.15 + 1e-12)
        self.assertLessEqual(sum(weights.values()), 1.0 + 1e-12)

    def test_hysteresis_and_no_trade_band_preserve_feasible_holdings(self):
        ranking = sleeve(
            self.master,
            "quality_profitability_neutral",
            {"B": 3.0, "C": 2.0, "A": 1.0, "D": 0.0},
        )
        config = NormalPortfolioConfig(
            top_n=2,
            exit_rank_buffer=1,
            no_trade_band=1.0,
            target_gross_exposure=0.4,
            max_single_stock_weight=1.0,
            max_sector_weight=1.0,
            max_market_beta_exposure=10.0,
            target_volatility_annual=10.0,
            max_adv_participation=1.0,
        )
        request = self.request(
            sleeves=(ranking,),
            sleeve_confidences={ranking.name: 1.0},
            sleeve_return_history={ranking.name: history(0.01)},
            current_weights={"A": 0.2, "B": 0.2},
        )

        report = NormalPortfolioAllocator(config).allocate(request)

        self.assertEqual(report.selected_security_ids, ("B", "A"))
        self.assertEqual(report.targets.weights, {"A": 0.2, "B": 0.2})
        self.assertEqual(report.expected_turnover, 0.0)

    def test_missing_asset_history_fails_closed_and_request_is_immutable(self):
        histories = dict(self.asset_history)
        histories.pop("A")
        request = self.request(
            asset_return_history=histories,
            current_weights={"A": 0.2},
        )

        report = NormalPortfolioAllocator().allocate(request)

        self.assertNotIn("A", report.targets.weights)
        self.assertIn("A:missing_asset_return_history", report.exclusions)
        with self.assertRaises(TypeError):
            request.current_weights["A"] = 1.0

    def test_daily_strategy_uses_shared_target_portfolio_contract_and_previous_targets(self):
        class Provider:
            def __init__(self, outer):
                self.outer = outer
                self.seen_current = []

            def build_request(self, snapshot, current_weights):
                self.seen_current.append(dict(current_weights))
                return self.outer.request(current_weights=current_weights)

        provider = Provider(self)
        strategy = PointInTimeNormalStrategy(
            provider,
            NormalPortfolioAllocator(
                NormalPortfolioConfig(
                    top_n=4,
                    max_single_stock_weight=1.0,
                    max_sector_weight=1.0,
                    max_market_beta_exposure=10.0,
                    target_volatility_annual=10.0,
                    max_adv_participation=1.0,
                )
            ),
        )
        snapshot = PointInTimeSnapshot(
            observed_at=self.as_of,
            security_master=self.master,
        )

        first = strategy.generate_targets(snapshot)
        cached = strategy.generate_targets(snapshot)
        next_event = PointInTimeSnapshot(
            observed_at=self.as_of,
            security_master=self.master,
        )
        strategy.generate_targets(next_event)

        self.assertIsInstance(first, TargetPortfolio)
        self.assertEqual(first.observed_at, self.as_of)
        self.assertEqual(cached, first)
        self.assertEqual(provider.seen_current[0], {})
        self.assertEqual(provider.seen_current[1], dict(first.weights))


if __name__ == "__main__":
    unittest.main()
