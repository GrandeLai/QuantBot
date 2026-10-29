"""Synthetic checks for synchronous two-symbol features; no market data is opened."""

import pandas as pd
import pytest

from quantbot.intraday import research


def history(symbols, *, days=1):
    sessions, rows = {}, []
    paths = ([100, 100.4, 101.2, 101.6], [100, 100.2, 100.8, 100.0], [100, 101, 102, 103])
    for day in range(days):
        opening = pd.Timestamp("2025-01-02T09:30:00Z") + pd.Timedelta(days=day)
        closing = opening + pd.Timedelta(hours=4)
        sessions[opening.date().isoformat()] = (opening, closing)
        for i, stamp in enumerate(pd.date_range(opening, closing, freq="5min", inclusive="left")):
            for symbol, path in zip(symbols, paths, strict=False):
                close = path[min(i // 3, len(path) - 1)] * (day + 1)
                rows.append({"timestamp": stamp, "symbol": symbol, "open": close,
                             "high": close + 0.1, "low": close - 0.1, "close": close, "volume": 10000})
    return pd.DataFrame(rows), sessions


@pytest.mark.parametrize("symbols", [["BTC/USDT", "ETH/USDT"], ["SPY", "QQQ"]])
def test_pair_uses_both_symbols_at_same_completed_timestamp(symbols):
    frame, sessions = history(symbols)
    prepared = research.prepare_sessions(frame, sessions, symbols)
    signals = prepared["2025-01-02"]["signals"]
    now = signals[pd.Timestamp("2025-01-02T10:15:00Z")]
    assert now[symbols[0]]["net_move_30m_bps"] == pytest.approx(120)
    assert now[symbols[1]]["net_move_30m_bps"] == pytest.approx(80)
    assert all(v["pair_net_move_30m_bps"] == pytest.approx(80) for v in now.values())
    later = signals[pd.Timestamp("2025-01-02T10:30:00Z")]
    assert all(v["pair_net_move_30m_bps"] < 0 for v in later.values())
    # Symbol order cannot select a different peer or use its preceding timestamp.
    reversed_signals = research.prepare_sessions(frame, sessions, list(reversed(symbols)))
    assert reversed_signals["2025-01-02"]["signals"] == signals


def test_future_price_mutation_cannot_change_completed_pair_features():
    symbols = ["BTC/USDT", "ETH/USDT"]
    frame, sessions = history(symbols)
    before = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    boundary = pd.Timestamp("2025-01-02T10:15:00Z")
    later = frame["timestamp"] >= boundary
    for field in ("open", "high", "low", "close"):
        frame.loc[later, field] *= 10
    after = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    for stamp in before:
        if stamp <= boundary:
            assert after[stamp] == before[stamp]
    assert after[boundary + pd.Timedelta(minutes=15)] != before[boundary + pd.Timedelta(minutes=15)]


def test_missing_session_history_is_unavailable_and_resets_each_day():
    symbols = ["SPY", "QQQ"]
    frame, sessions = history(symbols, days=2)
    prepared = research.prepare_sessions(frame, sessions, symbols)
    for day in sessions:
        values = list(prepared[day]["signals"].values())
        for first in values[:2]:
            assert all(v["net_move_30m_bps"] is None and v["pair_net_move_30m_bps"] is None
                       for v in first.values())
        assert all(v["pair_net_move_30m_bps"] == pytest.approx(80) for v in values[2].values())


@pytest.mark.parametrize("symbols", [["BTC/USDT"], ["BTC/USDT", "ETH/USDT", "SOL/USDT"]])
def test_pair_requires_exactly_two_symbols_and_cannot_confirm_itself(symbols):
    frame, sessions = history(symbols)
    signals = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    assert any(v["net_move_30m_bps"] is not None for values in signals.values() for v in values.values())
    assert all(v["pair_net_move_30m_bps"] is None for values in signals.values() for v in values.values())
