"""基本面信号 API 端点（Phase F.4）.

端点：
  GET /api/fundamental/pead?ticker=AAPL       — PEAD 信号（EPS surprise + 历史漂移）
  GET /api/fundamental/piotroski?ticker=AAPL  — Piotroski F-Score（9 维财务评分）
  GET /api/fundamental/summary?ticker=AAPL    — PEAD + Piotroski 合并摘要

数据来源：yfinance（免费）
免责声明：本 API 输出仅供参考，不构成投资建议。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from quantbot.stocks.fundamental.engine import (
    PEADSignal,
    PiotroskiScore,
    compute_pead_signal,
    compute_piotroski_fscore,
)

router = APIRouter(prefix="/fundamental", tags=["fundamental"])


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------


def _pead_to_dict(sig: PEADSignal) -> dict[str, Any]:
    s = sig.latest_surprise
    return {
        "ticker": sig.ticker,
        "surprise_magnitude": sig.surprise_magnitude,
        "signal_strength": sig.signal_strength,
        "historical_drift_7d": sig.historical_drift_7d,
        "historical_drift_30d": sig.historical_drift_30d,
        "historical_drift_60d": sig.historical_drift_60d,
        "latest_surprise": {
            "ticker": s.ticker,
            "quarter": s.quarter.isoformat(),
            "eps_actual": s.eps_actual,
            "eps_estimate": s.eps_estimate,
            "eps_difference": s.eps_difference,
            "surprise_pct": s.surprise_pct,
        },
    }


def _piotroski_to_dict(ps: PiotroskiScore) -> dict[str, Any]:
    return {
        "ticker": ps.ticker,
        "score": ps.score,
        "grade": ps.grade,
        "signals": ps.signals,
        "as_of_date": ps.as_of_date.isoformat(),
        "interpretation": ps.interpretation,
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


@router.get("/pead")
async def pead_endpoint(
    ticker: str = Query(..., description="证券代码，如 AAPL"),
) -> dict[str, Any]:
    """返回最新 EPS surprise + 历史 PEAD 漂移估算.

    若无数据（非美股 / yfinance 无 EPS 历史）返回 404。
    """
    try:
        sig = compute_pead_signal(ticker)
        if sig is None:
            raise HTTPException(
                status_code=404,
                detail=f"无法获取 {ticker.upper()} 的 EPS 数据，请确认为美股 ticker。",
            )
        return _pead_to_dict(sig)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"fundamental/pead 错误: {ticker}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/piotroski")
async def piotroski_endpoint(
    ticker: str = Query(..., description="证券代码，如 AAPL"),
) -> dict[str, Any]:
    """返回 Piotroski F-Score（0-9）及 9 个子信号明细.

    若财务报表数据不足返回 404。
    """
    try:
        ps = compute_piotroski_fscore(ticker)
        if ps is None:
            raise HTTPException(
                status_code=404,
                detail=f"无法获取 {ticker.upper()} 的财务数据，请确认为美股 ticker。",
            )
        return _piotroski_to_dict(ps)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"fundamental/piotroski 错误: {ticker}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/summary")
async def summary_endpoint(
    ticker: str = Query(..., description="证券代码，如 AAPL"),
) -> dict[str, Any]:
    """PEAD + Piotroski 合并摘要.

    两个引擎并行调用，任一失败时该字段返回 null（不影响另一个）。
    """
    import asyncio  # noqa: PLC0415

    ticker_upper = ticker.upper()
    pead_result: dict[str, Any] | None = None
    piotroski_result: dict[str, Any] | None = None

    try:
        loop = asyncio.get_event_loop()
        pead_sig, piotroski_ps = await asyncio.gather(
            loop.run_in_executor(None, compute_pead_signal, ticker_upper),
            loop.run_in_executor(None, compute_piotroski_fscore, ticker_upper),
            return_exceptions=True,
        )

        if isinstance(pead_sig, PEADSignal):
            pead_result = _pead_to_dict(pead_sig)
        if isinstance(piotroski_ps, PiotroskiScore):
            piotroski_result = _piotroski_to_dict(piotroski_ps)

    except Exception as exc:  # noqa: BLE001
        logger.exception(f"fundamental/summary 错误: {ticker}")
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if pead_result is None and piotroski_result is None:
        raise HTTPException(
            status_code=404,
            detail=f"无法获取 {ticker_upper} 的基本面数据。",
        )

    return {
        "ticker": ticker_upper,
        "pead": pead_result,
        "piotroski": piotroski_result,
    }
