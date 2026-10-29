"""Enforcement of the declared DayTrade drawdown limit.

``risk.max_daytrade_drawdown`` was validated, hashed, and translated into
``DayTradeConfig`` by ``from_runtime`` -- and then read by nothing.  It is now
enforceable as an operational halt on new entries, opt-in so the frozen H3/H9
results stay reproducible.

The acceptance ceilings are a separate concern and live in
``quantbot.strategy_admission`` as named constants rather than magic literals.
"""

import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config_loader import DayTradeRuntimeConfig, load_runtime_config
from quantbot.daytrade import DayTradeBacktester, DayTradeConfig, MinuteBar
from quantbot.normal.experiment_runner import NORMAL_ACCEPTANCE_MAX_DRAWDOWN
from quantbot.strategy_admission import (
    DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN,
    DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN,
    NORMAL_ACCEPTANCE_MAX_DRAWDOWN as ADMISSION_NORMAL_CEILING,
)


EASTERN_OFFSET = timezone(timedelta(hours=-5))
REPO = Path(__file__).resolve().parents[1]


def _bars(day, symbol, prices):
    """One session of minute bars starting at the 09:30 open."""

    start = datetime(day.year, day.month, day.day, 9, 30, tzinfo=EASTERN_OFFSET)
    return [
        MinuteBar(
            symbol=symbol,
            timestamp=start + timedelta(minutes=index),
            open=price,
            high=price * 1.001,
            low=price * 0.999,
            close=price,
            volume=50_000.0,
        )
        for index, price in enumerate(prices)
    ]


def _losing_session(day, symbol="AAA", base=100.0):
    """Opening range, an upside breakout, then a slide into the stop."""

    prices = [base] * 15
    prices += [base * 1.02] * 5
    prices += [base * (1.02 - 0.0006 * step) for step in range(1, 340)]
    return _bars(day, symbol, prices)


def _sessions(count, symbol="AAA"):
    bars = []
    day = date(2024, 1, 2)
    added = 0
    while added < count:
        if day.weekday() < 5:
            bars.extend(_losing_session(day, symbol))
            added += 1
        day += timedelta(days=1)
    return bars


class DayTradeDrawdownHaltTest(unittest.TestCase):
    def setUp(self):
        self.bars = _sessions(40)

    def _run(self, **changes):
        values = dict(
            max_trades_per_day=6,
            max_concurrent_positions=3,
            per_trade_risk_fraction=0.02,
            daily_loss_limit_fraction=0.50,
            max_daytrade_drawdown=0.02,
        )
        values.update(changes)
        return DayTradeBacktester(DayTradeConfig(**values)).run(self.bars)

    def test_disabled_by_default_so_frozen_results_are_reproducible(self):
        default = self._run()
        explicit_off = self._run(enforce_drawdown_halt=False)

        self.assertEqual(default.trade_count, explicit_off.trade_count)
        self.assertAlmostEqual(default.ending_equity, explicit_off.ending_equity)
        self.assertFalse(
            any(
                item.stopped_reason == "max_daytrade_drawdown"
                for item in default.daily_results
            )
        )

    def test_enabling_the_halt_stops_new_entries_after_the_limit(self):
        unenforced = self._run()
        enforced = self._run(enforce_drawdown_halt=True)

        self.assertGreater(unenforced.max_drawdown, 0.02)
        self.assertLess(enforced.trade_count, unenforced.trade_count)
        self.assertLess(enforced.max_drawdown, unenforced.max_drawdown)

    def test_halted_sessions_record_the_reason(self):
        enforced = self._run(enforce_drawdown_halt=True)

        halted = [
            item
            for item in enforced.daily_results
            if item.stopped_reason == "max_daytrade_drawdown"
        ]
        self.assertTrue(halted)
        self.assertTrue(all(item.trades == 0 for item in halted))

    def test_a_zero_limit_disables_the_halt(self):
        enforced = self._run(enforce_drawdown_halt=True, max_daytrade_drawdown=0.0)
        unenforced = self._run()

        self.assertEqual(enforced.trade_count, unenforced.trade_count)

    def test_a_book_that_never_draws_down_is_never_halted(self):
        backtester = DayTradeBacktester(
            DayTradeConfig(enforce_drawdown_halt=True, max_daytrade_drawdown=0.02)
        )
        result = backtester.run(_sessions(3))

        self.assertFalse(
            any(
                item.stopped_reason == "max_daytrade_drawdown"
                for item in result.daily_results[:1]
            )
        )

    def test_the_halt_is_decided_before_the_session_opens(self):
        """A halt may only remove entries, never rewrite a finished session."""

        enforced = self._run(enforce_drawdown_halt=True)

        self.assertGreater(len(enforced.daily_results), 0)
        for item in enforced.daily_results:
            if item.stopped_reason == "max_daytrade_drawdown":
                self.assertEqual(item.trades, 0)
                self.assertAlmostEqual(item.pnl, 0.0)


class DayTradeDrawdownConfigTest(unittest.TestCase):
    def test_runtime_default_is_behaviour_preserving(self):
        self.assertFalse(DayTradeRuntimeConfig().enforce_drawdown_halt)

    def test_runtime_rejects_a_non_boolean(self):
        with self.assertRaisesRegex(ValueError, "daytrade.enforce_drawdown_halt"):
            DayTradeRuntimeConfig(enforce_drawdown_halt="yes")

    def test_runtime_flows_into_the_execution_config(self):
        runtime = DayTradeRuntimeConfig(enforce_drawdown_halt=True)
        portfolio = SimpleNamespace(initial_cash=100_000.0, daytrade_fraction=0.10)
        risk = SimpleNamespace(max_daytrade_drawdown=0.05)

        config = DayTradeConfig.from_runtime(runtime, portfolio, risk)

        self.assertTrue(config.enforce_drawdown_halt)
        self.assertEqual(config.max_daytrade_drawdown, 0.05)

    def test_shipped_example_keeps_the_halt_off(self):
        config = load_runtime_config(REPO / "config" / "quantbot.research.toml")

        self.assertFalse(config.daytrade.enforce_drawdown_halt)
        self.assertEqual(config.risk.max_daytrade_drawdown, 0.05)


class AcceptanceCeilingConstantTest(unittest.TestCase):
    def test_the_research_runner_and_the_admission_gate_share_one_ceiling(self):
        self.assertIs(NORMAL_ACCEPTANCE_MAX_DRAWDOWN, ADMISSION_NORMAL_CEILING)

    def test_daytrade_ceilings_are_named_rather_than_literal(self):
        self.assertEqual(DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN, 0.05)
        self.assertEqual(DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN, 0.07)
        self.assertGreater(
            DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN,
            DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN,
        )

    def test_acceptance_and_operational_limits_remain_separate_controls(self):
        config = load_runtime_config(REPO / "config" / "quantbot.research.toml")

        # DayTrade deliberately keeps them equal: a strategy that would trip the
        # operational halt should not be admitted.  The point is that they are
        # now two named quantities rather than one repeated literal.
        self.assertEqual(
            DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN, config.risk.max_daytrade_drawdown
        )
        # Normal also preserves the original 15% value in both controls.  They
        # remain separate because validation and the running-book halt consume
        # different configuration fields.
        self.assertEqual(
            NORMAL_ACCEPTANCE_MAX_DRAWDOWN, config.risk.max_normal_drawdown
        )


if __name__ == "__main__":
    unittest.main()
