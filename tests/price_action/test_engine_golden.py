"""回测引擎黄金样例：合成数据上的撮合与资金守恒不变量。

引擎此前完全没有测试覆盖——本文件用可控的合成行情锁住核心撮合语义：
    1. 入场成交价 = 信号次日开盘价 ×（1 + 滑点）
    2. 通道离场成交价 = 次日开盘价 ×（1 - 滑点）
    3. 盘中止损成交价落在当日价格区间内（含滑点修正）
    4. 资金守恒：期末权益 = 初始资金 + 全部交易盈亏之和
       （零佣金、关闭现金计息时严格成立）
任何引擎改动（含向量化重构）都必须保持这些断言通过。
"""

from __future__ import annotations

import copy

import pandas as pd
import pytest

from quantbot.price_action.backtest.engine import BacktestEngine

from .conftest import make_cfg, make_trending_ohlcv


@pytest.fixture(scope="module")
def golden_result():
    """在锯齿上升 + 末段崩盘的合成行情上跑一次完整回测。"""
    data = {
        "SPY": make_trending_ohlcv(400, start_price=300.0),
        "TST": make_trending_ohlcv(400, start_price=100.0),
    }
    # 末段人工制造崩盘：连续大阴线击穿一切止损，保证产生离场交易
    tst = data["TST"].copy()
    n = len(tst)
    for i in range(n - 15, n):
        prev_close = tst.iloc[i - 1]["close"]
        c = round(prev_close * 0.94, 2)
        tst.iloc[i, tst.columns.get_indexer(["open"])[0]] = round(prev_close * 0.97, 2)
        tst.iloc[i, tst.columns.get_indexer(["close"])[0]] = c
        tst.iloc[i, tst.columns.get_indexer(["high"])[0]] = round(prev_close * 0.975, 2)
        tst.iloc[i, tst.columns.get_indexer(["low"])[0]] = round(c * 0.99, 2)
    data["TST"] = tst

    cfg = make_cfg()
    # 回测窗口留足特征回看期（前 280 根做预热）
    cfg["backtest"]["start"] = str(tst.index[280].date())
    cfg["backtest"]["end"] = str(tst.index[-1].date())
    result = BacktestEngine(data, cfg).run()
    return data, cfg, result


def test_produces_trades(golden_result):
    """合成的趋势 + 崩盘行情必须产生至少一笔完整交易。"""
    _, _, result = golden_result
    assert len(result.trades) >= 1
    assert (result.equity > 0).all()


def test_entry_price_is_next_open(golden_result):
    """入场成交价 = 执行日开盘价（零滑点配置下严格相等）。"""
    data, _, result = golden_result
    tst = data["TST"]
    for t in result.trades:
        assert t.symbol == "TST"
        assert t.entry_price == pytest.approx(
            float(tst.loc[t.entry_date, "open"]), abs=1e-9
        ), f"{t.entry_date} 的入场价不等于当日开盘价"


def test_exit_price_within_day_range(golden_result):
    """离场成交价必须落在离场当日的价格区间内（无凭空价格）。"""
    data, _, result = golden_result
    tst = data["TST"]
    for t in result.trades:
        if t.exit_reason == "end":
            continue  # 期末清算按收盘价，单独一类
        day = tst.loc[t.exit_date]
        assert day["low"] - 1e-9 <= t.exit_price <= day["high"] + 1e-9, \
            f"{t.exit_date} 的离场价 {t.exit_price} 超出当日区间"


def test_channel_exit_at_open(golden_result):
    """通道离场（收盘信号）在次日开盘成交。"""
    data, _, result = golden_result
    tst = data["TST"]
    for t in result.trades:
        if t.exit_reason == "channel":
            assert t.exit_price == pytest.approx(
                float(tst.loc[t.exit_date, "open"]), abs=1e-9
            )


def test_cash_conservation(golden_result):
    """资金守恒：期末权益 = 初始资金 + Σ交易盈亏（零佣金零利息）。"""
    _, cfg, result = golden_result
    initial = cfg["backtest"]["initial_capital"]
    total_pnl = sum(t.pnl for t in result.trades)
    final_equity = float(result.equity.iloc[-1])
    # pnl 有 2 位小数舍入，容差按笔数放宽
    assert final_equity == pytest.approx(initial + total_pnl,
                                         abs=0.02 * max(len(result.trades), 1))


def test_slippage_applied():
    """滑点配置生效：买入价上浮、卖出价下调各 slip。"""
    data = {
        "SPY": make_trending_ohlcv(400, start_price=300.0),
        "TST": make_trending_ohlcv(400, start_price=100.0),
    }
    cfg = make_cfg()
    cfg["backtest"]["slippage_bps"] = 10  # 10bp 便于区分
    cfg["backtest"]["start"] = str(data["TST"].index[280].date())
    cfg["backtest"]["end"] = str(data["TST"].index[-1].date())
    result = BacktestEngine(data, cfg).run()
    assert len(result.trades) >= 1
    tst = data["TST"]
    t = result.trades[0]
    expected = float(tst.loc[t.entry_date, "open"]) * (1 + 0.001)
    assert t.entry_price == pytest.approx(expected, rel=1e-9)


def test_position_size_respects_risk_budget(golden_result):
    """首笔交易的初始美元风险不超过单笔风险预算（2.5% 权益）。"""
    _, cfg, result = golden_result
    t = result.trades[0]
    # 初始风险 = 股数 × 每股风险；每股风险无从交易记录直接读出，
    # 用入场价与市值上限做弱校验：市值不超过权益上限
    max_value = cfg["backtest"]["initial_capital"] * cfg["risk"]["max_weight"]
    assert t.entry_price * t.shares <= max_value * 1.05  # 5% 容差（权益浮动）


def test_deterministic_rerun(golden_result):
    """同一数据同一配置重跑，权益曲线与交易记录逐位一致。"""
    data, cfg, result = golden_result
    result2 = BacktestEngine(copy.deepcopy(data), copy.deepcopy(cfg)).run()
    pd.testing.assert_series_equal(result.equity, result2.equity)
    assert [t.__dict__ for t in result.trades] == [t.__dict__ for t in result2.trades]
