"""组合级事件驱动回测引擎。

================== 撮合模型（避免未来函数的关键） ==================

时间线上每个交易日 t 依次发生三件事：

    1. 开盘：执行前一天收盘后排队的订单（入场买单 / 金字塔加仓单 /
       收盘级离场卖单），成交价 = 当日开盘价 ± 滑点。
    2. 盘中：检查持仓的硬止损——
       若开盘价已低于止损价（跳空低开），按开盘价成交；
       否则若当日最低价触及止损价，按止损价成交。
    3. 收盘：用第 t 天完整的 K 线计算信号，
       生成 t+1 开盘要执行的订单；更新吊灯跟踪止损；
       闲置现金按 13 周国债利率计息；
       按收盘价对组合估值，记录权益曲线。

即：信号永远用"已经走完"的 K 线计算，成交永远发生在信号之后，
不存在用当天收盘信号成交当天收盘价的自欺行为。
====================================================================
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..data.universe import CASH_PROXY
from ..data.downloader import resolve_data_config
from ..priceaction.features import build_features
from ..risk.manager import position_size
from ..strategy.trend_breakout import (
    close_exit_signal,
    entry_signal,
    pyramid_trigger,
    regime_level_series,
    short_close_exit_signal,
    short_entry_signal,
    update_trailing_stop,
    update_trailing_stop_short,
)


@dataclass
class Position:
    """一笔持仓的运行时状态。"""
    symbol: str
    shares: int                 # 恒为正数，方向由 direction 表示
    entry_date: pd.Timestamp
    signal_date: pd.Timestamp   # 产生入场信号的已完成 K 线日期
    entry_price: float          # 加权平均入场价（加仓后更新）
    initial_entry_price: float  # 首次成交价（复盘口径不随加仓改变）
    stop: float                 # 当前止损价（多头随吊灯上移 / 空头随吊灯下移）
    initial_stop: float         # 首次入场止损（复盘证据）
    highest_close: float        # 入场以来的"有利极值收盘价"
                                # （多头 = 最高收盘，空头 = 最低收盘，吊灯锚点）
    trigger: str                # 入场触发类型（breakout / pullback / short）
    init_risk: float            # 初始每股风险 R（|入场价 - 初始止损|）
    adds: int = 0               # 已金字塔加仓次数（仅多头）
    direction: int = 1          # 1 = 多头，-1 = 空头
    age_days: int = 0           # 持有交易日数（时间止损用）
    mfe_r: float = 0.0          # 最大有利波动，按初始风险 R 计
    mae_r: float = 0.0          # 最大不利波动，按初始风险 R 计（正值）


@dataclass
class Trade:
    """一笔已完成交易的记录（用于统计与复盘）。"""
    symbol: str
    entry_date: pd.Timestamp
    signal_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float          # 加权平均入场价
    initial_entry_price: float
    initial_stop: float
    init_risk: float
    exit_price: float
    shares: int
    pnl: float
    ret: float                  # 单笔收益率（按平均入场价，空头方向已折算）
    trigger: str                # 入场触发类型
    exit_reason: str            # 离场原因（stop / channel / end）
    adds: int                   # 金字塔加仓次数
    holding_days: int = 0
    mfe_r: float = 0.0
    mae_r: float = 0.0
    direction: int = 1          # 1 = 多头，-1 = 空头


@dataclass
class BacktestResult:
    """回测输出：权益曲线 + 交易明细 + 基准曲线 + 期末持仓。"""
    equity: pd.Series
    trades: list[Trade] = field(default_factory=list)
    benchmark: pd.Series | None = None
    # 回测结束时仍持有的仓位（清算前快照），影子对账 / 信号一致性测试用：
    # {代码: {"direction", "shares", "stop", "highest_close",
    #        "entry_price", "init_risk", "adds"}}
    open_positions: dict[str, dict] = field(default_factory=dict)
    # 全程现金利息净额（收到的国债利息 - 支付的融资/融券成本），归因用
    total_interest: float = 0.0
    # 最后一个交易日收盘后排队、尚未执行的决策（回测与实盘的
    # 信号一致性测试用）：{"entries": [(代码,止损,触发,方向)],
    #                    "exits": [(代码,原因)], "adds": [代码]}
    final_signals: dict = field(default_factory=dict)

    def trades_df(self) -> pd.DataFrame:
        """交易明细转 DataFrame，便于导出 CSV。"""
        return pd.DataFrame([t.__dict__ for t in self.trades])


class BacktestEngine:
    """组合级回测引擎。

    用法::

        engine = BacktestEngine(data, cfg)
        result = engine.run()
    """

    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        cfg: dict,
        features: dict[str, pd.DataFrame] | None = None,
    ):
        """
        参数:
            data:     {代码: OHLCV DataFrame}，须包含基准（如 SPY），
                      可含现金利率代理 ^IRX（用于闲置现金计息）
            cfg:      完整配置 dict（config.yaml 加载结果）
            features: 可选的预计算特征表（build_features 的输出）。
                      walk-forward 等需要在同一特征集上跑多组配置的场景
                      传入以省掉重复计算；调用方必须保证特征与
                      cfg["strategy"] 中影响特征的参数一致。
        """
        self.cfg = cfg
        data_cfg = resolve_data_config(cfg)["data"]
        readonly_data = bool(data_cfg.get("snapshot"))
        self.scfg = cfg["strategy"]
        self.rcfg = cfg["risk"]
        bench_sym = cfg["regime"]["symbol"]

        if features is not None:
            self.features = features
        else:
            print("[backtest] 计算全部股票的价格行为特征 ...")
            # 预先向量化计算每只股票的特征表（特征本身已保证无未来函数），
            # 之后的逐日循环只做查表，速度快且逻辑清晰。
            # ^IRX 是利率序列而非股票，跳过特征计算。
            self.features = {
                sym: build_features(df, self.scfg)
                for sym, df in data.items()
                if sym != CASH_PROXY
            }

        # 基准与大盘风控开关（三档分级）
        self.bench = self.features[bench_sym]
        self.regime = regime_level_series(self.bench, cfg["regime"])

        # 交易日历以基准为准（SPY 覆盖所有正常交易日）
        bcfg = cfg["backtest"]
        cal = self.bench.index
        self.dates = cal[(cal >= bcfg["start"]) & (cal <= bcfg["end"])]

        # 可交易标的（剔除基准与利率代理）
        self.symbols = [s for s in data if s not in (bench_sym, CASH_PROXY)]

        self.slip = bcfg["slippage_bps"] / 10000.0   # 滑点（小数）
        self.commission = bcfg["commission_per_share"]
        self.initial_capital = bcfg["initial_capital"]

        # 闲置现金日利率序列：^IRX 为年化贴现收益率（百分数），
        # 除以 100 与 252 得到近似日利率；缺数据的日子按 0 计。
        self.daily_rate = pd.Series(0.0, index=self.dates)
        if bcfg.get("cash_yield", False) and CASH_PROXY in data:
            irx = data[CASH_PROXY]["close"].reindex(self.dates).ffill()
            self.daily_rate = (irx / 100.0 / 252.0).fillna(0.0)

        # 波动率目标杠杆配置（未配置时 enabled=False，行为与无杠杆版完全一致）
        self.lcfg = cfg.get("leverage", {}) or {}
        self.lev_enabled = bool(self.lcfg.get("enabled", False))

        # SPY 底仓配置：开关全额档时闲置资金买入大盘 β
        self.base_cfg = cfg.get("base_position", {}) or {}
        self.base_enabled = bool(self.base_cfg.get("enabled", False))

        # 熊市做空配置
        self.short_cfg = cfg.get("shorts", {}) or {}
        self.shorts_enabled = bool(self.short_cfg.get("enabled", False))

        # ---- 入场扫描预筛选（性能优化，不改变任何交易决策） ----
        # 把入场的必要条件向量化成"日期 → 满足条件的股票集合"，
        # 逐日循环只对命中股票调用 entry_signal 做最终判定。
        # 信号逻辑的唯一来源仍是 entry_signal——预筛选是它的精确前置条件，
        # tests/test_engine_equivalence.py 永久锁定两条路径输出逐位一致。
        self.use_prefilter = bool(cfg.get("backtest", {}).get("use_prefilter", True))
        self.prefilter_by_date: dict[pd.Timestamp, frozenset[str]] | None = None
        self.short_prefilter_by_date: dict[pd.Timestamp, frozenset[str]] | None = None
        if self.use_prefilter:
            self._build_prefilters(cfg)

        # 行业集中度约束：同一 GICS 行业最多持有 max_per_sector 个仓位
        # （0 = 关闭）。动量排序爱扎堆热门板块（如 2022 年能源），
        # 强制行业分散避免单一板块反转重伤组合。
        self.sector_map: dict[str, str] = {}
        self.max_per_sector = int(self.rcfg.get("max_per_sector", 0))
        if self.max_per_sector > 0:
            from ..data.sectors import load_sector_map

            self.sector_map = load_sector_map(
                [s for s in data if s not in (bench_sym, CASH_PROXY)],
                data_cfg["cache_dir"], readonly=readonly_data,
            )

        # 财报回避：入场前 N 个日历日内有财报的信号跳过（0 = 关闭）。
        # 财报日历取不到的股票自动不过滤（免费数据源的优雅降级）。
        self.earnings: dict[str, list] | None = None
        self.earnings_avoid_days = int(self.scfg.get("earnings_avoid_days", 0))
        if self.earnings_avoid_days > 0:
            from ..data.earnings import load_earnings_map

            self.earnings = load_earnings_map(
                [s for s in data if s not in (bench_sym, CASH_PROXY)],
                data_cfg["cache_dir"], readonly=readonly_data,
            )

        # 流动性池：每天只允许"滚动成交额排名前 top_adv"的股票入场。
        # 成交额用的是当时的数据（点时可计算），相当于在无偏的宽池内
        # 重构"当时的大盘股名单"，不含任何后见之明。
        self.adv_by_date: dict[pd.Timestamp, frozenset[str]] | None = None
        top_n = int(cfg.get("universe", {}).get("top_adv", 0) or 0)
        if top_n > 0:
            symbols_all = [s for s in data if s not in (bench_sym, CASH_PROXY)]
            adv = pd.DataFrame(
                {s: self.features[s]["adv"] for s in symbols_all}
            ).reindex(self.dates)
            eligible = adv.rank(axis=1, ascending=False) <= top_n
            self.adv_by_date = {
                d: frozenset(eligible.columns[eligible.loc[d].values])
                for d in eligible.index
            }

        # 点时成分股模式：预计算每个交易日的"当日指数成员"集合，
        # 入场候选仅限当天真实在指数里的股票（消除选股幸存者偏差）。
        # 已持仓股票被移出指数不强制卖出，仍由止损体系管理。
        self.members_by_date: dict[pd.Timestamp, frozenset[str]] | None = None
        if cfg.get("universe", {}).get("mode") == "sp500_pit":
            from ..data.sp500_pit import load_membership

            membership = load_membership(data_cfg["cache_dir"], readonly=readonly_data)
            self.members_by_date = {}
            j = -1
            current: frozenset[str] = frozenset()
            for date in self.dates:
                # 双指针推进：取生效日期 <= date 的最近一行
                while j + 1 < len(membership) and membership[j + 1][0] <= date:
                    j += 1
                    current = membership[j][1]
                self.members_by_date[date] = current

    # ---------- 内部工具 ----------

    def _row(self, sym: str, date: pd.Timestamp) -> pd.Series | None:
        """取某股票某天的特征行；当天停牌 / 未上市返回 None。"""
        df = self.features[sym]
        try:
            return df.loc[date]
        except KeyError:
            return None

    def _buy_price(self, price: float) -> float:
        """买入成交价：加滑点。"""
        return price * (1 + self.slip)

    def _sell_price(self, price: float) -> float:
        """卖出成交价：减滑点。"""
        return price * (1 - self.slip)

    def _build_prefilters(self, cfg: dict) -> None:
        """向量化预计算每个交易日"满足入场必要条件"的股票集合。

        多头条件（与 entry_signal 的前置检查逐条对应）：
            特征就绪（hh_break/atr/mom 非空、atr>0、close>0）
            + 上升结构 + 动量过滤 + (突破 | 回调反转)
        空头条件（shorts 开启时）：镜像。
        """
        # 动量过滤阈值：absolute 为 0，relative 为 SPY 同期动量。
        # ★ NaN 必须回退为 0——与 entry_signal 对"基准动量缺失"的处理
        #   保持逐位一致（否则 mom > NaN 恒 False，快路径会静默漏信号）。
        if self.scfg.get("momentum_filter", "absolute") == "relative":
            thresh = self.bench["mom"].reindex(self.dates).fillna(0.0)
        else:
            thresh = pd.Series(0.0, index=self.dates)

        long_cols: dict[str, pd.Series] = {}
        short_cols: dict[str, pd.Series] = {}
        for sym in self.symbols:
            f = self.features[sym].reindex(self.dates)
            ready = (
                f["atr"].notna() & f["mom"].notna()
                & (f["atr"] > 0) & (f["close"] > 0)
            )
            reversal = f["reversal"].fillna(False).astype(bool)
            if self.scfg.get("use_inside_bar"):
                reversal = reversal | f["ib_breakout"].fillna(False).astype(bool)
            trig_long = (f["close"] > f["hh_break"]) | (
                f["pullback"].fillna(False).astype(bool) & reversal
            )
            long_pre = (
                ready & f["hh_break"].notna()
                & f["uptrend"].fillna(False).astype(bool)
                & (f["mom"] > thresh) & trig_long
            )
            if self.scfg.get("use_weekly_confirm"):
                long_pre = long_pre & f["weekly_uptrend"].fillna(False).astype(bool)
            long_cols[sym] = long_pre
            if self.shorts_enabled:
                short_cols[sym] = (
                    ready & f["ll_break"].notna()
                    & f["downtrend"].fillna(False).astype(bool)
                    & (f["mom"] < 0) & (f["close"] < f["ll_break"])
                )

        long_df = pd.DataFrame(long_cols)
        arr = long_df.to_numpy()
        cols = long_df.columns
        self.prefilter_by_date = {
            d: frozenset(cols[arr[i]]) for i, d in enumerate(long_df.index)
        }
        if self.shorts_enabled:
            short_df = pd.DataFrame(short_cols)
            arr_s = short_df.to_numpy()
            self.short_prefilter_by_date = {
                d: frozenset(short_df.columns[arr_s[i]])
                for i, d in enumerate(short_df.index)
            }

    def _leverage_factor(self, equity_history: list[float]) -> float:
        """由组合已实现波动率计算下一交易日的杠杆倍数 f。

        f = clip(目标年化波动率 / 已实现年化波动率, min_leverage, max_leverage)

        已实现波动率取最近 vol_window 个交易日的权益日收益率标准差年化。
        历史不足或波动率为 0 时返回 1.0（不加杠杆）。
        """
        if not self.lev_enabled:
            return 1.0
        window = int(self.lcfg.get("vol_window", 20))
        if len(equity_history) < window + 1:
            return 1.0
        eq = np.asarray(equity_history[-(window + 1):])
        rets = eq[1:] / eq[:-1] - 1.0
        realized = float(np.std(rets)) * math.sqrt(252)
        if realized <= 1e-8:
            return 1.0
        f = self.lcfg.get("target_vol", 0.18) / realized
        return float(np.clip(f, self.lcfg.get("min_leverage", 0.5),
                             self.lcfg.get("max_leverage", 1.5)))

    def _allowed_positions(self, level: int) -> int:
        """按大盘开关档位折算允许的最大持仓数。

        档位 2 → 全额配额；档位 1（复苏期）→ 半额配额；档位 0 → 0。
        只约束新开仓，已有持仓不受影响（由各自止损管理）。
        """
        max_pos = self.rcfg["max_positions"]
        if level >= 2:
            return max_pos
        if level == 1:
            return math.ceil(max_pos / 2)
        return 0

    # ---------- 主循环 ----------

    def run(self) -> BacktestResult:
        """执行回测，返回权益曲线与交易明细。"""
        cash = float(self.initial_capital)
        positions: dict[str, Position] = {}
        trades: list[Trade] = []

        # 收盘后排队、次日开盘执行的订单
        # (代码, 止损价, 触发类型, 方向 1/-1)
        pending_entries: list[tuple[str, float, str, int]] = []
        pending_exits: list[tuple[str, str]] = []           # (代码, 离场原因)
        pending_adds: list[str] = []                        # 金字塔加仓的代码（仅多头）

        equity_curve: dict[pd.Timestamp, float] = {}
        equity_history: list[float] = []   # 用于已实现波动率计算
        lev = 1.0                          # 当日生效的杠杆倍数（由前一日收盘计算）
        missing_days: dict[str, int] = {}  # 持仓连续无数据天数（退市侦测）
        total_interest = 0.0               # 现金利息净额累计（绩效归因用）

        # SPY 底仓状态
        base_shares = 0                    # 当前底仓股数
        pending_base_sell = False          # 次日开盘清空底仓
        pending_base_buy = False           # 次日开盘用剩余现金买入底仓

        for date_idx, date in enumerate(self.dates):
            # 底仓标的当日行情（交易日历以基准为准，必然存在）
            base_row = self.bench.loc[date] if self.base_enabled else None
            # ================ 1. 开盘：执行排队订单 ================
            # 先卖后买，腾出现金
            for sym, reason in pending_exits:
                pos = positions.get(sym)
                row = self._row(sym, date)
                if pos is None:
                    continue
                if row is None:
                    continue  # 停牌则继续持有，下次收盘信号会重新触发
                if pos.direction == 1:
                    # 多头平仓：卖出，回收市值
                    px = self._sell_price(float(row["open"]))
                    cash += pos.shares * px - pos.shares * self.commission
                else:
                    # 空头回补：买入，支付市值（开仓时的卖出款已在现金中）
                    px = self._buy_price(float(row["open"]))
                    cash -= pos.shares * px + pos.shares * self.commission
                trades.append(self._make_trade(pos, date, px, reason))
                del positions[sym]
            pending_exits = []

            # ---- 底仓：清仓指令（开关跌下全额档后的次日开盘执行） ----
            if self.base_enabled and pending_base_sell and base_shares > 0:
                cash += base_shares * self._sell_price(float(base_row["open"]))
                base_shares = 0
            pending_base_sell = False

            # 当前权益（持仓按"前一交易日收盘价"估值——开盘时点
            # 当天收盘价尚不可知，用 before=True 严格避免未来函数）
            equity_now = cash + sum(
                p.direction * p.shares * self._last_close(p.symbol, date, before=True)
                for p in positions.values()
            )
            if self.base_enabled and base_shares > 0:
                equity_now += base_shares * self._last_close(
                    self.cfg["regime"]["symbol"], date, before=True
                )

            # ---- 底仓：为今日的入场/加仓腾挪资金（按需卖出部分底仓） ----
            if self.base_enabled and base_shares > 0 and (pending_entries or pending_adds):
                est_need = 0.0
                for sym, stop, _, _d in pending_entries:
                    pc = self._last_close(sym, date, before=True)
                    if pc > stop > 0:
                        n_est = min(
                            equity_now * self.rcfg["risk_per_trade"] * lev / (pc - stop),
                            equity_now * self.rcfg["max_weight"] * lev / pc,
                        )
                        est_need += n_est * pc
                frac = self.scfg.get("pyramid_size_frac", 0.5)
                for sym in pending_adds:
                    pos = positions.get(sym)
                    pc = self._last_close(sym, date, before=True)
                    if pos and pc > pos.stop:
                        n_est = frac * equity_now * self.rcfg["risk_per_trade"] * lev \
                            / (pc - pos.stop)
                        est_need += min(n_est * pc, equity_now * self.rcfg["max_weight"])
                sell_px = self._sell_price(float(base_row["open"]))
                shortfall = est_need * 1.02 - max(0.0, cash)   # 2% 余量覆盖滑点
                if shortfall > 0 and sell_px > 0:
                    sell_n = min(base_shares, math.ceil(shortfall / sell_px))
                    cash += sell_n * sell_px
                    base_shares -= sell_n

            # 可用购买力：未开杠杆时就是现金；开杠杆后为
            # 现金 + (f-1) × 权益（f<1 时主动降杠杆，购买力小于现金）
            available = cash + (lev - 1.0) * equity_now if self.lev_enabled else cash

            # 1a. 金字塔加仓单
            for sym in pending_adds:
                pos = positions.get(sym)
                row = self._row(sym, date)
                if pos is None or row is None:
                    continue
                px = self._buy_price(float(row["open"]))
                add_shares = self._pyramid_size(pos, px, equity_now, available, lev)
                if add_shares <= 0:
                    continue
                cost = add_shares * px + add_shares * self.commission
                cash -= cost
                available -= cost
                # 更新加权平均入场价与股数
                total = pos.shares + add_shares
                pos.entry_price = (
                    pos.entry_price * pos.shares + px * add_shares
                ) / total
                pos.shares = total
                pos.adds += 1
            pending_adds = []

            # 1b. 新开仓（多头买入 / 空头卖出开仓）
            for sym, stop, trigger, direction in pending_entries:
                if sym in positions:
                    continue
                row = self._row(sym, date)
                if row is None:
                    continue
                if direction == 1:
                    if len(positions) >= self.rcfg["max_positions"]:
                        continue
                    px = self._buy_price(float(row["open"]))
                    # 隔夜跳空导致开盘价已低于止损 → 放弃该信号
                    if px <= stop:
                        continue
                    shares = position_size(equity_now, available, px, stop,
                                           self.rcfg, risk_scale=lev)
                    if shares <= 0:
                        continue
                    cost = shares * px + shares * self.commission
                    cash -= cost
                    available -= cost
                else:
                    # 空头开仓：数量受空头独立上限约束
                    n_shorts = sum(1 for p in positions.values() if p.direction == -1)
                    if n_shorts >= int(self.short_cfg.get("max_positions", 5)):
                        continue
                    px = self._sell_price(float(row["open"]))
                    # 隔夜跳空高开已越过止损 → 放弃
                    if px >= stop:
                        continue
                    risk_per_share = stop - px
                    if risk_per_share <= 0:
                        continue
                    shares = min(
                        equity_now * self.rcfg["risk_per_trade"] / risk_per_share,
                        equity_now * self.rcfg["max_weight"] / px,
                        max(0.0, available) * 0.995 / px,  # 名义市值占用购买力（保守保证金模型）
                    )
                    shares = max(0, math.floor(shares))
                    if shares <= 0:
                        continue
                    cash += shares * px - shares * self.commission  # 卖空所得计入现金
                    available -= shares * px                        # 但占用购买力
                positions[sym] = Position(
                    symbol=sym, shares=shares, entry_date=date,
                    signal_date=(self.dates[date_idx - 1] if date_idx > 0 else date),
                    entry_price=px, initial_entry_price=px,
                    stop=stop, initial_stop=stop, highest_close=px,
                    trigger=trigger, init_risk=abs(px - stop),
                    direction=direction,
                )
            pending_entries = []

            # ---- 底仓：入场完成后用剩余现金回补（保留缓冲） ----
            if self.base_enabled and pending_base_buy:
                buffer = self.base_cfg.get("cash_buffer", 0.02) * equity_now
                invest = cash - buffer
                # 低于权益 1% 的零钱不折腾，避免每天微量买卖
                if invest > 0.01 * equity_now:
                    px = self._buy_price(float(base_row["open"]))
                    n = math.floor(invest / px)
                    if n > 0:
                        cash -= n * px
                        base_shares += n
            pending_base_buy = False

            # ================ 2. 盘中：硬止损检查 ================
            for sym in list(positions):
                pos = positions[sym]
                row = self._row(sym, date)
                if row is None:
                    continue
                o, hi, lo = float(row["open"]), float(row["high"]), float(row["low"])
                if pos.init_risk > 0:
                    if pos.direction == 1:
                        pos.mfe_r = max(
                            pos.mfe_r,
                            (hi - pos.initial_entry_price) / pos.init_risk,
                        )
                        pos.mae_r = max(
                            pos.mae_r,
                            (pos.initial_entry_price - lo) / pos.init_risk,
                        )
                    else:
                        pos.mfe_r = max(
                            pos.mfe_r,
                            (pos.initial_entry_price - lo) / pos.init_risk,
                        )
                        pos.mae_r = max(
                            pos.mae_r,
                            (hi - pos.initial_entry_price) / pos.init_risk,
                        )
                if pos.direction == 1:
                    # 多头止损：向下击穿
                    if o <= pos.stop:
                        px = self._sell_price(o)      # 跳空低开：按开盘价成交
                    elif lo <= pos.stop:
                        px = self._sell_price(pos.stop)  # 盘中触及：按止损价成交
                    else:
                        continue
                    cash += pos.shares * px - pos.shares * self.commission
                else:
                    # 空头止损：向上击穿（买入回补）
                    if o >= pos.stop:
                        px = self._buy_price(o)
                    elif hi >= pos.stop:
                        px = self._buy_price(pos.stop)
                    else:
                        continue
                    cash -= pos.shares * px + pos.shares * self.commission
                trades.append(self._make_trade(pos, date, px, "stop"))
                del positions[sym]

            # ================ 2b. 僵尸持仓清算 ================
            # 持仓连续 5 个交易日无行情（退市/被收购摘牌）→ 按最后可得
            # 收盘价强制清算。真实收购对价未知，按停牌前价是保守近似；
            # 不清算的话该仓位会冻结资金并占用持仓槽直到回测结束
            # （实证：ESRX 2018-12-20 止损离场距其数据终点仅 1 天，
            #   差一点就会被冻结 7 年）。
            for sym in list(positions):
                if self._row(sym, date) is None:
                    missing_days[sym] = missing_days.get(sym, 0) + 1
                    if missing_days[sym] >= 5:
                        pos = positions[sym]
                        last_px = self._last_close(sym, date)
                        px = (self._sell_price(last_px) if pos.direction == 1
                              else self._buy_price(last_px))
                        cash += pos.direction * pos.shares * px
                        trades.append(self._make_trade(pos, date, px, "delisted"))
                        del positions[sym]
                        del missing_days[sym]
                else:
                    missing_days.pop(sym, None)

            # ================ 3. 收盘：信号计算与止损上移 ================
            level = int(self.regime.get(date, 0))

            # 3a. 持仓管理：更新吊灯止损 + 收盘级离场 + 金字塔触发
            time_stop_days = int(self.scfg.get("time_stop_days", 0))
            time_stop_min_r = self.scfg.get("time_stop_min_r", 1.0)
            for sym, pos in positions.items():
                row = self._row(sym, date)
                if row is None:
                    continue
                pos.age_days += 1
                c = float(row["close"])
                if pos.direction == 1:
                    pos.highest_close = max(pos.highest_close, c)
                    pos.stop = update_trailing_stop(
                        pos.stop, pos.highest_close, float(row["atr"]), self.scfg
                    )
                    if close_exit_signal(row):
                        pending_exits.append((sym, "channel"))
                    elif (time_stop_days > 0 and pos.age_days >= time_stop_days
                          and c - pos.entry_price < time_stop_min_r * pos.init_risk):
                        # 时间止损：持有 N 日仍未走出 min_r 倍初始风险的浮盈
                        # →"死钱"，释放槽位给新趋势
                        pending_exits.append((sym, "time"))
                    elif pyramid_trigger(c, pos.entry_price, pos.init_risk,
                                         pos.adds, self.scfg):
                        pending_adds.append(sym)
                else:
                    # 空头：highest_close 字段承载"最低收盘价"（有利极值）
                    pos.highest_close = min(pos.highest_close, c)
                    pos.stop = update_trailing_stop_short(
                        pos.stop, pos.highest_close, float(row["atr"]), self.scfg
                    )
                    if short_close_exit_signal(row):
                        pending_exits.append((sym, "channel"))
                    # 空头持仓每日支付融券费（按收盘市值折算），
                    # 并计入利息归因（total_interest 的口径 = 利息收入 - 融资/融券成本）
                    borrow = self.short_cfg.get("borrow_rate", 0.01) / 252.0
                    fee = pos.shares * c * borrow
                    cash -= fee
                    total_interest -= fee

            exiting = {s for s, _ in pending_exits}

            # 3b. 入场信号扫描（配额随大盘开关档位折算）
            allowed = self._allowed_positions(level)
            free_slots = allowed - (len(positions) - len(exiting))
            if free_slots > 0:
                bench_mom = float(self.bench.loc[date, "mom"]) \
                    if date in self.bench.index and not pd.isna(self.bench.loc[date, "mom"]) else None
                # 点时模式下仅扫描"当天真实在指数里"的股票
                members = (
                    self.members_by_date.get(date)
                    if self.members_by_date is not None
                    else None
                )
                # 流动性池：当天成交额排名前 top_adv 的股票
                liquid = (
                    self.adv_by_date.get(date)
                    if self.adv_by_date is not None
                    else None
                )
                # 预筛选剪枝：只扫当天满足入场必要条件的股票
                # （保持 self.symbols 的遍历顺序，确保与慢路径逐位一致）
                if self.prefilter_by_date is not None:
                    pre = self.prefilter_by_date.get(date, frozenset())
                    scan_syms = [s for s in self.symbols if s in pre]
                else:
                    scan_syms = self.symbols
                candidates = []
                for sym in scan_syms:
                    if sym in positions:
                        continue
                    if members is not None and sym not in members:
                        continue
                    if liquid is not None and sym not in liquid:
                        continue
                    row = self._row(sym, date)
                    if row is None:
                        continue
                    # 财报回避：N 天内有财报的不入场
                    if self.earnings is not None:
                        from ..data.earnings import earnings_within

                        if earnings_within(self.earnings.get(sym, []),
                                           date, self.earnings_avoid_days):
                            continue
                    sig = entry_signal(row, level, self.scfg, bench_mom)
                    if sig is not None:
                        candidates.append((sym, sig))
                # 按排序分从高到低择优，同时执行行业集中度约束
                candidates.sort(key=lambda x: x[1].rank, reverse=True)
                sector_counts: dict[str, int] = {}
                if self.max_per_sector > 0:
                    for sym in positions:
                        if sym in exiting:
                            continue
                        sec = self.sector_map.get(sym)
                        if sec and sec != "Unknown":
                            sector_counts[sec] = sector_counts.get(sec, 0) + 1
                # 同公司多股别去重：已持有任一股别则跳过其余股别
                dedup = bool(self.rcfg.get("dedup_share_classes", False))
                held_groups: set[str] = set()
                if dedup:
                    from ..data.aliases import company_group

                    held_groups = {
                        company_group(s) for s in positions if s not in exiting
                    }
                picked = 0
                for sym, sig in candidates:
                    if picked >= free_slots:
                        break
                    if dedup:
                        grp = company_group(sym)
                        if grp in held_groups:
                            continue  # 同公司另一股别已持有
                    if self.max_per_sector > 0:
                        sec = self.sector_map.get(sym)
                        if (sec and sec != "Unknown"
                                and sector_counts.get(sec, 0) >= self.max_per_sector):
                            continue  # 该行业仓位已满
                        if sec and sec != "Unknown":
                            sector_counts[sec] = sector_counts.get(sec, 0) + 1
                    pending_entries.append((sym, sig.stop, sig.trigger, 1))
                    picked += 1
                    if dedup:
                        held_groups.add(company_group(sym))

            # 3b'. 熊市做空信号扫描（仅开关档位 0 且模块开启时）
            if self.shorts_enabled and level == 0:
                n_shorts = sum(
                    1 for s, p in positions.items()
                    if p.direction == -1 and s not in exiting
                )
                short_slots = int(self.short_cfg.get("max_positions", 5)) - n_shorts
                if short_slots > 0:
                    members = (
                        self.members_by_date.get(date)
                        if self.members_by_date is not None else None
                    )
                    liquid = (
                        self.adv_by_date.get(date)
                        if self.adv_by_date is not None else None
                    )
                    if self.short_prefilter_by_date is not None:
                        pre_s = self.short_prefilter_by_date.get(date, frozenset())
                        scan_s = [s for s in self.symbols if s in pre_s]
                    else:
                        scan_s = self.symbols
                    short_cands = []
                    for sym in scan_s:
                        if sym in positions:
                            continue
                        if members is not None and sym not in members:
                            continue
                        if liquid is not None and sym not in liquid:
                            continue
                        row = self._row(sym, date)
                        if row is None:
                            continue
                        sig = short_entry_signal(row, level, self.scfg)
                        if sig is not None:
                            short_cands.append((sym, sig))
                    short_cands.sort(key=lambda x: x[1].rank, reverse=True)
                    for sym, sig in short_cands[:short_slots]:
                        pending_entries.append((sym, sig.stop, sig.trigger, -1))

            # 3c. 现金利息：正现金按 ^IRX 计息（模拟短债 ETF 收益）；
            #     负现金（融资余额）按 ^IRX + 融资利差付息
            rate = float(self.daily_rate.get(date, 0.0))
            if cash > 0:
                interest = cash * rate
                cash += interest
                total_interest += interest
            elif cash < 0:
                spread = self.lcfg.get("margin_spread", 0.015) / 252.0
                interest = cash * (rate + spread)   # cash 为负 → 利息为扣减
                cash += interest
                total_interest += interest

            # 3d. 底仓调度：全额档保持底仓（次日回补），否则清仓
            if self.base_enabled:
                if level >= int(self.base_cfg.get("min_level", 2)):
                    pending_base_buy = True
                elif base_shares > 0:
                    pending_base_sell = True

            # 3e. 收盘估值，记录权益曲线；更新下一交易日的杠杆倍数
            # （空头开仓所得已在现金中，故持仓贡献取负值：direction × 市值）
            equity = cash + sum(
                p.direction * p.shares * self._last_close(p.symbol, date)
                for p in positions.values()
            )
            if self.base_enabled and base_shares > 0:
                equity += base_shares * float(base_row["close"])
            equity_curve[date] = equity
            equity_history.append(equity)
            lev = self._leverage_factor(equity_history)

        # ================ 收尾：清算剩余持仓（按最后收盘价） ================
        # 清算前先快照期末持仓与排队决策
        # （影子对账与"回测↔实盘信号一致性"测试都需要它们）
        open_positions = {
            sym: {"direction": p.direction, "shares": p.shares,
                  "stop": round(p.stop, 6),
                  "initial_stop": round(p.initial_stop, 6),
                  "highest_close": round(p.highest_close, 6),
                  "entry_price": round(p.entry_price, 6),
                  "initial_entry_price": round(p.initial_entry_price, 6),
                  "signal_date": str(p.signal_date.date()),
                  "init_risk": round(p.init_risk, 6),
                  "adds": p.adds,
                  "mfe_r": round(p.mfe_r, 6),
                  "mae_r": round(p.mae_r, 6)}
            for sym, p in positions.items()
        }
        final_signals = {
            "entries": list(pending_entries),
            "exits": list(pending_exits),
            "adds": list(pending_adds),
        }
        last_date = self.dates[-1]
        for pos in positions.values():
            last_px = self._last_close(pos.symbol, last_date)
            px = (self._sell_price(last_px) if pos.direction == 1
                  else self._buy_price(last_px))
            trades.append(self._make_trade(pos, last_date, px, "end"))

        equity_s = pd.Series(equity_curve, name="equity")

        # 基准（SPY 买入持有）归一化到同样的初始资金
        bench_close = self.bench["close"].reindex(self.dates).ffill()
        benchmark = bench_close / bench_close.iloc[0] * self.initial_capital

        return BacktestResult(equity=equity_s, trades=trades, benchmark=benchmark,
                              open_positions=open_positions,
                              total_interest=round(total_interest, 2),
                              final_signals=final_signals)

    # ---------- 辅助 ----------

    def _pyramid_size(self, pos: Position, price: float,
                      equity: float, available: float, lev: float = 1.0) -> int:
        """计算金字塔加仓股数。

        规模 = 标准风险单位的 size_frac 倍（按当前止损距离折算），
        同时受单仓市值上限与购买力约束；开杠杆时两者随 f 缩放。
        """
        frac = self.scfg.get("pyramid_size_frac", 0.5)
        risk_per_share = price - pos.stop
        if risk_per_share <= 0:
            return 0
        shares = frac * equity * self.rcfg["risk_per_trade"] * lev / risk_per_share
        # 单仓市值上限：现有持仓 + 加仓部分合计不超过 max_weight × f
        max_value = equity * self.rcfg["max_weight"] * lev
        room = max_value - pos.shares * price
        shares = min(shares, max(0.0, room) / price)
        shares = min(shares, max(0.0, available) * 0.995 / price)
        return max(0, math.floor(shares))

    def _last_close(self, sym: str, date: pd.Timestamp, before: bool = False) -> float:
        """截至 date 的最近收盘价（处理停牌日估值）。

        参数:
            before: True 时取 date 之前（不含 date 当天）的最近收盘价，
                    用于开盘时点的权益估值（当天收盘价尚不可知）。
        """
        df = self.features[sym]
        side = "left" if before else "right"
        idx = df.index.searchsorted(date, side=side) - 1
        if idx < 0:
            return 0.0
        return float(df["close"].iloc[idx])

    @staticmethod
    def _make_trade(pos: Position, date: pd.Timestamp, px: float, reason: str) -> Trade:
        """由持仓与离场信息生成交易记录（多空方向已折算）。"""
        pnl = pos.direction * (px - pos.entry_price) * pos.shares
        return Trade(
            symbol=pos.symbol,
            entry_date=pos.entry_date,
            signal_date=pos.signal_date,
            exit_date=date,
            entry_price=round(pos.entry_price, 4),
            initial_entry_price=round(pos.initial_entry_price, 4),
            initial_stop=round(pos.initial_stop, 4),
            init_risk=round(pos.init_risk, 4),
            exit_price=round(px, 4),
            shares=pos.shares,
            pnl=round(pnl, 2),
            ret=round(pos.direction * (px / pos.entry_price - 1), 4),
            trigger=pos.trigger,
            exit_reason=reason,
            adds=pos.adds,
            holding_days=pos.age_days,
            mfe_r=round(pos.mfe_r, 6),
            mae_r=round(pos.mae_r, 6),
            direction=pos.direction,
        )
