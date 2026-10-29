import sys
import inspect
import tempfile
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.backtest import (
    compute_group_ic,
    compute_ic_series,
    evaluate_alpha_subset_acceptance,
    evaluate_oos_backtest_window,
    evaluate_regime_ic_differentiation,
    quintile_backtest,
    summarize_ic,
    turnover_rate,
)
from quantbot.dashboard import DashboardSnapshot
from quantbot.data.alignment import align_fundamentals_to_signals
from quantbot.execution import AlpacaBrokerAdapter, OrderJournal
from quantbot.oos_backtest import MomentumOOSBacktestConfig, run_momentum_oos_backtest
from quantbot.optimization import inverse_volatility_weights, mean_variance_score_weights
from quantbot.portfolio import PortfolioRiskScaler, apply_hedge_overlay, estimate_portfolio_capacity
from quantbot.regime import Regime


class StockBotBacktestExecutionTest(unittest.TestCase):
    def test_fundamental_alignment_uses_release_date_plus_two_day_lag(self):
        signals = [
            {"date": date(2024, 5, 2), "ticker": "AAPL"},
            {"date": date(2024, 5, 3), "ticker": "AAPL"},
            {"date": date(2024, 5, 6), "ticker": "AAPL"},
        ]
        fundamentals = [
            {
                "ticker": "AAPL",
                "release_date": date(2024, 5, 1),
                "eps_revision": 0.08,
            }
        ]

        aligned = align_fundamentals_to_signals(signals, fundamentals, lag_days=2)

        self.assertNotIn("eps_revision", aligned[0])
        self.assertEqual(aligned[1]["eps_revision"], 0.08)
        self.assertEqual(aligned[2]["eps_revision"], 0.08)

    def test_ic_series_summary_and_quintile_backtest_report_factor_quality(self):
        factor_scores = {
            date(2024, 1, 2): {"A": 5, "B": 4, "C": 3, "D": 2, "E": 1},
            date(2024, 1, 3): {"A": 1, "B": 2, "C": 3, "D": 4, "E": 5},
        }
        forward_returns = {
            date(2024, 1, 2): {"A": 0.05, "B": 0.03, "C": 0.0, "D": -0.01, "E": -0.03},
            date(2024, 1, 3): {"A": -0.02, "B": -0.01, "C": 0.0, "D": 0.02, "E": 0.04},
        }

        ic_series = compute_ic_series(factor_scores, forward_returns)
        summary = summarize_ic("quality_momentum", ic_series)
        backtest = quintile_backtest(factor_scores, forward_returns, quantiles=5)

        self.assertEqual(len(ic_series), 2)
        self.assertGreater(summary.ic_mean, 0.95)
        self.assertGreater(summary.icir, 10)
        self.assertEqual(summary.ic_positive_rate, 1.0)
        self.assertAlmostEqual(backtest.long_short_return, 0.07)
        self.assertTrue(backtest.is_monotonic)

    def test_alpha_subset_acceptance_requires_average_ic_and_monotonic_pass_rate(self):
        accepted = evaluate_alpha_subset_acceptance(
            summaries=[
                type("Summary", (), {"ic_mean": 0.04})(),
                type("Summary", (), {"ic_mean": 0.035})(),
                type("Summary", (), {"ic_mean": 0.025})(),
            ],
            quantile_results=[
                type("Backtest", (), {"is_monotonic": True})(),
                type("Backtest", (), {"is_monotonic": True})(),
                type("Backtest", (), {"is_monotonic": False})(),
            ],
            min_average_ic=0.03,
            min_monotonic_rate=0.60,
            min_factor_count=1,
            max_factor_count=10,
        )
        rejected = evaluate_alpha_subset_acceptance(
            summaries=[
                type("Summary", (), {"ic_mean": 0.02})(),
                type("Summary", (), {"ic_mean": 0.015})(),
            ],
            quantile_results=[
                type("Backtest", (), {"is_monotonic": True})(),
                type("Backtest", (), {"is_monotonic": False})(),
            ],
            min_average_ic=0.03,
            min_monotonic_rate=0.60,
            min_factor_count=1,
            max_factor_count=10,
        )

        self.assertTrue(accepted.passed)
        self.assertAlmostEqual(accepted.average_ic, (0.04 + 0.035 + 0.025) / 3)
        self.assertFalse(rejected.passed)
        self.assertIn("average_ic", rejected.reasons)
        self.assertIn("monotonic_rate", rejected.reasons)

    def test_alpha_subset_acceptance_enforces_expected_50_to_70_factor_count(self):
        summaries = [type("Summary", (), {"ic_mean": 0.04})() for _ in range(49)]
        quantiles = [type("Backtest", (), {"is_monotonic": True})() for _ in range(49)]

        too_few = evaluate_alpha_subset_acceptance(summaries, quantiles)
        accepted = evaluate_alpha_subset_acceptance(
            [type("Summary", (), {"ic_mean": 0.04})() for _ in range(50)],
            [type("Backtest", (), {"is_monotonic": True})() for _ in range(50)],
        )
        too_many = evaluate_alpha_subset_acceptance(
            [type("Summary", (), {"ic_mean": 0.04})() for _ in range(71)],
            [type("Backtest", (), {"is_monotonic": True})() for _ in range(71)],
        )

        self.assertFalse(too_few.passed)
        self.assertIn("factor_count", too_few.reasons)
        self.assertTrue(accepted.passed)
        self.assertFalse(too_many.passed)
        self.assertIn("factor_count", too_many.reasons)

    def test_regime_ic_differentiation_requires_factor_behavior_to_vary_by_regime(self):
        report = evaluate_regime_ic_differentiation(
            {
                "momentum": {"R1": 0.06, "R2": 0.04, "R3": 0.01, "R4": -0.02},
                "quality": {"R1": 0.03, "R2": 0.031, "R3": 0.029, "R4": 0.030},
            },
            min_ic_spread=0.04,
        )

        self.assertTrue(report.passed)
        self.assertEqual(report.differentiated_factors, ["momentum"])
        self.assertGreater(report.ic_spread_by_factor["momentum"], 0.04)

    def test_group_ic_reports_factor_quality_by_industry(self):
        factor_scores = {
            date(2024, 1, 2): {"AAPL": 3, "MSFT": 2, "JPM": 1, "BAC": 0},
            date(2024, 1, 3): {"AAPL": 4, "MSFT": 3, "JPM": 0, "BAC": -1},
        }
        forward_returns = {
            date(2024, 1, 2): {"AAPL": 0.03, "MSFT": 0.02, "JPM": 0.01, "BAC": 0.0},
            date(2024, 1, 3): {"AAPL": 0.04, "MSFT": 0.03, "JPM": 0.0, "BAC": -0.01},
        }

        group_ic = compute_group_ic(
            factor_scores,
            forward_returns,
            groups={"AAPL": "Technology", "MSFT": "Technology", "JPM": "Financials", "BAC": "Financials"},
        )

        self.assertIn("Technology", group_ic)
        self.assertIn("Financials", group_ic)
        for summary in group_ic.values():
            self.assertIsNone(summary.ic_mean)
            self.assertIsNone(summary.icir)
            self.assertEqual(summary.status, "insufficient_evidence")
            self.assertEqual(summary.missing_observations, 2)

    def test_turnover_rate_tracks_average_portfolio_weight_change(self):
        previous = {"AAPL": 0.05, "MSFT": 0.05}
        current = {"AAPL": 0.02, "NVDA": 0.06}

        self.assertAlmostEqual(turnover_rate(previous, current), 0.07)

    def test_oos_backtest_window_gate_matches_phase5_train_and_validation_dates(self):
        valid = evaluate_oos_backtest_window(
            train_start=date(2015, 1, 1),
            train_end=date(2022, 12, 31),
            validation_start=date(2023, 1, 1),
            validation_end=date(2024, 12, 31),
        )
        invalid = evaluate_oos_backtest_window(
            train_start=date(2016, 1, 1),
            train_end=date(2023, 1, 2),
            validation_start=date(2023, 1, 1),
            validation_end=date(2024, 6, 30),
        )

        self.assertTrue(valid.passed)
        self.assertEqual(valid.reasons, [])
        self.assertFalse(invalid.passed)
        self.assertIn("train_start_after_2015", invalid.reasons)
        self.assertIn("train_window_after_2022", invalid.reasons)
        self.assertIn("lookahead_overlap", invalid.reasons)
        self.assertIn("validation_end_before_2024", invalid.reasons)

    def test_momentum_oos_backtest_runs_fixed_parameters_over_validation_window(self):
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB", "CCC"),
            benchmark="SPY",
            lookback_days=20,
            top_n=1,
        )

        result = run_momentum_oos_backtest(_synthetic_oos_prices(), config)

        self.assertTrue(result.window_report.passed)
        self.assertEqual(result.coverage_reasons, ())
        self.assertGreater(result.train_observations, 1500)
        self.assertGreater(result.validation_observations, 500)
        self.assertGreater(result.summary.excess_alpha_annual, 0.05)
        self.assertGreater(result.summary.sharpe, 1.0)
        self.assertLess(result.summary.max_drawdown, 0.20)
        self.assertLess(result.annual_turnover, 4.0)

    def test_momentum_oos_backtest_delays_close_only_execution_and_drifts_weights(self):
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 28),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 1, 4),
            lookback_days=1,
            top_n=2,
        )

        result = run_momentum_oos_backtest(_execution_semantics_prices(), config)
        returns = [point.portfolio_return for point in result.validation_daily_returns]

        self.assertEqual(len(returns), 3)
        self.assertAlmostEqual(returns[0], 0.0)
        self.assertAlmostEqual(returns[1], 0.5)
        self.assertAlmostEqual(returns[2], 2.0 / 3.0)

    def test_momentum_oos_backtest_deducts_configured_cost_from_rebalance_day(self):
        self.assertTrue(hasattr(MomentumOOSBacktestConfig, "two_way_cost_rate"))
        base = dict(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 28),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 1, 4),
            lookback_days=1,
            top_n=2,
        )

        gross = run_momentum_oos_backtest(
            _execution_semantics_prices(),
            MomentumOOSBacktestConfig(**base),
        )
        net = run_momentum_oos_backtest(
            _execution_semantics_prices(),
            MomentumOOSBacktestConfig(two_way_cost_rate=0.02, **base),
        )

        self.assertAlmostEqual(gross.validation_daily_returns[0].portfolio_return, 0.0)
        self.assertAlmostEqual(net.validation_daily_returns[0].portfolio_return, -0.01)
        self.assertEqual(
            gross.validation_daily_returns[1].portfolio_return,
            net.validation_daily_returns[1].portfolio_return,
        )
        self.assertLess(net.summary.annual_return, gross.summary.annual_return)

    def test_first_next_open_rebalance_accepts_empty_overnight_mapping_without_old_holdings(self):
        self.assertIn("execution_returns", inspect.signature(run_momentum_oos_backtest).parameters)
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 28),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 1, 4),
            lookback_days=1,
            top_n=2,
        )
        execution_returns = {
            "AAA": {date(2024, 1, 2): 0.20},
            "BBB": {date(2024, 1, 2): 0.00},
        }

        result = run_momentum_oos_backtest(
            _execution_semantics_prices(),
            config,
            execution_returns=execution_returns,
            overnight_returns={},
        )

        self.assertAlmostEqual(result.validation_daily_returns[0].portfolio_return, 0.10)

    def test_next_open_execution_returns_require_overnight_returns_mapping(self):
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 28),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 1, 4),
            lookback_days=1,
            top_n=2,
        )

        with self.assertRaises(ValueError):
            run_momentum_oos_backtest(
                _execution_semantics_prices(),
                config,
                execution_returns={},
            )

    def test_overnight_returns_require_next_open_execution_returns(self):
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 28),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 1, 4),
            lookback_days=1,
            top_n=2,
        )

        with self.assertRaises(ValueError):
            run_momentum_oos_backtest(
                _execution_semantics_prices(),
                config,
                overnight_returns={},
            )

    def test_next_open_rebalance_fails_closed_when_old_holding_overnight_return_is_missing(self):
        dates = [
            date(2023, 12, 28),
            date(2023, 12, 29),
            date(2024, 1, 2),
            date(2024, 1, 31),
            date(2024, 2, 1),
        ]
        prices = {
            "AAA": dict(zip(dates, [90.0, 100.0, 100.0, 100.0, 90.0])),
            "BBB": dict(zip(dates, [90.0, 90.0, 90.0, 100.0, 132.0])),
            "SPY": dict(zip(dates, [100.0] * len(dates))),
        }
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 29),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 2, 1),
            lookback_days=1,
            top_n=1,
        )

        with self.assertRaisesRegex(ValueError, r"2024-02-01.*AAA"):
            run_momentum_oos_backtest(
                prices,
                config,
                execution_returns={
                    "AAA": {date(2024, 1, 2): 0.0},
                    "BBB": {date(2024, 2, 1): 0.10},
                },
                overnight_returns={},
            )

    def test_next_open_rebalance_fails_closed_when_new_target_execution_return_is_missing(self):
        dates = [
            date(2023, 12, 28),
            date(2023, 12, 29),
            date(2024, 1, 2),
            date(2024, 1, 31),
            date(2024, 2, 1),
        ]
        prices = {
            "AAA": dict(zip(dates, [90.0, 100.0, 100.0, 100.0, 90.0])),
            "BBB": dict(zip(dates, [90.0, 90.0, 90.0, 100.0, 132.0])),
            "SPY": dict(zip(dates, [100.0] * len(dates))),
        }
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 29),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 2, 1),
            lookback_days=1,
            top_n=1,
        )

        with self.assertRaisesRegex(ValueError, r"2024-02-01.*BBB"):
            run_momentum_oos_backtest(
                prices,
                config,
                execution_returns={
                    "AAA": {date(2024, 1, 2): 0.0},
                },
                overnight_returns={
                    "AAA": {date(2024, 2, 1): -0.10},
                },
            )

    def test_monthly_open_rebalance_applies_old_holding_overnight_before_new_intraday_return(self):
        self.assertIn("overnight_returns", inspect.signature(run_momentum_oos_backtest).parameters)
        dates = [
            date(2023, 12, 28),
            date(2023, 12, 29),
            date(2024, 1, 2),
            date(2024, 1, 31),
            date(2024, 2, 1),
        ]
        prices = {
            "AAA": dict(zip(dates, [90.0, 100.0, 100.0, 100.0, 90.0])),
            "BBB": dict(zip(dates, [90.0, 90.0, 90.0, 100.0, 132.0])),
            "SPY": dict(zip(dates, [100.0] * len(dates))),
        }
        config = MomentumOOSBacktestConfig(
            symbols=("AAA", "BBB"),
            benchmark="SPY",
            train_start=date(2023, 12, 29),
            train_end=date(2023, 12, 29),
            validation_start=date(2024, 1, 2),
            validation_end=date(2024, 2, 1),
            lookback_days=1,
            top_n=1,
        )
        execution_returns = {
            "AAA": {date(2024, 1, 2): 0.0},
            "BBB": {date(2024, 2, 1): 0.10},
        }
        overnight_returns = {
            "AAA": {date(2024, 1, 2): 0.0, date(2024, 2, 1): -0.10},
            "BBB": {date(2024, 2, 1): 0.20},
        }

        result = run_momentum_oos_backtest(
            prices,
            config,
            execution_returns=execution_returns,
            overnight_returns=overnight_returns,
        )
        february = next(
            point for point in result.validation_daily_returns
            if point.trading_date == date(2024, 2, 1)
        )

        self.assertAlmostEqual(february.portfolio_return, -0.01)

    def test_inverse_volatility_weights_allocate_more_to_lower_risk_assets(self):
        weights = inverse_volatility_weights({"AAPL": 0.20, "MSFT": 0.10, "NVDA": 0.40})

        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertGreater(weights["MSFT"], weights["AAPL"])
        self.assertGreater(weights["AAPL"], weights["NVDA"])

    def test_mean_variance_score_weights_use_expected_return_per_unit_risk(self):
        weights = mean_variance_score_weights(
            expected_returns={"AAPL": 0.08, "MSFT": 0.06, "NVDA": -0.02},
            variances={"AAPL": 0.04, "MSFT": 0.01, "NVDA": 0.09},
        )

        self.assertAlmostEqual(sum(weights.values()), 1.0)
        self.assertGreater(weights["MSFT"], weights["AAPL"])
        self.assertEqual(weights["NVDA"], 0.0)

    def test_portfolio_risk_scaler_applies_volatility_cap_without_changing_relative_weights(self):
        scaler = PortfolioRiskScaler(max_portfolio_vol_annual=0.18)
        weights = {"AAPL": 0.10, "MSFT": 0.20}

        scaled = scaler.scale_for_volatility(weights, estimated_vol_annual=0.36)

        self.assertEqual(scaled, {"AAPL": 0.05, "MSFT": 0.10})

    def test_portfolio_capacity_estimate_uses_adv_participation_limit(self):
        estimate = estimate_portfolio_capacity(
            weights={"AAPL": 0.05, "MSFT": 0.10},
            adv_dollars={"AAPL": 100_000_000, "MSFT": 50_000_000},
            max_adv_participation=0.01,
        )

        self.assertEqual(estimate.bottleneck_ticker, "MSFT")
        self.assertAlmostEqual(estimate.max_capital, 5_000_000)
        self.assertAlmostEqual(estimate.capacity_by_ticker["AAPL"], 20_000_000)

    def test_hedge_overlay_adds_spy_short_when_optional_hedging_is_enabled(self):
        hedged = apply_hedge_overlay(
            weights={"AAPL": 0.05, "MSFT": 0.05},
            hedge_symbol="SPY",
            hedge_weight=-0.03,
            enabled=True,
        )
        unhedged = apply_hedge_overlay(
            weights={"AAPL": 0.05},
            hedge_symbol="SPY",
            hedge_weight=-0.03,
            enabled=False,
        )

        self.assertEqual(hedged["SPY"], -0.03)
        self.assertNotIn("SPY", unhedged)

    def test_alpaca_adapter_maps_broker_protocol_without_network_in_unit_tests(self):
        rest = FakeAlpacaRest(portfolio_value=125_000)
        broker = AlpacaBrokerAdapter(rest)

        broker.submit_order("AAPL", notional=5000, side="buy")
        value = broker.get_portfolio_value()
        broker.close_all_positions()

        self.assertEqual(value, 125_000)
        self.assertEqual(
            rest.orders,
            [{"symbol": "AAPL", "notional": 5000, "side": "buy", "type": "market", "time_in_force": "day"}],
        )
        self.assertTrue(rest.closed)

    def test_order_journal_persists_trade_log_for_auditability(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = OrderJournal(Path(tmpdir) / "orders.jsonl")
            journal.record(
                timestamp="2024-01-02T21:00:00Z",
                symbol="AAPL",
                side="buy",
                notional=5000,
                reason="rebalance",
            )

            records = journal.read_all()

        self.assertEqual(records[0]["symbol"], "AAPL")
        self.assertEqual(records[0]["reason"], "rebalance")

    def test_dashboard_snapshot_collects_regime_positions_performance_and_factor_exposure(self):
        snapshot = DashboardSnapshot.from_state(
            regime=Regime.R2_BULL_HIGH_VOL,
            positions={"AAPL": 0.05, "MSFT": 0.04},
            performance={"sharpe": 1.25, "drawdown": 0.07},
            factor_exposure={"momentum": 0.22, "quality": 0.11},
        )

        self.assertEqual(snapshot.regime, "R2_BULL_HIGH_VOL")
        self.assertEqual(snapshot.gross_exposure, 0.09)
        self.assertEqual(snapshot.top_positions, [("AAPL", 0.05), ("MSFT", 0.04)])
        self.assertEqual(snapshot.performance["sharpe"], 1.25)


class FakeAlpacaRest:
    def __init__(self, portfolio_value):
        self.portfolio_value = portfolio_value
        self.orders = []
        self.closed = False

    def get_account(self):
        return type("Account", (), {"portfolio_value": str(self.portfolio_value)})()

    def submit_order(self, **kwargs):
        self.orders.append(kwargs)

    def close_all_positions(self):
        self.closed = True


def _synthetic_oos_prices():
    from datetime import timedelta

    rates = {
        "AAA": (0.0012, 0.0004),
        "BBB": (0.0002, 0.0002),
        "CCC": (-0.0001, 0.0002),
        "SPY": (0.00035, 0.00015),
    }
    prices = {ticker: {} for ticker in rates}
    levels = {ticker: 100.0 for ticker in rates}
    current = date(2014, 1, 1)
    end = date(2024, 12, 31)
    index = 0
    while current <= end:
        if current.weekday() < 5:
            for ticker, (base, amplitude) in rates.items():
                levels[ticker] *= 1 + base + amplitude * ((index % 5) - 2)
                prices[ticker][current] = levels[ticker]
            index += 1
        current += timedelta(days=1)
    return prices


def _execution_semantics_prices():
    dates = [
        date(2023, 12, 27),
        date(2023, 12, 28),
        date(2023, 12, 29),
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    ]
    return {
        "AAA": dict(zip(dates, [50.0, 100.0, 100.0, 200.0, 400.0, 800.0])),
        "BBB": dict(zip(dates, [100.0] * len(dates))),
        "SPY": dict(zip(dates, [100.0] * len(dates))),
    }


if __name__ == "__main__":
    unittest.main()
