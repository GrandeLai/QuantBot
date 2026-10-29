"""特征汇总：把价格行为算法与波幅 / 动量 / 通道特征拼成一张特征表。

回测引擎与实盘 runner 都只消费这张表，
保证两边的信号计算逻辑完全一致（同一份代码）。

★ 无未来函数约定：
    所有"通道"类特征（N 日最高 / 最低）都 shift(1)，
    即第 t 天看到的是"截至 t-1 天"的通道值，
    这样"收盘价突破 N 日高点"才是真正的突破而非自我比较。
"""

from __future__ import annotations

import pandas as pd

from .patterns import bullish_engulfing, bullish_reversal, hammer, inside_bar
from .swings import find_swings, uptrend_structure


def atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """平均真实波幅 ATR（简单均值版）。

    真实波幅 TR = max(高-低, |高-昨收|, |低-昨收|)，
    ATR 是纯粹由价格振幅派生的度量，用于止损距离与仓位计算。
    """
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(window).mean()


def build_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """为单只股票计算全部策略特征。

    参数:
        df:  标准 OHLCV DataFrame
        cfg: 配置中的 strategy 小节

    返回在原 OHLCV 基础上追加以下列：
        atr        平均真实波幅
        hh_break   前 N 日最高价（突破通道上轨，已 shift 防未来函数）
        ll_exit    前 M 日最低价（离场通道下轨，已 shift）
        mom        6 个月动量（价格比值 - 1），正值代表中期趋势向上
        rank_mom   12-1 个月动量（跳过最近 1 个月），用于多只候选间排序
        uptrend    是否处于 HH+HL 上升结构
        sl1        最近已确认摆动低点（结构止损参考位）
        pullback   是否出现"有效回调"（距 10 日高点回撤 >= 1.5 ATR）
        reversal   是否出现看涨反转 K 线（吞没 / 锤子线）
    """
    n_break = cfg["breakout_window"]
    n_exit = cfg["exit_window"]
    n_atr = cfg["atr_window"]
    n_mom = cfg["momentum_window"]
    k = cfg["swing_k"]
    pb_depth = cfg["pullback_depth_atr"]

    out = df.copy()
    out["atr"] = atr(df, n_atr)

    # ---- 通道特征（shift(1)：只用截至昨天的数据） ----
    out["hh_break"] = df["high"].rolling(n_break).max().shift(1)
    out["ll_exit"] = df["low"].rolling(n_exit).min().shift(1)
    # 做空镜像通道：跌破 N 日低点入场（做空），升破 M 日高点离场（回补）
    out["ll_break"] = df["low"].rolling(n_break).min().shift(1)
    out["hh_exit"] = df["high"].rolling(n_exit).max().shift(1)

    # ---- 动量特征（纯价格比值） ----
    out["mom"] = df["close"] / df["close"].shift(n_mom) - 1
    # 12-1 动量：跳过最近 21 个交易日，学术上证明比原始 12 月动量更稳健
    out["rank_mom"] = df["close"].shift(21) / df["close"].shift(252) - 1

    # ---- 排序与股票池辅助特征（宽池模式下使用，均为点时可计算） ----
    # 年化波动率：用于"夏普动量"排序（动量 / 波动率），
    # 偏好平滑稳定的趋势，排斥高波动的垃圾脉冲行情
    out["ann_vol"] = df["close"].pct_change().rolling(63).std() * (252 ** 0.5)
    # 滚动日均成交额（美元）：用于"当时的大盘股"流动性池筛选
    out["adv"] = (df["close"] * df["volume"]).rolling(63).mean()

    # ---- 市场结构（摆动点已内置确认滞后） ----
    sw = find_swings(df["high"], df["low"], k=k)
    out["uptrend"] = uptrend_structure(sw)

    # ---- 周线级市场结构（多时间框架确认，strategy.use_weekly_confirm 门控）----
    # 防未来函数三重保障：
    #   1. 周线摆动点自带 k=2 周确认滞后（find_swings 内置）；
    #   2. shift(1) 使任意一天只能看到"上一根已完结周线"的结论；
    #   3. 属性测试（截断 vs 全量一致）覆盖本列。
    wk = df.resample("W-FRI").agg(
        {"high": "max", "low": "min", "close": "last"}
    ).dropna()
    if len(wk) >= 8:
        wsw = find_swings(wk["high"], wk["low"], k=2)
        w_up = ((wsw["sh1"] > wsw["sh2"]) & (wsw["sl1"] > wsw["sl2"])).shift(1, fill_value=False)
        out["weekly_uptrend"] = (
            w_up.reindex(df.index, method="ffill", fill_value=False)
        )
    else:
        out["weekly_uptrend"] = False
    # 下降结构：摆动高点与摆动低点同时降低（LH + LL），做空的前提
    out["downtrend"] = (
        (sw["sh1"] < sw["sh2"]) & (sw["sl1"] < sw["sl2"])
    ).fillna(False)
    out["sl1"] = sw["sl1"]
    out["sl2"] = sw["sl2"]
    out["sh1"] = sw["sh1"]   # 最近已确认摆动高点（做空的结构止损参考位）
    out["sh2"] = sw["sh2"]

    # ---- 回调 + 反转形态 ----
    # 有效回调：收盘价距最近 10 日最高收盘回撤超过 pb_depth 倍 ATR
    hh10 = df["close"].rolling(10).max()
    out["pullback_anchor"] = hh10
    out["pullback_depth_atr"] = (hh10 - df["close"]) / out["atr"]
    out["pullback"] = out["pullback_depth_atr"] >= pb_depth
    out["bullish_engulfing"] = bullish_engulfing(df)
    out["hammer"] = hammer(df)
    out["inside_bar"] = inside_bar(df)
    out["reversal"] = bullish_reversal(df)
    # 内包线突破：昨日为内包线（波动收缩），今日收盘突破内包线高点
    # ——潜在的第三种回调入场触发（由 strategy.use_inside_bar 开关控制）
    out["ib_breakout"] = (
        out["inside_bar"].shift(1, fill_value=False)
        & (df["close"] > df["high"].shift(1))
    )

    return out
