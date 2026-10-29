"""影子对账（Shadow Reconciliation）。

对账必须是 fail-closed 的安全检查：只有券商快照和最新完整行情都可用时，
才允许给出 ``MATCH`` 或 ``DRIFT``。券商不可达、缓存陈旧或回测失败均返回
``UNKNOWN``，绝不拿本地状态文件冒充券商事实。

``cmd_reconcile`` 返回可 JSON 序列化的 :class:`ReconciliationResult`；上层可
把 ``result.to_dict()`` 写入交易证据账本。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from ..backtest.engine import BacktestEngine
from ..broker.base import Broker
from ..data.downloader import load_market_data
from .notify import notify

MATCH = "MATCH"
DRIFT = "DRIFT"
UNKNOWN = "UNKNOWN"
DEFAULT_MAX_CACHE_AGE_DAYS = 4


@dataclass(frozen=True)
class PositionMismatch:
    """同一标的在理论与券商快照中的签名股数差异。"""

    symbol: str
    theory_shares: int
    actual_shares: int


@dataclass(frozen=True)
class ActualPositionsSnapshot:
    """券商持仓读取结果；未知状态下 ``positions`` 必须为 ``None``。"""

    status: str
    source: str
    positions: dict[str, int] | None
    error: str | None = None


@dataclass
class ReconciliationResult:
    """一次可持久化的结构化对账结果。

    股数统一使用带方向的整数：多头为正、空头为负。``UNKNOWN`` 与空仓
    严格区分：未知时 ``actual_positions`` 为 ``None``，合法空仓则为 ``{}``。
    """

    status: str
    asof: str | None
    source: str
    theory_positions: dict[str, int] = field(default_factory=dict)
    actual_positions: dict[str, int] | None = None
    matched_symbols: list[str] = field(default_factory=list)
    only_theory: list[str] = field(default_factory=list)
    only_actual: list[str] = field(default_factory=list)
    direction_mismatches: list[PositionMismatch] = field(default_factory=list)
    quantity_mismatches: list[PositionMismatch] = field(default_factory=list)
    error: str | None = None
    generated_at: str = field(
        default_factory=lambda: pd.Timestamp.now(tz="UTC").isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        """转成只含 JSON 基础类型的字典，供上层证据账本持久化。"""
        return asdict(self)


def _get_actual_positions(
    cfg: dict,
    broker: Broker | None = None,
) -> ActualPositionsSnapshot:
    """读取券商持仓；任何异常都显式返回 ``UNKNOWN``。

    ``broker`` 仅用于依赖注入和复用已有连接。生产默认仍按配置连接 Alpaca。
    本函数有意不读取 ``live_state.json``：本地策略状态不是券商事实来源。
    """
    source = "券商实际持仓"
    try:
        if broker is None:
            from ..broker.alpaca_broker import AlpacaBroker

            broker = AlpacaBroker(paper=cfg["broker"].get("paper", True))
        positions = broker.get_positions()
        return ActualPositionsSnapshot(
            status=MATCH,
            source=source,
            positions={symbol: int(position.shares) for symbol, position in positions.items()},
        )
    except Exception as exc:  # noqa: BLE001 - 对账边界必须把依赖失败收敛为 UNKNOWN
        return ActualPositionsSnapshot(
            status=UNKNOWN,
            source=source,
            positions=None,
            error=f"无法读取券商持仓：{exc}",
        )


def _signed_theory_positions(theory: Mapping[str, Any]) -> dict[str, int]:
    """把回测仓位转换成与券商一致的带方向股数。"""
    signed: dict[str, int] = {}
    for symbol, position in theory.items():
        if isinstance(position, Mapping):
            direction = int(position["direction"])
            shares = int(position["shares"])
            qty = direction * abs(shares)
        else:
            qty = int(position)
        if qty:
            signed[str(symbol)] = qty
    return signed


def compare_positions(
    theory: Mapping[str, Any],
    actual: Mapping[str, int],
    *,
    asof: str | None,
    source: str = "券商实际持仓",
) -> ReconciliationResult:
    """逐标的比较存在性、方向和准确股数，返回结构化结果。"""
    theory_signed = _signed_theory_positions(theory)
    actual_signed = {
        str(symbol): int(shares)
        for symbol, shares in actual.items()
        if int(shares) != 0
    }
    theory_symbols = set(theory_signed)
    actual_symbols = set(actual_signed)
    only_theory = sorted(theory_symbols - actual_symbols)
    only_actual = sorted(actual_symbols - theory_symbols)

    matched: list[str] = []
    direction_mismatches: list[PositionMismatch] = []
    quantity_mismatches: list[PositionMismatch] = []
    for symbol in sorted(theory_symbols & actual_symbols):
        expected = theory_signed[symbol]
        observed = actual_signed[symbol]
        mismatch = PositionMismatch(symbol, expected, observed)
        if (expected < 0) != (observed < 0):
            direction_mismatches.append(mismatch)
        elif expected != observed:
            quantity_mismatches.append(mismatch)
        else:
            matched.append(symbol)

    has_drift = bool(
        only_theory or only_actual or direction_mismatches or quantity_mismatches
    )
    return ReconciliationResult(
        status=DRIFT if has_drift else MATCH,
        asof=asof,
        source=source,
        theory_positions=theory_signed,
        actual_positions=actual_signed,
        matched_symbols=matched,
        only_theory=only_theory,
        only_actual=only_actual,
        direction_mismatches=direction_mismatches,
        quantity_mismatches=quantity_mismatches,
    )


def _as_eastern(now: pd.Timestamp | None = None) -> pd.Timestamp:
    """把注入时间统一为美东时区，便于稳定测试与交易日判断。"""
    if now is None:
        return pd.Timestamp.now(tz="America/New_York")
    value = pd.Timestamp(now)
    if value.tzinfo is None:
        return value.tz_localize("America/New_York")
    return value.tz_convert("America/New_York")


def validate_cache_freshness(
    data: Mapping[str, pd.DataFrame],
    cfg: Mapping[str, Any],
    *,
    now: pd.Timestamp | None = None,
) -> str:
    """校验基准行情是最新且完整的，返回最后完整 bar 日期。

    默认最多容忍 4 个日历日，覆盖普通周末和单日休市。阈值可通过
    ``live.reconcile_max_cache_age_days`` 收紧，但不应调大来掩盖数据故障。
    """
    benchmark = str((cfg.get("regime", {}) or {}).get("symbol", "SPY"))
    frame = data.get(benchmark)
    if frame is None or frame.empty:
        raise RuntimeError(f"实盘缓存缺少基准行情 {benchmark}")

    asof = pd.Timestamp(frame.index[-1])
    now_et = _as_eastern(now)
    asof_date = asof.date()
    age_days = (now_et.date() - asof_date).days
    max_age = int(
        (cfg.get("live", {}) or {}).get(
            "reconcile_max_cache_age_days", DEFAULT_MAX_CACHE_AGE_DAYS
        )
    )
    if max_age < 0:
        raise ValueError("reconcile_max_cache_age_days 不能小于 0")
    if age_days < 0:
        raise RuntimeError(f"基准行情日期 {asof_date} 晚于当前美东日期")
    if asof_date == now_et.date() and now_et.hour < 16:
        raise RuntimeError(f"基准行情 {asof_date} 可能仍是盘中未完成 K 线")
    if age_days > max_age:
        raise RuntimeError(
            f"实盘缓存陈旧：{benchmark} 最后行情日 {asof_date}，"
            f"距今 {age_days} 天（上限 {max_age} 天）"
        )
    return str(asof_date)


def _unknown_result(
    error: str,
    *,
    asof: str | None = None,
    source: str = "对账依赖",
    theory: Mapping[str, Any] | None = None,
) -> ReconciliationResult:
    return ReconciliationResult(
        status=UNKNOWN,
        asof=asof,
        source=source,
        theory_positions=_signed_theory_positions(theory or {}),
        actual_positions=None,
        error=error,
    )


def render_reconciliation_report(result: ReconciliationResult) -> str:
    """把结构化结果渲染成人工可读报告。"""
    lines = [
        f"====== 影子对账报告（数据截至 {result.asof or '未知'}） ======",
        f"状态: {result.status}",
    ]
    if result.status == UNKNOWN:
        lines.append(f"⚠️ 无法确认持仓是否一致：{result.error or '未知错误'}")
        lines.append("已按 fail-closed 处理，不使用本地状态替代券商快照。")
        return "\n".join(lines)

    actual = result.actual_positions or {}
    lines.extend(
        [
            f"理论持仓（影子回测）: {len(result.theory_positions)} 只",
            f"实际持仓（{result.source}）: {len(actual)} 只",
            f"标的、方向、股数均一致: {len(result.matched_symbols)} 只",
        ]
    )
    if result.only_theory:
        lines.append(
            "⚠️ 仅理论持有（实盘缺失）: " + ", ".join(result.only_theory)
        )
    if result.only_actual:
        lines.append(
            "⚠️ 仅实际持有（理论不认）: " + ", ".join(result.only_actual)
        )
    for mismatch in result.direction_mismatches:
        lines.append(
            f"⚠️ {mismatch.symbol} 方向不一致：理论 {mismatch.theory_shares:+d}，"
            f"实际 {mismatch.actual_shares:+d}"
        )
    for mismatch in result.quantity_mismatches:
        lines.append(
            f"⚠️ {mismatch.symbol} 股数不一致：理论 {mismatch.theory_shares:+d}，"
            f"实际 {mismatch.actual_shares:+d}"
        )
    if result.status == MATCH:
        lines.append("✅ 完全一致，无漂移")
    lines.append("（注：上线初期可能存在路径依赖，但数量差异仍会被如实记录）")
    return "\n".join(lines)


def _publish_result(result: ReconciliationResult) -> None:
    report = render_reconciliation_report(result)
    print(report)
    if result.status == MATCH:
        return
    try:
        notify(report)
    except Exception as exc:  # noqa: BLE001 - 通知失败不能吞掉结构化对账结果
        print(f"[reconcile] 推送对账结果失败：{exc}")


def cmd_reconcile(
    cfg: dict,
    *,
    broker: Broker | None = None,
    now: pd.Timestamp | None = None,
    cache_dir: str | Path | None = None,
) -> ReconciliationResult:
    """执行一次 fail-closed 影子对账并返回结构化结果。"""
    print("[reconcile] 运行影子回测（回测起点 → 最新完整数据日）...")
    shadow_cfg = json.loads(json.dumps(cfg))  # 深拷贝，避免污染原配置
    now_et = _as_eastern(now)
    shadow_cfg["backtest"]["end"] = now_et.strftime("%Y-%m-%d")
    shadow_cfg["data"]["snapshot"] = None
    shadow_cfg["data"]["cache_dir"] = str(
        cache_dir or cfg.get("live", {}).get("cache_dir", "data_cache_live")
    )

    try:
        # 每次对账都重新获取截至最近已完成交易日的数据。只读旧缓存无法
        # 发现“每日 runner 漏跑”，会把运维故障误报成持仓一致。
        refresh_end = now_et.normalize()
        if now_et.hour >= 16:
            refresh_end += pd.Timedelta(days=1)
        data = load_market_data(
            shadow_cfg,
            force=True,
            end=refresh_end.strftime("%Y-%m-%d"),
            cache_dir_override=shadow_cfg["data"]["cache_dir"],
            allow_empty_markers=False,
        )
        cache_asof = validate_cache_freshness(data, shadow_cfg, now=now_et)
    except Exception as exc:  # noqa: BLE001 - 数据未知时禁止继续给出一致结论
        result = _unknown_result(str(exc), source="实盘行情缓存")
        _publish_result(result)
        return result

    try:
        backtest_result = BacktestEngine(data, shadow_cfg).run()
        if backtest_result.equity.empty:
            raise RuntimeError("影子回测没有生成权益序列")
        theory = backtest_result.open_positions
        asof = str(backtest_result.equity.index[-1].date())
        if asof != cache_asof:
            raise RuntimeError(
                f"影子回测截止日 {asof} 与缓存截止日 {cache_asof} 不一致"
            )
    except Exception as exc:  # noqa: BLE001 - 理论侧失败同样只能给 UNKNOWN
        result = _unknown_result(str(exc), asof=cache_asof, source="影子回测")
        _publish_result(result)
        return result

    actual_snapshot = _get_actual_positions(shadow_cfg, broker=broker)
    if actual_snapshot.status == UNKNOWN or actual_snapshot.positions is None:
        result = _unknown_result(
            actual_snapshot.error or "券商持仓未知",
            asof=asof,
            source=actual_snapshot.source,
            theory=theory,
        )
        _publish_result(result)
        return result

    actual = dict(actual_snapshot.positions)
    base_cfg = shadow_cfg.get("base_position", {}) or {}
    if bool(base_cfg.get("enabled", False)):
        actual.pop(str(base_cfg.get("symbol", "SPY")), None)

    result = compare_positions(
        theory,
        actual,
        asof=asof,
        source=actual_snapshot.source,
    )
    _publish_result(result)
    return result
