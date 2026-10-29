"""经典 K 线形态识别（价格行为反转 / 持续信号）。

本模块只依赖 OHLC 四个价格，全部为向量化实现。
所有形态都以"当根 K 线收盘后"即可判定，无未来函数。

实现的形态：
    bullish_engulfing  看涨吞没：阳线实体完全吞没前一根阴线实体
    hammer             锤子线（Pin Bar）：长下影、小实体、实体位于上部
    inside_bar         内包线：整根 K 线被前一根包住（波动收缩，常作突破前兆）
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def bullish_engulfing(df: pd.DataFrame) -> pd.Series:
    """看涨吞没形态。

    条件：
        1. 前一根为阴线（收 < 开）
        2. 当前为阳线（收 > 开）
        3. 当前实体覆盖前一根实体（开 <= 前收，收 >= 前开）
        4. 当前实体大于前一根实体（力量对比反转）
    """
    o, c = df["open"], df["close"]
    po, pc = o.shift(1), c.shift(1)
    body, pbody = (c - o).abs(), (pc - po).abs()
    cond = (
        (pc < po)            # 前一根阴线
        & (c > o)            # 当前阳线
        & (o <= pc)          # 开盘吞没前收
        & (c >= po)          # 收盘吞没前开
        & (body > pbody)     # 实体更大
    )
    return cond.fillna(False)


def hammer(df: pd.DataFrame) -> pd.Series:
    """锤子线 / 看涨 Pin Bar。

    长下影线代表空头打压后被强力买盘拒绝（rejection），
    出现在回调低位时是经典的价格行为做多信号。

    条件：
        1. 下影线 >= 实体的 2 倍
        2. 上影线 <= 整根振幅的 25%
        3. 下影线 >= 整根振幅的一半（即实体与上影都被挤在上半部）
    """
    o, h, l, c = df["open"], df["high"], df["low"], df["close"]
    body = (c - o).abs()
    rng = (h - l).replace(0, np.nan)         # 避免一字线除零
    lower_shadow = o.combine(c, min) - l     # 下影线长度
    upper_shadow = h - o.combine(c, max)     # 上影线长度
    cond = (
        (lower_shadow >= 2 * body)
        & (upper_shadow <= 0.25 * rng)
        & ((o.combine(c, min) - l) / rng >= 0.5)  # 实体在上半部
    )
    return cond.fillna(False).astype(bool)


def inside_bar(df: pd.DataFrame) -> pd.Series:
    """内包线：当前 K 线的高低点完全在前一根范围之内。

    表示波动收缩、多空暂时平衡，向上突破内包线高点
    常被价格行为交易者用作趋势延续的入场触发。
    """
    h, l = df["high"], df["low"]
    cond = (h < h.shift(1)) & (l > l.shift(1))
    return cond.fillna(False)


def bullish_reversal(df: pd.DataFrame) -> pd.Series:
    """汇总看涨反转信号：吞没或锤子线任一出现即为 True。

    策略在"上升趋势 + 有效回调"的背景下使用该信号入场。
    """
    return bullish_engulfing(df) | hammer(df)
