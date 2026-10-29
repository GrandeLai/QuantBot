"""TWAP/VWAP 分单引擎 + TCA（交易成本分析）（Phase F.3）.

功能：
- TWAP（Time-Weighted Average Price）等量均分子单
- VWAP（Volume-Weighted Average Price）按 volume profile 权重切分
- TCA：实际成交 vs arrival/VWAP/close 的滑点（bps）计算
- ADV check：判断订单量是否需要分单（超过 ADV 0.5% 阈值）

典型使用场景：
- 单笔订单 > ADV 0.5%（约 100 万股/日的 0.5% = 5000 股）时建议分单
- TWAP 适合对市场冲击敏感、时间充裕的情况
- VWAP 适合希望接近当日 VWAP 的机构操作
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class ChildOrder:
    """子单（child order）— 来自 TWAP/VWAP 分单的单个切片."""

    order_id: str
    parent_order_id: str
    ticker: str
    quantity: float
    scheduled_time: datetime
    algo: Literal["TWAP", "VWAP"]
    slice_index: int       # 从 0 开始
    total_slices: int


@dataclass
class ExecutionReport:
    """执行计划报告 — 包含所有子单."""

    parent_order_id: str
    ticker: str
    total_quantity: float
    algo: Literal["TWAP", "VWAP"]
    child_orders: list[ChildOrder] = field(default_factory=list)
    estimated_avg_price: float | None = None   # 如提供，用于 TCA 到达价格对比
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


@dataclass
class TCARecord:
    """TCA（交易成本分析）记录 — 记录单笔订单的执行质量."""

    parent_order_id: str
    ticker: str
    algo: str
    arrival_price: float         # 下单时刻现价（到达价格）
    executed_avg_price: float    # 实际成交均价
    vwap_price: float | None     # 区间 VWAP（可选，由用户提供或查询）
    close_price: float | None    # 收盘价（可选）
    slippage_bps: float          # (executed - arrival) / arrival * 10000（正值表示不利滑点）
    total_quantity: float
    execution_date: date


# ---------------------------------------------------------------------------
# 核心函数
# ---------------------------------------------------------------------------


def create_twap_slices(
    ticker: str,
    total_quantity: float,
    start_time: datetime,
    end_time: datetime,
    *,
    num_slices: int | None = None,
    time_interval_minutes: int = 15,
    parent_order_id: str | None = None,
) -> ExecutionReport:
    """生成 TWAP（等量均分）子单列表.

    TWAP 将总数量等量切分到 N 个时间片，每片在 start_time 到 end_time 之间均匀分布。

    Args:
        ticker: 证券代码
        total_quantity: 总下单数量（股）
        start_time: 分单开始时间
        end_time: 分单结束时间
        num_slices: 切片数量（None → 由 time_interval_minutes 推导）
        time_interval_minutes: 每片间隔（分钟），num_slices 指定时忽略
        parent_order_id: 父单 ID（None → 自动生成 UUID）

    Returns:
        ExecutionReport（含 ChildOrder 列表）

    Raises:
        ValueError: end_time <= start_time 或 total_quantity <= 0
    """
    if end_time <= start_time:
        raise ValueError("end_time 必须在 start_time 之后")
    if total_quantity <= 0:
        raise ValueError("total_quantity 必须 > 0")

    pid = parent_order_id or str(uuid.uuid4())
    duration_minutes = (end_time - start_time).total_seconds() / 60.0

    if num_slices is None:
        num_slices = max(1, int(duration_minutes / time_interval_minutes))

    per_slice_qty = total_quantity / num_slices
    interval = (end_time - start_time) / num_slices

    child_orders: list[ChildOrder] = []
    for i in range(num_slices):
        scheduled = start_time + interval * i
        child_orders.append(
            ChildOrder(
                order_id=str(uuid.uuid4()),
                parent_order_id=pid,
                ticker=ticker.upper(),
                quantity=round(per_slice_qty, 6),
                scheduled_time=scheduled,
                algo="TWAP",
                slice_index=i,
                total_slices=num_slices,
            )
        )

    return ExecutionReport(
        parent_order_id=pid,
        ticker=ticker.upper(),
        total_quantity=total_quantity,
        algo="TWAP",
        child_orders=child_orders,
    )


def create_vwap_slices(
    ticker: str,
    total_quantity: float,
    start_time: datetime,
    end_time: datetime,
    *,
    volume_profile: list[float] | None = None,
    num_slices: int = 10,
    parent_order_id: str | None = None,
) -> ExecutionReport:
    """生成 VWAP（按 volume profile 加权）子单列表.

    VWAP 按成交量分布切分数量：量多的时段分得更多，量少的时段分得更少。
    若未提供 volume_profile，退化为等量均分（与 TWAP 等价）。

    Args:
        ticker: 证券代码
        total_quantity: 总下单数量（股）
        start_time: 分单开始时间
        end_time: 分单结束时间
        volume_profile: 每时段的相对成交量权重（长度 = num_slices，会归一化）
                        None → 均匀分布（与 TWAP 等价）
        num_slices: 切片总数
        parent_order_id: 父单 ID（None → 自动生成 UUID）

    Returns:
        ExecutionReport（含 ChildOrder 列表，数量按权重分配）

    Raises:
        ValueError: end_time <= start_time、total_quantity <= 0、
                    volume_profile 长度不匹配或含负值
    """
    if end_time <= start_time:
        raise ValueError("end_time 必须在 start_time 之后")
    if total_quantity <= 0:
        raise ValueError("total_quantity 必须 > 0")
    if num_slices <= 0:
        raise ValueError("num_slices 必须 > 0")

    pid = parent_order_id or str(uuid.uuid4())

    # 计算权重
    if volume_profile is None:
        weights = [1.0] * num_slices
    else:
        if len(volume_profile) != num_slices:
            raise ValueError(
                f"volume_profile 长度（{len(volume_profile)}）须等于 num_slices（{num_slices}）"
            )
        if any(w < 0 for w in volume_profile):
            raise ValueError("volume_profile 不可含负值")
        weights = list(volume_profile)

    weight_sum = sum(weights)
    if weight_sum <= 0:
        raise ValueError("volume_profile 权重之和必须 > 0")

    normalized = [w / weight_sum for w in weights]
    interval = (end_time - start_time) / num_slices

    child_orders: list[ChildOrder] = []
    for i in range(num_slices):
        scheduled = start_time + interval * i
        qty = total_quantity * normalized[i]
        child_orders.append(
            ChildOrder(
                order_id=str(uuid.uuid4()),
                parent_order_id=pid,
                ticker=ticker.upper(),
                quantity=round(qty, 6),
                scheduled_time=scheduled,
                algo="VWAP",
                slice_index=i,
                total_slices=num_slices,
            )
        )

    return ExecutionReport(
        parent_order_id=pid,
        ticker=ticker.upper(),
        total_quantity=total_quantity,
        algo="VWAP",
        child_orders=child_orders,
    )


def compute_tca(
    arrival_price: float,
    executed_avg_price: float,
    *,
    vwap_price: float | None = None,
    close_price: float | None = None,
    total_quantity: float = 1.0,
    ticker: str = "",
    parent_order_id: str = "",
    algo: str = "",
    execution_date: date | None = None,
) -> TCARecord:
    """计算交易成本分析（TCA）— 测量实际成交质量.

    slippage_bps：正值 = 买单成交价高于到达价（不利）；负值 = 有利成交。

    公式：slippage_bps = (executed_avg_price - arrival_price) / arrival_price × 10000

    Args:
        arrival_price: 下单时刻的市场价格（到达价格，arrival price）
        executed_avg_price: 实际成交均价（来自 broker 成交回报）
        vwap_price: 同区间 VWAP（可选，用于对比）
        close_price: 当日收盘价（可选）
        total_quantity: 成交数量（股）
        ticker: 证券代码
        parent_order_id: 父单 ID
        algo: 执行算法名称（TWAP/VWAP/Market 等）
        execution_date: 成交日期（None → 今天）

    Returns:
        TCARecord

    Raises:
        ValueError: arrival_price <= 0
    """
    if arrival_price <= 0:
        raise ValueError("arrival_price 必须 > 0")

    slippage_bps = (executed_avg_price - arrival_price) / arrival_price * 10000.0

    return TCARecord(
        parent_order_id=parent_order_id,
        ticker=ticker.upper() if ticker else "",
        algo=algo,
        arrival_price=arrival_price,
        executed_avg_price=executed_avg_price,
        vwap_price=vwap_price,
        close_price=close_price,
        slippage_bps=round(slippage_bps, 4),
        total_quantity=total_quantity,
        execution_date=execution_date or date.today(),
    )


def adv_check(
    total_quantity: float,
    avg_daily_volume: float,
    *,
    threshold_pct: float = 0.005,   # ADV 0.5%
) -> bool:
    """判断订单量是否超过 ADV 阈值，需要分单.

    Args:
        total_quantity: 计划下单数量（股）
        avg_daily_volume: 该标的平均每日成交量（股/日）
        threshold_pct: 阈值百分比（默认 0.5%）

    Returns:
        True → 建议分单；False → 可以直接市价单

    Raises:
        ValueError: avg_daily_volume <= 0
    """
    if avg_daily_volume <= 0:
        raise ValueError("avg_daily_volume 必须 > 0")
    return total_quantity / avg_daily_volume > threshold_pct
