import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.daytrade import (
    DayTradeBacktester,
    DayTradeConfig,
    DayTradeRiskGate,
    MinuteBar,
)


class DayTradeBrokerCapacityTest(unittest.TestCase):
    def test_same_minute_uses_opening_information_score_before_symbol_tiebreak(self):
        config = DayTradeConfig(
            account_equity=100.0,
            capital_fraction=1.0,
            per_trade_risk_fraction=0.01,
            max_concurrent_positions=1,
        )

        trades, _daily = _run_day(config, _competing_signals(), starting_equity=100.0)

        self.assertEqual([trade.symbol for trade in trades], ["BSTRONG"])

    def test_same_minute_pending_notional_is_capped_by_loss_reduced_equity(self):
        config = DayTradeConfig(
            account_equity=100.0,
            capital_fraction=1.0,
            per_trade_risk_fraction=0.01,
            max_concurrent_positions=2,
        )

        trades, _daily = _run_day(config, _competing_signals(), starting_equity=60.0)

        self.assertEqual(len(trades), 1)
        self.assertLessEqual(sum(trade.notional for trade in trades), 60.0)
        self.assertEqual(trades[0].symbol, "BSTRONG")

    def test_all_same_minute_exits_are_processed_before_new_candidates(self):
        config = DayTradeConfig(
            account_equity=100.0,
            capital_fraction=1.0,
            per_trade_risk_fraction=0.01,
            max_concurrent_positions=1,
        )

        trades, _daily = _run_day(
            config,
            _exit_then_entry_same_minute(),
            starting_equity=100.0,
        )

        self.assertEqual([trade.symbol for trade in trades], ["BSTRONG", "AWEAK"])

    def test_open_positions_reserve_entry_notional_during_unrealized_moves(self):
        config = DayTradeConfig(
            account_equity=10_000.0,
            capital_fraction=1.0,
            per_trade_risk_fraction=0.01,
            max_concurrent_positions=3,
        )

        for current_price in (99.70, 100.20):
            with self.subTest(current_price=current_price):
                trades, _daily = _run_day(
                    config,
                    _staggered_capacity_signals(current_price),
                    starting_equity=6_660.0,
                )
                self.assertNotIn("BLATER", [trade.symbol for trade in trades])
                self.assertLessEqual(
                    sum(trade.notional for trade in trades),
                    6_660.0,
                )


def _run_day(config, bars, starting_equity):
    backtester = DayTradeBacktester(config)
    normalized = sorted(bars, key=lambda item: (item.timestamp, item.symbol))
    ranges = backtester.strategy.opening_ranges(normalized)
    trading_day = normalized[0].timestamp.astimezone().date()
    return backtester._run_day(
        trading_day=trading_day,
        bars=normalized,
        opening_ranges=ranges,
        risk_gate=DayTradeRiskGate(config),
        starting_equity=starting_equity,
    )


def _opening_bars():
    bars = []
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for offset in range(15):
        timestamp = start.replace(minute=30 + offset)
        bars.extend(
            [
                MinuteBar(
                    "AWEAK",
                    timestamp,
                    99.5 if offset == 0 else 99.8,
                    100.0,
                    99.0,
                    99.8,
                    1_000,
                    99.7,
                ),
                MinuteBar(
                    "BSTRONG",
                    timestamp,
                    50.0 if offset == 0 else 50.8,
                    52.0,
                    48.0,
                    51.0,
                    5_000,
                    50.5,
                ),
            ]
        )
    return start, bars


def _competing_signals():
    start, bars = _opening_bars()
    bars.extend(
        [
            MinuteBar("AWEAK", start.replace(minute=45), 100.1, 101.2, 100.0, 101.0, 2_000, 100.2),
            MinuteBar("BSTRONG", start.replace(minute=45), 52.1, 53.2, 52.0, 53.0, 10_000, 51.5),
            MinuteBar("AWEAK", start.replace(minute=46), 101.0, 102.2, 100.9, 102.0, 2_000, 101.0),
            MinuteBar("BSTRONG", start.replace(minute=46), 53.0, 54.2, 52.9, 54.0, 10_000, 53.0),
        ]
    )
    return bars


def _exit_then_entry_same_minute():
    start, bars = _opening_bars()
    bars.extend(
        [
            MinuteBar("AWEAK", start.replace(minute=45), 99.8, 100.0, 99.5, 99.8, 1_000, 99.7),
            MinuteBar("BSTRONG", start.replace(minute=45), 52.1, 53.2, 52.0, 53.0, 10_000, 51.5),
            MinuteBar("AWEAK", start.replace(minute=46), 99.8, 100.0, 99.5, 99.8, 1_000, 99.7),
            MinuteBar("BSTRONG", start.replace(minute=46), 53.0, 53.2, 52.9, 53.1, 10_000, 53.0),
            MinuteBar("AWEAK", start.replace(minute=47), 100.1, 101.2, 100.0, 101.0, 2_000, 100.2),
            MinuteBar("BSTRONG", start.replace(minute=47), 53.1, 54.2, 53.0, 54.0, 10_000, 53.2),
            MinuteBar("AWEAK", start.replace(minute=48), 101.0, 102.2, 100.9, 102.0, 2_000, 101.0),
            MinuteBar("BSTRONG", start.replace(minute=48), 54.0, 54.1, 53.8, 54.0, 10_000, 54.0),
        ]
    )
    return bars


def _staggered_capacity_signals(current_price):
    bars = []
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for offset in range(15):
        timestamp = start.replace(minute=30 + offset)
        bars.extend(
            [
                MinuteBar("AEARLY", timestamp, 99.5, 100.0, 99.0, 99.8, 10_000, 99.7),
                MinuteBar("BLATER", timestamp, 49.5, 50.0, 49.0, 49.8, 20_000, 49.7),
            ]
        )
    bars.extend(
        [
            MinuteBar("AEARLY", start.replace(minute=45), 100.0, 100.2, 99.9, 100.1, 10_000, 100.0),
            MinuteBar("BLATER", start.replace(minute=45), 49.8, 49.9, 49.6, 49.8, 20_000, 49.7),
            MinuteBar("AEARLY", start.replace(minute=46), 100.0, 100.2, 99.8, 100.0, 10_000, 100.0),
            MinuteBar("BLATER", start.replace(minute=46), 49.8, 49.9, 49.6, 49.8, 20_000, 49.7),
            MinuteBar("AEARLY", start.replace(minute=47), current_price, min(100.3, current_price + 0.1), max(99.65, current_price - 0.05), current_price, 10_000, current_price),
            MinuteBar("BLATER", start.replace(minute=47), 50.0, 50.2, 49.9, 50.1, 20_000, 49.9),
            MinuteBar("AEARLY", start.replace(minute=48), current_price, min(100.3, current_price + 0.1), max(99.65, current_price - 0.05), current_price, 10_000, current_price),
            MinuteBar("BLATER", start.replace(minute=48), 50.1, 51.0, 50.0, 50.8, 20_000, 50.1),
        ]
    )
    return bars


if __name__ == "__main__":
    unittest.main()
