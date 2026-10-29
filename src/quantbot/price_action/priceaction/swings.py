"""摆动高低点（Swing High / Swing Low）识别与趋势结构判断。

价格行为分析的基石是市场结构（Market Structure）：
    上升趋势 = 不断抬高的摆动高点（Higher High）+ 不断抬高的摆动低点（Higher Low）
    下降趋势 = 不断降低的摆动高点（Lower High）+ 不断降低的摆动低点（Lower Low）

摆动点用分形（fractal）方法识别：
    第 i 根 K 线是摆动高点，当且仅当它的最高价严格高于左右各 k 根 K 线的最高价。

★ 防未来函数的关键：
    第 i 根的摆动点要等到第 i+k 根 K 线走完才能"确认"。
    因此本模块所有输出都做了 k 根的确认滞后（shift(k)），
    保证在任意一天 t 能看到的摆动点，都是当天真实可知的。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def find_swings(high: pd.Series, low: pd.Series, k: int = 3) -> pd.DataFrame:
    """识别摆动高低点，并输出"截至每一天已确认"的最近两个摆动点。

    参数:
        high/low: 最高价、最低价序列
        k:        分形左右宽度（越大摆动点越少越显著）

    返回 DataFrame，各列含义（均已做确认滞后，无未来函数）：
        sh1 / sh2: 最近一个 / 上一个已确认摆动高点的价格
        sl1 / sl2: 最近一个 / 上一个已确认摆动低点的价格
    """
    n = len(high)
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)

    # ---- 第一步：找出所有分形摆动点（向量化，中心视角） ----
    # 打破平局的规则：严格高于左侧邻域、不低于右侧邻域。
    # 这样等高双顶/双底（价格离散化下很常见）会把"第一个峰"记为摆动点，
    # 而不是像"严格唯一峰值"那样把两个都漏掉。
    # left_max[i] = max(h[i-k:i])；right_max[i] = max(h[i+1:i+k+1])
    # （右侧极值通过序列反转 + 滚动窗口得到，与逐点循环逐位等价）
    hs, ls = pd.Series(h), pd.Series(l)
    left_max = hs.rolling(k).max().shift(1).to_numpy()
    right_max = hs[::-1].rolling(k).max().shift(1).to_numpy()[::-1]
    left_min = ls.rolling(k).min().shift(1).to_numpy()
    right_min = ls[::-1].rolling(k).min().shift(1).to_numpy()[::-1]
    is_sh = (h > left_max) & (h >= right_max)
    is_sl = (l < left_min) & (l <= right_min)

    # ---- 第二步：滞后 k 根确认，再前向填充出"最近两个摆动点" ----
    def last_two(mask: np.ndarray, price: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """逐日维护最近两个已确认摆动点的价格（p1 最近，p2 次近）。

        向量化实现：第 j 根的摆动点在第 j+k 根被"确认"——
        在确认位置放入价格事件，前向填充即得任意一天可见的
        最近（p1）与次近（p2）摆动点。
        """
        p1 = np.full(n, np.nan)
        p2 = np.full(n, np.nan)
        idx = np.flatnonzero(mask)
        confirm = idx + k
        ok = confirm < n
        idx, confirm = idx[ok], confirm[ok]
        p1[confirm] = price[idx]
        if len(idx) > 1:
            p2[confirm[1:]] = price[idx[:-1]]
        return (pd.Series(p1).ffill().to_numpy(),
                pd.Series(p2).ffill().to_numpy())

    sh1, sh2 = last_two(is_sh, h)
    sl1, sl2 = last_two(is_sl, l)

    return pd.DataFrame(
        {"sh1": sh1, "sh2": sh2, "sl1": sl1, "sl2": sl2}, index=high.index
    )


def uptrend_structure(swings: pd.DataFrame) -> pd.Series:
    """判断上升趋势结构：最近的摆动高点和摆动低点都在抬高（HH + HL）。

    这是价格行为对"上升趋势"的标准定义，
    比均线多头排列更贴近交易员的肉眼判断。
    """
    hh = swings["sh1"] > swings["sh2"]  # Higher High
    hl = swings["sl1"] > swings["sl2"]  # Higher Low
    return (hh & hl).fillna(False)
