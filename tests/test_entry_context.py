"""冻结入场面板的时间对齐、因果性和无效值；只用合成行情。"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import math

import pandas as pd
import pytest

from quantbot.ai.config import load_config
from quantbot.ai.feeds import Snapshot, stock_calendar
from quantbot.factors.entry_context import FACTOR_DEFINITIONS, FACTOR_IDS, build_entry_panel, factor_pass


def crypto_snapshot(count=260, rows=None):
    config = load_config(environ={})
    config["market"].update(symbols=["BTC/USDT", "ETH/USDT"], benchmark="BTC/USDT")
    if rows is None:
        rows = []
        for index in range(count):
            at = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=index)
            for symbol in config["market"]["symbols"]:
                close = 100 * math.exp(index / 500) * (1 + 0.01 * math.sin(index)) \
                    if symbol == "BTC/USDT" else 500 - index
                rows.append({"timestamp": at.isoformat(), "symbol": symbol, "bar": "1d",
                             "open": close, "high": close + (2 if symbol == "BTC/USDT" else 1),
                             "low": close - (1 if symbol == "BTC/USDT" else 4), "close": close,
                             "volume": 1000 if symbol == "BTC/USDT" else 100 + index ** 2})
    return Snapshot("crypto", rows, config, "synthetic_test")


@pytest.mark.parametrize("factor_id,threshold,below,above", [
    ("volume_confirmation", 1.0, False, True),
    ("close_location", 0.75, False, True),
    ("volatility_compression", 1.0, True, False),
    ("trend_persistence", 0.8, False, True),
])
def test_frozen_factor_thresholds_and_invalid_values(factor_id, threshold, below, above):
    assert factor_pass(factor_id, threshold)
    assert factor_pass(factor_id, threshold - 0.001) is below
    assert factor_pass(factor_id, threshold + 0.001) is above
    for missing in (None, float("nan"), float("inf"), -float("inf")):
        assert factor_pass(factor_id, missing) is False
    assert tuple(d["id"] for d in FACTOR_DEFINITIONS) == FACTOR_IDS
    assert json.loads(json.dumps(FACTOR_DEFINITIONS, allow_nan=False))[FACTOR_IDS.index(factor_id)]["threshold"] \
        == threshold
    with pytest.raises(ValueError, match="未登记"):
        factor_pass("unfrozen_factor", None)


def test_crypto_panel_is_causal_and_does_not_change_snapshot():
    original = crypto_snapshot()
    saved = deepcopy(original.rows)
    before = build_entry_panel(original)
    changed_rows = deepcopy(saved)
    cutoff = original.times[220].isoformat()
    for row in changed_rows[221 * 2:]:
        row.update(open=10000, high=11000, low=9000, close=10000, volume=1)
    after = build_entry_panel(crypto_snapshot(rows=changed_rows))
    pd.testing.assert_frame_equal(before.loc[before.as_of <= cutoff], after.loc[after.as_of <= cutoff])
    assert original.rows == saved
    assert original._contexts == {}
    assert len(before) == len(original.times) * 2
    assert set(before.as_of) == {at.astimezone(UTC).isoformat() for at in original.times}
    assert before.as_of.iloc[0] == "2024-01-02T00:00:00+00:00"  # 开盘 + 1 日。


def test_symbols_are_independent_but_share_benchmark_state_and_rows_can_be_unsorted():
    original = crypto_snapshot()
    panel = build_entry_panel(original)
    shuffled = crypto_snapshot(rows=list(reversed(original.rows)))
    pd.testing.assert_frame_equal(panel, build_entry_panel(shuffled))
    final = panel.loc[panel.as_of == panel.as_of.max()].set_index("symbol")
    assert final.loc["BTC/USDT", "asset_trend"] == "up"
    assert final.loc["ETH/USDT", "asset_trend"] == "down"
    assert final.loc["BTC/USDT", "market_state"].startswith("up_")
    assert final.loc["BTC/USDT", "market_state"] == final.loc["ETH/USDT", "market_state"]
    assert final.loc["BTC/USDT", "close_location"] == pytest.approx(1 / 3)
    assert final.loc["ETH/USDT", "close_location"] == pytest.approx(0.8)
    assert final.loc["BTC/USDT", "volume_confirmation"] == 1
    assert final.loc["ETH/USDT", "volume_confirmation"] > 1
    altered = deepcopy(original.rows)
    for row in altered:
        if row["symbol"] == "ETH/USDT":
            row.update(open=100, high=101, low=99, close=100, volume=0)
    changed = build_entry_panel(crypto_snapshot(rows=altered))
    pd.testing.assert_frame_equal(panel.loc[panel.symbol == "BTC/USDT"],
                                  changed.loc[changed.symbol == "BTC/USDT"])


def test_volatility_median_uses_126_prior_observations_and_excludes_current():
    original = crypto_snapshot()
    panel = build_entry_panel(original).query("symbol == 'BTC/USDT'").reset_index(drop=True)
    index = 220
    prices = pd.Series([row["close"] for row in original.rows if row["symbol"] == "BTC/USDT"])
    rv20 = prices.pct_change(fill_method=None).rolling(20).std(ddof=1)
    expected = rv20.iloc[index - 126:index].median()
    assert panel.loc[index, "prior_rv20_median126"] == pytest.approx(expected)
    assert panel.loc[:145, "prior_rv20_median126"].isna().all()
    assert pd.notna(panel.loc[146, "prior_rv20_median126"])
    changed_rows = deepcopy(original.rows)
    changed_rows[index * 2].update(open=1000, high=1001, low=999, close=1000)
    changed = build_entry_panel(crypto_snapshot(rows=changed_rows)).query("symbol == 'BTC/USDT'").reset_index(drop=True)
    assert changed.loc[index, "prior_rv20_median126"] == panel.loc[index, "prior_rv20_median126"]
    assert changed.loc[index, "rv20"] > panel.loc[index, "rv20"]
    assert changed.loc[index, "market_state"] == "up_high"


def test_warmup_and_zero_denominators_remain_invalid_without_filling():
    original = crypto_snapshot(count=220)
    for row in original.rows:
        row.update(open=100, high=100, low=100, close=100, volume=0)
    panel = build_entry_panel(crypto_snapshot(rows=original.rows))
    base = panel.query("symbol == 'BTC/USDT'").reset_index(drop=True)
    assert base.loc[:198, "market_state"].eq("unavailable").all()
    assert base.loc[199:, "market_state"].eq("mixed_normal").all()
    assert base.loc[:27, "trend_persistence"].isna().all()
    assert base.loc[28:, "trend_persistence"].eq(0).all()
    assert base.loc[28:, "trend_persistence_valid"].all()
    for factor_id in ("volume_confirmation", "close_location", "volatility_compression"):
        assert base[factor_id].isna().all()
        assert not base[f"{factor_id}_valid"].any()
        assert not any(factor_pass(factor_id, value) for value in base[factor_id])


def test_current_zero_volume_is_valid_zero_but_previous_zero_mean_is_invalid():
    original = crypto_snapshot(count=40)
    original.rows[-2]["volume"] = 0
    panel = build_entry_panel(crypto_snapshot(rows=original.rows))
    last = panel.query("symbol == 'BTC/USDT'").iloc[-1]
    assert last.volume_confirmation == 0
    assert last.volume_confirmation_valid
    assert not factor_pass("volume_confirmation", last.volume_confirmation)


@pytest.mark.parametrize("start,end,expected", [
    ("2024-11-27", "2024-11-29", ["2024-11-27T21:00:00+00:00", "2024-11-29T18:00:00+00:00"]),
    ("2024-03-08", "2024-03-11", ["2024-03-08T21:00:00+00:00", "2024-03-11T20:00:00+00:00"]),
])
def test_stock_panel_uses_snapshot_session_closes_for_early_close_and_dst(start, end, expected):
    config = load_config(environ={})
    config["market"].update(symbols=["SPY", "QQQ"], benchmark="SPY")
    calendar = stock_calendar(start, end)
    rows = [{"date": str(at.date()), "symbol": symbol, "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 1000}
            for at in calendar.sessions_in_range(start, end) for symbol in config["market"]["symbols"]]
    snapshot = Snapshot("stocks", rows, config, "synthetic_test", calendar)
    panel = build_entry_panel(snapshot)
    assert panel.as_of.unique().tolist() == expected
    assert panel.market_state.eq("unavailable").all()
    assert len(panel) == 4


def test_panel_rejects_non_daily_crypto_and_missing_fixed_benchmark():
    snapshot = crypto_snapshot(count=2)
    snapshot.rows[0]["bar"] = "1h"
    with pytest.raises(ValueError, match="1d"):
        build_entry_panel(snapshot)
    snapshot.config["market"]["symbols"] = ["ETH/USDT"]
    with pytest.raises(ValueError, match="BTC/USDT"):
        build_entry_panel(snapshot)
