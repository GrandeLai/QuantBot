"""CSCV fold selection must not switch the PBO gate off by accident.

``cscv_probability_of_backtest_overfitting`` requires the observation count to
divide exactly into equal even folds.  Both runners used to demand that of the
raw sample, so an odd session count -- which has no even divisor at all --
reported ``pbo_unavailable`` and the overfitting gate silently vanished.  CSCV
drops the remainder by construction, so the partition helper trims it and says
how much it dropped.
"""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.daytrade import DAYTRADE_CSCV_FOLD_PREFERENCE, _daytrade_cscv_partition
from quantbot.normal.experiment_runner import _normal_cscv_partition
from quantbot.statistical_validation import (
    DEFAULT_CSCV_FOLD_PREFERENCE,
    cscv_partition,
    cscv_probability_of_backtest_overfitting,
)


def _series(count, scale=1.0):
    return tuple(scale * (((index % 13) - 6) / 100.0) for index in range(count))


def _trials(count):
    return {"baseline": _series(count), "candidate": _series(count, 1.3)}


class CSCVPartitionTest(unittest.TestCase):
    def test_odd_observation_counts_still_produce_a_partition(self):
        for count in (2769, 1005, 501, 253):
            with self.subTest(count=count):
                partition = cscv_partition(_trials(count))

                self.assertIsNotNone(partition)
                self.assertEqual(partition.observations % partition.folds, 0)
                self.assertEqual(
                    partition.dropped_observations, count - partition.observations
                )

    def test_an_exactly_divisible_sample_drops_nothing(self):
        partition = cscv_partition(_trials(2768))

        self.assertEqual(partition.dropped_observations, 0)
        self.assertEqual(partition.folds, 16)
        self.assertEqual(partition.observations, 2768)

    def test_the_largest_preferred_fold_count_wins(self):
        partition = cscv_partition(_trials(2769))

        self.assertEqual(partition.folds, DEFAULT_CSCV_FOLD_PREFERENCE[0])
        self.assertEqual(partition.dropped_observations, 1)

    def test_the_oldest_observations_are_the_ones_dropped(self):
        count = 2769
        trials = _trials(count)
        partition = cscv_partition(trials)
        dropped = partition.dropped_observations

        self.assertGreater(dropped, 0)
        for name, values in partition.trial_returns.items():
            self.assertEqual(values, tuple(trials[name][dropped:]))

    def test_the_partition_feeds_the_pbo_estimator_directly(self):
        partition = cscv_partition(_trials(1005))

        result = cscv_probability_of_backtest_overfitting(
            partition.trial_returns, folds=partition.folds
        )

        self.assertEqual(result.folds, partition.folds)
        self.assertEqual(result.strategy_count, 2)
        self.assertGreaterEqual(result.probability, 0.0)
        self.assertLessEqual(result.probability, 1.0)

    def test_every_trial_keeps_the_same_trimmed_length(self):
        partition = cscv_partition(_trials(1005))

        lengths = {len(values) for values in partition.trial_returns.values()}
        self.assertEqual(lengths, {partition.observations})

    def test_a_sample_too_small_for_any_fold_returns_none(self):
        self.assertIsNone(cscv_partition(_trials(5), fold_preference=(16, 12, 8)))

    def test_odd_fold_preference_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "even integers"):
            cscv_partition(_trials(100), fold_preference=(9,))

    def test_misaligned_trials_are_rejected(self):
        with self.assertRaises(ValueError):
            cscv_partition({"a": _series(100), "b": _series(99)})


class RunnerPartitionWiringTest(unittest.TestCase):
    def test_normal_runner_helper_handles_an_odd_session_count(self):
        partition = _normal_cscv_partition(_trials(2769))

        self.assertIsNotNone(partition)
        self.assertEqual(partition.dropped_observations, 1)

    def test_normal_runner_helper_returns_none_instead_of_raising(self):
        self.assertIsNone(_normal_cscv_partition({"a": (0.01,), "b": (0.02,)}))

    def test_daytrade_helper_keeps_its_declared_fold_preference(self):
        partition = _daytrade_cscv_partition(_trials(505))

        self.assertIsNotNone(partition)
        self.assertIn(partition.folds, DAYTRADE_CSCV_FOLD_PREFERENCE)
        self.assertLessEqual(partition.folds, 8)

    def test_daytrade_helper_returns_none_instead_of_raising(self):
        self.assertIsNone(_daytrade_cscv_partition({"a": (0.01,), "b": (0.02,)}))


if __name__ == "__main__":
    unittest.main()
