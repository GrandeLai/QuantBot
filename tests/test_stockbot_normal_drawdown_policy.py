"""Enforcement of the declared normal drawdown limit.

``risk.max_normal_drawdown`` was parsed, hashed, and read by nothing: no normal
strategy path enforced it.  ``NormalDrawdownPolicy`` makes it enforceable, and
the backtester applies it on the daily risk clock rather than the monthly
signal clock, because a monthly-only check arrives after the drawdown has
already happened.
"""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.normal import (
    NormalBacktestSession,
    NormalDrawdownPolicy,
    PointInTimeNormalBacktester,
)
from quantbot.trading import PointInTimeSnapshot, TargetPortfolio


UTC = timezone.utc


class _Provider:
    def __init__(self, weights):
        self.weights = dict(weights)
        self.calls = 0

    def build_request(self, snapshot, current_weights):
        self.calls += 1
        del current_weights
        return (snapshot.observed_at, self.weights)


class _Allocator:
    def allocate(self, request):
        return SimpleNamespace(targets=TargetPortfolio(request[0], request[1]))


def _snapshot(day):
    return PointInTimeSnapshot(
        observed_at=datetime(day.year, day.month, day.day, 21, tzinfo=UTC)
    )


def _sessions(prices, signal_on=(0,)):
    """One security whose close follows ``prices``; signals on given indexes."""

    days = []
    day = date(2025, 1, 2)
    while len(days) < len(prices):
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    result = []
    for index, (day, price) in enumerate(zip(days, prices)):
        previous = prices[index - 1] if index else prices[0]
        result.append(
            NormalBacktestSession(
                trading_date=day,
                open_prices={"AAA": previous},
                close_prices={"AAA": price},
                signal_snapshot=_snapshot(day) if index in signal_on else None,
            )
        )
    return tuple(result)


class DrawdownPolicyScheduleTest(unittest.TestCase):
    def setUp(self):
        self.policy = NormalDrawdownPolicy()

    def test_full_size_while_shallow(self):
        self.assertEqual(self.policy.exposure_scale(0.0, 1.0), 1.0)
        self.assertEqual(self.policy.exposure_scale(0.05, 1.0), 1.0)

    def test_review_threshold_halves_exposure(self):
        self.assertEqual(self.policy.exposure_scale(0.15, 1.0), 0.5)
        self.assertEqual(self.policy.exposure_scale(0.20, 1.0), 0.5)

    def test_halt_threshold_removes_exposure(self):
        self.assertEqual(self.policy.exposure_scale(0.225, 0.5), 0.0)
        self.assertEqual(self.policy.exposure_scale(0.40, 0.5), 0.0)

    def test_hysteresis_band_holds_the_previous_scale(self):
        # Between recovery and review the scale must not drift back up.
        self.assertEqual(self.policy.exposure_scale(0.12, 0.5), 0.5)
        self.assertEqual(self.policy.exposure_scale(0.12, 1.0), 1.0)

    def test_recovery_restores_full_size(self):
        self.assertEqual(self.policy.exposure_scale(0.10, 0.0), 1.0)
        self.assertEqual(self.policy.exposure_scale(0.02, 0.5), 1.0)

    def test_de_risking_never_re_levers_within_the_review_band(self):
        self.assertEqual(self.policy.exposure_scale(0.18, 0.0), 0.0)

    def test_from_runtime_derives_the_schedule_from_the_declared_limit(self):
        policy = NormalDrawdownPolicy.from_runtime(
            SimpleNamespace(max_normal_drawdown=0.12)
        )

        self.assertAlmostEqual(policy.review_drawdown, 0.12)
        self.assertAlmostEqual(policy.halt_drawdown, 0.18)
        self.assertAlmostEqual(policy.recovery_drawdown, 0.08)

    def test_from_runtime_rejects_a_non_positive_limit(self):
        with self.assertRaisesRegex(ValueError, "max_normal_drawdown"):
            NormalDrawdownPolicy.from_runtime(
                SimpleNamespace(max_normal_drawdown=0.0)
            )

    def test_inverted_thresholds_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "halt_drawdown"):
            NormalDrawdownPolicy(review_drawdown=0.2, halt_drawdown=0.1)
        with self.assertRaisesRegex(ValueError, "recovery_drawdown"):
            NormalDrawdownPolicy(review_drawdown=0.1, recovery_drawdown=0.2)
        with self.assertRaisesRegex(ValueError, "halt_scale"):
            NormalDrawdownPolicy(review_scale=0.2, halt_scale=0.5)


class DrawdownPolicyBacktestTest(unittest.TestCase):
    def _run(self, prices, policy, signal_on=(0,)):
        return PointInTimeNormalBacktester(
            _Provider({"AAA": 1.0}),
            _Allocator(),
            cost_bps_per_side=0,
            drawdown_policy=policy,
        ).run(_sessions(prices, signal_on=signal_on))

    def test_points_report_drawdown_and_exposure_scale(self):
        result = self._run([100, 100, 90, 80, 70], NormalDrawdownPolicy())

        self.assertEqual(result.points[0].drawdown, 0.0)
        self.assertEqual(result.points[0].exposure_scale, 1.0)
        self.assertGreater(result.points[-1].drawdown, 0.15)
        self.assertLess(result.points[-1].exposure_scale, 1.0)

    def test_no_policy_leaves_the_book_untouched(self):
        result = self._run([100, 100, 90, 80, 70], None)

        self.assertEqual(result.drawdown_scaled_rebalances, 0)
        for point in result.points:
            self.assertEqual(point.exposure_scale, 1.0)

    def test_de_risking_fires_on_a_session_with_no_signal(self):
        """The whole point: the risk clock is daily, the signal clock is not."""

        result = self._run([100, 100, 95, 80, 78, 76], NormalDrawdownPolicy())

        self.assertGreater(result.drawdown_scaled_rebalances, 0)
        signal_count = sum(1 for point in result.points if point.turnover > 0)
        self.assertGreater(signal_count, 1)

    def test_a_scaled_book_holds_less_than_an_unscaled_one(self):
        prices = [100, 100, 92, 80, 74, 74, 74, 74]
        scaled = self._run(prices, NormalDrawdownPolicy())
        unscaled = self._run(prices, None)

        self.assertLess(
            sum(scaled.points[-1].weights.values()),
            sum(unscaled.points[-1].weights.values()),
        )

    def test_a_book_that_never_draws_down_is_never_scaled(self):
        result = self._run([100, 101, 102, 103, 104], NormalDrawdownPolicy())

        self.assertEqual(result.drawdown_scaled_rebalances, 0)
        for point in result.points:
            self.assertEqual(point.exposure_scale, 1.0)

    def test_drawdown_is_measured_against_the_running_peak(self):
        result = self._run([100, 120, 110, 108], None)

        self.assertAlmostEqual(result.points[1].drawdown, 0.0)
        self.assertGreater(result.points[2].drawdown, 0.0)
        self.assertAlmostEqual(
            result.points[2].drawdown,
            1.0 - result.points[2].equity / result.points[1].equity,
        )

    def test_invalid_policy_type_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "NormalDrawdownPolicy"):
            PointInTimeNormalBacktester(
                _Provider({"AAA": 1.0}), _Allocator(), drawdown_policy="aggressive"
            )


if __name__ == "__main__":
    unittest.main()
