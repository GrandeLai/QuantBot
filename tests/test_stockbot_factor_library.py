import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.factors.alpha158 import Alpha158Lite
from quantbot.factors.custom import (
    earnings_revision_factor,
    earnings_sentiment_factor,
    insider_activity_factor,
    option_iv_skew_factor,
    search_trend_attention_factor,
    short_interest_pressure_factor,
    smart_money_13f_factor,
    supply_chain_margin_pressure_factor,
)
from quantbot.factors.normalization import cross_sectional_winsorized_zscore, market_cap_bucket_neutralize, sector_neutralize
from quantbot.factors.pool import evaluate_pool_average_correlation, evaluate_signal_diversity


class StockBotFactorLibraryTest(unittest.TestCase):
    def test_alpha158_lite_includes_ma_rsi_and_bollinger_style_features(self):
        history = [
            {"ticker": "AAPL", "date": date(2024, 1, day), "adjusted_close": float(day), "volume": 100 + day}
            for day in range(1, 7)
        ]

        factors = Alpha158Lite(momentum_windows=(3,), volatility_windows=(3,)).compute(history)["AAPL"]

        self.assertAlmostEqual(factors["ma_deviation_3"], 6 / 5 - 1)
        self.assertEqual(factors["rsi_3"], 100.0)
        self.assertGreater(factors["bollinger_position_3"], 0)
        self.assertEqual(factors["earnings_season"], 1.0)

    def test_alpha158_lite_marks_non_earnings_season_signal_dates(self):
        history = [
            {"ticker": "AAPL", "date": date(2024, 2, day), "adjusted_close": float(day), "volume": 100 + day}
            for day in range(1, 7)
        ]

        factors = Alpha158Lite(momentum_windows=(3,), volatility_windows=(3,)).compute(history)["AAPL"]

        self.assertEqual(factors["earnings_season"], 0.0)

    def test_custom_factors_cover_earnings_short_interest_insider_and_sentiment_directions(self):
        self.assertAlmostEqual(earnings_revision_factor(current_eps=1.20, previous_eps=1.00), 0.20)
        self.assertAlmostEqual(
            short_interest_pressure_factor(previous_short_volume=10, previous_total_volume=100, current_short_volume=30, current_total_volume=100),
            -0.20,
        )
        self.assertAlmostEqual(insider_activity_factor(buy_value=100, sell_value=25), 0.60)
        self.assertAlmostEqual(smart_money_13f_factor(current_shares=1500, previous_shares=1000, fund_quality_weight=1.2), 0.60)
        self.assertAlmostEqual(smart_money_13f_factor(current_shares=500, previous_shares=1000), -0.50)
        self.assertAlmostEqual(earnings_sentiment_factor(sentiment=0.5, guidance="raised"), 0.65)
        self.assertAlmostEqual(earnings_sentiment_factor(sentiment=0.5, guidance="lowered"), 0.35)

    def test_custom_factors_cover_options_supply_chain_and_search_trend_directions(self):
        self.assertAlmostEqual(option_iv_skew_factor(call_iv=0.35, put_iv=0.25), 0.10)
        self.assertAlmostEqual(supply_chain_margin_pressure_factor(input_price_change=0.08, pricing_power=0.03), -0.05)
        self.assertAlmostEqual(search_trend_attention_factor(current_search_volume=130, baseline_search_volume=100), 0.30)

    def test_cross_sectional_winsorized_zscore_clips_outliers_per_factor(self):
        normalized = cross_sectional_winsorized_zscore(
            {
                "AAPL": {"momentum": 1.0, "quality": 3.0},
                "MSFT": {"momentum": 2.0, "quality": 4.0},
                "NVDA": {"momentum": 100.0, "quality": 5.0},
            },
            lower_quantile=0.25,
            upper_quantile=0.75,
        )
        momentum_values = [normalized[ticker]["momentum"] for ticker in ("AAPL", "MSFT", "NVDA")]

        self.assertAlmostEqual(sum(momentum_values), 0.0)
        self.assertAlmostEqual((sum(value ** 2 for value in momentum_values) / len(momentum_values)) ** 0.5, 1.0)
        self.assertLess(normalized["NVDA"]["momentum"], 1.42)
        self.assertIn("quality", normalized["AAPL"])

    def test_sector_neutralize_removes_within_industry_mean_exposure(self):
        neutral = sector_neutralize(
            {
                "AAPL": {"momentum": 2.0},
                "MSFT": {"momentum": 4.0},
                "JPM": {"momentum": 1.0},
                "BAC": {"momentum": 3.0},
            },
            sectors={"AAPL": "Technology", "MSFT": "Technology", "JPM": "Financials", "BAC": "Financials"},
        )

        self.assertAlmostEqual(neutral["AAPL"]["momentum"], -1.0)
        self.assertAlmostEqual(neutral["MSFT"]["momentum"], 1.0)
        self.assertAlmostEqual(neutral["JPM"]["momentum"], -1.0)
        self.assertAlmostEqual(neutral["BAC"]["momentum"], 1.0)

    def test_market_cap_bucket_neutralize_removes_size_bucket_mean_exposure(self):
        neutral = market_cap_bucket_neutralize(
            {
                "SMALL1": {"value": 1.0},
                "SMALL2": {"value": 3.0},
                "LARGE1": {"value": 10.0},
                "LARGE2": {"value": 14.0},
            },
            market_caps={"SMALL1": 1_000, "SMALL2": 2_000, "LARGE1": 100_000, "LARGE2": 200_000},
            buckets=2,
        )

        self.assertAlmostEqual(neutral["SMALL1"]["value"], -1.0)
        self.assertAlmostEqual(neutral["SMALL2"]["value"], 1.0)
        self.assertAlmostEqual(neutral["LARGE1"]["value"], -2.0)
        self.assertAlmostEqual(neutral["LARGE2"]["value"], 2.0)

    def test_signal_diversity_gate_limits_kronos_correlation_against_alpha158(self):
        accepted = evaluate_signal_diversity(
            candidate_values=[0.01, -0.02, 0.03, -0.01],
            reference_values=[-0.03, -0.01, 0.02, 0.04],
            max_abs_correlation=0.40,
        )
        rejected = evaluate_signal_diversity(
            candidate_values=[1.0, 2.0, 3.0, 4.0],
            reference_values=[2.0, 4.0, 6.0, 8.0],
            max_abs_correlation=0.40,
        )

        self.assertTrue(accepted.passed)
        self.assertFalse(rejected.passed)
        self.assertGreater(rejected.abs_correlation, 0.40)

    def test_pool_average_correlation_gate_matches_candidate_pool_acceptance_target(self):
        accepted = evaluate_pool_average_correlation(
            {
                "alpha": [1, 2, 3, 4],
                "kronos": [2, 4, 1, 3],
                "rd": [3, 1, 4, 2],
            },
            max_average_abs_correlation=0.40,
        )
        rejected = evaluate_pool_average_correlation(
            {
                "alpha": [1, 2, 3, 4],
                "clone": [2, 4, 6, 8],
                "near_clone": [1, 2, 2, 4],
            },
            max_average_abs_correlation=0.40,
        )

        self.assertTrue(accepted.passed)
        self.assertEqual(accepted.pair_count, 3)
        self.assertFalse(rejected.passed)
        self.assertIn("average_correlation", rejected.reasons)


if __name__ == "__main__":
    unittest.main()
