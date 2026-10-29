"""GEX API — Dealer Gamma Exposure 端点.

Endpoints (double-mounted via include_with_api_alias):
  GET /options/gex/snapshot?ticker=SPY&max_dte=45&min_oi=10
  GET /options/gex/levels?ticker=SPY&max_dte=45&min_oi=10

phaseF.options-gex-api
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from quantbot.stocks.options.chain_provider import fetch_chain_yfinance
from quantbot.stocks.options.gex_engine import GEXSnapshot, compute_gex_snapshot

router = APIRouter(prefix="/options/gex", tags=["options-gex"])

_DEFAULT_TICKERS = {"SPY", "QQQ", "IWM", "SPX", "NDX", "TSLA", "AAPL", "NVDA"}


async def _build_snapshot(
    ticker: str,
    max_dte: int,
    min_oi: int,
    r: float,
) -> GEXSnapshot:
    """Fetch chain + compute GEX snapshot (raises HTTPException on failure)."""
    import yfinance as yf

    # 1. Get current spot price
    try:
        info = yf.Ticker(ticker).fast_info
        spot = float(info.last_price)
        if spot <= 0:
            raise ValueError(f"invalid spot {spot}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"options chain unavailable: {exc}") from exc

    # 2. Fetch options chain
    try:
        chain = fetch_chain_yfinance(ticker, max_dte=max_dte, min_oi=min_oi)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"options chain unavailable: {exc}") from exc

    if not chain:
        raise HTTPException(status_code=503, detail="options chain unavailable: empty chain")

    # 3. Compute GEX
    try:
        snapshot = compute_gex_snapshot(ticker, spot, chain, r=r)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"gex computation error: {exc}") from exc

    return snapshot


def _snapshot_to_dict(snap: GEXSnapshot) -> dict[str, Any]:
    """Serialize GEXSnapshot → JSON-safe dict."""
    return {
        "ticker": snap.ticker,
        "spot": snap.spot,
        "snapshot_time": snap.snapshot_time,
        "net_gex_total": round(snap.net_gex_total, 2),
        "gamma_flip_level": snap.gamma_flip_level,
        "major_magnet": snap.major_magnet,
        "high_vol_trigger": snap.high_vol_trigger,
        "gex_by_strike": [
            {
                "strike": g.strike,
                "call_oi": g.call_oi,
                "put_oi": g.put_oi,
                "call_gex": round(g.call_gex, 2),
                "put_gex": round(g.put_gex, 2),
                "net_gex": round(g.net_gex, 2),
                "gamma": round(g.gamma, 8),
                "dte": round(g.dte, 1),
            }
            for g in snap.gex_by_strike
        ],
    }


@router.get("/snapshot")
async def gex_snapshot(
    ticker: str = Query(default="SPY", description="标的代码，如 SPY / QQQ / IWM"),
    max_dte: int = Query(default=45, ge=1, le=180, description="最大到期天数"),
    min_oi: int = Query(default=10, ge=0, description="最小持仓量过滤"),
    r: float = Query(default=0.05, ge=0.0, le=0.20, description="无风险利率"),
) -> dict[str, Any]:
    """完整 GEX 快照 — 含各行权价 GEX 分布."""
    snap = await _build_snapshot(ticker.upper(), max_dte, min_oi, r)
    return _snapshot_to_dict(snap)


@router.get("/levels")
async def gex_levels(
    ticker: str = Query(default="SPY"),
    max_dte: int = Query(default=45, ge=1, le=180),
    min_oi: int = Query(default=10, ge=0),
    r: float = Query(default=0.05, ge=0.0, le=0.20),
) -> dict[str, Any]:
    """仅返回关键水位（不含 gex_by_strike 列表，轻量级）."""
    snap = await _build_snapshot(ticker.upper(), max_dte, min_oi, r)
    return {
        "ticker": snap.ticker,
        "spot": snap.spot,
        "snapshot_time": snap.snapshot_time,
        "net_gex_total": round(snap.net_gex_total, 2),
        "gamma_flip_level": snap.gamma_flip_level,
        "major_magnet": snap.major_magnet,
        "high_vol_trigger": snap.high_vol_trigger,
    }
