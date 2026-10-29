import math
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.statistical_validation import (
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)


class StatisticalValidationTest(unittest.TestCase):
    def test_dsr_penalizes_more_and_more_variable_trials(self):
        returns = tuple(
            0.0003 + 0.01 * ((index % 7) - 3) / 3.0
            for index in range(252)
        )

        narrow = deflated_sharpe_ratio(
            returns,
            trial_sharpes_annual=(0.8, 0.9, 1.0),
            periods_per_year=252,
        )
        broad = deflated_sharpe_ratio(
            returns,
            trial_sharpes_annual=(-1.0, 0.0, 1.0, 2.0, 3.0),
            periods_per_year=252,
        )

        self.assertEqual(narrow.trial_count, 3)
        self.assertEqual(broad.trial_count, 5)
        self.assertGreater(broad.expected_max_sharpe_annual, narrow.expected_max_sharpe_annual)
        self.assertLess(broad.confidence, narrow.confidence)
        self.assertTrue(0.0 <= broad.confidence <= 1.0)
        self.assertTrue(math.isfinite(broad.test_statistic))

    def test_dsr_rejects_invalid_returns_and_unreported_trials(self):
        with self.assertRaisesRegex(ValueError, "returns"):
            deflated_sharpe_ratio((0.1, math.nan, 0.2), (1.0,))
        with self.assertRaisesRegex(ValueError, "trial_sharpes"):
            deflated_sharpe_ratio((0.1, 0.2, 0.3), ())
        with self.assertRaisesRegex(ValueError, "variance"):
            deflated_sharpe_ratio((0.1, 0.1, 0.1), (1.0,))

    def test_cscv_pbo_uses_symmetric_is_oos_rank_logits(self):
        strategy_a = []
        strategy_b = []
        for fold in range(4):
            sign = 1.0 if fold < 2 else -1.0
            strategy_a.extend((0.010 * sign, 0.012 * sign, 0.008 * sign, 0.011 * sign))
            strategy_b.extend((-0.010 * sign, -0.012 * sign, -0.008 * sign, -0.011 * sign))

        result = cscv_probability_of_backtest_overfitting(
            {"A": strategy_a, "B": strategy_b},
            folds=4,
        )

        self.assertEqual(result.combination_count, 6)
        self.assertEqual(len(result.logits), 6)
        self.assertGreaterEqual(result.probability, 2 / 3)
        self.assertTrue(all(math.isfinite(value) for value in result.logits))

    def test_cscv_requires_aligned_matrix_and_even_equal_folds(self):
        with self.assertRaisesRegex(ValueError, "aligned"):
            cscv_probability_of_backtest_overfitting(
                {"A": (0.1,) * 8, "B": (0.1,) * 7},
                folds=4,
            )
        with self.assertRaisesRegex(ValueError, "even"):
            cscv_probability_of_backtest_overfitting(
                {"A": (0.1,) * 9, "B": (0.2,) * 9},
                folds=3,
            )


if __name__ == "__main__":
    unittest.main()
