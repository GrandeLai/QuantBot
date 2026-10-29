"""做空模块单元测试：信号镜像与空头风控。"""

import pandas as pd

from quantbot.price_action.strategy.trend_breakout import (
    short_entry_signal,
    short_close_exit_signal,
    update_trailing_stop_short,
)

STRATEGY_CFG = {
    "atr_stop_mult": 2.5,
    "atr_trail_mult": 4.5,
    "rank_by": "momentum",
}


def _row(**kw) -> pd.Series:
    """构造一行特征（做空信号所需字段）。"""
    base = {
        "close": 90.0, "atr": 2.0, "mom": -0.2, "rank_mom": -0.25,
        "ll_break": 92.0, "hh_exit": 110.0,
        "downtrend": True, "uptrend": False,
        "sh1": 100.0, "ann_vol": 0.3,
    }
    base.update(kw)
    return pd.Series(base)


def test_short_signal_fires_in_bear():
    """档位 0 + 下降结构 + 负动量 + 跌破通道 → 触发做空。"""
    sig = short_entry_signal(_row(), 0, STRATEGY_CFG)
    assert sig is not None
    assert sig.trigger == "short"
    assert sig.stop > 90.0            # 空头止损在入场价上方
    assert sig.rank > 0               # 动量越负排序分越高（已取负号）


def test_short_signal_blocked_outside_bear():
    """档位非 0（非熊市）不允许做空。"""
    assert short_entry_signal(_row(), 2, STRATEGY_CFG) is None
    assert short_entry_signal(_row(), 1, STRATEGY_CFG) is None


def test_short_signal_needs_breakdown():
    """未跌破 N 日低点通道 → 无信号。"""
    assert short_entry_signal(_row(close=95.0), 0, STRATEGY_CFG) is None


def test_short_signal_structural_stop():
    """摆动高点比 ATR 止损更近时采用结构止损。"""
    # ATR 止损 = 90 + 2.5*2 = 95；摆动高点 93 更近 → 用 93
    sig = short_entry_signal(_row(sh1=93.0), 0, STRATEGY_CFG)
    assert sig is not None and sig.stop == 93.0


def test_short_trailing_only_moves_down():
    """空头吊灯止损只下移不上移。"""
    # 最低收盘 80，ATR 2 → 吊灯 = 80 + 9 = 89 < 当前止损 95
    assert update_trailing_stop_short(95.0, 80.0, 2.0, STRATEGY_CFG) == 89.0
    # 波动放大使吊灯位上升时，维持原止损
    assert update_trailing_stop_short(89.0, 80.0, 4.0, STRATEGY_CFG) == 89.0


def test_short_exit_on_channel_break():
    """收盘升破 N 日高点通道 → 空头回补信号。"""
    assert short_close_exit_signal(_row(close=111.0))
    assert not short_close_exit_signal(_row(close=105.0))
