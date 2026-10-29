"""K 线形态识别的单元测试：用手工构造的 K 线验证判定逻辑。"""

import pandas as pd

from quantbot.price_action.priceaction.patterns import bullish_engulfing, hammer, inside_bar


def _bars(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """由 (open, high, low, close) 元组列表构造 OHLC DataFrame。"""
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def test_bullish_engulfing_positive():
    """标准看涨吞没：大阳线实体完全覆盖前一根阴线实体。"""
    df = _bars([
        (10.0, 10.2, 9.5, 9.6),   # 阴线
        (9.5, 10.6, 9.4, 10.5),   # 阳线，实体 [9.5, 10.5] 吞没 [9.6, 10.0]
    ])
    assert bool(bullish_engulfing(df).iloc[-1])


def test_bullish_engulfing_negative_small_body():
    """阳线实体没有覆盖前一根实体 → 不是吞没。"""
    df = _bars([
        (10.0, 10.2, 9.5, 9.6),
        (9.7, 10.1, 9.6, 9.9),    # 实体 [9.7, 9.9] 未覆盖 [9.6, 10.0]
    ])
    assert not bool(bullish_engulfing(df).iloc[-1])


def test_hammer_positive():
    """标准锤子线：长下影、小实体、实体位于上部。"""
    df = _bars([
        (10.0, 10.1, 8.5, 9.9),   # 下影 1.4，实体 0.1，上影 0.2 → 锤子
    ])
    assert bool(hammer(df).iloc[-1])


def test_hammer_negative_long_upper_shadow():
    """上影线过长 → 不是锤子线。"""
    df = _bars([
        (10.0, 11.5, 9.0, 10.1),  # 上影 1.4 超过振幅 25%
    ])
    assert not bool(hammer(df).iloc[-1])


def test_hammer_doji_no_crash():
    """一字线（高=低）不应因除零崩溃，且判定为 False。"""
    df = _bars([(10.0, 10.0, 10.0, 10.0)])
    assert not bool(hammer(df).iloc[-1])


def test_inside_bar():
    """内包线：当前 K 线完全在前一根范围之内。"""
    df = _bars([
        (10.0, 11.0, 9.0, 10.5),
        (10.3, 10.8, 9.5, 10.0),  # 高低点均在前一根之内
    ])
    assert bool(inside_bar(df).iloc[-1])
