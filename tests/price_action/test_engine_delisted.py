"""僵尸持仓强制清算的单元测试：数据中断 5 日后按最后收盘价清算。

锁定多空两个方向的现金处理正确性（清算是本轮新增路径，
错误的符号会直接造成资金账户错乱）。
"""

from __future__ import annotations

import pandas as pd
import pytest

from quantbot.price_action.backtest.engine import BacktestEngine

from .conftest import make_cfg, make_trending_ohlcv


def _truncated_data():
    """TST 在倒数第 30 天起停止有数据（模拟退市摘牌）。"""
    spy = make_trending_ohlcv(400, start_price=300.0)
    tst = make_trending_ohlcv(400, start_price=100.0).iloc[:-30]
    return {"SPY": spy, "TST": tst}


def test_delisted_position_liquidated():
    """持仓股数据中断 → 5 个交易日后以 delisted 理由强制清算。"""
    data = _truncated_data()
    cfg = make_cfg()
    cfg["backtest"]["start"] = str(data["SPY"].index[280].date())
    cfg["backtest"]["end"] = str(data["SPY"].index[-1].date())
    result = BacktestEngine(data, cfg).run()

    delisted = [t for t in result.trades if t.exit_reason == "delisted"]
    assert delisted, "数据中断的持仓必须被强制清算"
    t = delisted[0]
    # 清算价 = 最后可得收盘价 ×（1 - 滑点），滑点为 0 时严格相等
    last_close = float(data["TST"]["close"].iloc[-1])
    assert t.exit_price == pytest.approx(last_close, rel=1e-9)
    # 清算日在数据终点之后 5 个交易日附近（含确认滞后）
    gap_days = (t.exit_date - data["TST"].index[-1]).days
    assert 5 <= gap_days <= 12


def test_delisted_liquidation_cash_conservation():
    """清算后资金守恒依然成立：期末权益 = 初始 + Σ交易盈亏。"""
    data = _truncated_data()
    cfg = make_cfg()
    cfg["backtest"]["start"] = str(data["SPY"].index[280].date())
    cfg["backtest"]["end"] = str(data["SPY"].index[-1].date())
    result = BacktestEngine(data, cfg).run()
    initial = cfg["backtest"]["initial_capital"]
    total_pnl = sum(t.pnl for t in result.trades)
    assert float(result.equity.iloc[-1]) == pytest.approx(
        initial + total_pnl, abs=0.02 * max(len(result.trades), 1)
    )
    # 清算后不再持有该仓位（期末快照为空或不含 TST）
    assert "TST" not in result.open_positions
