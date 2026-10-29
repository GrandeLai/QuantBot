"""Crypto research API — 市场机制检测 + 策略参数推荐.

Endpoints:
  GET  /crypto/research/latest          最新研究摘要（市场机制 + 推荐策略）
  POST /crypto/research/optimize        参数优化（MVP: 基于机制的固定参数）
  GET  /crypto/research/optimize/latest 最新优化结果（同 optimize）
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.crypto.crypto_derivs.analytics import funding_extreme_signal
from quantbot.crypto.crypto_derivs.collector import (
    fetch_aggregated_derivs,
    fetch_binance_funding_history,
    fetch_okx_funding_history,
)

router = APIRouter(prefix="/crypto/research", tags=["crypto-research"])

# ---------- regime detection --------------------------------------------------

_REGIME_STRATEGIES: dict[str, list[str]] = {
    "overheated_bull": ["mean_reversion", "funding_arbitrage"],
    "bull_trending": ["vwap_ema_trend", "momentum_breakout"],
    "ranging": ["vwap_ema_trend", "grid_trading", "mean_reversion"],
    "bear_ranging": ["mean_reversion", "cash_preservation"],
    "bear_trending": ["funding_arbitrage", "counter_trend_long"],
}

_REGIME_TIMEFRAMES: dict[str, list[str]] = {
    "overheated_bull": ["1h", "4h"],
    "bull_trending": ["15m", "1h", "4h"],
    "ranging": ["15m", "1h"],
    "bear_ranging": ["1h", "4h"],
    "bear_trending": ["4h", "1d"],
}

_REGIME_PARAMS: dict[str, dict[str, int | float]] = {
    "overheated_bull": {
        "fast_period": 5,
        "slow_period": 20,
        "vwap_window": 10,
        "trailing_stop_pct": 0.02,
        "max_hold_bars": 24,
    },
    "bull_trending": {
        "fast_period": 8,
        "slow_period": 21,
        "vwap_window": 20,
        "trailing_stop_pct": 0.025,
        "max_hold_bars": 48,
    },
    "ranging": {
        "fast_period": 5,
        "slow_period": 20,
        "vwap_window": 10,
        "trailing_stop_pct": 0.02,
        "max_hold_bars": 24,
    },
    "bear_ranging": {
        "fast_period": 5,
        "slow_period": 20,
        "vwap_window": 10,
        "trailing_stop_pct": 0.015,
        "max_hold_bars": 16,
    },
    "bear_trending": {
        "fast_period": 8,
        "slow_period": 30,
        "vwap_window": 20,
        "trailing_stop_pct": 0.03,
        "max_hold_bars": 48,
    },
}


async def _detect_regime(asset: str) -> tuple[str, float, float]:
    """Returns (market_regime, current_funding_rate, percentile)."""
    try:
        results = await asyncio.gather(
            fetch_binance_funding_history(asset, limit=90),
            fetch_okx_funding_history(asset, limit=90),
            fetch_aggregated_derivs(asset),
            return_exceptions=True,
        )
        binance_hist = results[0] if not isinstance(results[0], BaseException) else []
        okx_hist = results[1] if not isinstance(results[1], BaseException) else []
        agg = results[2] if not isinstance(results[2], BaseException) else {}
    except Exception:
        return "ranging", 0.0001, 50.0

    history = [f.funding_rate for f in binance_hist] + [f.funding_rate for f in okx_hist]

    if isinstance(agg, dict) and agg.get("funding"):
        b_fr = agg["funding"].get("binance")
        o_fr = agg["funding"].get("okx")
        current = float(b_fr.funding_rate if b_fr else (o_fr.funding_rate if o_fr else 0.0))
    else:
        current = history[-1] if history else 0.0001

    if len(history) < 30:
        return "ranging", current, 50.0

    try:
        sig = funding_extreme_signal(current, history)
    except Exception:
        return "ranging", current, 50.0

    pct = float(sig.get("percentile", 50.0))
    z = float(sig.get("z_score", 0.0))

    if pct >= 90 or z >= 2.0:
        regime = "overheated_bull"
    elif pct >= 65:
        regime = "bull_trending"
    elif pct <= 10 or z <= -2.0:
        regime = "bear_trending"
    elif pct <= 35:
        regime = "bear_ranging"
    else:
        regime = "ranging"

    return regime, current, pct


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


# ---------- endpoints --------------------------------------------------------


@router.get("/latest")
async def research_latest(
    symbol: str = Query(default="BTC-USDT"),
    base_timeframe: str = Query(default="15m"),
) -> dict[str, Any]:
    """最新市场研究摘要."""
    asset = symbol.split("-")[0].upper()
    regime, current_funding, pct = await _detect_regime(asset)
    return {
        "symbol": symbol,
        "base_timeframe": base_timeframe,
        "market_regime": regime,
        "recommended_strategy_ids": _REGIME_STRATEGIES.get(regime, ["vwap_ema_trend"]),
        "recommended_timeframes": _REGIME_TIMEFRAMES.get(regime, ["15m", "1h"]),
        "parameter_search_ready": True,
        "generated_at": _now_iso(),
        "funding_percentile": round(pct, 1),
    }


class OptimizeRequest(BaseModel):
    symbol: str = "BTC-USDT"
    base_timeframe: str = "15m"
    higher_timeframes: list[str] = ["1h", "4h", "1d"]
    limit: int = 180
    param_grid: dict[str, list[int | float]] = {}
    strategy_id: str = "vwap_ema_trend"


@router.post("/optimize")
async def research_optimize(req: OptimizeRequest) -> dict[str, Any]:
    """参数优化（MVP：返回基于当前市场机制的推荐参数）."""
    asset = req.symbol.split("-")[0].upper()
    regime, _, _ = await _detect_regime(asset)
    best_params = dict(_REGIME_PARAMS.get(regime, _REGIME_PARAMS["ranging"]))
    return {
        "symbol": req.symbol,
        "strategy_id": req.strategy_id,
        "best_params": best_params,
        "regime": regime,
        "method": "regime_based_defaults",
        "generated_at": _now_iso(),
    }


@router.get("/optimize/latest")
async def research_optimize_latest(
    symbol: str = Query(default="BTC-USDT"),
    base_timeframe: str = Query(default="15m"),
    strategy_id: str = Query(default="vwap_ema_trend"),
) -> dict[str, Any]:
    """最新优化结果（同 optimize，走机制推荐参数）."""
    asset = symbol.split("-")[0].upper()
    regime, _, _ = await _detect_regime(asset)
    best_params = dict(_REGIME_PARAMS.get(regime, _REGIME_PARAMS["ranging"]))
    return {
        "symbol": symbol,
        "strategy_id": strategy_id,
        "best_params": best_params,
        "regime": regime,
        "method": "regime_based_defaults",
        "generated_at": _now_iso(),
    }
