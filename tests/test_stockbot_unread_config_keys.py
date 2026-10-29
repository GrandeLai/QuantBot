"""Configuration keys that were declared, validated, hashed -- and never read.

An audit of every canonical key against its consumers found several that no
strategy code touched.  A key that cannot change behaviour is worse than a
missing one: it describes a control the operator does not actually have.

Each fix keeps its default behaviour-preserving, so existing runs and frozen
experiment identities are unaffected, and each is pinned here.
"""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config_loader import (
    DataRuntimeConfig,
    DayTradeRuntimeConfig,
    ExecutionRuntimeConfig,
    NormalRuntimeConfig,
    PortfolioRuntimeConfig,
    RiskRuntimeConfig,
    RuntimeConfig,
    ValidationRuntimeConfig,
    load_runtime_config,
)
from quantbot.daytrade import DayTradeBacktester, DayTradeConfig, MinuteBar
from quantbot.normal import NormalBacktestSession, PointInTimeNormalBacktester
from quantbot.trading import PointInTimeSnapshot, TargetPortfolio


UTC = timezone.utc
REPO = Path(__file__).resolve().parents[1]


def _config(*, execution=None, portfolio=None, normal=None):
    return RuntimeConfig(
        DataRuntimeConfig(),
        normal or NormalRuntimeConfig(),
        DayTradeRuntimeConfig(),
        portfolio or PortfolioRuntimeConfig(),
        RiskRuntimeConfig(),
        execution or ExecutionRuntimeConfig(),
        ValidationRuntimeConfig(),
    )


class _Provider:
    def __init__(self, weights):
        self.weights = dict(weights)

    def build_request(self, snapshot, current_weights):
        del current_weights
        return (snapshot.observed_at, self.weights)


class _Allocator:
    def allocate(self, request):
        return SimpleNamespace(targets=TargetPortfolio(request[0], request[1]))


def _normal_sessions():
    days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
    return (
        NormalBacktestSession(
            days[0],
            {"AAA": 100.0},
            {"AAA": 100.0},
            signal_snapshot=PointInTimeSnapshot(
                observed_at=datetime(2025, 1, 2, 21, tzinfo=UTC)
            ),
        ),
        NormalBacktestSession(days[1], {"AAA": 100.0}, {"AAA": 100.0}),
        NormalBacktestSession(days[2], {"AAA": 100.0}, {"AAA": 100.0}),
    )


class SlippageIsAppliedTest(unittest.TestCase):
    """`execution.slippage_bps_per_side` used to have no consumer at all."""

    def _run(self, **changes):
        return PointInTimeNormalBacktester(
            _Provider({"AAA": 1.0}), _Allocator(), **changes
        ).run(_normal_sessions())

    def test_zero_slippage_reproduces_the_commission_only_cost(self):
        without = self._run(cost_bps_per_side=5.0)
        explicit_zero = self._run(cost_bps_per_side=5.0, slippage_bps_per_side=0.0)

        self.assertAlmostEqual(without.fees, explicit_zero.fees)
        self.assertAlmostEqual(without.final_equity, explicit_zero.final_equity)

    def test_slippage_adds_to_the_per_side_cost(self):
        cheap = self._run(cost_bps_per_side=5.0)
        dear = self._run(cost_bps_per_side=5.0, slippage_bps_per_side=5.0)

        # Not exactly 2x: a higher cost rate shrinks the affordable target
        # base, so slightly less notional is traded.
        self.assertGreater(dear.fees, cheap.fees * 1.99)
        self.assertLessEqual(dear.fees, cheap.fees * 2.0)
        self.assertLess(dear.final_equity, cheap.final_equity)

    def test_slippage_is_equivalent_to_the_same_commission(self):
        split = self._run(cost_bps_per_side=3.0, slippage_bps_per_side=4.0)
        combined = self._run(cost_bps_per_side=7.0)

        self.assertAlmostEqual(split.fees, combined.fees)

    def test_negative_slippage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "slippage_bps_per_side"):
            self._run(slippage_bps_per_side=-1.0)

    def test_runtime_exposes_the_total_per_side_cost(self):
        config = _config(execution=ExecutionRuntimeConfig(slippage_bps_per_side=3.0))

        self.assertEqual(config.normal_cost_bps_per_side(), 8.0)
        self.assertEqual(config.daytrade_cost_bps_per_side(), 8.0)

    def test_shipped_example_adds_no_slippage(self):
        config = load_runtime_config(REPO / "config" / "quantbot.research.toml")

        self.assertEqual(config.execution.slippage_bps_per_side, 0.0)
        self.assertEqual(
            config.normal_cost_bps_per_side(), config.normal.cost_bps_per_side
        )


class DayTradeSlippageTest(unittest.TestCase):
    def _bars(self):
        start = datetime(2024, 1, 2, 9, 30, tzinfo=timezone(timedelta(hours=-5)))
        prices = [100.0] * 15 + [102.0] * 5 + [102.0 + 0.01 * i for i in range(60)]
        return [
            MinuteBar("AAA", start + timedelta(minutes=index), price, price * 1.001,
                      price * 0.999, price, 50_000.0)
            for index, price in enumerate(prices)
        ]

    def test_slippage_worsens_fills_like_commissions(self):
        bars = self._bars()
        cheap = DayTradeBacktester(DayTradeConfig(cost_bps=5.0)).run(bars)
        dear = DayTradeBacktester(
            DayTradeConfig(cost_bps=5.0, slippage_bps=5.0)
        ).run(bars)
        combined = DayTradeBacktester(DayTradeConfig(cost_bps=10.0)).run(bars)

        self.assertEqual(cheap.trade_count, dear.trade_count)
        self.assertLess(dear.ending_equity, cheap.ending_equity)
        self.assertAlmostEqual(dear.ending_equity, combined.ending_equity)

    def test_negative_slippage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "slippage_bps"):
            DayTradeBacktester(DayTradeConfig(slippage_bps=-1.0))

    def test_from_runtime_reads_the_execution_section(self):
        config = DayTradeConfig.from_runtime(
            DayTradeRuntimeConfig(),
            SimpleNamespace(initial_cash=100_000.0, daytrade_fraction=0.10),
            SimpleNamespace(max_daytrade_drawdown=0.05),
            ExecutionRuntimeConfig(slippage_bps_per_side=4.0),
        )

        self.assertEqual(config.slippage_bps, 4.0)

    def test_from_runtime_without_execution_keeps_zero_slippage(self):
        config = DayTradeConfig.from_runtime(
            DayTradeRuntimeConfig(),
            SimpleNamespace(initial_cash=100_000.0, daytrade_fraction=0.10),
            SimpleNamespace(max_daytrade_drawdown=0.05),
        )

        self.assertEqual(config.slippage_bps, 0.0)


class NormalSleeveCapitalTest(unittest.TestCase):
    """`portfolio.normal_fraction` had no consumer; only DayTrade scaled."""

    def test_the_split_is_now_readable_for_both_sleeves(self):
        config = _config()

        self.assertAlmostEqual(config.normal_capital(), 90_000.0)
        self.assertAlmostEqual(config.daytrade_capital(), 10_000.0)
        self.assertAlmostEqual(
            config.normal_capital() + config.daytrade_capital(),
            config.portfolio.initial_cash,
        )

    def test_the_sleeves_track_a_different_split(self):
        config = _config(
            portfolio=PortfolioRuntimeConfig(
                initial_cash=250_000.0, normal_fraction=0.70, daytrade_fraction=0.30
            )
        )

        self.assertAlmostEqual(config.normal_capital(), 175_000.0)
        self.assertAlmostEqual(config.daytrade_capital(), 75_000.0)

    def test_daytrade_execution_config_agrees_with_the_accessor(self):
        config = _config()
        execution = DayTradeConfig.from_runtime(
            config.daytrade, config.portfolio, config.risk
        )

        self.assertAlmostEqual(execution.daytrade_capital, config.daytrade_capital())


class ResidualSkipMonthsTest(unittest.TestCase):
    """`normal.residual_skip_months` was hardcoded to 1 inside the provider."""

    def _provider(self, **changes):
        from quantbot.normal import PointInTimeNormalRequestProvider

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

        return PointInTimeNormalRequestProvider(_Port(), **changes)

    def test_the_provider_default_matches_the_declared_default(self):
        self.assertEqual(self._provider().residual_skip_months, 1)
        self.assertEqual(NormalRuntimeConfig().residual_skip_months, 1)

    def test_the_provider_accepts_a_different_skip(self):
        self.assertEqual(self._provider(residual_skip_months=0).residual_skip_months, 0)

    def test_a_negative_skip_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "residual_skip_months"):
            self._provider(residual_skip_months=-1)

    def test_the_frozen_experiments_still_pin_it_at_one(self):
        from quantbot.normal import (
            NormalFactorExperiment,
            run_normal_h1_experiment,
            run_normal_h2_experiment,
        )

        drifted = self._provider(residual_skip_months=2)
        candidate = self._provider(
            residual_skip_months=2, experiment=NormalFactorExperiment(include_quality=True)
        )

        with self.assertRaisesRegex(ValueError, "residual_skip_months is frozen"):
            run_normal_h1_experiment(
                baseline_provider=drifted,
                candidate_provider=candidate,
                sessions=(object(),),
            )
        with self.assertRaisesRegex(ValueError, "residual_skip_months is frozen"):
            run_normal_h2_experiment(provider=drifted, sessions=(object(),))


class ExecutionPriceChoiceTest(unittest.TestCase):
    """The config offered a `vwap` execution mode the code does not implement."""

    def test_only_the_implemented_mode_is_accepted(self):
        self.assertEqual(NormalRuntimeConfig().execution_price, "next_open")
        with self.assertRaisesRegex(ValueError, "normal.execution_price"):
            NormalRuntimeConfig(execution_price="vwap")


if __name__ == "__main__":
    unittest.main()
