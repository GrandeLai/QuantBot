"""Crypto-only advisor API for QuantBot.

This module exposes the investment assistant contract without any stock factor
dependencies. Signals are derived from Binance/OKX funding history and current
derivatives snapshots.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Query

from quantbot.crypto.crypto_derivs.analytics import funding_extreme_signal
from quantbot.crypto.crypto_derivs.collector import (
    fetch_aggregated_derivs,
    fetch_binance_funding_history,
    fetch_okx_funding_history,
)

router = APIRouter(prefix="/advisor", tags=["advisor"])


def _extract_asset(symbol: str) -> str:
    """Return the base asset from an OKX spot symbol."""
    return symbol.split("-")[0].upper()


def _now_iso() -> str:
    """Return an RFC 3339 timestamp."""
    return datetime.now(tz=UTC).isoformat()


async def _fetch_both_histories(asset: str) -> tuple[list[Any], list[Any]]:
    """Fetch Binance and OKX funding histories concurrently."""
    import asyncio

    results = await asyncio.gather(
        fetch_binance_funding_history(asset, limit=90),
        fetch_okx_funding_history(asset, limit=90),
        return_exceptions=True,
    )
    binance = results[0] if not isinstance(results[0], BaseException) else []
    okx = results[1] if not isinstance(results[1], BaseException) else []
    return binance, okx  # type: ignore[return-value]


async def _get_funding_signal(asset: str) -> dict[str, Any]:
    """Fetch funding history and compute the current extreme signal."""
    try:
        binance_hist, okx_hist = await _fetch_both_histories(asset)
    except Exception:
        return {"signal": "neutral", "z_score": 0.0, "percentile": 50.0, "funding_rate": 0.0}

    history = [f.funding_rate for f in binance_hist] + [f.funding_rate for f in okx_hist]

    try:
        agg = await fetch_aggregated_derivs(asset)
        b_fr = agg["funding"].get("binance")
        o_fr = agg["funding"].get("okx")
        current = float(b_fr.funding_rate if b_fr else (o_fr.funding_rate if o_fr else 0.0))
    except Exception:
        current = history[-1] if history else 0.0

    if len(history) < 30:
        return {"signal": "neutral", "z_score": 0.0, "percentile": 50.0, "funding_rate": current}

    try:
        sig = funding_extreme_signal(current, history)
    except Exception:
        sig = {"signal": "neutral", "z_score": 0.0, "percentile": 50.0}
    return {**sig, "funding_rate": current}


@router.get("/overview")
def advisor_overview() -> dict[str, Any]:
    """Return a crypto assistant overview payload."""
    return {
        "net_worth": 0.0,
        "cash_ratio": 1.0,
        "positions": [],
        "generated_at": _now_iso(),
    }


@router.get("/crypto/opportunities")
async def crypto_opportunities(symbol: str = Query(default="BTC-USDT")) -> dict[str, Any]:
    """Return crypto opportunity cards based on funding extremes."""
    sig = await _get_funding_signal(_extract_asset(symbol))
    signal = str(sig.get("signal", "neutral"))
    z = float(sig.get("z_score", 0.0))
    pct = float(sig.get("percentile", 50.0))
    funding = float(sig.get("funding_rate", 0.0))
    funding_pct = funding * 100.0

    items: list[dict[str, Any]] = []
    if signal == "contrarian_long":
        confidence = round(min(0.5 + abs(z) * 0.15, 0.92), 3)
        items.append(
            {
                "type": "long_opportunity",
                "subject": symbol,
                "recommendation": (
                    f"资金费率极度偏低（{funding_pct:.4f}%/8h，历史 {pct:.0f}% 百分位），"
                    "空头仓位过度拥挤，反向做多机会窗口开启。"
                ),
                "confidence": confidence,
                "evidence": [
                    {
                        "source": "Binance/OKX 资金费率",
                        "summary": f"funding={funding_pct:.4f}%/8h，z-score={z:.2f}，历史 {pct:.0f}% 分位",
                        "observed_at": _now_iso(),
                    }
                ],
                "risk_notes": ["反向信号不保证立即触发，需配合价格确认", "建议小仓试探，止损设 2%"],
                "freshness": _now_iso(),
            }
        )
    elif signal == "neutral" and pct < 30:
        confidence = round(0.40 + (30.0 - pct) * 0.008, 3)
        items.append(
            {
                "type": "basis_opportunity",
                "subject": symbol,
                "recommendation": (
                    f"资金费率偏低（{funding_pct:.4f}%/8h，{pct:.0f}% 历史分位），"
                    "Cash-and-Carry 套利性价比提升：做多现货 + 对冲永续。"
                ),
                "confidence": confidence,
                "evidence": [
                    {
                        "source": "跨所资金费率",
                        "summary": f"funding={funding_pct:.4f}%/8h，历史 {pct:.0f}% 分位",
                        "observed_at": _now_iso(),
                    }
                ],
                "risk_notes": ["Basis 可能进一步收窄", "套利需双边流动性支撑"],
                "freshness": _now_iso(),
            }
        )
    return {"items": items}


@router.get("/crypto/risks")
async def crypto_risks(symbol: str = Query(default="BTC-USDT")) -> dict[str, Any]:
    """Return crypto risk cards based on funding extremes."""
    sig = await _get_funding_signal(_extract_asset(symbol))
    signal = str(sig.get("signal", "neutral"))
    z = float(sig.get("z_score", 0.0))
    pct = float(sig.get("percentile", 50.0))
    funding = float(sig.get("funding_rate", 0.0))
    funding_pct = funding * 100.0

    items: list[dict[str, Any]] = []
    if signal == "contrarian_short":
        confidence = round(min(0.5 + abs(z) * 0.15, 0.92), 3)
        funding_annual_pct = funding * 1095 * 100.0
        items.append(
            {
                "type": "crowded_long_risk",
                "subject": symbol,
                "recommendation": (
                    f"资金费率极高（{funding_pct:.4f}%/8h，历史 {pct:.0f}% 百分位），"
                    "多头仓位过度拥挤，强平风险上升，建议减仓或对冲。"
                ),
                "confidence": confidence,
                "evidence": [
                    {
                        "source": "Binance/OKX 资金费率",
                        "summary": f"funding={funding_pct:.4f}%/8h，z-score={z:.2f}，历史 {pct:.0f}% 分位",
                        "observed_at": _now_iso(),
                    }
                ],
                "risk_notes": [f"持仓年化资金成本约 {funding_annual_pct:.1f}%", "历史极值分位通常伴随回撤/震荡风险"],
                "freshness": _now_iso(),
            }
        )
    elif signal == "neutral" and pct > 70:
        confidence = round(0.35 + (pct - 70.0) * 0.008, 3)
        items.append(
            {
                "type": "elevated_funding_risk",
                "subject": symbol,
                "recommendation": (
                    f"资金费率偏高（{funding_pct:.4f}%/8h，{pct:.0f}% 历史分位），"
                    "多头持续付费，若价格停滞则持仓成本累积。"
                ),
                "confidence": confidence,
                "evidence": [
                    {
                        "source": "跨所资金费率监控",
                        "summary": f"funding={funding_pct:.4f}%/8h，处于 {pct:.0f}% 历史分位",
                        "observed_at": _now_iso(),
                    }
                ],
                "risk_notes": ["未达强平触发线，但累积成本需关注"],
                "freshness": _now_iso(),
            }
        )
    return {"items": items}
