from copy import deepcopy
from datetime import timedelta

import pandas as pd
import pytest

from quantbot.intraday.data import FIELDS, load_bars, session_grid
from quantbot.intraday.research import prepare_sessions


def fixture_frame(protocol, market):
    sessions = session_grid(protocol, market)
    rows = []
    for opening, closing in sessions.values():
        for at in pd.date_range(opening, closing, freq="5min", inclusive="left"):
            for symbol in protocol["markets"][market]["symbols"]:
                rows.append([at.isoformat(), symbol, "100", "101", "99", "100", "10000"])
    return pd.DataFrame(rows, columns=FIELDS)


def protocol(market="stocks", start="2025-11-26", end="2025-11-29"):
    return {
        "range": {"start": start, "end_exclusive": end},
        "markets": {market: {"symbols": ["SPY", "QQQ"] if market == "stocks" else ["BTC/USDT", "ETH/USDT"]}},
    }


def test_us_calendar_respects_holiday_and_early_close(tmp_path):
    p = protocol()
    frame = fixture_frame(p, "stocks")
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    _, sessions, quality = load_bars(path, p, "stocks")
    assert list(sessions) == ["2025-11-26", "2025-11-28"]
    assert sessions["2025-11-28"][1] - sessions["2025-11-28"][0] == timedelta(hours=3, minutes=30)
    assert quality["complete"] and quality["rows"] == 240


def test_missing_bar_is_blocked_without_forward_fill(tmp_path):
    p = protocol()
    frame = fixture_frame(p, "stocks").iloc[:-1]
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    parsed, _, quality = load_bars(path, p, "stocks")
    assert not quality["complete"] and len(parsed) == len(frame)
    assert quality["by_symbol"]["QQQ"]["missing"] == 1


@pytest.mark.parametrize(
    "mutation,reason", [("duplicate", "重复"), ("bad_ohlc", "OHLCV"), ("naive", "时区"), ("outside", "窗口")]
)
def test_invalid_or_protected_rows_are_not_silently_repaired(tmp_path, mutation, reason):
    p = protocol()
    frame = fixture_frame(p, "stocks")
    if mutation == "duplicate":
        frame = pd.concat([frame, frame.iloc[:1]], ignore_index=True)
    elif mutation == "bad_ohlc":
        frame.loc[0, "open"] = "103"
    elif mutation == "naive":
        frame.loc[0, "timestamp"] = "2025-11-26T14:30:00"
    else:
        frame.loc[0, "timestamp"] = "2026-07-01T14:30:00+00:00"
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    with pytest.raises(ValueError, match=reason):
        load_bars(path, p, "stocks")


def test_features_use_only_prior_sessions_and_completed_bars(tmp_path):
    p = protocol("crypto", "2025-01-01", "2025-01-24")
    frame = fixture_frame(p, "crypto")
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    parsed, sessions, _ = load_bars(path, p, "crypto")
    before = prepare_sessions(parsed, sessions, p["markets"]["crypto"]["symbols"])
    altered = deepcopy(parsed)
    future = altered.timestamp >= pd.Timestamp("2025-01-23", tz="UTC")
    for key in ("open", "high", "low", "close"):
        altered.loc[future, key] = (altered.loc[future, key].astype(float) * 2).astype(str)
    after = prepare_sessions(altered, sessions, p["markets"]["crypto"]["symbols"])
    assert before["2025-01-22"]["signals"] == after["2025-01-22"]["signals"]
    # Today's doubled range must not contaminate today's already-known daily ATR.
    at = next(iter(after["2025-01-23"]["signals"]))
    assert (
        before["2025-01-23"]["signals"][at]["BTC/USDT"]["atr"]
        == after["2025-01-23"]["signals"][at]["BTC/USDT"]["atr"]
    )
    assert not before["2025-01-20"]["feature_valid"]
    assert before["2025-01-21"]["feature_valid"]


def test_daily_atr_retains_original_14_session_window(tmp_path):
    p = protocol("crypto", "2025-01-01", "2025-01-24")
    frame = fixture_frame(p, "crypto")
    for index in range(1, 24):
        mask = frame.timestamp.str.startswith(f"2025-01-{index:02d}")
        frame.loc[mask, "high"] = str(100 + index)
        frame.loc[mask, "low"] = str(100 - index)
    path = tmp_path / "bars.csv"
    frame.to_csv(path, index=False)
    parsed, sessions, _ = load_bars(path, p, "crypto")
    prepared = prepare_sessions(parsed, sessions, p["markets"]["crypto"]["symbols"])
    values = next(iter(prepared["2025-01-23"]["signals"].values()))
    assert values["BTC/USDT"]["atr"] == 31  # Prior days 9..22; a 20-day ATR would be 25.


def test_opening_range_and_later_bars_are_not_visible_in_earlier_features(tmp_path):
    p = protocol("crypto", "2025-01-01", "2025-01-24")
    path = tmp_path / "bars.csv"
    fixture_frame(p, "crypto").to_csv(path, index=False)
    frame, sessions, _ = load_bars(path, p, "crypto")
    before = prepare_sessions(frame, sessions, p["markets"]["crypto"]["symbols"])
    first = pd.Timestamp("2025-01-22T00:15Z")
    assert before["2025-01-22"]["signals"][first]["BTC/USDT"]["opening_high"] is None
    # The second half of the opening range is still future at 00:15.
    future = frame.timestamp >= first
    for key in ("open", "high", "low", "close", "volume"):
        frame.loc[future, key] = (frame.loc[future, key].astype(float) * 2).astype(str)
    after = prepare_sessions(frame, sessions, p["markets"]["crypto"]["symbols"])
    assert before["2025-01-22"]["signals"][first] == after["2025-01-22"]["signals"][first]
