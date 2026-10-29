"""无未来函数属性测试（系统可信度的核心保障）。

方法：只喂前 t 根 K 线 vs 喂全量数据，第 t 天的输出必须完全一致。
若任何特征"偷看"了未来数据，截断序列与全量序列在同一天的值就会不同。

任何对 priceaction/ 模块的改动都必须通过本测试。

运行：.venv/bin/python -m pytest tests/ -q
"""

import numpy as np
import pandas as pd
import pytest

from quantbot.price_action.priceaction.features import build_features
from quantbot.price_action.priceaction.swings import find_swings

# 测试用的策略参数（与 config.yaml 结构一致即可，数值不必相同）
STRATEGY_CFG = {
    "breakout_window": 55,
    "exit_window": 45,
    "atr_window": 14,
    "atr_stop_mult": 2.5,
    "atr_trail_mult": 4.5,
    "swing_k": 3,
    "momentum_window": 126,
    "pullback_depth_atr": 1.5,
}


@pytest.fixture(scope="module")
def ohlcv() -> pd.DataFrame:
    """生成一段确定性的合成 OHLCV 数据（带趋势与噪声，含等高价格）。"""
    rng = np.random.default_rng(42)
    n = 800
    # 随机游走叠加缓慢上升趋势；round(1) 制造等高双顶/双底场景
    close = np.cumsum(rng.normal(0.05, 1.0, n)) + 100
    close = np.round(np.maximum(close, 5.0), 1)
    high = close + np.round(rng.uniform(0, 1.5, n), 1)
    low = close - np.round(rng.uniform(0, 1.5, n), 1)
    open_ = np.round((high + low) / 2, 1)
    idx = pd.bdate_range("2020-01-01", periods=n)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": np.full(n, 1e6)},
        index=idx,
    )


@pytest.mark.parametrize("t", [200, 400, 600, 799])
def test_swings_no_lookahead(ohlcv, t):
    """摆动点：截断数据与全量数据在第 t 天的输出必须一致。"""
    full = find_swings(ohlcv["high"], ohlcv["low"], k=3)
    part = find_swings(ohlcv["high"].iloc[:t], ohlcv["low"].iloc[:t], k=3)
    pd.testing.assert_series_equal(
        part.iloc[-1], full.iloc[t - 1], check_names=False
    )


@pytest.mark.parametrize("t", [300, 500, 799])
def test_features_no_lookahead(ohlcv, t):
    """整张特征表：截断与全量在第 t 天的每一列必须一致（NaN 视为相等）。"""
    full = build_features(ohlcv, STRATEGY_CFG).iloc[t - 1]
    part = build_features(ohlcv.iloc[:t], STRATEGY_CFG).iloc[-1]
    for col in part.index:
        a, b = full[col], part[col]
        if pd.isna(a) and pd.isna(b):
            continue
        assert a == b, f"列 {col} 在第 {t} 天不一致: 全量={a} vs 截断={b}"


def test_boolean_features_keep_their_types_with_future_pandas_behavior(ohlcv):
    # 新版 pandas 不再隐式把 object 缺失值填充结果降为 bool；首根和短历史仍须是明确的 False。
    with pd.option_context("future.no_silent_downcasting", True):
        features = build_features(ohlcv, STRATEGY_CFG)
        short = build_features(ohlcv.iloc[:30], STRATEGY_CFG)
    for frame in (features, short):
        assert frame["weekly_uptrend"].dtype == bool and frame["ib_breakout"].dtype == bool
        assert not frame["weekly_uptrend"].isna().any() and not frame["ib_breakout"].isna().any()
        assert not frame["weekly_uptrend"].iloc[0] and not frame["ib_breakout"].iloc[0]


def test_swing_confirmation_lag(ohlcv):
    """摆动点必须有 k 根确认滞后：sh1 不允许等于最近 k 根内的最高价。

    换句话说，第 t 天记录的最近摆动高点，其位置必须在 t-k 之前。
    """
    k = 3
    sw = find_swings(ohlcv["high"], ohlcv["low"], k=k)
    h = ohlcv["high"]
    for t in range(100, len(h)):
        sh1 = sw["sh1"].iloc[t]
        if pd.isna(sh1):
            continue
        # 最近 k 根 K 线的高点即使更高，也不可能已被"确认"为摆动点
        recent_high = h.iloc[t - k + 1 : t + 1].max()
        if recent_high > h.iloc[: t - k + 1].max():
            assert sh1 != recent_high, (
                f"第 {t} 天的 sh1 引用了尚未确认的近期高点（未来函数）"
            )
