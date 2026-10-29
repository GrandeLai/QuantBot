"""TLH（税务亏损收割）API 端点（Phase F.3）.

端点：
  POST /api/tlh/scan          — 扫描 lots，返回 TLH 候选列表
  GET  /api/tlh/replacement   — 查询替代 ETF
  POST /api/tlh/estimate-saving — 估算节税金额

免责声明：本 API 输出仅供参考，不构成税务建议，请咨询 CPA 确认。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from loguru import logger
from pydantic import BaseModel, Field

from quantbot.stocks.tlh.engine import (
    TaxLot,
    TLHCandidate,
    estimate_tax_saving,
    get_replacement_tickers,
    scan_tlh_candidates,
)

router = APIRouter(prefix="/tlh", tags=["tlh"])


# ---------------------------------------------------------------------------
# 请求 / 响应模型
# ---------------------------------------------------------------------------


class TaxLotInput(BaseModel):
    ticker: str
    quantity: float = Field(gt=0)
    cost_basis: float = Field(gt=0)           # 每股成本（$）
    acquisition_date: date
    lot_id: str = ""


class ScanRequest(BaseModel):
    lots: list[TaxLotInput]
    current_prices: dict[str, float]          # {ticker: 当前价格}
    recent_purchases: dict[str, date] = {}    # {ticker: 最近买入日期}
    min_loss_pct: float = -0.05               # 最小跌幅阈值（负值）
    min_loss_usd: float = 500.0               # 最小亏损金额（$）


class CandidateResponse(BaseModel):
    ticker: str
    lot_id: str
    quantity: float
    cost_basis: float
    acquisition_date: date
    current_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    holding_days: int
    is_long_term: bool
    replacement_tickers: list[str]
    wash_sale_risk: bool


class ScanResponse(BaseModel):
    candidates: list[CandidateResponse]
    estimated_tax_saving: float               # 按默认税率估算
    generated_at: str


class ReplacementResponse(BaseModel):
    ticker: str
    replacements: list[str]


class EstimateSavingRequest(BaseModel):
    candidates: list[CandidateResponse]
    short_term_rate: float = Field(default=0.37, ge=0.0, le=1.0)
    long_term_rate: float = Field(default=0.20, ge=0.0, le=1.0)


class EstimateSavingResponse(BaseModel):
    tax_saving_usd: float
    details: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.post("/scan", response_model=ScanResponse)
async def scan_tlh(body: ScanRequest) -> ScanResponse:
    """扫描 tax lots，返回满足 TLH 条件的候选仓位.

    Body：
    - lots: 持仓 lot 列表
    - current_prices: 各 ticker 当前价格
    - recent_purchases: 近期买入记录（用于 wash sale 检查）
    - min_loss_pct: 最小跌幅（默认 -5%）
    - min_loss_usd: 最小亏损金额（默认 $500）

    响应包含候选列表 + 预估节税金额（按默认税率）。
    """
    try:
        lots = [
            TaxLot(
                ticker=inp.ticker,
                quantity=inp.quantity,
                cost_basis=inp.cost_basis,
                acquisition_date=inp.acquisition_date,
                lot_id=inp.lot_id,
            )
            for inp in body.lots
        ]
        candidates: list[TLHCandidate] = scan_tlh_candidates(
            lots,
            body.current_prices,
            body.recent_purchases,
            min_loss_pct=body.min_loss_pct,
            min_loss_usd=body.min_loss_usd,
        )
        saving = estimate_tax_saving(candidates)
        candidate_responses = [_to_response(c) for c in candidates]
        return ScanResponse(
            candidates=candidate_responses,
            estimated_tax_saving=saving,
            generated_at=datetime.now(tz=timezone.utc).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("tlh/scan 错误")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/replacement", response_model=ReplacementResponse)
async def get_replacement(
    ticker: str = Query(..., description="证券代码，如 SPY"),
) -> ReplacementResponse:
    """查询给定 ticker 的替代 ETF 列表.

    返回 _REPLACEMENT_MAP 中对应的替代品列表；未知 ticker 返回空列表。
    """
    replacements = get_replacement_tickers(ticker)
    return ReplacementResponse(ticker=ticker.upper(), replacements=replacements)


@router.post("/estimate-saving", response_model=EstimateSavingResponse)
async def estimate_saving_endpoint(body: EstimateSavingRequest) -> EstimateSavingResponse:
    """根据候选列表和用户税率，精确估算可节约的税额.

    Body：
    - candidates: 候选仓位列表（来自 /tlh/scan 的响应）
    - short_term_rate: 短期资本利得税率（默认 0.37）
    - long_term_rate: 长期资本利得税率（默认 0.20）
    """
    try:
        # 将 CandidateResponse 还原为 TLHCandidate（只需要部分字段）
        tlh_candidates: list[TLHCandidate] = [
            _from_response(cr) for cr in body.candidates
        ]
        total_saving = estimate_tax_saving(
            tlh_candidates,
            short_term_rate=body.short_term_rate,
            long_term_rate=body.long_term_rate,
        )
        details = [
            {
                "ticker": c.lot.ticker,
                "lot_id": c.lot.lot_id,
                "unrealized_pnl": c.unrealized_pnl,
                "is_long_term": c.is_long_term,
                "tax_rate": body.long_term_rate if c.is_long_term else body.short_term_rate,
                "saving_usd": round(
                    abs(c.unrealized_pnl)
                    * (body.long_term_rate if c.is_long_term else body.short_term_rate),
                    2,
                ),
            }
            for c in tlh_candidates
        ]
        return EstimateSavingResponse(tax_saving_usd=total_saving, details=details)
    except Exception as exc:  # noqa: BLE001
        logger.exception("tlh/estimate-saving 错误")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _to_response(c: TLHCandidate) -> CandidateResponse:
    return CandidateResponse(
        ticker=c.lot.ticker,
        lot_id=c.lot.lot_id,
        quantity=c.lot.quantity,
        cost_basis=c.lot.cost_basis,
        acquisition_date=c.lot.acquisition_date,
        current_price=c.current_price,
        unrealized_pnl=c.unrealized_pnl,
        unrealized_pnl_pct=c.unrealized_pnl_pct,
        holding_days=c.holding_days,
        is_long_term=c.is_long_term,
        replacement_tickers=c.replacement_tickers,
        wash_sale_risk=c.wash_sale_risk,
    )


def _from_response(cr: CandidateResponse) -> TLHCandidate:
    """将 API 响应体还原为 TLHCandidate（用于 estimate-saving 端点）."""
    lot = TaxLot(
        ticker=cr.ticker,
        quantity=cr.quantity,
        cost_basis=cr.cost_basis,
        acquisition_date=cr.acquisition_date,
        lot_id=cr.lot_id,
    )
    return TLHCandidate(
        lot=lot,
        current_price=cr.current_price,
        unrealized_pnl=cr.unrealized_pnl,
        unrealized_pnl_pct=cr.unrealized_pnl_pct,
        holding_days=cr.holding_days,
        is_long_term=cr.is_long_term,
        replacement_tickers=cr.replacement_tickers,
        wash_sale_risk=cr.wash_sale_risk,
    )
