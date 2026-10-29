"""策略与风控逻辑的单元测试。"""

import pandas as pd

from quantbot.price_action.risk.manager import position_size
from quantbot.price_action.strategy.trend_breakout import (
    pyramid_trigger,
    regime_level_series,
    update_trailing_stop,
)

STRATEGY_CFG = {
    "atr_trail_mult": 4.5,
    "pyramid_max_adds": 2,
    "pyramid_trigger_r": 2.0,
    "pyramid_size_frac": 0.5,
}

RISK_CFG = {"risk_per_trade": 0.025, "max_positions": 10, "max_weight": 0.20}


def test_position_size_risk_budget():
    """标准情形：股数由单笔风险预算决定。"""
    # 权益 10 万、风险 2.5% = $2500；每股风险 $5 → 500 股
    # 500 股 × $100 = $50000 > 权益 20% 上限 $20000 → 压到 200 股
    assert position_size(100000, 100000, 100.0, 95.0, RISK_CFG) == 200


def test_position_size_cash_constraint():
    """现金不足时按现金缩减（不加杠杆）。"""
    shares = position_size(100000, 5000, 100.0, 95.0, RISK_CFG)
    assert shares <= 5000 * 0.995 / 100.0


def test_position_size_invalid_stop():
    """止损价高于成交价 → 拒绝开仓。"""
    assert position_size(100000, 100000, 100.0, 105.0, RISK_CFG) == 0


def test_trailing_stop_only_moves_up():
    """吊灯止损只上移不下移。"""
    # 最高收盘 110，ATR 2 → 吊灯 = 110 - 9 = 101 > 当前止损 95
    assert update_trailing_stop(95.0, 110.0, 2.0, STRATEGY_CFG) == 101.0
    # 波动放大导致吊灯位下降时，维持原止损不动
    assert update_trailing_stop(101.0, 110.0, 4.0, STRATEGY_CFG) == 101.0


def test_pyramid_trigger_ladder():
    """金字塔加仓阶梯：第一次 +2R，第二次 +4R，超过上限拒绝。"""
    # 入场 100，初始风险 R=5
    assert not pyramid_trigger(105.0, 100.0, 5.0, 0, STRATEGY_CFG)  # +1R 不够
    assert pyramid_trigger(110.0, 100.0, 5.0, 0, STRATEGY_CFG)      # +2R 触发第 1 次
    assert not pyramid_trigger(115.0, 100.0, 5.0, 1, STRATEGY_CFG)  # +3R 不够第 2 次
    assert pyramid_trigger(120.0, 100.0, 5.0, 1, STRATEGY_CFG)      # +4R 触发第 2 次
    assert not pyramid_trigger(200.0, 100.0, 5.0, 2, STRATEGY_CFG)  # 已达上限


def test_structural_reopen_flag():
    """结构化重开：动量为负但创 N 日新高 → 档位 2（默认关闭不影响）。"""
    # 前段高位 300，中段深跌至 180，末段反弹到 240：
    # 126 日动量仍为负（126 日前价格约 280），但已创出前 55 日新高
    prices = ([300.0] * 120 + [300 - 1.5 * i for i in range(80)]
              + [180 + 1.0 * i for i in range(60)])
    idx = pd.bdate_range("2020-01-01", periods=len(prices))
    bench = pd.DataFrame({"close": prices, "high": prices}, index=idx)
    off = regime_level_series(bench, {"momentum_window": 126,
                                      "recovery_window": None})
    on = regime_level_series(bench, {"momentum_window": 126,
                                     "recovery_window": None,
                                     "structural_reopen": True,
                                     "reopen_window": 55})
    # 末段反弹突破前 55 日高点后：关闭时仍 0 档（126 日动量未转正），
    # 开启时应恢复 2 档
    assert off.iloc[-1] == 0
    assert on.iloc[-1] == 2


def test_share_class_dedup_grouping():
    """同公司多股别分组：GOOG/GOOGL 同组，普通代码各自成组。"""
    from quantbot.price_action.data.aliases import company_group

    assert company_group("GOOG") == company_group("GOOGL")
    assert company_group("AAPL") != company_group("MSFT")


def test_regime_levels():
    """分级开关：长动量>0 → 2 档；仅短动量>0 → 1 档；均<0 → 0 档。"""
    n = 300
    # 构造：前 200 天缓涨（长短动量均为正），随后 60 天急跌，
    # 最后 40 天 V 型反弹（短动量转正、长动量仍负）
    prices = (
        [100 + 0.3 * i for i in range(200)]
        + [160 - 1.2 * i for i in range(60)]
        + [88 + 1.0 * i for i in range(40)]
    )
    idx = pd.bdate_range("2020-01-01", periods=n)
    bench = pd.DataFrame({"close": prices}, index=idx)
    level = regime_level_series(bench, {"momentum_window": 126, "recovery_window": 63})
    assert level.iloc[199] == 2      # 上升期：全额档
    assert level.iloc[259] == 0      # 急跌末端：停止开仓
    assert level.iloc[-1] == 1       # V 型反弹确认：半额档
    # 二值模式：recovery_window 为 null 时不存在 1 档
    binary = regime_level_series(bench, {"momentum_window": 126, "recovery_window": None})
    assert set(binary.unique()) <= {0, 2}
