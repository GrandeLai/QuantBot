import math
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.data.cleaning import filter_liquid_universe, forward_fill_missing_sessions, sanitize_price_history
from quantbot.execution import AutoExecutor, daily_signal_execution_plan
from quantbot.factors.alpha158 import Alpha158Lite
from quantbot.factors.evaluation import FactorMetrics, passes_admission, pearson_ic
from quantbot.factors.kronos import KronosFeatureExtractor
from quantbot.factors.pool import select_representative_factors
from quantbot.regime import (
    Regime,
    RegimePrediction,
    RegimeSnapshot,
    RuleBasedRegimeDetector,
    StableRegimeDetector,
)
from quantbot.risk import RiskConfig, RiskManager
from quantbot.trading_agent import TradingAgent, default_regime_weight_config
from quantbot.trading import BrokerAccountSnapshot, MarginStatus


class StockBotCoreTest(unittest.TestCase):
    def test_sanitize_price_history_forward_fills_adjusted_close_per_ticker(self):
        rows = [
            {"date": date(2024, 1, 1), "ticker": "AAPL", "close": 100.0, "adjusted_close": 100.0, "volume": 10},
            {"date": date(2024, 1, 2), "ticker": "AAPL", "close": 101.0, "adjusted_close": None, "volume": 11},
            {"date": date(2024, 1, 1), "ticker": "MSFT", "close": 200.0, "adjusted_close": 198.0, "volume": 12},
        ]

        cleaned = sanitize_price_history(rows)

        aapl_second = [row for row in cleaned if row["ticker"] == "AAPL" and row["date"] == date(2024, 1, 2)][0]
        self.assertEqual(aapl_second["adjusted_close"], 100.0)
        self.assertEqual([row["ticker"] for row in cleaned], ["AAPL", "AAPL", "MSFT"])

    def test_forward_fill_missing_sessions_uses_trading_calendar_and_marks_synthetic_rows(self):
        rows = [
            {"date": date(2024, 1, 2), "ticker": "AAPL", "close": 100.0, "adjusted_close": 100.0, "volume": 1000},
            {"date": date(2024, 1, 4), "ticker": "AAPL", "close": 102.0, "adjusted_close": 102.0, "volume": 1200},
            {"date": date(2024, 1, 2), "ticker": "MSFT", "close": 200.0, "adjusted_close": 200.0, "volume": 800},
        ]

        filled = forward_fill_missing_sessions(
            rows,
            sessions=[date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)],
        )

        aapl = [row for row in filled if row["ticker"] == "AAPL"]
        msft = [row for row in filled if row["ticker"] == "MSFT"]
        self.assertEqual([row["date"] for row in aapl], [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])
        self.assertEqual(aapl[1]["adjusted_close"], 100.0)
        self.assertEqual(aapl[1]["close"], 100.0)
        self.assertEqual(aapl[1]["volume"], 0)
        self.assertTrue(aapl[1]["is_forward_filled"])
        self.assertFalse(aapl[0]["is_forward_filled"])
        self.assertEqual([row["date"] for row in msft], [date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)])

    def test_filter_liquid_universe_applies_history_and_dollar_volume_thresholds(self):
        rows = []
        start = date(2024, 1, 1)
        for offset in range(5):
            rows.append(
                {
                    "date": start + timedelta(days=offset),
                    "ticker": "AAPL",
                    "adjusted_close": 100.0,
                    "volume": 100_000,
                }
            )
            rows.append(
                {
                    "date": start + timedelta(days=offset),
                    "ticker": "ILLIQ",
                    "adjusted_close": 10.0,
                    "volume": 1_000,
                }
            )
        rows.append({"date": start + timedelta(days=4), "ticker": "IPO", "adjusted_close": 100.0, "volume": 100_000})

        universe = filter_liquid_universe(rows, min_avg_dollar_volume=5_000_000, min_history_days=5, lookback_days=5)

        self.assertEqual(universe, ["AAPL"])

    def test_alpha158_lite_uses_adjusted_prices_for_momentum_and_volatility(self):
        history = [
            {"ticker": "AAPL", "date": date(2024, 1, day), "adjusted_close": float(day), "volume": 100}
            for day in range(1, 7)
        ]

        factors = Alpha158Lite(momentum_windows=(3,), volatility_windows=(3,)).compute(history)

        latest = factors["AAPL"]
        self.assertAlmostEqual(latest["momentum_3"], 6 / 3 - 1)
        self.assertGreater(latest["volatility_3"], 0)
        self.assertIn("volume_ratio_3", latest)

    def test_kronos_feature_extractor_returns_distribution_features(self):
        features = KronosFeatureExtractor(context_length=5).extract([0.01, 0.02, -0.01, 0.03, 0.0])

        self.assertAlmostEqual(features["kronos_mean"], 0.01)
        self.assertGreater(features["kronos_vol"], 0)
        self.assertIn("kronos_skew", features)

    def test_factor_admission_requires_ic_icir_hit_rate_and_oos(self):
        accepted = FactorMetrics(
            name="quality_momentum",
            ic_mean=0.031,
            icir=0.42,
            ic_positive_rate=0.58,
            long_short_return_annual=0.09,
            decay_days=5,
            oos_ic=0.024,
        )
        rejected = FactorMetrics(
            name="overfit_signal",
            ic_mean=0.031,
            icir=0.42,
            ic_positive_rate=0.58,
            long_short_return_annual=0.09,
            decay_days=5,
            oos_ic=0.005,
        )
        high_turnover = FactorMetrics(
            name="too_fast",
            ic_mean=0.031,
            icir=0.42,
            ic_positive_rate=0.58,
            long_short_return_annual=0.09,
            decay_days=5,
            oos_ic=0.024,
            turnover_annual=12.0,
        )

        self.assertTrue(passes_admission(accepted))
        self.assertFalse(passes_admission(rejected))
        self.assertFalse(passes_admission(high_turnover))

    def test_factor_pool_keeps_best_icir_from_correlated_cluster(self):
        factor_values = {
            "mom_fast": [1.0, 2.0, 3.0, 4.0],
            "mom_slow": [1.1, 2.1, 2.9, 4.1],
            "quality": [4.0, 1.0, 3.0, 2.0],
        }
        metrics = {
            "mom_fast": FactorMetrics("mom_fast", 0.03, 0.36, 0.55, 0.10, 4, 0.03),
            "mom_slow": FactorMetrics("mom_slow", 0.03, 0.50, 0.56, 0.10, 4, 0.03),
            "quality": FactorMetrics("quality", 0.03, 0.40, 0.56, 0.10, 4, 0.03),
        }

        selected = select_representative_factors(factor_values, metrics, corr_threshold=0.65)

        self.assertIn("mom_slow", selected)
        self.assertNotIn("mom_fast", selected)
        self.assertIn("quality", selected)

    def test_rule_based_regime_matches_design_thresholds(self):
        detector = RuleBasedRegimeDetector()

        self.assertEqual(detector.predict(RegimeSnapshot(vix=36, spy_1m=0.01, yield_curve=0.5)).regime, Regime.R5_CRISIS)
        self.assertEqual(detector.predict(RegimeSnapshot(vix=28, spy_1m=-0.06, yield_curve=0.5)).regime, Regime.R4_BEAR)
        self.assertEqual(detector.predict(RegimeSnapshot(vix=12, spy_1m=0.03, yield_curve=0.5)).regime, Regime.R1_BULL_LOW_VOL)
        self.assertEqual(detector.predict(RegimeSnapshot(vix=20, spy_1m=0.01, yield_curve=0.5)).regime, Regime.R2_BULL_HIGH_VOL)
        self.assertEqual(detector.predict(RegimeSnapshot(vix=18, spy_1m=-0.01, yield_curve=0.5)).regime, Regime.R3_SIDEWAYS)

    def test_stable_regime_detector_keeps_previous_state_when_confidence_low(self):
        stable = StableRegimeDetector(min_confidence=0.60, previous_regime=Regime.R2_BULL_HIGH_VOL)

        prediction = stable.stabilize(RegimePrediction(Regime.R4_BEAR, confidence=0.42, source="hmm"))

        self.assertEqual(prediction.regime, Regime.R2_BULL_HIGH_VOL)
        self.assertEqual(prediction.source, "hmm:held_previous")

    def test_trading_agent_scores_and_watchlist_apply_regime_weights_and_filters(self):
        agent = TradingAgent(
            candidate_factors=["momentum", "quality", "low_vol"],
            weight_config={
                Regime.R1_BULL_LOW_VOL: {"momentum": 0.6, "quality": 0.3, "low_vol": -0.1},
            },
        )
        factor_matrix = {
            "AAPL": {"momentum": 1.2, "quality": 0.7, "low_vol": 0.2},
            "MSFT": {"momentum": 0.9, "quality": 0.9, "low_vol": 0.4},
            "LOWADV": {"momentum": 2.0, "quality": 0.1, "low_vol": 0.1},
            "EARN": {"momentum": 1.6, "quality": 0.2, "low_vol": 0.1},
        }

        scores = agent.compute_composite_scores(factor_matrix, Regime.R1_BULL_LOW_VOL)
        watchlist = agent.update_watchlist(
            scores,
            top_n=2,
            average_dollar_volume={"AAPL": 10_000_000, "MSFT": 8_000_000, "LOWADV": 1_000_000, "EARN": 9_000_000},
            earnings_window={"EARN"},
        )

        self.assertGreater(scores["AAPL"], scores["MSFT"])
        self.assertEqual(watchlist, ["AAPL", "MSFT"])

    def test_default_regime_weight_config_matches_design_strategy_matrix(self):
        weights = default_regime_weight_config()

        self.assertGreater(weights[Regime.R1_BULL_LOW_VOL]["momentum"], 0)
        self.assertGreater(weights[Regime.R1_BULL_LOW_VOL]["quality"], 0)
        self.assertGreater(weights[Regime.R1_BULL_LOW_VOL]["small_cap"], 0)
        self.assertLess(weights[Regime.R1_BULL_LOW_VOL]["low_volatility"], 0)
        self.assertLess(weights[Regime.R3_SIDEWAYS]["momentum"], 0)
        self.assertGreater(weights[Regime.R3_SIDEWAYS]["value"], 0)
        self.assertGreater(weights[Regime.R4_BEAR]["short_interest_pressure"], 0)
        self.assertLess(weights[Regime.R4_BEAR]["momentum"], 0)
        self.assertEqual(weights[Regime.R5_CRISIS]["defensive_cash"], 1.0)

    def test_risk_manager_scales_weights_by_regime_and_caps_single_stock(self):
        manager = RiskManager(RiskConfig(max_single_stock_weight=0.05, max_sector_weight=0.25))

        weights = manager.build_target_weights(
            watchlist=["AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "CRM", "ORCL", "ADBE", "AVGO"],
            sectors={
                "AAPL": "Technology",
                "MSFT": "Technology",
                "NVDA": "Technology",
                "META": "Communication",
                "AMZN": "Consumer",
                "GOOGL": "Communication",
                "CRM": "Technology",
                "ORCL": "Technology",
                "ADBE": "Technology",
                "AVGO": "Technology",
            },
            regime=Regime.R4_BEAR,
        )

        self.assertAlmostEqual(sum(weights.values()), 0.30)
        self.assertTrue(all(weight <= 0.05 for weight in weights.values()))

    def test_risk_manager_scales_portfolio_when_factor_beta_exceeds_cap(self):
        manager = RiskManager(RiskConfig(max_factor_beta=0.30))

        scaled = manager.scale_for_factor_beta(
            weights={"AAPL": 0.20, "MSFT": 0.20},
            factor_betas={"AAPL": 1.0, "MSFT": 1.0},
        )

        self.assertAlmostEqual(sum(weight * 1.0 for weight in scaled.values()), 0.30)
        self.assertAlmostEqual(scaled["AAPL"], 0.15)

    def test_risk_manager_scales_portfolio_when_volatility_exceeds_cap(self):
        manager = RiskManager(RiskConfig(max_portfolio_vol_annual=0.18))

        scaled = manager.scale_for_portfolio_volatility(
            weights={"AAPL": 0.30, "MSFT": 0.30},
            annual_volatility=0.24,
        )
        unchanged = manager.scale_for_portfolio_volatility(
            weights={"AAPL": 0.30, "MSFT": 0.30},
            annual_volatility=0.12,
        )

        self.assertAlmostEqual(scaled["AAPL"], 0.225)
        self.assertEqual(unchanged, {"AAPL": 0.30, "MSFT": 0.30})

    def test_risk_manager_deleverages_when_regime_confidence_is_low_or_conflicted(self):
        manager = RiskManager()
        weights = {"AAPL": 0.10, "MSFT": 0.08}

        low_confidence = manager.scale_for_regime_confidence(
            weights,
            RegimePrediction(Regime.R4_BEAR, confidence=0.45, source="hmm"),
        )
        conflicted = manager.scale_for_regime_confidence(
            weights,
            RegimePrediction(Regime.R4_BEAR, confidence=0.70, source="hmm:rule_conflict"),
        )
        stable = manager.scale_for_regime_confidence(
            weights,
            RegimePrediction(Regime.R2_BULL_HIGH_VOL, confidence=0.80, source="hmm+rule"),
        )

        self.assertEqual(low_confidence, {"AAPL": 0.05, "MSFT": 0.04})
        self.assertEqual(conflicted, {"AAPL": 0.05, "MSFT": 0.04})
        self.assertEqual(stable, weights)

    def test_risk_manager_reports_drawdown_actions_for_review_and_halt(self):
        manager = RiskManager(RiskConfig(max_drawdown_halt=0.15))

        normal = manager.drawdown_action(0.10)
        review = manager.drawdown_action(0.15)
        halt = manager.drawdown_action(0.25)

        self.assertFalse(normal.manual_review)
        self.assertEqual(normal.position_scale, 1.0)
        self.assertTrue(review.manual_review)
        self.assertFalse(review.halt_trading)
        self.assertEqual(review.position_scale, 0.5)
        self.assertTrue(halt.halt_trading)
        self.assertEqual(halt.position_scale, 0.0)

    def test_auto_executor_submits_only_material_rebalance_orders(self):
        broker = RecordingBroker(portfolio_value=100_000)
        executor = AutoExecutor(broker)

        executor.rebalance(
            target_weights={"AAPL": 0.10, "MSFT": 0.1004, "NVDA": 0.0},
            current_holdings={"AAPL": 0.06, "MSFT": 0.10, "NVDA": 0.02},
        )

        self.assertEqual(
            broker.orders,
            [
                {"symbol": "AAPL", "notional": 4000.0, "side": "buy", "order_type": "market", "time_in_force": "day"},
                {"symbol": "NVDA", "notional": 2000.0, "side": "sell", "order_type": "market", "time_in_force": "day"},
            ],
        )

    def test_auto_executor_can_route_market_on_open_or_vwap_order_styles(self):
        broker = RecordingBroker(portfolio_value=100_000)
        executor = AutoExecutor(broker)

        executor.rebalance(
            target_weights={"AAPL": 0.10},
            current_holdings={},
            order_type="market",
            time_in_force="opg",
        )
        executor.rebalance(
            target_weights={"MSFT": 0.10},
            current_holdings={},
            order_type="vwap",
            time_in_force="day",
        )

        self.assertEqual(broker.orders[0]["time_in_force"], "opg")
        self.assertEqual(broker.orders[1]["order_type"], "vwap")

    def test_daily_signal_execution_plan_supports_next_open_and_vwap_styles(self):
        next_open = daily_signal_execution_plan("next_open")
        vwap = daily_signal_execution_plan("vwap")

        self.assertEqual(next_open, {"order_type": "market", "time_in_force": "opg"})
        self.assertEqual(vwap, {"order_type": "vwap", "time_in_force": "day"})

    def test_pearson_ic_reports_cross_sectional_relationship(self):
        ic = pearson_ic({"AAPL": 0.3, "MSFT": 0.2, "NVDA": -0.1}, {"AAPL": 0.04, "MSFT": 0.02, "NVDA": -0.03})

        self.assertTrue(math.isclose(ic, 0.9992600812897369))


class RecordingBroker:
    def __init__(self, portfolio_value):
        self.portfolio_value = portfolio_value
        self.orders = []
        self.closed = False

    def get_portfolio_value(self):
        return self.portfolio_value

    def account_snapshot(self):
        return BrokerAccountSnapshot(
            broker="test",
            observed_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
            equity=self.portfolio_value,
            buying_power=self.portfolio_value,
            day_trade_buying_power=None,
            trading_blocked=False,
            shorting_enabled=True,
            margin_status=MarginStatus.MARGIN,
        )

    def submit_order(self, symbol, notional, side, order_type="market", time_in_force="day"):
        self.orders.append(
            {
                "symbol": symbol,
                "notional": notional,
                "side": side,
                "order_type": order_type,
                "time_in_force": time_in_force,
            }
        )

    def close_all_positions(self):
        self.closed = True


if __name__ == "__main__":
    unittest.main()
