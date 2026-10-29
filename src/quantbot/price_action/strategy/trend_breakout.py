"""主策略：价格行为趋势跟随（突破 + 回调反转双入场）。

============================ 策略逻辑 ============================

【大盘风控开关（三档分级 Regime Filter）】
    档位 2（正常）：SPY 126 日动量 > 0 → 开放全部仓位配额
    档位 1（复苏）：126 日动量 < 0 但 63 日动量 > 0
                   （熊市中的 V 型反转确认）→ 开放一半仓位配额
    档位 0（熊市）：两者皆负 → 停止开新仓
    纯价格判断。相比二值开关，分级设计解决了 2019 / 2020 这类
    深跌后快速反转时"126 日动量恢复太慢导致踏空"的问题。

【入场（两种触发，满足其一）】
    共同前提：处于 HH+HL 上升结构、动量过滤通过、开关档位 > 0。
    动量过滤两种模式（config: momentum_filter）：
        absolute —— 个股 6 个月涨幅 > 0
        relative —— 个股 6 个月涨幅 > SPY 同期涨幅（只买领导股）
    A. 突破入场：收盘价创出前 55 日最高价新高
       （强者恒强，捕捉主升浪的开端或延续）
    B. 回调入场：出现 >=1.5 ATR 的有效回调后，
       打出看涨反转 K 线（吞没 / 锤子线）
       （在趋势中的折返低点上车，赔率更好）

【候选排序】
    同一天信号多于剩余仓位时，按 12-1 个月动量从高到低择优。

【金字塔加仓】
    持仓浮盈每达到 trigger_r × 初始风险（R）时加仓一次，
    加仓规模为标准风险单位的 size_frac 倍，最多 max_adds 次。
    把资金进一步集中到已被市场证明正确的趋势上（海龟系统经典组件）。

【离场（满足任一，先触发先执行）】
    1. 硬止损：盘中跌破止损价（初始 = 入场价 - 2.5 ATR，
       与最近摆动低点取较高者，即"结构止损"）
    2. 吊灯跟踪止损：止损价随"入场后最高收盘价 - 4.5 ATR"上移，
       只升不降，锁定趋势利润
    3. 通道离场：收盘价跌破前 45 日最低价（趋势结构破坏）

【仓位管理】见 risk/manager.py：单笔风险预算固定占比权益、
    单仓市值上限、最大并发持仓数。（具体数值以 config.yaml 为准）
================================================================
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass
class EntrySignal:
    """一条入场信号。

    属性:
        rank:     排序分（12-1 动量，越大越优先）
        stop:     建议初始止损价
        trigger:  触发类型（breakout / pullback），用于事后统计
    """
    rank: float
    stop: float
    trigger: str


@dataclass(frozen=True)
class EntryEvaluation:
    """入场评估结果；无信号时保留稳定原因码供复盘。"""

    signal: EntrySignal | None
    reason_code: str


def evaluate_entry_signal(
    row: pd.Series,
    regime_level: int,
    cfg: dict,
    bench_mom: float | None = None,
) -> EntryEvaluation:
    """判断入场并返回可审计的接受/拒绝原因。

    参数:
        row:          该股票当天的特征行（build_features 的输出）
        regime_level: 大盘开关档位（0/1/2，0 时不开仓）
        cfg:          配置中的 strategy 小节
        bench_mom:    基准（SPY）当天的 6 个月动量，
                      momentum_filter=relative 时用于对比

    返回 EntryEvaluation；策略调用方可只消费其中的 signal，复盘调用方
    同时记录 reason_code。
    """
    # 特征尚未就绪（上市初期回看窗口不足）直接跳过
    if pd.isna(row["hh_break"]) or pd.isna(row["atr"]) or pd.isna(row["mom"]):
        return EntryEvaluation(None, "features_not_ready")
    if regime_level <= 0:
        return EntryEvaluation(None, "regime_closed")
    # 共同前提：上升结构 + 动量过滤
    if not row["uptrend"]:
        return EntryEvaluation(None, "not_uptrend")
    # 多时间框架确认（可选）：周线级也须处于 HH+HL 上升结构
    if cfg.get("use_weekly_confirm") and not row.get("weekly_uptrend", False):
        return EntryEvaluation(None, "weekly_structure_rejected")
    mom_threshold = 0.0
    if cfg.get("momentum_filter", "absolute") == "relative" and bench_mom is not None:
        mom_threshold = bench_mom  # 相对模式：必须跑赢大盘
    if not row["mom"] > mom_threshold:
        return EntryEvaluation(None, "momentum_rejected")

    close, atr_v = row["close"], row["atr"]
    if atr_v <= 0 or close <= 0:
        return EntryEvaluation(None, "invalid_price_or_atr")

    # 触发 A：收盘创 55 日新高（突破）
    is_breakout = close > row["hh_break"]
    # 触发 B：有效回调后的看涨反转 K 线
    # （use_inside_bar 开启时，内包线突破也算反转确认）
    reversal_ok = bool(row["reversal"])
    if cfg.get("use_inside_bar") and not reversal_ok:
        reversal_ok = bool(row.get("ib_breakout", False))
    is_pullback = bool(row["pullback"] and reversal_ok)

    if not (is_breakout or is_pullback):
        return EntryEvaluation(None, "no_price_action_trigger")

    # ---- 初始止损：ATR 止损与结构止损（最近摆动低点）取较高者 ----
    # 较高者意味着更近的止损 → 风险更小、仓位更大；
    # 但摆动低点若离得太远（超过 ATR 止损），则退回 ATR 止损。
    atr_stop = close - cfg["atr_stop_mult"] * atr_v
    stop = atr_stop
    if not pd.isna(row["sl1"]) and atr_stop < row["sl1"] < close:
        stop = row["sl1"]

    # ---- 候选排序分 ----
    rank = row["rank_mom"] if not pd.isna(row["rank_mom"]) else row["mom"]
    if cfg.get("rank_by", "momentum") == "sharpe_momentum":
        # 夏普动量：动量除以年化波动率。宽池（数百只候选）下必须使用——
        # 原始动量排序总是挑到波动最极端的板块脉冲，夏普动量偏好平滑趋势。
        vol = row.get("ann_vol")
        if vol is not None and not pd.isna(vol) and vol > 1e-6:
            rank = rank / float(vol)
    return EntryEvaluation(
        EntrySignal(
            rank=float(rank),
            stop=float(stop),
            trigger="breakout" if is_breakout else "pullback",
        ),
        "accepted",
    )


def entry_signal(
    row: pd.Series,
    regime_level: int,
    cfg: dict,
    bench_mom: float | None = None,
) -> EntrySignal | None:
    """兼容入口：返回价格行为入场信号或 None。"""
    return evaluate_entry_signal(row, regime_level, cfg, bench_mom).signal


def short_entry_signal(
    row: pd.Series,
    regime_level: int,
    cfg: dict,
) -> EntrySignal | None:
    """熊市做空信号：多头逻辑的镜像（仅开关档位 0 时激活）。

    条件：
        1. 大盘开关档位 0（SPY 长动量为负——系统性熊市）
        2. LH + LL 下降结构
        3. 个股 6 个月动量为负
        4. 收盘价跌破前 N 日最低价（向下突破）
    止损：入场价 + atr_stop_mult × ATR，与最近摆动高点取较低者（结构止损）。
    排序：动量越负（夏普动量模式下按波动率归一）越优先。
    """
    if pd.isna(row.get("ll_break")) or pd.isna(row["atr"]) or pd.isna(row["mom"]):
        return None
    if regime_level != 0:
        return None
    if not (row["downtrend"] and row["mom"] < 0):
        return None

    close, atr_v = row["close"], row["atr"]
    if atr_v <= 0 or close <= 0:
        return None
    if not close < row["ll_break"]:
        return None

    # 结构止损：ATR 止损与最近摆动高点取较低者（更近 → 风险更小）
    atr_stop = close + cfg["atr_stop_mult"] * atr_v
    stop = atr_stop
    sh1 = row.get("sh1")
    if sh1 is not None and not pd.isna(sh1) and close < sh1 < atr_stop:
        stop = float(sh1)

    rank = row["rank_mom"] if not pd.isna(row["rank_mom"]) else row["mom"]
    if cfg.get("rank_by", "momentum") == "sharpe_momentum":
        vol = row.get("ann_vol")
        if vol is not None and not pd.isna(vol) and vol > 1e-6:
            rank = rank / float(vol)
    # 做空候选：动量越负越优先 → 取负号后仍按从大到小排序
    return EntrySignal(rank=-float(rank), stop=float(stop), trigger="short")


def short_close_exit_signal(row: pd.Series) -> bool:
    """空头收盘级离场：收盘价升破 N 日高点通道（下降趋势结构破坏）。"""
    return (not pd.isna(row.get("hh_exit"))) and row["close"] > row["hh_exit"]


def update_trailing_stop_short(
    current_stop: float,
    lowest_close: float,
    atr_value: float,
    cfg: dict,
) -> float:
    """空头吊灯跟踪止损：只下移、不上移。

    新止损 = min(当前止损, 入场后最低收盘价 + atr_trail_mult × ATR)
    """
    if pd.isna(atr_value):
        return current_stop
    chandelier = lowest_close + cfg["atr_trail_mult"] * atr_value
    return min(current_stop, chandelier)


def update_trailing_stop(
    current_stop: float,
    highest_close: float,
    atr_value: float,
    cfg: dict,
) -> float:
    """更新吊灯跟踪止损：只上移、不下移。

    新止损 = max(当前止损, 入场后最高收盘价 - atr_trail_mult * ATR)
    """
    if pd.isna(atr_value):
        return current_stop
    chandelier = highest_close - cfg["atr_trail_mult"] * atr_value
    return max(current_stop, chandelier)


def close_exit_signal(row: pd.Series) -> bool:
    """收盘级离场判断：收盘价跌破 N 日通道下轨（趋势结构破坏）。

    该信号在收盘后判定，次日开盘执行。
    盘中硬止损由回测引擎 / 实盘 runner 单独处理。
    """
    return (not pd.isna(row["ll_exit"])) and row["close"] < row["ll_exit"]


def pyramid_trigger(
    close: float,
    avg_entry: float,
    init_risk: float,
    adds_done: int,
    cfg: dict,
) -> bool:
    """判断是否触发金字塔加仓。

    条件：已加仓次数未达上限，且收盘价相对（首次）入场价的浮盈
    达到 (已加仓次数 + 1) × trigger_r × 初始每股风险 R。
    即第一次加仓在 +2R，第二次在 +4R（默认 trigger_r=2）。
    """
    max_adds = cfg.get("pyramid_max_adds", 0)
    if adds_done >= max_adds or init_risk <= 0:
        return False
    trigger_r = cfg.get("pyramid_trigger_r", 2.0)
    return close >= avg_entry + (adds_done + 1) * trigger_r * init_risk


def regime_level_series(bench_features: pd.DataFrame, cfg_regime: dict) -> pd.Series:
    """计算大盘风控开关的三档档位序列。

    2 = SPY 长窗口（默认 126 日）动量 > 0（正常牛市）
    1 = 长窗口 < 0 但短窗口（默认 63 日）动量 > 0（熊市后复苏确认）
    0 = 两者皆负（熊市，停止开仓）

    recovery_window 配置为 null 时退化为二值开关（只有 0 / 2 两档）。

    structural_reopen 开启时：SPY 收盘创出前 N 日（reopen_window，
    默认复用 55）最高价新高 → 即使动量仍为负也恢复到 2 档。
    动机：126 日动量在 V 型反转后要 3-4 个月才转正（2019 年 Q1 踏空），
    而"创 55 日新高"是更快且更难被熊市反弹伪造的结构性证据
    ——熊市反弹通常到不了前 55 日高点就衰竭。
    """
    n_long = cfg_regime["momentum_window"]
    n_short = cfg_regime.get("recovery_window")
    close = bench_features["close"]
    mom_long = close / close.shift(n_long) - 1
    level = pd.Series(0, index=close.index, dtype=int)
    if n_short:
        mom_short = close / close.shift(n_short) - 1
        level[mom_short > 0] = 1
    level[mom_long > 0] = 2
    if cfg_regime.get("structural_reopen"):
        n_reopen = int(cfg_regime.get("reopen_window", 55))
        hh = bench_features["high"].rolling(n_reopen).max().shift(1)
        level[close > hh] = 2
    return level
