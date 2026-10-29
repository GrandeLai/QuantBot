import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.analytics import (
    CrossBacktestValidationGate,
    PerformanceSummary,
    StrategyAcceptanceGate,
    performance_summary,
)
from quantbot.config import SystemConfig
from quantbot.data.failover import ProviderFailover
from quantbot.overfit import OverfitGuard, RegimeValidationResult
from quantbot.regime import Regime
from quantbot.risk_controls import LiquidityShockMonitor, PDTProtector, RegimeTurnoverLimiter
from quantbot.trading import BrokerAccountSnapshot, MarginStatus


class StockBotGuardsAnalyticsTest(unittest.TestCase):
    def test_provider_failover_uses_backup_when_primary_raises(self):
        calls = []

        def primary():
            calls.append("primary")
            raise RuntimeError("alpaca outage")

        def backup():
            calls.append("backup")
            return [{"ticker": "AAPL"}]

        result = ProviderFailover([primary, backup]).fetch()

        self.assertEqual(result.records, [{"ticker": "AAPL"}])
        self.assertEqual(result.provider_index, 1)
        self.assertEqual(calls, ["primary", "backup"])

    def test_overfit_guard_requires_two_year_holdout_and_three_positive_regimes(self):
        guard = OverfitGuard(min_holdout_years=2, min_positive_regimes=3)
        valid = guard.evaluate(
            holdout_years=2.0,
            tuned_on_holdout=False,
            regime_results=[
                RegimeValidationResult("R1", 0.03),
                RegimeValidationResult("R2", 0.02),
                RegimeValidationResult("R3", 0.01),
                RegimeValidationResult("R4", -0.01),
            ],
            parameter_count=2,
            base_ic_threshold=0.025,
            observed_ic=0.03,
        )
        invalid = guard.evaluate(
            holdout_years=1.0,
            tuned_on_holdout=True,
            regime_results=[RegimeValidationResult("R1", 0.03)],
            parameter_count=12,
            base_ic_threshold=0.025,
            observed_ic=0.03,
        )

        self.assertTrue(valid.passed)
        self.assertFalse(invalid.passed)
        self.assertIn("holdout", invalid.reasons)
        self.assertIn("data_snooping", invalid.reasons)
        self.assertIn("regime_coverage", invalid.reasons)
        self.assertIn("complexity_adjusted_ic", invalid.reasons)

    def test_pdt_compatibility_protector_uses_broker_reported_facts(self):
        snapshot = BrokerAccountSnapshot(
            broker="test",
            observed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            equity=20_000,
            buying_power=10_000,
            day_trade_buying_power=5_000,
            trading_blocked=False,
            shorting_enabled=False,
            margin_status=MarginStatus.MARGIN,
        )
        protector = PDTProtector(snapshot)

        self.assertTrue(protector.can_day_trade(5_000))
        self.assertFalse(protector.can_day_trade(5_001))

    def test_liquidity_shock_monitor_flags_wide_spread_and_large_adv_usage(self):
        monitor = LiquidityShockMonitor(max_spread_bps=20, max_adv_participation=0.01)

        self.assertTrue(monitor.is_shocked(bid=99.80, ask=100.20, order_notional=2_000_000, adv_dollars=100_000_000))
        self.assertTrue(monitor.is_shocked(bid=99.99, ask=100.01, order_notional=2_000_000, adv_dollars=100_000_000))
        self.assertFalse(monitor.is_shocked(bid=99.99, ask=100.01, order_notional=500_000, adv_dollars=100_000_000))

    def test_regime_turnover_limiter_pauses_crisis_and_stricts_bear_turnover(self):
        limiter = RegimeTurnoverLimiter()

        self.assertTrue(limiter.allow(Regime.R1_BULL_LOW_VOL, proposed_turnover=0.80))
        self.assertFalse(limiter.allow(Regime.R4_BEAR, proposed_turnover=0.35))
        self.assertTrue(limiter.allow(Regime.R4_BEAR, proposed_turnover=0.20))
        self.assertFalse(limiter.allow(Regime.R5_CRISIS, proposed_turnover=0.01))

    def test_performance_summary_reports_sharpe_drawdown_and_excess_alpha(self):
        summary = performance_summary(
            daily_returns=[0.01, 0.01, -0.005, 0.02, -0.01],
            benchmark_returns=[0.005, 0.004, -0.002, 0.008, -0.004],
            periods_per_year=252,
        )

        self.assertGreater(summary.annual_return, summary.benchmark_annual_return)
        self.assertGreater(summary.excess_alpha_annual, 0)
        self.assertGreater(summary.sharpe, 0)
        self.assertGreaterEqual(summary.max_drawdown, 0)

    def test_performance_summary_rejects_non_finite_returns(self):
        for invalid_return in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid_return=invalid_return):
                with self.assertRaisesRegex(ValueError, "finite"):
                    performance_summary(
                        daily_returns=[0.01, invalid_return],
                        benchmark_returns=[0.0, 0.0],
                    )

    def test_performance_summary_rejects_non_finite_benchmark_returns(self):
        with self.assertRaisesRegex(ValueError, "finite"):
            performance_summary(
                daily_returns=[0.01, 0.02],
                benchmark_returns=[0.0, float("nan")],
            )

    def test_strategy_acceptance_gate_enforces_design_backtest_targets_and_turnover_costs(self):
        gate = StrategyAcceptanceGate(
            min_excess_alpha_annual=0.05,
            min_sharpe=1.0,
            max_drawdown=0.20,
            max_annual_turnover=4.0,
            max_annual_cost_drag=0.006,
        )
        accepted = gate.evaluate(
            summary=type(
                "Summary",
                (),
                {
                    "excess_alpha_annual": 0.061,
                    "sharpe": 1.12,
                    "max_drawdown": 0.18,
                },
            )(),
            annual_turnover=3.5,
            two_way_cost_rate=0.0015,
            summary_includes_costs=True,
        )
        rejected = gate.evaluate(
            summary=type(
                "Summary",
                (),
                {
                    "excess_alpha_annual": 0.03,
                    "sharpe": 0.8,
                    "max_drawdown": 0.25,
                },
            )(),
            annual_turnover=5.0,
            two_way_cost_rate=0.002,
        )

        self.assertTrue(accepted.passed)
        self.assertEqual(accepted.reasons, [])
        self.assertFalse(rejected.passed)
        self.assertIn("excess_alpha", rejected.reasons)
        self.assertIn("sharpe", rejected.reasons)
        self.assertIn("drawdown", rejected.reasons)
        self.assertIn("turnover", rejected.reasons)
        self.assertIn("cost_drag", rejected.reasons)

    def test_strategy_acceptance_gate_rejects_non_finite_summary_metrics(self):
        gate = StrategyAcceptanceGate()
        for field_name in (
            "annual_return",
            "benchmark_annual_return",
            "excess_alpha_annual",
            "annual_volatility",
            "sharpe",
            "max_drawdown",
        ):
            values = {
                "annual_return": 0.10,
                "benchmark_annual_return": 0.04,
                "excess_alpha_annual": 0.06,
                "annual_volatility": 0.10,
                "sharpe": 1.2,
                "max_drawdown": 0.10,
            }
            values[field_name] = float("nan")

            with self.subTest(field_name=field_name):
                decision = gate.evaluate(
                    PerformanceSummary(**values),
                    annual_turnover=1.0,
                    two_way_cost_rate=0.001,
                    summary_includes_costs=True,
                )

                self.assertFalse(decision.passed)
                self.assertIn("non_finite_summary", decision.reasons)

    def test_strategy_acceptance_gate_rejects_negative_drawdown_turnover_and_cost(self):
        gate = StrategyAcceptanceGate()
        summary = PerformanceSummary(0.10, 0.04, 0.06, 0.10, 1.2, -0.01)

        decision = gate.evaluate(
            summary,
            annual_turnover=-1.0,
            two_way_cost_rate=-0.001,
            summary_includes_costs=True,
        )

        self.assertFalse(decision.passed)
        self.assertIn("negative_drawdown", decision.reasons)
        self.assertIn("negative_turnover", decision.reasons)
        self.assertIn("negative_cost_rate", decision.reasons)

    def test_strategy_acceptance_gate_rejects_non_finite_turnover_and_cost(self):
        gate = StrategyAcceptanceGate()
        summary = PerformanceSummary(0.10, 0.04, 0.06, 0.10, 1.2, 0.10)

        turnover = gate.evaluate(summary, float("inf"), 0.001, summary_includes_costs=True)
        cost = gate.evaluate(summary, 1.0, float("nan"), summary_includes_costs=True)

        self.assertFalse(turnover.passed)
        self.assertIn("non_finite_turnover", turnover.reasons)
        self.assertFalse(cost.passed)
        self.assertIn("non_finite_cost_rate", cost.reasons)

    def test_strategy_gate_rejects_gross_summary_when_costs_hide_net_risk_metrics(self):
        gate = StrategyAcceptanceGate()
        summary = PerformanceSummary(
            annual_return=0.20,
            benchmark_annual_return=0.10,
            excess_alpha_annual=0.10,
            annual_volatility=0.10,
            sharpe=1.5,
            max_drawdown=0.10,
        )

        decision = gate.evaluate(
            summary,
            annual_turnover=1.0,
            two_way_cost_rate=0.001,
            summary_includes_costs=False,
        )

        self.assertFalse(decision.passed)
        self.assertEqual(decision.reasons, ["gross_summary_not_admissible"])

    def test_strategy_gate_rejects_gross_alpha_that_falls_below_target_after_costs(self):
        gate = StrategyAcceptanceGate(
            min_excess_alpha_annual=0.05,
            min_sharpe=1.0,
            max_drawdown=0.20,
            max_annual_turnover=4.0,
            max_annual_cost_drag=0.006,
        )
        summary = PerformanceSummary(
            annual_return=0.10,
            benchmark_annual_return=0.046,
            excess_alpha_annual=0.054,
            annual_volatility=0.10,
            sharpe=1.2,
            max_drawdown=0.10,
        )

        decision = gate.evaluate(summary, annual_turnover=4.0, two_way_cost_rate=0.0015)

        self.assertFalse(decision.passed)
        self.assertAlmostEqual(decision.net_excess_alpha_annual, 0.048)
        self.assertIn("excess_alpha", decision.reasons)
        self.assertIn("gross_summary_not_admissible", decision.reasons)

    def test_strategy_gate_does_not_double_deduct_from_summary_that_includes_costs(self):
        gate = StrategyAcceptanceGate(max_annual_cost_drag=0.006)
        summary = PerformanceSummary(
            annual_return=0.10,
            benchmark_annual_return=0.046,
            excess_alpha_annual=0.054,
            annual_volatility=0.10,
            sharpe=1.2,
            max_drawdown=0.10,
        )

        decision = gate.evaluate(
            summary,
            annual_turnover=4.0,
            two_way_cost_rate=0.0015,
            summary_includes_costs=True,
        )

        self.assertTrue(decision.passed)
        self.assertAlmostEqual(decision.net_excess_alpha_annual, 0.054)

    def test_strategy_acceptance_gate_from_system_config_uses_production_target_sharpe(self):
        gate = StrategyAcceptanceGate.from_system_config(SystemConfig.default())

        decision = gate.evaluate(
            summary=type(
                "Summary",
                (),
                {
                    "excess_alpha_annual": 0.061,
                    "sharpe": 1.10,
                    "max_drawdown": 0.18,
                },
            )(),
            annual_turnover=3.5,
            two_way_cost_rate=0.0015,
        )

        self.assertFalse(decision.passed)
        self.assertIn("sharpe", decision.reasons)
        self.assertEqual(gate.min_excess_alpha_annual, 0.05)
        self.assertEqual(gate.min_sharpe, 1.2)

    def test_cross_backtest_validation_compares_qlib_against_quantconnect_results(self):
        gate = CrossBacktestValidationGate(max_alpha_deviation=0.01, max_sharpe_deviation=0.20)

        accepted = gate.evaluate(
            primary={"excess_alpha_annual": 0.06, "sharpe": 1.10},
            secondary={"excess_alpha_annual": 0.055, "sharpe": 1.00},
        )
        rejected = gate.evaluate(
            primary={"excess_alpha_annual": 0.06, "sharpe": 1.10},
            secondary={"excess_alpha_annual": 0.03, "sharpe": 0.60},
        )

        self.assertTrue(accepted.passed)
        self.assertFalse(rejected.passed)
        self.assertIn("alpha_deviation", rejected.reasons)
        self.assertIn("sharpe_deviation", rejected.reasons)

    def test_cross_backtest_validation_rejects_non_finite_metrics(self):
        decision = CrossBacktestValidationGate().evaluate(
            primary={"excess_alpha_annual": float("nan"), "sharpe": 1.10},
            secondary={"excess_alpha_annual": 0.055, "sharpe": float("inf")},
        )

        self.assertFalse(decision.passed)
        self.assertIn("non_finite_metrics", decision.reasons)


if __name__ == "__main__":
    unittest.main()
