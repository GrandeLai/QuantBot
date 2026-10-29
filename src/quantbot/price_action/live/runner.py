"""实盘 / 模拟盘每日调度器。

================== 运行模型（与回测共享信号时序） ==================

回测的撮合模型是：第 T-1 天收盘后算信号，第 T 天开盘执行。
因此实盘 runner 设计为 **每个交易日开盘后不久运行一次**
（建议美东时间 09:35，用 cron / launchd 定时触发）：

    0. 交易日检查（假日直接退出）+ 进程文件锁（防并发双跑）；
    1. 刷新行情（写实盘专用缓存，不污染研究数据），
       剔除美股盘中未走完的半根 K 线，校验数据新鲜度；
    2. 同步账户与持仓，侦测拆股（券商股数与本地记录突变 →
       按比例缩放止损等价格状态），校验每只持仓都有可用数据；
    3. 离场：昨日盘中触及止损、或昨日收盘破离场通道 → 市价单，
       ★ 等成交确认后才删除本地状态；失败则保留原止损并告警；
    4. 金字塔加仓（仅多头）：等成交确认，用真实成交均价更新状态；
    5. 入场（多头 / 熊市空头）：先实时报价做跳空保护，
       下单后等成交确认，用真实成交价写入状态；拒单不产生幽灵持仓；
    6. SPY 底仓调度（默认关闭）；
    7. 原子化落盘状态 + 执行摘要通知 + 外部心跳。

正式 Paper 的收盘事实由独立 paper-close 只读采集。回测盘中止损与这里
次日检查后市价卖出仍是不同的成交时点，信号 parity 不代表成交模型一致。

任何一步网络/订单失败都不会静默：要么保留保守状态，要么推送告警。

持仓股数以券商为准（source of truth），
本地状态文件只额外记录券商没有的信息：止损价、初始风险 R、
已加仓次数、方向、成交股数（拆股侦测用）等。

Alpaca paper/live 必须由命令与 config.yaml 的 broker.paper 双重一致确认；
live 入口还要求显式 ``--confirm-live``，避免 cron 或误操作静默切换账户。
================================================================
"""

from __future__ import annotations

import fcntl
import copy
import hashlib
import json
import math
import os
import shutil
from pathlib import Path

import pandas as pd

from ..broker.base import Broker
from ..config import config_hash
from ..data.downloader import load_all, load_all_batch, load_market_data
from ..data.universe import BENCHMARK, CASH_PROXY, UNIVERSE
from ..data.session import market_now
from ..domain.decision import (
    DecisionIntent,
    DecisionSource,
    ExposureEffect,
    PriceActionEvidence,
    evidence_from_feature_row,
)
from ..governance import source_fingerprint, validate_exposure_change
from ..priceaction.features import build_features
from ..risk.manager import position_size
from ..review import PaperCampaignSpec
from ..strategy.trend_breakout import (
    close_exit_signal,
    entry_signal,
    evaluate_entry_signal,
    pyramid_trigger,
    regime_level_series,
    short_close_exit_signal,
    short_entry_signal,
    update_trailing_stop,
    update_trailing_stop_short,
)
from .evidence import LiveEvidenceRecorder, OrderEvidence, market_data_fingerprint
from .notify import heartbeat, notify
from .paths import reject_legacy_state, runtime_paths
from .recovery import RecoveryHoldError, assert_safe_to_trade, inspect_startup_recovery


_NO_FILL_TERMINAL_STATUSES = frozenset(
    {"dead", "canceled", "expired", "rejected"}
)


def drop_partial_bar(data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """丢弃"美东时间今天且尚未收盘"的半根 K 线。

    背景：本机若在北京时间晚间（= 美东盘中）运行，
    pd.Timestamp.today() 的日期比美东超前一天，yfinance 会把
    美东当天正在交易中的半根日线也返回回来。用这半根算信号
    会与回测逻辑（只用已收盘 K 线）系统性偏离，必须剔除。

    判定：按美东时区取当前时刻，若某标的最后一根 bar 的日期
    等于美东"今天"且现在还不到收盘（16:00 ET），则丢弃该行。
    """
    now_et = pd.Timestamp(market_now())
    market_closed = now_et.hour >= 16  # 16:00 ET 收盘后当日 K 线视为完整
    out: dict[str, pd.DataFrame] = {}
    for sym, df in data.items():
        if len(df) and df.index[-1].date() == now_et.date() and not market_closed:
            out[sym] = df.iloc[:-1]
        else:
            out[sym] = df
    return out


class LiveRunner:
    """每日一次的实盘 / 模拟盘执行器。"""

    # 拆股侦测阈值：券商股数 / 本地记录股数偏离该倍数即视为拆股/并股
    SPLIT_RATIO_THRESHOLD = 1.5
    # 数据新鲜度阈值：信号基准日距今超过该天数视为数据陈旧
    STALE_DAYS = 4

    def __init__(
        self,
        broker: Broker,
        cfg: dict,
        *,
        mode: str,
        journal=None,
        campaign: PaperCampaignSpec | None = None,
    ):
        if campaign is not None and mode != "paper":
            raise ValueError("PaperCampaignSpec 只能绑定 mode=paper 的 runner")
        if campaign is not None and campaign.candidate_config_hash != config_hash(cfg):
            raise ValueError("Paper campaign 的候选配置哈希与当前配置不一致")
        self.broker = broker
        self.cfg = copy.deepcopy(cfg)
        self.scfg = self.cfg["strategy"]
        self.rcfg = self.cfg["risk"]
        reject_legacy_state(cfg, mode)
        self.account_fingerprint = broker.account_fingerprint()
        self.paths = runtime_paths(cfg, mode, self.account_fingerprint)
        self.state_path = self.paths.state_file
        self.mode = mode
        self.campaign = campaign
        self.market_session = None
        self.evidence = LiveEvidenceRecorder(
            self.cfg,
            mode,
            journal=journal,
            campaign=campaign,
            journal_path=self.paths.journal_file,
        )

    def _price_evidence(
        self,
        asof: pd.Timestamp,
        row: pd.Series,
        level: int,
        bench_mom: float | None,
        **extra,
    ) -> PriceActionEvidence:
        return evidence_from_feature_row(
            asof,
            row,
            regime_level=level,
            bench_momentum=bench_mom,
            extra=extra,
        )

    def _submit_order(
        self,
        *,
        key: str,
        symbol: str,
        side: str,
        quantity: int,
        reference_price: float,
        action: str,
        reason_code: str,
        evidence: PriceActionEvidence,
        exposure_effect: ExposureEffect,
        source: DecisionSource = DecisionSource.PRICE_ACTION,
    ) -> tuple[str, OrderEvidence]:
        """先落决策/订单意图，再以稳定 client ID 调用券商。"""
        if self.evidence.requires_recovery:
            raise RecoveryHoldError.from_issue(
                "runtime_order_unresolved",
                "本次运行已经出现未决订单，禁止继续产生新的券商写入",
            )
        DecisionIntent(
            symbol=symbol,
            intent=action,
            source=source,
            exposure_effect=exposure_effect,
            reason_code=reason_code,
            evidence=evidence,
        )
        decision_id = self.evidence.decision(
            key=f"{key}:approved",
            symbol=symbol,
            action=action,
            outcome="approved",
            reason_code=reason_code,
            evidence=evidence.to_dict(),
            intended_quantity=quantity,
            reference_price=reference_price,
        )
        signed_quantity = quantity if side == "buy" else -quantity
        if exposure_effect == ExposureEffect.INCREASE:
            before_shares, after_shares = 0, signed_quantity
        elif exposure_effect == ExposureEffect.DECREASE:
            before_shares, after_shares = -signed_quantity, 0
        else:
            before_shares = after_shares = 0
        validate_exposure_change(
            symbol=symbol,
            before_shares=before_shares,
            after_shares=after_shares,
            source=source,
            reason=reason_code,
            evidence_id=decision_id,
        )
        order = self.evidence.plan_order(
            key=key,
            decision_id=decision_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            reference_price=reference_price,
        )
        try:
            if side == "buy":
                broker_order_id = self.broker.market_buy(
                    symbol, quantity, order.client_order_id
                )
            else:
                broker_order_id = self.broker.market_sell(
                    symbol, quantity, order.client_order_id
                )
        except Exception:
            self.evidence.terminal(order, status="submit_error", fill=None)
            raise
        return broker_order_id, self.evidence.submitted(order, broker_order_id)

    def _settle_evidenced_order(
        self,
        broker_order_id: str,
        order: OrderEvidence,
    ) -> tuple[str, tuple[int, float] | None, str]:
        status, fill = self._settle_order(
            broker_order_id,
            order.client_order_id,
        )
        event_id = self.evidence.terminal(order, status=status, fill=fill)
        return status, fill, event_id

    # ---------- 状态文件 ----------
    # 文件结构（v3）：
    #   {"meta": {"last_run_date": "..."},
    #    "positions": {代码: {stop, highest_close, entry_price, entry_date,
    #                        init_risk, adds, direction, qty}}}
    # 兼容 v1（顶层即持仓表）与 v2（无 qty 字段）自动迁移。

    def _load_state(self) -> tuple[dict, dict]:
        """读取本地状态，返回 (meta, positions)。"""
        if not self.state_path.exists():
            return {}, {}
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        if "positions" in raw and "meta" in raw:
            return raw["meta"], raw["positions"]
        return {}, raw  # v1 旧格式：顶层即持仓表

    def _save_state(self, meta: dict, positions: dict) -> str:
        """持久化状态：文件 fsync 后原子替换，再同步父目录项。"""
        tmp = self.state_path.with_suffix(".json.tmp")
        payload = json.dumps(
            {"meta": meta, "positions": positions},
            indent=2,
            ensure_ascii=False,
        ).encode("utf-8")
        state_sha256 = hashlib.sha256(payload).hexdigest()
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.state_path)
        # 目录项无法确认持久化时，绝不能向账本宣称 state_committed。
        directory_fd = os.open(self.state_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return state_sha256

    def _backup_state(self) -> None:
        """运行前把现有状态备份一份（按日期命名，损坏时可回滚）。"""
        if not self.state_path.exists():
            return
        backup_dir = self.state_path.parent / "state_backups"
        backup_dir.mkdir(exist_ok=True)
        stamp = pd.Timestamp(market_now()).strftime("%Y%m%d")
        shutil.copy2(self.state_path, backup_dir / f"live_state_{stamp}.json")

    # ---------- 订单结算 ----------

    def _settle_order(
        self,
        order_id: str,
        client_order_id: str,
    ) -> tuple[str, tuple[int, float] | None]:
        """等待订单终态；超时先撤单再复核，绝不留下"无人跟踪的活订单"。

        返回 (状态, 成交明细)：
            filled    完全成交，真实副作用模式必须同时取得数量与均价
            partial   券商终态前已部分成交（仍需下一次启动人工核对）
            canceled / rejected / expired
                      券商明确确认的未成交终态
            uncertain 状态不明（撤单失败且查询不到成交）——调用方必须
                      停止后续开仓并告警，防止双倍建仓
        """
        if self.broker.wait_order(order_id):
            fill = self.broker.order_fill(order_id)
            if fill is None and self.mode in {"paper", "live", "sim"}:
                # 券商虽报告完全成交，但数量/均价证据读取未知。真实副作用
                # 模式不能用计划数量和参考价伪造交接；下一次启动只读恢复。
                return "uncertain", None
            return "filled", fill
        # 未确认成交：超时的订单可能仍在市场上存活，先撤单
        # （若已是拒单终态，撤单请求无害）
        self.broker.cancel_order(order_id)
        # 撤单 API 成功只表示请求被接受，不代表订单已经终止。必须再按稳定
        # client ID 读取券商事实；读取异常向上抛出，由启动门禁接管。
        snapshot = self.broker.get_order_by_client_id(client_order_id)
        if snapshot is None or snapshot.order_id != order_id:
            return "uncertain", None
        if snapshot.is_open:
            return "uncertain", None
        fill = snapshot.fill
        if snapshot.filled_quantity > 0 and fill is None:
            # 取消竞态中已成交但均价缺失，绝不能冒充“零成交撤单”。
            return "uncertain", None
        if snapshot.status == "filled":
            return ("filled", fill) if fill is not None else ("uncertain", None)
        if fill is not None and fill[0] > 0:
            # 终态撤单但已有部分成交：本地虽会按实际数量记账，下一次启动
            # 仍需人工核对，因此保留兼容状态 partial。
            return "partial", fill
        return snapshot.status, None

    # ---------- 主流程 ----------

    def run_once(self, force: bool = False, data: dict | None = None) -> None:
        """执行一次完整的每日决策流程。

        参数:
            force: True 时跳过交易日检查与当日幂等性检查（人工补跑用）。
            data:  预加载的行情数据（单元测试注入用；生产环境留空，
                   由本方法自行刷新下载）。
        """
        cfg = self.cfg
        if self.campaign is not None and force:
            raise ValueError("正式 Paper campaign 禁止使用 --force 绕过交易时段")
        actions: list[str] = []   # 本次运行的动作记录（日终摘要 / 通知用）
        fills: list[dict] = []    # 本次运行的成交记录（实测滑点统计用）

        # ---- 0. 进程文件锁：防止 cron 重试 / 人工并发造成双重下单 ----
        lock_path = self.state_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")  # noqa: SIM115 - 锁需持有到流程结束
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            notify("⚠️ PABot 检测到另一个实例正在运行，本次退出（防双重下单）")
            print("[live] 已有实例在运行（文件锁被占用），退出")
            lock_file.close()
            self.evidence.close()
            return

        try:
            # ---- 0a. 启动恢复门禁：每次启动都先核对，再判断是否交易日 ----
            # 旧研究测试未显式指定 mode 且未开启账本时保持兼容；真实的
            # paper/live/sim 则必须有账本，否则 inspect 会 fail-closed。
            if self.evidence.journal is not None or self.mode in {"paper", "live", "sim"}:
                recovery = inspect_startup_recovery(
                    self.evidence.journal,
                    self.broker,
                    state_path=self.state_path,
                )
                if not recovery.safe:
                    message = str(RecoveryHoldError(recovery))
                    print(f"[live] {message}")
                    notify(f"⚠️ {message}")
                assert_safe_to_trade(recovery)
            # ---- 0b. 交易日检查：假日 / 休市时 cron 触发直接退出 ----
            # 即使休市也不能跳过上面的恢复核对，否则周末会把未知订单隐藏成
            # “正常跳过”，延迟人工处置。
            if not force and not self.broker.is_market_open():
                print("[live] 当前非交易时段（周末/假日/盘外），跳过本次运行")
                return
            if self.campaign is not None:
                now = market_now()
                self.market_session = self.broker.get_market_session(now.date().isoformat())
                if not self.market_session.contains_execution(now):
                    raise RuntimeError("当前时刻不在 Paper 权威交易会话内")
            self._run_locked(cfg, actions, fills, force, data)
        except RecoveryHoldError:
            # HOLD 是预期的安全终止，不得再追加一个含混的 failed 终态。
            raise
        except Exception as exc:
            self.evidence.finish(
                "failed",
                metadata={"error_type": type(exc).__name__, "error": str(exc)},
            )
            raise
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
            lock_file.close()
            self.evidence.close()

    def _run_locked(self, cfg: dict, actions: list[str], fills: list[dict],
                    force: bool, data: dict | None) -> None:
        """持有进程锁后的主体流程（内部方法）。"""
        # ---- 1. 刷新行情 ----
        if data is None:
            # 写入实盘专用缓存目录，物理隔离研究用缓存；
            # 实盘路径禁写"退市空标记"（防止瞬时故障把活股票永久拉黑）
            print("[live] 刷新行情数据 ...")
            # 多取一天，末尾的半根 K 线由 drop_partial_bar 统一处理。
            end = (pd.Timestamp(market_now()) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if cfg.get("universe", {}).get("mode") == "sp500_pit":
                from ..data.sp500_pit import load_membership, membership_asof

                # 先确定完整信号日，再选该日成员；执行日生效的成分调整
                # 不能提前改变昨日信号。持仓即使已退出指数也必须检查止损。
                benchmarks = drop_partial_bar(load_all(
                    [BENCHMARK, CASH_PROXY], cfg["data"]["start"], end,
                    self.paths.cache_dir, force=True,
                ))
                signal_day = benchmarks[BENCHMARK].index[-1]
                membership = load_membership(self.paths.cache_dir, force=True)
                symbols = set(membership_asof(membership, signal_day))
                symbols.update(self.broker.get_positions())
                symbols.difference_update((BENCHMARK, CASH_PROXY))
                print(f"[data] 信号日 {signal_day.date()} 成员与持仓：{len(symbols)} 只标的")
                data = load_all_batch(
                    sorted(symbols), cfg["data"]["start"], end, self.paths.cache_dir,
                    force=True, allow_empty_markers=False,
                )
                data.update(benchmarks)
            else:
                data = load_market_data(
                    cfg, force=True, end=end,
                    cache_dir_override=str(self.paths.cache_dir),
                    allow_empty_markers=False,
                )
            data = drop_partial_bar(data)
        features = {
            sym: build_features(df, self.scfg)
            for sym, df in data.items()
            if sym != CASH_PROXY
        }

        bench = features[BENCHMARK]
        regime = regime_level_series(bench, cfg["regime"])
        # "信号基准日" = 基准数据的最后一个完整交易日
        asof = bench.index[-1]
        level = int(regime.loc[asof])
        bench_mom = float(bench.loc[asof, "mom"]) \
            if not pd.isna(bench.loc[asof, "mom"]) else None
        print(f"[live] 信号基准日: {asof.date()}，大盘开关档位: {level}"
              f"（2=开仓 0=停止开仓）")

        # ---- 1a. 数据新鲜度校验：基准数据陈旧说明数据源故障 ----
        now_et = pd.Timestamp(market_now())
        age_days = (now_et.date() - asof.date()).days
        source_hash = None
        account_fingerprint = None
        if self.campaign is not None:
            execution_day = now_et.date().isoformat()
            if execution_day not in self.campaign.expected_trading_dates:
                raise RuntimeError(
                    f"{execution_day} 不在 Paper campaign 冻结交易日中，拒绝交易"
                )
            campaign_index = self.campaign.expected_trading_dates.index(execution_day)
            expected_signal_day = self.campaign.expected_signal_dates[campaign_index]
            if asof.date().isoformat() != expected_signal_day:
                raise RuntimeError(
                    "Paper campaign 信号日不匹配："
                    f"期望 {expected_signal_day}，实际 {asof.date().isoformat()}"
                )
            source_hash = source_fingerprint(
                Path(__file__).resolve().parents[4]
            ).source_hash
            account_fingerprint = self.broker.account_fingerprint()
            if source_hash != self.campaign.source_hash:
                raise RuntimeError("当前源码哈希与 Paper campaign 不一致，拒绝交易")
            if account_fingerprint != self.campaign.account_fingerprint:
                raise RuntimeError("当前匿名账户指纹与 Paper campaign 不一致，拒绝交易")
        self.evidence.start(
            asof=asof,
            market_level=level,
            data_hash=market_data_fingerprint(data, asof),
            data_status="stale" if age_days > self.STALE_DAYS else "fresh",
            execution_date=now_et,
            source_hash=source_hash,
            account_fingerprint=account_fingerprint,
            market_session=self.market_session,
        )
        if age_days > self.STALE_DAYS:
            msg = (f"⚠️ PABot 数据陈旧：信号基准日 {asof.date()} 距今 {age_days} 天，"
                   f"疑似数据源故障，本次不交易")
            print(f"[live] {msg}")
            notify(msg)
            self.evidence.finish("skipped", metadata={"reason": "stale_data"})
            return

        # ---- 1b. 幂等性检查：同一信号基准日只执行一次 ----
        meta, state = self._load_state()
        if not force and meta.get("last_run_date") == str(asof.date()):
            print(f"[live] 信号基准日 {asof.date()} 已执行过，"
                  f"跳过（重复 cron 触发保护；--force 可强制重跑）")
            self.evidence.finish("skipped", metadata={"reason": "already_run"})
            return
        self._backup_state()

        # ---- 2. 同步账户与持仓 ----
        acct = self.broker.get_account()
        broker_pos = self.broker.get_positions()
        print(f"[live] 账户权益 ${acct.equity:,.0f}，现金 ${acct.cash:,.0f}，"
              f"持仓 {len(broker_pos)} 只")

        # 底仓配置：SPY 底仓由 broker 侧持仓充当，不属于策略持仓
        base_cfg = cfg.get("base_position", {}) or {}
        base_enabled = bool(base_cfg.get("enabled", False))
        base_sym = base_cfg.get("symbol", BENCHMARK)

        # 清理状态文件中已不在券商侧的持仓（例如被手工平仓）
        state = {s: v for s, v in state.items() if s in broker_pos}

        # ---- 2a. 持仓数据覆盖校验：缺数据的持仓无法检查止损，必须告警 ----
        uncovered = []
        for sym in broker_pos:
            if base_enabled and sym == base_sym:
                continue
            feat = features.get(sym)
            if feat is None or asof not in feat.index:
                uncovered.append(sym)
        if uncovered:
            msg = (f"⚠️ PABot 持仓数据缺失/陈旧，今日无法检查其止损："
                   f"{', '.join(uncovered)}（请人工核查）")
            print(f"[live] {msg}")
            notify(msg)

        # ---- 2b. 拆股侦测：券商股数与本地记录突变 → 缩放价格状态 ----
        # ★ 必须交叉验证：股数突变也可能来自超时订单晚成交、部分成交、
        #   手工加仓——只有券商侧持仓均价同比例反向变化才认定为拆股；
        #   无法佐证时冻结该标的当日一切决策并告警，绝不盲目缩放止损。
        frozen: set[str] = set()
        for sym, st in state.items():
            bp = broker_pos.get(sym)
            rec_qty = st.get("qty")
            if bp is None or not rec_qty:
                continue
            cur_qty = abs(bp.shares)
            if cur_qty == 0 or rec_qty == 0:
                continue
            ratio = cur_qty / rec_qty
            if ratio >= self.SPLIT_RATIO_THRESHOLD or ratio <= 1 / self.SPLIT_RATIO_THRESHOLD:
                # 交叉验证：拆股时券商的持仓均价会同比例调整
                # （entry_price/ratio ≈ 新 avg_price）；晚成交/手工加仓不会
                price_ok = False
                if st.get("entry_price") and bp.avg_price > 0:
                    expected = st["entry_price"] / ratio
                    price_ok = abs(bp.avg_price / expected - 1) < 0.05
                if price_ok:
                    # 股数放大 ratio 倍 → 价格缩小 ratio 倍（正向拆股），反之亦然
                    for key in ("stop", "highest_close", "entry_price", "init_risk"):
                        if key in st:
                            st[key] = st[key] / ratio
                    st["qty"] = cur_qty
                    msg = (f"PABot 侦测到 {sym} 拆股/并股（股数 {rec_qty}→{cur_qty}），"
                           f"止损等价格状态已按 1/{ratio:.2f} 缩放")
                    print(f"[live] {msg}")
                    notify(msg)
                    actions.append(f"拆股缩放 {sym}（{rec_qty}→{cur_qty}）")
                else:
                    frozen.add(sym)
                    msg = (f"⚠️ PABot {sym} 股数异常（{rec_qty}→{cur_qty}）且价格"
                           f"不符合拆股特征，已冻结该标的今日决策，请人工核查"
                           f"（可能是订单晚成交/部分成交/手工操作）")
                    print(f"[live] {msg}")
                    notify(msg)
            elif cur_qty != rec_qty:
                # 小幅差异（部分成交残留等）：只更新记录，不动价格
                st["qty"] = cur_qty

        # ---- 3. 离场判断（用昨日完整 K 线）----
        # 先提交全部离场单，再统一等成交确认：
        # ★ 确认成交后才删除本地状态；失败保留原止损并告警。
        exit_orders: list[tuple[str, str, OrderEvidence, str, float, str]] = []
        for sym, bp in broker_pos.items():
            if base_enabled and sym == base_sym:
                continue  # 底仓不走个股离场逻辑
            if sym in frozen:
                continue  # 股数异常待人工核查，今日不动
            feat = features.get(sym)
            if feat is None or asof not in feat.index:
                continue  # 已在 2a 告警
            row = feat.loc[asof]
            # 方向：券商侧股数为负即空头（Alpaca 空头持仓 qty < 0）
            direction = -1 if bp.shares < 0 else 1
            qty = abs(bp.shares)
            st = state.get(sym)
            if st is None:
                # 状态缺失（如手工建仓）：按当前价补建止损
                atr_v = float(row["atr"])
                stop0 = float(row["close"]) - direction * self.scfg["atr_stop_mult"] * atr_v
                st = {
                    "stop": stop0,
                    "highest_close": float(row["close"]),
                    "entry_price": bp.avg_price,
                    "entry_date": str(asof.date()),
                    "init_risk": abs(float(row["close"]) - stop0),
                    "adds": 0,
                    "direction": direction,
                    "qty": qty,
                }
                state[sym] = st

            if direction == 1:
                hit_stop = float(row["low"]) <= st["stop"]
                hit_channel = close_exit_signal(row)
                # 时间止损：持有 N 交易日仍未达 min_r 倍初始风险的浮盈。
                # 持有天数按"该股票实际有行情 bar 的交易日"计数，
                # 与回测引擎的 age_days 口径逐位一致
                # （日历工作日 busday 会把交易所假日多算进去，实盘会提前触发）
                ts_days = int(self.scfg.get("time_stop_days", 0))
                if ts_days > 0 and not (hit_stop or hit_channel):
                    entry_ts = pd.Timestamp(st["entry_date"])
                    age = int(((feat.index > entry_ts)
                               & (feat.index <= asof)).sum())
                    min_r = self.scfg.get("time_stop_min_r", 1.0)
                    if (age >= ts_days and float(row["close"]) - st["entry_price"]
                            < min_r * st.get("init_risk", 0.0)):
                        hit_channel = True  # 走同一离场执行路径
            else:
                hit_stop = float(row["high"]) >= st["stop"]
                hit_channel = short_close_exit_signal(row)
            if hit_stop or hit_channel:
                reason = "硬止损" if hit_stop else "收盘破离场通道/时间止损"
                reason_code = "hard_stop" if hit_stop else "channel_exit"
                act = "离场" if direction == 1 else "空头回补"
                print(f"[live] {act} {sym}：{reason}")
                pa_evidence = self._price_evidence(
                    asof,
                    row,
                    level,
                    bench_mom,
                    trigger=reason_code,
                    active_stop=st["stop"],
                    direction=direction,
                )
                side = "sell" if direction == 1 else "buy"
                oid, order_ctx = self._submit_order(
                    key=f"exit:{sym}:{reason_code}",
                    symbol=sym,
                    side=side,
                    quantity=qty,
                    reference_price=float(row["close"]),
                    action="exit_long" if direction == 1 else "cover_short",
                    reason_code=reason_code,
                    evidence=pa_evidence,
                    exposure_effect=ExposureEffect.DECREASE,
                )
                exit_orders.append((
                    sym,
                    oid,
                    order_ctx,
                    f"{act} {sym}（{reason}）",
                    float(row["close"]),
                    side,
                ))
            elif direction == 1:
                # 多头吊灯跟踪止损（只上移）
                old_stop = st["stop"]
                st["highest_close"] = max(st["highest_close"], float(row["close"]))
                st["stop"] = update_trailing_stop(
                    st["stop"], st["highest_close"], float(row["atr"]), self.scfg
                )
                if st["stop"] != old_stop:
                    self.evidence.position(
                        key=f"stop:{sym}:{st['stop']:.8f}",
                        order=None,
                        order_event_id=None,
                        symbol=sym,
                        event_type="trailing_stop_moved",
                        quantity_delta=0,
                        quantity_after=qty,
                        price=float(row["close"]),
                        metadata={"old_stop": old_stop, "new_stop": st["stop"]},
                    )
            else:
                # 空头吊灯跟踪止损（只下移；highest_close 字段承载最低收盘）
                old_stop = st["stop"]
                st["highest_close"] = min(st["highest_close"], float(row["close"]))
                st["stop"] = update_trailing_stop_short(
                    st["stop"], st["highest_close"], float(row["atr"]), self.scfg
                )
                if st["stop"] != old_stop:
                    self.evidence.position(
                        key=f"stop:{sym}:{st['stop']:.8f}",
                        order=None,
                        order_event_id=None,
                        symbol=sym,
                        event_type="trailing_stop_moved",
                        quantity_delta=0,
                        quantity_after=-qty,
                        price=float(row["close"]),
                        metadata={"old_stop": old_stop, "new_stop": st["stop"]},
                    )

        # 等待全部离场单结算：完全成交才删状态；部分成交更新剩余股数；
        # 失败/状态不明保留原止损并告警，且该标的今日不得再被加仓
        failed_exits: set[str] = set()
        halt_opening = False   # 出现"状态不明"订单时停止本次一切新开仓
        for sym, oid, order_ctx, desc, ref_close, side in exit_orders:
            status, fill, terminal_id = self._settle_evidenced_order(oid, order_ctx)
            if status == "filled":
                old_qty = int(state[sym].get("qty", order_ctx.quantity))
                state.pop(sym, None)
                actions.append(desc)
                effective_qty, effective_px = fill if fill else (old_qty, ref_close)
                self.evidence.position(
                    key=f"exit:{sym}:filled",
                    order=order_ctx,
                    order_event_id=terminal_id,
                    symbol=sym,
                    event_type="closed",
                    quantity_delta=-effective_qty if side == "sell" else effective_qty,
                    quantity_after=0,
                    price=effective_px,
                    metadata={"fill_price_inferred": fill is None},
                )
                if fill:
                    fills.append({"symbol": sym, "side": side,
                                  "ref_price": ref_close, "fill_price": fill[1],
                                  "qty": fill[0]})
            elif status == "partial":
                # 部分成交后已撤单：剩余股数继续持有，保留原止损
                remaining = max(0, state[sym].get("qty", 0) - fill[0])
                state[sym]["qty"] = remaining
                failed_exits.add(sym)
                halt_opening = True
                direction = int(state[sym].get("direction", 1))
                self.evidence.position(
                    key=f"exit:{sym}:partial:{fill[0]}",
                    order=order_ctx,
                    order_event_id=terminal_id,
                    symbol=sym,
                    event_type="partially_closed",
                    quantity_delta=-fill[0] if side == "sell" else fill[0],
                    quantity_after=direction * remaining,
                    price=fill[1],
                )
                fills.append({"symbol": sym, "side": side,
                              "ref_price": ref_close, "fill_price": fill[1],
                              "qty": fill[0]})
                notify(f"⚠️ PABot 离场单部分成交：{desc}，已成交 {fill[0]} 股，"
                       f"剩余 {remaining} 股保留原止损，请关注")
            else:
                failed_exits.add(sym)
                if status == "uncertain":
                    halt_opening = True
                msg = (f"⚠️ PABot 离场单未成交（{status}）：{desc}，"
                       f"本地止损状态已保留，请人工核查订单 {oid}")
                print(f"[live] {msg}")
                notify(msg)
        if exit_orders:
            acct = self.broker.get_account()  # 刷新资金
        cash = acct.cash

        # ---- 4. 金字塔加仓（仅多头持仓，浮盈达标）----
        for sym in list(state):
            if halt_opening:
                break  # 存在状态不明的订单，本次不再开任何新仓
            st = state[sym]
            if st.get("direction", 1) != 1:
                continue  # 空头不做金字塔加仓
            if sym in failed_exits or sym in frozen:
                # 离场失败/股数异常的持仓：系统已判定要离场或状态存疑，
                # 绝不能反向加仓
                continue
            feat = features.get(sym)
            if feat is None or asof not in feat.index:
                continue
            row = feat.loc[asof]
            if not pyramid_trigger(float(row["close"]), st["entry_price"],
                                   st.get("init_risk", 0.0), st.get("adds", 0),
                                   self.scfg):
                continue
            price = self.broker.get_last_price(sym) or float(row["close"])
            risk_per_share = price - st["stop"]
            if risk_per_share <= 0:
                continue
            frac = self.scfg.get("pyramid_size_frac", 0.5)
            add_shares = math.floor(
                frac * acct.equity * self.rcfg["risk_per_trade"] / risk_per_share
            )
            # 单仓市值上限约束
            held_value = abs(broker_pos[sym].market_value)
            room = acct.equity * self.rcfg["max_weight"] - held_value
            add_shares = min(add_shares, math.floor(max(0.0, room) / price),
                             math.floor(cash * 0.995 / price))
            if add_shares <= 0:
                continue
            print(f"[live] 金字塔加仓 {sym} x {add_shares}"
                  f"（第 {st.get('adds', 0) + 1} 次）")
            pa_evidence = self._price_evidence(
                asof,
                row,
                level,
                bench_mom,
                trigger="pyramid",
                entry_price=st["entry_price"],
                active_stop=st["stop"],
                adds_done=st.get("adds", 0),
            )
            oid, order_ctx = self._submit_order(
                key=f"pyramid:{sym}:{st.get('adds', 0) + 1}",
                symbol=sym,
                side="buy",
                quantity=add_shares,
                reference_price=price,
                action="pyramid_add",
                reason_code="pyramid_price_confirmation",
                evidence=pa_evidence,
                exposure_effect=ExposureEffect.INCREASE,
            )
            # ★ 结算订单（超时先撤单）：成交才更新状态，状态不明则停止开仓
            status, fill, terminal_id = self._settle_evidenced_order(oid, order_ctx)
            if status == "uncertain":
                halt_opening = True
                notify(f"⚠️ PABot 加仓单状态不明：{sym} x {add_shares}，"
                       f"已停止本次一切新开仓，请人工核查订单 {oid}")
                continue
            if status in _NO_FILL_TERMINAL_STATUSES:
                notify(f"⚠️ PABot 加仓单未成交：{sym} x {add_shares}，状态未变更")
                continue
            fill_qty, fill_px = fill if fill else (add_shares, price)
            fills.append({"symbol": sym, "side": "buy", "ref_price": price,
                          "fill_price": fill_px, "qty": fill_qty})
            old_qty = st.get("qty", abs(broker_pos[sym].shares))
            st["entry_price"] = (
                st["entry_price"] * old_qty + fill_px * fill_qty
            ) / (old_qty + fill_qty)
            st["qty"] = old_qty + fill_qty
            st["adds"] = st.get("adds", 0) + 1
            self.evidence.position(
                key=f"pyramid:{sym}:{st['adds']}:filled:{fill_qty}",
                order=order_ctx,
                order_event_id=terminal_id,
                symbol=sym,
                event_type="pyramid_added",
                quantity_delta=fill_qty,
                quantity_after=old_qty + fill_qty,
                price=fill_px,
                metadata={"fill_price_inferred": fill is None},
            )
            cash -= fill_qty * fill_px
            actions.append(f"金字塔加仓 {sym} x {fill_qty}")

        # ---- 5. 入场判断（多头，开关档位 > 0）----
        held = set(state)
        max_pos = self.rcfg["max_positions"]
        allowed = max_pos if level >= 2 else (math.ceil(max_pos / 2) if level == 1 else 0)
        free_slots = allowed - len(held)
        if free_slots > 0:
            # 底仓开启时：有入场机会先清空底仓腾出资金（步骤 6 会回补剩余）。
            # 卖出成交后必须让 broker_pos 快照失效（pop），
            # 否则步骤 6 会基于过期快照把底仓再"卖"一次（裸卖空事故）
            if base_enabled:
                bp0 = broker_pos.get(base_sym)
                if bp0 is not None and bp0.shares > 0 and level > 0:
                    base_evidence = self._price_evidence(
                        asof,
                        bench.loc[asof],
                        level,
                        bench_mom,
                        trigger="release_base_for_price_action_entry",
                    )
                    oid, order_ctx = self._submit_order(
                        key=f"base-release:{base_sym}",
                        symbol=base_sym,
                        side="sell",
                        quantity=bp0.shares,
                        reference_price=float(bench.loc[asof, "close"]),
                        action="release_base_position",
                        reason_code="fund_price_action_entries",
                        evidence=base_evidence,
                        exposure_effect=ExposureEffect.DECREASE,
                    )
                    status, fill, terminal_id = self._settle_evidenced_order(
                        oid, order_ctx
                    )
                    if status == "filled":
                        broker_pos.pop(base_sym, None)
                        fill_qty, fill_px = fill if fill else (
                            bp0.shares, float(bench.loc[asof, "close"])
                        )
                        self.evidence.position(
                            key=f"base-release:{base_sym}:filled",
                            order=order_ctx,
                            order_event_id=terminal_id,
                            symbol=base_sym,
                            event_type="base_released",
                            quantity_delta=-fill_qty,
                            quantity_after=max(0, bp0.shares - fill_qty),
                            price=fill_px,
                            metadata={"fill_price_inferred": fill is None},
                        )
                    elif status == "uncertain":
                        halt_opening = True
                        notify(
                            f"⚠️ PABot 底仓释放订单状态不明：{base_sym}，"
                            "已停止本次新开仓"
                        )
                    elif status == "partial":
                        halt_opening = True
                        broker_pos = self.broker.get_positions()
                        notify(
                            f"⚠️ PABot 底仓释放订单部分成交：{base_sym}，"
                            "已停止本次新开仓"
                        )
                    acct = self.broker.get_account()
                    cash = acct.cash

            # 候选池：点时模式取"当前的指数成员"，static 模式取固定名单
            if cfg.get("universe", {}).get("mode") == "sp500_pit":
                from ..data.sp500_pit import load_membership, membership_asof

                membership = load_membership(self.paths.cache_dir)
                candidate_pool = sorted(membership_asof(membership, asof))
            else:
                candidate_pool = UNIVERSE
            # 流动性池：只保留滚动成交额排名前 top_adv 的股票（与回测一致）
            top_n = int(cfg.get("universe", {}).get("top_adv", 0) or 0)
            if top_n > 0:
                advs = []
                for sym in candidate_pool:
                    feat = features.get(sym)
                    if feat is None or asof not in feat.index:
                        continue
                    adv = feat.loc[asof, "adv"]
                    if not pd.isna(adv):
                        advs.append((sym, float(adv)))
                advs.sort(key=lambda x: x[1], reverse=True)
                candidate_pool = [s for s, _ in advs[:top_n]]

            candidates = []
            candidate_log_limit = int(
                (cfg.get("review", {}) or {}).get("candidate_log_limit", 100)
            )
            logged_candidates = 0
            for sym in candidate_pool:
                if sym in held:
                    continue
                feat = features.get(sym)
                if feat is None or asof not in feat.index:
                    continue
                row = feat.loc[asof]
                evaluation = evaluate_entry_signal(row, level, self.scfg, bench_mom)
                # 保留 entry_signal 这个兼容接缝，便于既有调用方和测试注入；
                # 正常情况下它就是 evaluate_entry_signal(...).signal。
                sig = entry_signal(row, level, self.scfg, bench_mom)
                pa_evidence = self._price_evidence(
                    asof,
                    row,
                    level,
                    bench_mom,
                    evaluation_reason=(
                        "accepted" if sig is not None else evaluation.reason_code
                    ),
                )
                if logged_candidates < candidate_log_limit:
                    self.evidence.decision(
                        key=f"candidate:{sym}:signal",
                        symbol=sym,
                        action="entry_scan",
                        outcome="accepted" if sig is not None else "rejected",
                        reason_code=(
                            "accepted" if sig is not None else evaluation.reason_code
                        ),
                        evidence=pa_evidence.to_dict(),
                        reference_price=float(row["close"]),
                    )
                    logged_candidates += 1
                if sig is not None:
                    candidates.append((sym, sig, float(row["close"]), pa_evidence))
            candidates.sort(key=lambda x: x[1].rank, reverse=True)

            avoid_days = int(self.scfg.get("earnings_avoid_days", 0))
            # 行业集中度约束：统计现有持仓的行业分布
            max_per_sector = int(self.rcfg.get("max_per_sector", 0))
            sector_map: dict[str, str] = {}
            sector_counts: dict[str, int] = {}
            if max_per_sector > 0:
                from ..data.sectors import load_sector_map

                sector_map = load_sector_map(
                    list(held) + [s for s, _, _, _ in candidates],
                    self.paths.cache_dir,
                )
                for sym in held:
                    sec = sector_map.get(sym)
                    if sec and sec != "Unknown":
                        sector_counts[sec] = sector_counts.get(sec, 0) + 1

            # 同公司多股别去重：已持有任一股别则跳过其余股别
            dedup = bool(self.rcfg.get("dedup_share_classes", False))
            held_groups: set[str] = set()
            if dedup:
                from ..data.aliases import company_group

                held_groups = {company_group(s) for s in held}

            picked = 0
            for sym, sig, ref_price, pa_evidence in candidates:
                if picked >= free_slots:
                    break
                if dedup:
                    from ..data.aliases import company_group

                    if company_group(sym) in held_groups:
                        self.evidence.decision(
                            key=f"candidate:{sym}:portfolio:share_class",
                            symbol=sym,
                            action="entry",
                            outcome="rejected",
                            reason_code="duplicate_share_class",
                            evidence=pa_evidence.to_dict(),
                            reference_price=ref_price,
                        )
                        continue
                if max_per_sector > 0:
                    sec = sector_map.get(sym)
                    if (sec and sec != "Unknown"
                            and sector_counts.get(sec, 0) >= max_per_sector):
                        print(f"[live] 放弃 {sym}：{sec} 行业仓位已满")
                        self.evidence.decision(
                            key=f"candidate:{sym}:portfolio:sector",
                            symbol=sym,
                            action="entry",
                            outcome="rejected",
                            reason_code="sector_limit",
                            evidence=pa_evidence.to_dict(),
                            reference_price=ref_price,
                        )
                        continue
                # 财报回避：入场前刷新该股票的财报日历（只查候选，开销很小）
                if avoid_days > 0:
                    from ..data.earnings import earnings_within, fetch_earnings_dates

                    ed = fetch_earnings_dates(sym, self.paths.cache_dir, force=True)
                    if earnings_within(ed, asof, avoid_days):
                        print(f"[live] 放弃 {sym}：{avoid_days} 天内有财报")
                        self.evidence.decision(
                            key=f"candidate:{sym}:portfolio:earnings",
                            symbol=sym,
                            action="entry",
                            outcome="rejected",
                            reason_code="earnings_filter",
                            evidence=pa_evidence.to_dict(),
                            reference_price=ref_price,
                        )
                        continue
                # 跳空保护：与回测"开盘价低于止损则放弃"一致，
                # 用实时报价复核（演习模式取不到报价时回退昨收价）
                price = self.broker.get_last_price(sym) or ref_price
                if price <= sig.stop:
                    print(f"[live] 放弃 {sym}：现价 {price:.2f} 已低于止损 {sig.stop:.2f}（跳空）")
                    self.evidence.decision(
                        key=f"candidate:{sym}:execution:gap",
                        symbol=sym,
                        action="entry",
                        outcome="rejected",
                        reason_code="gap_below_stop",
                        evidence=pa_evidence.to_dict(),
                        reference_price=price,
                    )
                    continue
                shares = position_size(acct.equity, cash, price, sig.stop, self.rcfg)
                if shares <= 0:
                    self.evidence.decision(
                        key=f"candidate:{sym}:risk:position_size",
                        symbol=sym,
                        action="entry",
                        outcome="rejected",
                        reason_code="zero_position_size",
                        evidence=pa_evidence.to_dict(),
                        reference_price=price,
                    )
                    continue
                if halt_opening:
                    print("[live] 存在状态不明订单，停止后续入场")
                    self.evidence.decision(
                        key=f"candidate:{sym}:execution:halt",
                        symbol=sym,
                        action="entry",
                        outcome="rejected",
                        reason_code="uncertain_prior_order",
                        evidence=pa_evidence.to_dict(),
                        intended_quantity=shares,
                        reference_price=price,
                    )
                    break
                print(f"[live] 入场 {sym}（{sig.trigger}）x {shares}，止损 {sig.stop:.2f}")
                pa_evidence = self._price_evidence(
                    asof,
                    features[sym].loc[asof],
                    level,
                    bench_mom,
                    trigger=sig.trigger,
                    initial_stop=sig.stop,
                    rank=sig.rank,
                )
                oid, order_ctx = self._submit_order(
                    key=f"entry:{sym}:{sig.trigger}",
                    symbol=sym,
                    side="buy",
                    quantity=shares,
                    reference_price=price,
                    action="enter_long",
                    reason_code=f"price_action_{sig.trigger}",
                    evidence=pa_evidence,
                    exposure_effect=ExposureEffect.INCREASE,
                )
                # ★ 结算订单（超时先撤单再复核）：拒单不写状态（杜绝幽灵持仓）；
                #   部分成交按实际股数入账；状态不明立即停止后续入场
                status, fill, terminal_id = self._settle_evidenced_order(oid, order_ctx)
                if status == "uncertain":
                    halt_opening = True
                    notify(f"⚠️ PABot 入场单状态不明：{sym} x {shares}，"
                           f"已停止本次一切新开仓，请人工核查订单 {oid}")
                    continue
                if status in _NO_FILL_TERMINAL_STATUSES:
                    notify(f"⚠️ PABot 入场单未成交：{sym} x {shares}，未写入状态")
                    continue
                fill_qty, fill_px = fill if fill else (shares, price)
                fills.append({"symbol": sym, "side": "buy", "ref_price": price,
                              "fill_price": fill_px, "qty": fill_qty})
                cash -= fill_qty * fill_px
                state[sym] = {
                    "stop": sig.stop,
                    "highest_close": fill_px,
                    "entry_price": fill_px,
                    "entry_date": str(asof.date()),
                    "init_risk": fill_px - sig.stop,
                    "adds": 0,
                    "direction": 1,
                    "qty": fill_qty,
                }
                self.evidence.position(
                    key=f"entry:{sym}:{sig.trigger}:filled:{fill_qty}",
                    order=order_ctx,
                    order_event_id=terminal_id,
                    symbol=sym,
                    event_type="opened",
                    quantity_delta=fill_qty,
                    quantity_after=fill_qty,
                    price=fill_px,
                    metadata={
                        "trigger": sig.trigger,
                        "initial_stop": sig.stop,
                        "fill_price_inferred": fill is None,
                    },
                )
                actions.append(f"入场 {sym} x {fill_qty}（止损 {sig.stop:.2f}）")
                picked += 1
                if dedup:
                    from ..data.aliases import company_group

                    held_groups.add(company_group(sym))
                if max_per_sector > 0:
                    sec = sector_map.get(sym)
                    if sec and sec != "Unknown":
                        sector_counts[sec] = sector_counts.get(sec, 0) + 1
        elif level == 0:
            # ---- 5'. 熊市做空（模块开启时）----
            short_cfg = cfg.get("shorts", {}) or {}
            if not short_cfg.get("enabled", False):
                print("[live] 大盘开关档位 0（熊市），今日不开新仓")
            else:
                n_shorts = sum(1 for v in state.values()
                               if v.get("direction", 1) == -1)
                short_slots = int(short_cfg.get("max_positions", 5)) - n_shorts
                short_cands = []
                if short_slots > 0:
                    if cfg.get("universe", {}).get("mode") == "sp500_pit":
                        from ..data.sp500_pit import load_membership, membership_asof

                        pool = sorted(membership_asof(
                            load_membership(self.paths.cache_dir), asof))
                    else:
                        pool = UNIVERSE
                    for sym in pool:
                        if sym in state:
                            continue
                        feat = features.get(sym)
                        if feat is None or asof not in feat.index:
                            continue
                        sig = short_entry_signal(feat.loc[asof], level, self.scfg)
                        if sig is not None:
                            short_cands.append(
                                (sym, sig, float(feat.loc[asof, "close"])))
                    short_cands.sort(key=lambda x: x[1].rank, reverse=True)
                for sym, sig, ref_price in short_cands[:short_slots]:
                    price = self.broker.get_last_price(sym) or ref_price
                    if price >= sig.stop:
                        print(f"[live] 放弃做空 {sym}：现价已越过止损（跳空）")
                        continue
                    risk_per_share = sig.stop - price
                    if risk_per_share <= 0:
                        continue
                    shares = min(
                        math.floor(acct.equity * self.rcfg["risk_per_trade"]
                                   / risk_per_share),
                        math.floor(acct.equity * self.rcfg["max_weight"] / price),
                    )
                    if shares <= 0:
                        continue
                    if halt_opening:
                        break
                    print(f"[live] 做空 {sym} x {shares}，止损 {sig.stop:.2f}")
                    pa_evidence = self._price_evidence(
                        asof,
                        features[sym].loc[asof],
                        level,
                        bench_mom,
                        trigger="short_breakdown",
                        initial_stop=sig.stop,
                        rank=sig.rank,
                    )
                    oid, order_ctx = self._submit_order(
                        key=f"entry-short:{sym}:breakdown",
                        symbol=sym,
                        side="sell",
                        quantity=shares,
                        reference_price=price,
                        action="enter_short",
                        reason_code="price_action_short_breakdown",
                        evidence=pa_evidence,
                        exposure_effect=ExposureEffect.INCREASE,
                    )
                    status, fill, terminal_id = self._settle_evidenced_order(
                        oid, order_ctx
                    )
                    if status == "uncertain":
                        halt_opening = True
                        notify(f"⚠️ PABot 做空单状态不明：{sym} x {shares}，"
                               f"已停止本次一切新开仓，请人工核查订单 {oid}")
                        continue
                    if status in _NO_FILL_TERMINAL_STATUSES:
                        notify(f"⚠️ PABot 做空单未成交：{sym} x {shares}，未写入状态")
                        continue
                    fill_qty, fill_px = fill if fill else (shares, price)
                    fills.append({"symbol": sym, "side": "sell",
                                  "ref_price": price, "fill_price": fill_px,
                                  "qty": fill_qty})
                    state[sym] = {
                        "stop": sig.stop,
                        "highest_close": fill_px,   # 空头承载最低收盘价
                        "entry_price": fill_px,
                        "entry_date": str(asof.date()),
                        "init_risk": sig.stop - fill_px,
                        "adds": 0,
                        "direction": -1,
                        "qty": fill_qty,
                    }
                    self.evidence.position(
                        key=f"entry-short:{sym}:filled:{fill_qty}",
                        order=order_ctx,
                        order_event_id=terminal_id,
                        symbol=sym,
                        event_type="opened_short",
                        quantity_delta=-fill_qty,
                        quantity_after=-fill_qty,
                        price=fill_px,
                        metadata={
                            "trigger": "short_breakdown",
                            "initial_stop": sig.stop,
                            "fill_price_inferred": fill is None,
                        },
                    )
                    actions.append(f"做空 {sym} x {fill_qty}（止损 {sig.stop:.2f}）")

        # ---- 6. 底仓调度（默认关闭；开启时与回测逻辑一致）----
        if base_enabled:
            base_pos = broker_pos.get(base_sym)
            base_px = self.broker.get_last_price(base_sym) or float(
                bench.loc[asof, "close"]
            )
            if level >= int(base_cfg.get("min_level", 2)):
                # 全额档：剩余现金（扣缓冲）买入底仓
                acct2 = self.broker.get_account()  # 刷新买入后的现金
                buffer = base_cfg.get("cash_buffer", 0.02) * acct2.equity
                invest = acct2.cash - buffer
                if invest > 0.01 * acct2.equity and base_px > 0:
                    n = math.floor(invest / base_px)
                    if n > 0:
                        print(f"[live] 底仓买入 {base_sym} x {n}")
                        base_evidence = self._price_evidence(
                            asof,
                            bench.loc[asof],
                            level,
                            bench_mom,
                            trigger="base_regime_allocation",
                        )
                        oid, order_ctx = self._submit_order(
                            key=f"base-buy:{base_sym}",
                            symbol=base_sym,
                            side="buy",
                            quantity=n,
                            reference_price=base_px,
                            action="enter_base_position",
                            reason_code="price_action_regime_allocation",
                            evidence=base_evidence,
                            exposure_effect=ExposureEffect.INCREASE,
                        )
                        status, fill, terminal_id = self._settle_evidenced_order(
                            oid, order_ctx
                        )
                        if status in {"filled", "partial"}:
                            fill_qty, fill_px = fill if fill else (n, base_px)
                            actions.append(f"底仓买入 {base_sym} x {fill_qty}")
                            fills.append({
                                "symbol": base_sym,
                                "side": "buy",
                                "ref_price": base_px,
                                "fill_price": fill_px,
                                "qty": fill_qty,
                            })
                            self.evidence.position(
                                key=f"base-buy:{base_sym}:{fill_qty}",
                                order=order_ctx,
                                order_event_id=terminal_id,
                                symbol=base_sym,
                                event_type="base_opened",
                                quantity_delta=fill_qty,
                                quantity_after=(base_pos.shares if base_pos else 0)
                                + fill_qty,
                                price=fill_px,
                                metadata={"fill_price_inferred": fill is None},
                            )
                        elif status == "uncertain":
                            notify(f"⚠️ PABot 底仓买单状态不明：{base_sym} x {n}")
            elif base_pos is not None and base_pos.shares > 0:
                print(f"[live] 开关跌下全额档，清空底仓 {base_sym}")
                base_evidence = self._price_evidence(
                    asof,
                    bench.loc[asof],
                    level,
                    bench_mom,
                    trigger="base_regime_exit",
                )
                oid, order_ctx = self._submit_order(
                    key=f"base-exit:{base_sym}",
                    symbol=base_sym,
                    side="sell",
                    quantity=base_pos.shares,
                    reference_price=base_px,
                    action="exit_base_position",
                    reason_code="price_action_regime_closed",
                    evidence=base_evidence,
                    exposure_effect=ExposureEffect.DECREASE,
                )
                status, fill, terminal_id = self._settle_evidenced_order(
                    oid, order_ctx
                )
                if status in {"filled", "partial"}:
                    fill_qty, fill_px = fill if fill else (
                        base_pos.shares, base_px
                    )
                    actions.append(f"清空底仓 {base_sym} x {fill_qty}")
                    fills.append({
                        "symbol": base_sym,
                        "side": "sell",
                        "ref_price": base_px,
                        "fill_price": fill_px,
                        "qty": fill_qty,
                    })
                    self.evidence.position(
                        key=f"base-exit:{base_sym}:{fill_qty}",
                        order=order_ctx,
                        order_event_id=terminal_id,
                        symbol=base_sym,
                        event_type="base_closed" if status == "filled"
                        else "base_partially_closed",
                        quantity_delta=-fill_qty,
                        quantity_after=max(0, base_pos.shares - fill_qty),
                        price=fill_px,
                        metadata={"fill_price_inferred": fill is None},
                    )
                elif status == "uncertain":
                    notify(f"⚠️ PABot 底仓卖单状态不明：{base_sym}")

        # ---- 7. 绩效跟踪落盘：每日权益快照 + 实测成交滑点 ----
        # （main.py report 用这两个文件做"实际 vs 影子回测"对比与滑点校准）
        acct_final = self.broker.get_account()
        logs_dir = self.paths.logs_dir
        logs_dir.mkdir(parents=True, exist_ok=True)
        eq_path = logs_dir / "equity_history.csv"
        write_header = not eq_path.exists()
        with open(eq_path, "a", encoding="utf-8") as f:
            if write_header:
                f.write("date,equity,cash,n_positions,level\n")
            f.write(f"{asof.date()},{acct_final.equity:.2f},"
                    f"{acct_final.cash:.2f},{len(state)},{level}\n")
        if fills:
            fp = logs_dir / "fills.csv"
            write_header = not fp.exists()
            with open(fp, "a", encoding="utf-8") as f:
                if write_header:
                    f.write("date,symbol,side,ref_price,fill_price,qty,slip_bps\n")
                for r in fills:
                    # 滑点方向统一为"对我不利为正"：买入付高价、卖出得低价均为正
                    sign = 1 if r["side"] == "buy" else -1
                    slip = sign * (r["fill_price"] / r["ref_price"] - 1) * 10000
                    f.write(f"{asof.date()},{r['symbol']},{r['side']},"
                            f"{r['ref_price']:.4f},{r['fill_price']:.4f},"
                            f"{r['qty']},{slip:.1f}\n")

        # ---- 8. 原子化落盘状态 + 执行摘要通知 + 心跳 ----
        meta["last_run_date"] = str(asof.date())
        state_sha256 = self._save_state(meta, state)
        # 只有 live_state.json 完成原子替换后，filled/partial 才能追加本地
        # 状态交接事件。两者之间崩溃会由下一次启动门禁识别并冻结。
        self.evidence.state_committed(state_sha256)
        if self.evidence.requires_recovery:
            reasons = ", ".join(self.evidence.recovery_reasons)
            self.evidence.finish(
                "recovery_hold",
                metadata={"reasons": list(self.evidence.recovery_reasons)},
            )
            message = f"本次运行存在未决订单，已冻结后续运行：{reasons}"
            notify(f"⚠️ PABot {message}")
            raise RecoveryHoldError.from_issue("runtime_recovery_hold", message)
        self.evidence.equity(
            equity=acct_final.equity,
            cash=acct_final.cash,
            buying_power=acct_final.buying_power,
            position_count=len(state),
            market_level=level,
        )
        print(f"[live] 完成。当前跟踪持仓 {len(state)} 只，状态已写入 {self.state_path}")

        summary = (
            f"PABot 执行摘要 {now_et.date()}（信号 {asof.date()}）\n"
            f"大盘开关档位: {level}\n"
            f"权益 ${acct.equity:,.0f} | 持仓 {len(state)} 只\n"
            + ("动作:\n- " + "\n- ".join(actions) if actions else "动作: 无")
        )
        notify(summary)
        heartbeat()   # 死人开关：外部服务侦测"连续缺 ping = 系统停摆"
        self.evidence.finish(
            "completed",
            metadata={"actions": len(actions), "fills": len(fills)},
        )
