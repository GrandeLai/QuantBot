"""预筛选快路径与逐股慢路径的等价性测试。

引擎的入场扫描有两条路径：
    use_prefilter=True   向量化预筛选剪枝（默认，快 5-10 倍）
    use_prefilter=False  逐日逐股全扫描（原始实现，逻辑单一来源）

两条路径必须产生**逐位一致**的权益曲线与交易记录——
本测试永久锁定该等价性，任何预筛选逻辑的改动若引入行为差异，
在这里立刻暴露。
"""

from __future__ import annotations

import copy

import pandas as pd

from quantbot.price_action.backtest.engine import BacktestEngine

from .conftest import make_cfg, make_trending_ohlcv


def _run(data, cfg, use_prefilter: bool):
    cfg = copy.deepcopy(cfg)
    cfg["backtest"]["use_prefilter"] = use_prefilter
    return BacktestEngine(copy.deepcopy(data), cfg).run()


def test_prefilter_equivalence_long():
    """多头路径：快慢两条路径输出逐位一致。"""
    data = {
        "SPY": make_trending_ohlcv(400, start_price=300.0),
        "AAA": make_trending_ohlcv(400, start_price=100.0),
        "BBB": make_trending_ohlcv(400, start_price=50.0),
    }
    cfg = make_cfg()
    cfg["backtest"]["start"] = str(data["AAA"].index[280].date())
    cfg["backtest"]["end"] = str(data["AAA"].index[-1].date())

    fast = _run(data, cfg, True)
    slow = _run(data, cfg, False)
    pd.testing.assert_series_equal(fast.equity, slow.equity)
    assert [t.__dict__ for t in fast.trades] == [t.__dict__ for t in slow.trades]


def test_prefilter_equivalence_relative_mom_with_nan_bench():
    """relative 动量模式 + 基准历史不足（策略动量为 NaN 的交易日）：
    快慢路径仍必须逐位一致。

    回归背景：预筛选曾把 NaN 阈值当作恒不通过（mom > NaN 恒 False），
    而 entry_signal 把缺失的基准动量回退为 0 照常产生信号——
    两条路径在该分支上产生过完全不同的交易序列。
    """
    stocks = {
        "AAA": make_trending_ohlcv(400, start_price=100.0),
        "BBB": make_trending_ohlcv(400, start_price=50.0),
    }
    spy_full = make_trending_ohlcv(400, start_price=300.0)
    data = {"SPY": spy_full.iloc[-200:], **stocks}  # 基准只有 200 根历史

    cfg = make_cfg()
    cfg["strategy"]["momentum_filter"] = "relative"
    cfg["regime"]["momentum_window"] = 63   # 开关先于策略动量(126)就绪 → 触发 NaN 窗口
    # 回测窗口从基准第 80 根起：档位已开、但基准 126 日动量仍为 NaN
    cfg["backtest"]["start"] = str(data["SPY"].index[80].date())
    cfg["backtest"]["end"] = str(data["SPY"].index[-1].date())

    fast = _run(data, cfg, True)
    slow = _run(data, cfg, False)
    pd.testing.assert_series_equal(fast.equity, slow.equity)
    assert [t.__dict__ for t in fast.trades] == [t.__dict__ for t in slow.trades]
    assert len(fast.trades) > 0, "场景必须真实产生交易，否则测不到该分支"


def test_prefilter_equivalence_with_shorts():
    """含做空模块时（下跌行情触发空头信号）两条路径仍逐位一致。"""
    up = make_trending_ohlcv(400, start_price=300.0)
    # 构造后半段持续下跌的标的与基准（触发档位 0 与空头信号）
    down_bench = up.copy()
    n = len(down_bench)
    for i in range(n // 2, n):
        prev = down_bench.iloc[i - 1]["close"]
        c = round(prev * 0.995, 2)
        down_bench.iloc[i, down_bench.columns.get_indexer(["open"])[0]] = prev
        down_bench.iloc[i, down_bench.columns.get_indexer(["close"])[0]] = c
        down_bench.iloc[i, down_bench.columns.get_indexer(["high"])[0]] = round(prev * 1.002, 2)
        down_bench.iloc[i, down_bench.columns.get_indexer(["low"])[0]] = round(c * 0.998, 2)

    data = {
        "SPY": down_bench,
        "AAA": down_bench.copy() * 0.5,   # 同步下跌的个股（候选空头）
        "BBB": make_trending_ohlcv(400, start_price=50.0),
    }
    cfg = make_cfg()
    cfg["shorts"] = {"enabled": True, "max_positions": 5, "borrow_rate": 0.01}
    cfg["backtest"]["start"] = str(down_bench.index[280].date())
    cfg["backtest"]["end"] = str(down_bench.index[-1].date())

    fast = _run(data, cfg, True)
    slow = _run(data, cfg, False)
    pd.testing.assert_series_equal(fast.equity, slow.equity)
    assert [t.__dict__ for t in fast.trades] == [t.__dict__ for t in slow.trades]
