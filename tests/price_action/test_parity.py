"""回测 ↔ 实盘信号一致性（Parity）测试。

"回测出来的系统就是实盘交易的系统"是本项目的核心承诺，
但回测引擎与实盘 runner 的**编排层**（候选池构建、槽位计数、
止损检查顺序）是两份独立实现——本测试机械验证两者在同一份数据、
同一个决策日上做出完全一致的决策，永久防止两边悄悄漂移。

对齐方式：
    引擎跑到 asof-1 → 期末持仓（含止损等状态）灌给 runner；
    引擎跑到 asof   → 当日盘中止损离场 + 收盘排队的离场/入场决策；
    runner 在 asof+1 早晨用 asof 完整 K 线决策 → 两侧必须一致：
        - runner 的止损卖出 == 引擎 asof 当日的 stop 离场
        - runner 的入场买入 == 引擎 asof 收盘排队的入场（代码与止损价均相等）
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

import quantbot.price_action.live.runner as runner_mod
from quantbot.price_action.backtest.engine import BacktestEngine
from quantbot.price_action.live.runner import LiveRunner

from .conftest import MockBroker, make_cfg, make_trending_ohlcv, mock_runtime_paths


@pytest.fixture()
def parity_setup(tmp_path, monkeypatch):
    """构造场景：2 个持仓槽位；BBB 在最后一天崩盘触发止损，
    腾出的槽位应让引擎与 runner 排队同一个新入场。

    n=397 使最后一天落在锯齿行情的上涨腿（396 % 10 == 6），
    保证未持仓的趋势股当天有突破信号可排队。"""
    n = 397
    aaa = make_trending_ohlcv(n, start_price=100.0)
    bbb = make_trending_ohlcv(n, start_price=50.0)
    ccc = make_trending_ohlcv(n, start_price=80.0)
    spy = make_trending_ohlcv(n, start_price=300.0)

    # BBB 最后一天崩盘 -25%：盘中击穿任何合理的吊灯止损
    i = n - 1
    prev = float(bbb.iloc[i - 1]["close"])
    bbb.iloc[i, bbb.columns.get_indexer(["open"])[0]] = round(prev * 0.97, 2)
    bbb.iloc[i, bbb.columns.get_indexer(["high"])[0]] = round(prev * 0.975, 2)
    bbb.iloc[i, bbb.columns.get_indexer(["low"])[0]] = round(prev * 0.72, 2)
    bbb.iloc[i, bbb.columns.get_indexer(["close"])[0]] = round(prev * 0.75, 2)

    data = {"SPY": spy, "AAA": aaa, "BBB": bbb, "CCC": ccc}
    cfg = make_cfg(tmp_path)
    cfg["risk"]["max_positions"] = 2   # AAA/BBB 占满，CCC 排队候补
    cfg["backtest"]["start"] = str(spy.index[280].date())
    cfg["backtest"]["end"] = str(spy.index[-1].date())

    monkeypatch.setattr(runner_mod, "notify", lambda t: True)
    monkeypatch.setattr(runner_mod, "heartbeat", lambda: True)
    monkeypatch.setattr(runner_mod, "UNIVERSE", ["AAA", "BBB", "CCC"])
    # 合成数据可截取到较早日期，放宽 runner 的数据陈旧熔断
    monkeypatch.setattr(LiveRunner, "STALE_DAYS", 10_000)
    return data, cfg


def _run_engine(data, cfg, end_date):
    cfg = json.loads(json.dumps(cfg))
    cfg["backtest"]["end"] = str(end_date.date())
    return BacktestEngine(data, cfg).run()


def test_engine_runner_decision_parity(parity_setup, tmp_path):
    data, cfg = parity_setup
    dates = data["SPY"].index
    asof = dates[-1]

    # ---- 引擎跑到 asof-1：取其期末持仓状态灌给 runner ----
    res_prev = _run_engine(data, cfg, dates[-2])
    held = res_prev.open_positions
    assert len(held) == 2 and "BBB" in held, \
        "前置条件：引擎应满仓 2 只且含即将崩盘的 BBB"

    # ---- 引擎跑到 asof：当日止损离场 + 收盘排队决策 ----
    res_full = _run_engine(data, cfg, asof)
    engine_stop_exits = {
        t.symbol for t in res_full.trades
        if t.exit_date == asof and t.exit_reason == "stop"
    }
    engine_entries = {
        (sym, round(stop, 6)) for sym, stop, _trig, d in
        res_full.final_signals["entries"] if d == 1
    }
    # 场景健全性：崩盘必须触发止损，腾出的槽位必须排队一个新入场
    assert "BBB" in engine_stop_exits, "前置条件：引擎在 asof 应止损 BBB"
    assert engine_entries, "前置条件：引擎应在 asof 收盘排队新入场"

    # ---- runner 在 asof+1 早晨用相同数据决策 ----
    positions = {}
    state = {}
    for sym, p in held.items():
        positions[sym] = p["shares"] * p["direction"]
        state[sym] = {
            "stop": p["stop"], "highest_close": p["highest_close"],
            "entry_price": p["entry_price"], "entry_date": "2020-01-01",
            "init_risk": p["init_risk"], "adds": p["adds"],
            "direction": p["direction"], "qty": p["shares"],
        }
    with mock_runtime_paths(cfg).state_file.open("w", encoding="utf-8") as f:
        json.dump({"meta": {}, "positions": state}, f)

    broker = MockBroker(positions=positions)
    LiveRunner(broker, cfg, mode="dry-run").run_once(force=True, data=data)

    runner_sells = {s for side, s, _ in broker.orders if side == "sell"}
    runner_buys = {s for side, s, _ in broker.orders if side == "buy"}

    # ---- 一致性断言 ----
    assert runner_sells == engine_stop_exits, \
        f"离场决策不一致: runner={runner_sells} engine={engine_stop_exits}"
    assert runner_buys == {s for s, _ in engine_entries}, \
        f"入场决策不一致: runner={runner_buys} engine={engine_entries}"

    # 入场止损价必须逐位一致（两边调用同一 entry_signal 于同一行）
    new_state = json.loads(mock_runtime_paths(cfg).state_file.read_text(encoding="utf-8"))
    for sym, stop in engine_entries:
        assert new_state["positions"][sym]["stop"] == pytest.approx(stop, abs=1e-6), \
            f"{sym} 的入场止损价不一致"


def test_parity_no_action_day(parity_setup, tmp_path):
    """无事发生的一天：引擎无排队决策 ↔ runner 无任何下单。"""
    data, cfg = parity_setup
    dates = data["SPY"].index
    quiet_day = dates[-10]   # 崩盘之前的平静日

    res_prev = _run_engine(data, cfg, dates[-11])
    res_full = _run_engine(data, cfg, quiet_day)
    engine_acts = (
        # 当日真实离场（剔除期末清算的记账项 'end'——那不是决策）
        {t.symbol for t in res_full.trades
         if t.exit_date == quiet_day and t.exit_reason != "end"}
        | {s for s, *_ in res_full.final_signals["entries"]}
        | {s for s, _ in res_full.final_signals["exits"]}
    )

    held = res_prev.open_positions
    positions, state = {}, {}
    for sym, p in held.items():
        positions[sym] = p["shares"] * p["direction"]
        state[sym] = {
            "stop": p["stop"], "highest_close": p["highest_close"],
            "entry_price": p["entry_price"], "entry_date": "2020-01-01",
            "init_risk": p["init_risk"], "adds": p["adds"],
            "direction": p["direction"], "qty": p["shares"],
        }
    with mock_runtime_paths(cfg).state_file.open("w", encoding="utf-8") as f:
        json.dump({"meta": {}, "positions": state}, f)

    trimmed = {s: df.loc[:quiet_day] for s, df in data.items()}
    broker = MockBroker(positions=positions)
    LiveRunner(broker, cfg, mode="dry-run").run_once(force=True, data=trimmed)
    runner_acts = {s for _side, s, _ in broker.orders}

    assert runner_acts == engine_acts, \
        f"平静日决策不一致: runner={runner_acts} engine={engine_acts}"
