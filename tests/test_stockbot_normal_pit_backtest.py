import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.normal import NormalBacktestSession, PointInTimeNormalBacktester
from quantbot.trading import PointInTimeSnapshot, TargetPortfolio


UTC = timezone.utc


def _snapshot(day):
    return PointInTimeSnapshot(observed_at=datetime(day.year, day.month, day.day, 21, tzinfo=UTC))


class _Provider:
    def __init__(self, targets):
        self.targets = list(targets)
        self.current_weights = []

    def build_request(self, snapshot, current_weights):
        self.current_weights.append(dict(current_weights))
        return self.targets.pop(0)


class _Allocator:
    def allocate(self, request):
        return SimpleNamespace(targets=TargetPortfolio(request[0], request[1]))


class PointInTimeNormalBacktesterTest(unittest.TestCase):
    def test_close_signal_executes_only_at_next_open_and_charges_each_traded_side(self):
        day1 = date(2025, 1, 2)
        day2 = date(2025, 1, 3)
        provider = _Provider([(datetime(2025, 1, 2, 21, tzinfo=UTC), {"AAA": 1.0})])
        sessions = (
            NormalBacktestSession(
                day1,
                {"AAA": 100.0},
                {"AAA": 100.0},
                signal_snapshot=_snapshot(day1),
            ),
            NormalBacktestSession(day2, {"AAA": 110.0}, {"AAA": 121.0}),
        )

        gross = PointInTimeNormalBacktester(
            _Provider([(datetime(2025, 1, 2, 21, tzinfo=UTC), {"AAA": 1.0})]),
            _Allocator(),
            cost_bps_per_side=0,
        ).run(sessions)
        net = PointInTimeNormalBacktester(
            provider, _Allocator(), cost_bps_per_side=5
        ).run(sessions)
        stress = PointInTimeNormalBacktester(
            _Provider([(datetime(2025, 1, 2, 21, tzinfo=UTC), {"AAA": 1.0})]),
            _Allocator(),
            cost_bps_per_side=10,
        ).run(sessions)

        self.assertEqual(gross.points[0].portfolio_return, 0.0)
        self.assertAlmostEqual(gross.points[1].portfolio_return, 0.10)
        self.assertGreater(net.fees, 0.0)
        self.assertLess(net.final_equity, gross.final_equity)
        self.assertEqual(net.rebalance_count, 1)
        no_trade = net.points[0]
        self.assertEqual(no_trade.pre_trade_equity, 100_000.0)
        self.assertEqual(no_trade.traded_notional, 0.0)
        self.assertEqual(no_trade.fees, 0.0)
        self.assertEqual(no_trade.turnover, 0.0)
        traded = net.points[1]
        self.assertEqual(traded.pre_trade_equity, 100_000.0)
        self.assertAlmostEqual(traded.fees, traded.traded_notional * 0.0005)
        self.assertAlmostEqual(
            traded.turnover,
            0.5 * traded.traded_notional / traded.pre_trade_equity,
        )
        stress_trade = stress.points[1]
        self.assertAlmostEqual(
            stress_trade.fees,
            stress_trade.traded_notional * 0.001,
        )
        for result in (net, stress):
            previous_equity = result.initial_equity
            for point in result.points:
                self.assertAlmostEqual(
                    point.equity - previous_equity,
                    sum(point.security_pnl.values()),
                )
                previous_equity = point.equity

    def test_actual_drifted_close_weights_feed_the_next_signal(self):
        days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
        provider = _Provider(
            [
                (datetime(2025, 1, 2, 21, tzinfo=UTC), {"AAA": 0.5, "BBB": 0.5}),
                (datetime(2025, 1, 3, 21, tzinfo=UTC), {"AAA": 0.5, "BBB": 0.5}),
            ]
        )
        result = PointInTimeNormalBacktester(
            provider, _Allocator(), cost_bps_per_side=0
        ).run(
            (
                NormalBacktestSession(days[0], {"AAA": 100, "BBB": 100}, {"AAA": 100, "BBB": 100}, signal_snapshot=_snapshot(days[0])),
                NormalBacktestSession(days[1], {"AAA": 100, "BBB": 100}, {"AAA": 120, "BBB": 100}, signal_snapshot=_snapshot(days[1])),
                NormalBacktestSession(days[2], {"AAA": 120, "BBB": 100}, {"AAA": 120, "BBB": 100}),
            )
        )

        self.assertEqual(provider.current_weights[0], {})
        self.assertGreater(provider.current_weights[1]["AAA"], 0.5)
        self.assertLess(provider.current_weights[1]["BBB"], 0.5)
        self.assertGreater(result.annual_turnover, 0.0)

    def test_split_and_cash_distribution_conserve_value(self):
        days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
        result = PointInTimeNormalBacktester(
            _Provider([(datetime(2025, 1, 2, 21, tzinfo=UTC), {"AAA": 1.0})]),
            _Allocator(),
            cost_bps_per_side=0,
        ).run(
            (
                NormalBacktestSession(days[0], {"AAA": 100}, {"AAA": 100}, signal_snapshot=_snapshot(days[0])),
                NormalBacktestSession(days[1], {"AAA": 100}, {"AAA": 100}),
                NormalBacktestSession(
                    days[2],
                    {"AAA": 49},
                    {"AAA": 49},
                    split_ratios={"AAA": 2},
                    cash_distributions={"AAA": 1},
                ),
            )
        )

        self.assertAlmostEqual(result.points[-1].portfolio_return, 0.0)
        self.assertAlmostEqual(sum(result.pnl_by_security_id.values()), 0.0)

    def test_missing_price_for_held_security_fails_closed(self):
        days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
        with self.assertRaisesRegex(ValueError, "missing open price.*AAA"):
            PointInTimeNormalBacktester(
                _Provider([(datetime(2025, 1, 2, 21, tzinfo=UTC), {"AAA": 1.0})]),
                _Allocator(),
                cost_bps_per_side=0,
            ).run(
                (
                    NormalBacktestSession(days[0], {"AAA": 100}, {"AAA": 100}, signal_snapshot=_snapshot(days[0])),
                    NormalBacktestSession(days[1], {"AAA": 100}, {"AAA": 100}),
                    NormalBacktestSession(days[2], {"BBB": 100}, {"BBB": 100}),
                )
            )


if __name__ == "__main__":
    unittest.main()
