"""TWAP/VWAP/TCA 执行 API 端点（Phase F.3）.

端点：
  POST /api/execution/twap        — 生成 TWAP 子单计划
  POST /api/execution/vwap        — 生成 VWAP 子单计划
  POST /api/execution/tca         — 计算 TCA（滑点分析）
  GET  /api/execution/adv-check   — 判断是否需要分单
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from quantbot.stocks.execution.engine import (
    adv_check,
    compute_tca,
    create_twap_slices,
    create_vwap_slices,
)

router = APIRouter(prefix="/execution", tags=["execution"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class ChildOrderOut(BaseModel):
    order_id: str
    parent_order_id: str
    ticker: str
    quantity: float
    scheduled_time: datetime
    algo: str
    slice_index: int
    total_slices: int


class ExecutionReportOut(BaseModel):
    parent_order_id: str
    ticker: str
    total_quantity: float
    algo: str
    child_orders: list[ChildOrderOut]
    estimated_avg_price: float | None
    created_at: datetime


class TWAPRequest(BaseModel):
    ticker: str
    total_quantity: float = Field(gt=0)
    start_time: datetime
    end_time: datetime
    num_slices: int | None = None
    time_interval_minutes: int = Field(default=15, ge=1)


class VWAPRequest(BaseModel):
    ticker: str
    total_quantity: float = Field(gt=0)
    start_time: datetime
    end_time: datetime
    volume_profile: list[float] | None = None
    num_slices: int = Field(default=10, ge=1)


class TCARequest(BaseModel):
    ticker: str = ""
    parent_order_id: str = ""
    algo: str = ""
    arrival_price: float = Field(gt=0)
    executed_avg_price: float = Field(gt=0)
    vwap_price: float | None = None
    close_price: float | None = None
    total_quantity: float = Field(default=1.0, gt=0)
    execution_date: date | None = None


class TCAOut(BaseModel):
    parent_order_id: str
    ticker: str
    algo: str
    arrival_price: float
    executed_avg_price: float
    vwap_price: float | None
    close_price: float | None
    slippage_bps: float
    total_quantity: float
    execution_date: date


class ADVCheckOut(BaseModel):
    needs_slicing: bool
    threshold_pct: float
    quantity: float
    adv: float
    pct_of_adv: float
    recommended_slices: int


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post("/twap", response_model=ExecutionReportOut)
async def twap_endpoint(body: TWAPRequest) -> ExecutionReportOut:
    """生成 TWAP（时间加权平均价）子单计划.

    将 total_quantity 等量切分到 start_time→end_time 时间段，
    每片间隔 time_interval_minutes 分钟（或按 num_slices 切分）。
    """
    try:
        report = create_twap_slices(
            body.ticker,
            body.total_quantity,
            body.start_time,
            body.end_time,
            num_slices=body.num_slices,
            time_interval_minutes=body.time_interval_minutes,
        )
        return _report_to_out(report)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("execution/twap 错误")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/vwap", response_model=ExecutionReportOut)
async def vwap_endpoint(body: VWAPRequest) -> ExecutionReportOut:
    """生成 VWAP（成交量加权平均价）子单计划.

    按 volume_profile 权重切分数量；未提供时退化为均匀切分。
    """
    try:
        report = create_vwap_slices(
            body.ticker,
            body.total_quantity,
            body.start_time,
            body.end_time,
            volume_profile=body.volume_profile,
            num_slices=body.num_slices,
        )
        return _report_to_out(report)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("execution/vwap 错误")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/tca", response_model=TCAOut)
async def tca_endpoint(body: TCARequest) -> TCAOut:
    """计算 TCA（交易成本分析）—— 测量实际成交相对 arrival price 的滑点（bps）."""
    try:
        rec = compute_tca(
            arrival_price=body.arrival_price,
            executed_avg_price=body.executed_avg_price,
            vwap_price=body.vwap_price,
            close_price=body.close_price,
            total_quantity=body.total_quantity,
            ticker=body.ticker,
            parent_order_id=body.parent_order_id,
            algo=body.algo,
            execution_date=body.execution_date,
        )
        return TCAOut(
            parent_order_id=rec.parent_order_id,
            ticker=rec.ticker,
            algo=rec.algo,
            arrival_price=rec.arrival_price,
            executed_avg_price=rec.executed_avg_price,
            vwap_price=rec.vwap_price,
            close_price=rec.close_price,
            slippage_bps=rec.slippage_bps,
            total_quantity=rec.total_quantity,
            execution_date=rec.execution_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("execution/tca 错误")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/adv-check", response_model=ADVCheckOut)
async def adv_check_endpoint(
    ticker: str = Query(..., description="证券代码"),
    quantity: float = Query(..., gt=0, description="计划下单数量（股）"),
    adv: float = Query(..., gt=0, description="平均每日成交量（股）"),
    threshold_pct: float = Query(default=0.005, ge=0.0, le=1.0, description="阈值（默认 0.5%）"),
) -> ADVCheckOut:
    """判断订单量是否超过 ADV 阈值，并推荐切分数量.

    若 quantity/adv > threshold_pct，建议分单；
    推荐切分数量 = ceil(quantity / (adv × threshold_pct))。
    """
    needs = adv_check(quantity, adv, threshold_pct=threshold_pct)
    pct = quantity / adv
    recommended = math.ceil(quantity / (adv * threshold_pct)) if needs else 1
    return ADVCheckOut(
        needs_slicing=needs,
        threshold_pct=threshold_pct,
        quantity=quantity,
        adv=adv,
        pct_of_adv=round(pct, 6),
        recommended_slices=recommended,
    )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _report_to_out(report: Any) -> ExecutionReportOut:
    return ExecutionReportOut(
        parent_order_id=report.parent_order_id,
        ticker=report.ticker,
        total_quantity=report.total_quantity,
        algo=report.algo,
        child_orders=[
            ChildOrderOut(
                order_id=o.order_id,
                parent_order_id=o.parent_order_id,
                ticker=o.ticker,
                quantity=o.quantity,
                scheduled_time=o.scheduled_time,
                algo=o.algo,
                slice_index=o.slice_index,
                total_slices=o.total_slices,
            )
            for o in report.child_orders
        ],
        estimated_avg_price=report.estimated_avg_price,
        created_at=report.created_at,
    )
