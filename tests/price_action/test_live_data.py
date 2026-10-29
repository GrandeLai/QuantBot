"""生产行情范围按完整信号日收缩，保留退出指数持仓和历史池决策一致性。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import quantbot.price_action.data.downloader as downloader
from quantbot.price_action.live.runner import LiveRunner, drop_partial_bar

from .conftest import MockBroker, make_cfg, make_trending_ohlcv, mock_runtime_paths
from .test_runner_live import base_state, write_state


@pytest.fixture
def external_market(monkeypatch):
    raw = {symbol: make_trending_ohlcv(397, start_price=price)
           for symbol, price in (("SPY", 300), ("^IRX", 5), ("CURRENT", 100),
                                 ("SIGNAL", 80), ("NEXT", 90), ("FUTURE", 110),
                                 ("HELD", 70), ("HISTORICAL", 60))}
    for frame in raw.values():
        frame.loc[pd.Timestamp("2026-09-11")] = frame.iloc[-1]
    membership = pd.DataFrame({
        "date": ["2020-01-01", "2026-09-09", "2026-09-11", "2026-09-12"],
        "tickers": ["HISTORICAL,CURRENT", "CURRENT,SIGNAL", "CURRENT,NEXT", "CURRENT,FUTURE"],
    }).to_csv(index=False).encode()
    requests = []

    def download(symbols, **kwargs):
        symbols = [symbols] if isinstance(symbols, str) else list(symbols)
        requests.append(symbols)
        frames = {symbol: raw[symbol].rename(columns=str.title).copy() for symbol in symbols}
        if len(symbols) == 1 and not kwargs.get("multi_level_index", True):
            return frames[symbols[0]]
        return pd.concat(frames, axis=1)

    monkeypatch.setattr(downloader.yf, "download", download)
    monkeypatch.setattr("quantbot.price_action.data.sp500_pit.requests.get", lambda *args, **kwargs:
                        SimpleNamespace(content=membership, raise_for_status=lambda: None))
    return SimpleNamespace(raw=raw, requests=requests)


@pytest.mark.parametrize("hour,signal_day,expected_member", [
    (9, "2026-09-10", "SIGNAL"), (16, "2026-09-11", "NEXT"),
])
def test_refresh_uses_complete_signal_day_and_keeps_outside_positions(
    tmp_path, market_clock, external_market, hour, signal_day, expected_member,
):
    market_clock.now = market_clock.now.replace(hour=hour)
    cfg = make_cfg(tmp_path)
    cfg["universe"]["mode"] = "sp500_pit"
    cfg["risk"]["max_positions"] = 0
    cfg["data"]["snapshot"] = "untouched-research-snapshot"
    before = copy.deepcopy(cfg)
    broker = MockBroker(positions={"HELD": 3})
    runner = LiveRunner(broker, cfg, mode="dry-run")

    runner.run_once(force=True)

    requested = [symbol for batch in external_market.requests for symbol in batch]
    assert set(requested) == {"SPY", "^IRX", "CURRENT", expected_member, "HELD"}
    assert len(requested) == len(set(requested))
    state = json.loads(runner.state_path.read_text())
    assert state["meta"]["last_run_date"] == signal_day
    assert state["positions"]["HELD"]["qty"] == 3
    assert broker.orders == []
    assert cfg == before and runner.cfg == before
    assert not Path(cfg["data"]["cache_dir"]).exists()


def test_outside_index_position_still_gets_stop_exit(tmp_path, external_market):
    cfg = make_cfg(tmp_path)
    cfg["universe"]["mode"] = "sp500_pit"
    cfg["risk"]["max_positions"] = 0
    low = float(external_market.raw["HELD"].loc["2026-09-10", "low"])
    write_state(cfg, {"HELD": base_state(stop=low + 0.1, qty=3)})
    broker = MockBroker(positions={"HELD": 3})

    runner = LiveRunner(broker, cfg, mode="dry-run")
    runner.run_once(force=True)

    assert broker.orders == [("sell", "HELD", 3)]
    assert "HELD" not in json.loads(runner.state_path.read_text())["positions"]


def test_selected_refresh_preserves_full_history_decisions(tmp_path, external_market):
    low = float(external_market.raw["HELD"].loc["2026-09-10", "low"])
    results = []
    for name, data in (("full", drop_partial_bar(external_market.raw)), ("selected", None)):
        cfg = make_cfg(tmp_path / name)
        cfg["universe"]["mode"] = "sp500_pit"
        cfg["risk"]["max_positions"] = 2
        paths = mock_runtime_paths(cfg)
        # 注入全量数据的基线也使用同一份点时成员表。
        from quantbot.price_action.data.sp500_pit import load_membership
        load_membership(paths.cache_dir, force=True)
        write_state(cfg, {"HELD": base_state(stop=low + 0.1, qty=3)})
        broker = MockBroker(positions={"HELD": 3})
        runner = LiveRunner(broker, cfg, mode="dry-run")
        runner.run_once(force=True, data=data)
        results.append((broker.orders, json.loads(runner.state_path.read_text())))

    assert ("sell", "HELD", 3) in results[0][0]
    assert any(side == "buy" for side, _, _ in results[0][0])
    assert results[0] == results[1]
    assert "HISTORICAL" not in {s for batch in external_market.requests for s in batch}


def test_single_symbol_batch_accepts_yfinance_shape(tmp_path, external_market):
    data = downloader.load_all_batch(["CURRENT"], "2020-01-01", "2026-09-12", tmp_path,
                                     force=True, allow_empty_markers=False)
    pd.testing.assert_frame_equal(
        data["CURRENT"], external_market.raw["CURRENT"], check_names=False, check_freq=False,
    )
