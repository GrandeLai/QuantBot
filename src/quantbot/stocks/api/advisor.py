"""Advisor API — 给 assistant frontend 提供投资建议数据.

Endpoints:
  GET /advisor/overview               组合总览 (AdvisorOverviewPayload)
  GET /advisor/opportunities          美股机会卡片列表 (symbols 参数)
  GET /advisor/risks                  美股风险卡片列表 (symbols 参数)
  GET /advisor/crypto/opportunities   加密机会卡片列表 (symbol 参数)
  GET /advisor/crypto/risks           加密风险卡片列表 (symbol 参数)
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

_DEFAULT_STOCK_SYMBOLS = ["AAPL", "MSFT", "NVDA"]


# ---------- helpers ----------------------------------------------------------


def _extract_asset(symbol: str) -> str:
    """BTC-USDT → BTC."""
    return symbol.split("-")[0].upper()


async def _get_funding_signal(asset: str) -> dict[str, Any]:
    """Fetch funding history from both exchanges and compute extreme signal."""
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

    if len(history) >= 30:
        try:
            sig = funding_extreme_signal(current, history)
        except Exception:
            sig = {"signal": "neutral", "z_score": 0.0, "percentile": 50.0}
    else:
        sig = {"signal": "neutral", "z_score": 0.0, "percentile": 50.0}

    return {**sig, "funding_rate": current}


async def _fetch_both_histories(asset: str) -> tuple[list[Any], list[Any]]:
    """Fetch binance + okx funding history concurrently; return (binance, okx)."""
    import asyncio
    results = await asyncio.gather(
        fetch_binance_funding_history(asset, limit=90),
        fetch_okx_funding_history(asset, limit=90),
        return_exceptions=True,
    )
    binance = results[0] if not isinstance(results[0], BaseException) else []
    okx = results[1] if not isinstance(results[1], BaseException) else []
    return binance, okx  # type: ignore[return-value]


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _split_symbols(symbols: str | None) -> list[str]:
    """Parse a comma-separated symbol list for stock advisor cards."""
    if not symbols:
        return list(_DEFAULT_STOCK_SYMBOLS)
    parsed = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    return parsed[:12] or list(_DEFAULT_STOCK_SYMBOLS)


def _get_stock_evidence(symbol: str) -> dict[str, Any]:
    """Collect money-making stock evidence used by advisor cards."""
    from quantbot.stocks.analyst_consensus.engine import compute_analyst_consensus
    from quantbot.stocks.eps_revision.engine import compute_eps_revision_momentum
    from quantbot.stocks.relative_strength.engine import compute_rs
    from quantbot.stocks.technical_score.engine import compute_technical_score

    technical = compute_technical_score(symbol)
    relative_strength = compute_rs(symbol)
    eps_revision = compute_eps_revision_momentum(symbol)
    analyst = compute_analyst_consensus(symbol)

    return {
        "technical": {
            "signal": technical.signal,
            "score": technical.composite_score,
            "available": technical.data_available,
            "summary": technical.interpretation,
        },
        "relative_strength": {
            "signal": relative_strength.signal,
            "score": relative_strength.rs_score,
            "available": relative_strength.data_available,
            "summary": relative_strength.interpretation,
        },
        "eps_revision": {
            "signal": eps_revision.overall_direction if eps_revision else "neutral",
            "available": eps_revision is not None,
            "summary": (
                f"EPS 修正方向：{eps_revision.overall_direction}"
                if eps_revision
                else "EPS 修正数据不可用"
            ),
        },
        "analyst": {
            "signal": analyst.grade,
            "upside_pct": analyst.upside_pct,
            "available": analyst.data_available,
            "summary": analyst.interpretation,
        },
    }


def _available_evidence(evidence: dict[str, Any]) -> list[dict[str, str]]:
    """Convert evidence dict into advisor card evidence rows."""
    rows: list[dict[str, str]] = []
    labels = {
        "technical": "综合技术评分",
        "relative_strength": "相对强弱",
        "eps_revision": "EPS 预期修正",
        "analyst": "分析师共识",
    }
    for key, label in labels.items():
        item = evidence.get(key, {})
        if item.get("available"):
            rows.append(
                {
                    "source": label,
                    "summary": str(item.get("summary") or item.get("signal") or ""),
                    "observed_at": _now_iso(),
                }
            )
    return rows


def _stock_opportunity_card(symbol: str, evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Build a stock opportunity card from multi-factor evidence."""
    score = 0
    reasons: list[str] = []

    tech = evidence["technical"]
    if tech["signal"] in {"strong_buy", "buy"}:
        score += 1
        reasons.append("综合技术评分偏多")

    rs = evidence["relative_strength"]
    if rs["signal"] in {"strong_outperformer", "outperformer"}:
        score += 1
        reasons.append("相对 SPY 表现占优")

    eps = evidence["eps_revision"]
    if eps["signal"] in {"strong_upgrade", "upgrade"}:
        score += 1
        reasons.append("EPS 预期上修")

    analyst = evidence["analyst"]
    if analyst["signal"] in {"strong_buy", "buy"}:
        score += 1
        reasons.append("分析师共识偏买入")
    if analyst.get("upside_pct") is not None and float(analyst["upside_pct"]) >= 10.0:
        score += 1
        reasons.append("目标价仍有两位数上行空间")

    if score < 2:
        return None

    confidence = round(min(0.42 + score * 0.1, 0.9), 3)
    return {
        "type": "stock_long_opportunity",
        "subject": symbol,
        "recommendation": f"{symbol} 出现多证据做多窗口：" + "，".join(reasons[:3]) + "。",
        "confidence": confidence,
        "evidence": _available_evidence(evidence),
        "risk_notes": [
            "必须结合仓位上限和止损执行，不应单因子满仓",
            "若财报/宏观事件临近，等待事件后再加仓",
        ],
        "freshness": _now_iso(),
    }


def _stock_risk_card(symbol: str, evidence: dict[str, Any]) -> dict[str, Any] | None:
    """Build a stock risk card from multi-factor evidence."""
    score = 0
    reasons: list[str] = []

    tech = evidence["technical"]
    if tech["signal"] in {"strong_sell", "sell"}:
        score += 1
        reasons.append("综合技术评分转空")

    rs = evidence["relative_strength"]
    if rs["signal"] in {"strong_underperformer", "underperformer"}:
        score += 1
        reasons.append("相对 SPY 明显跑输")

    eps = evidence["eps_revision"]
    if eps["signal"] in {"strong_downgrade", "downgrade"}:
        score += 1
        reasons.append("EPS 预期下修")

    analyst = evidence["analyst"]
    if analyst["signal"] in {"strong_sell", "sell"}:
        score += 1
        reasons.append("分析师共识偏卖出")
    if analyst.get("upside_pct") is not None and float(analyst["upside_pct"]) <= -5.0:
        score += 1
        reasons.append("目标价隐含下行空间")

    if score < 2:
        return None

    confidence = round(min(0.40 + score * 0.1, 0.88), 3)
    return {
        "type": "stock_risk",
        "subject": symbol,
        "recommendation": f"{symbol} 出现多证据风险信号：" + "，".join(reasons[:3]) + "。",
        "confidence": confidence,
        "evidence": _available_evidence(evidence),
        "risk_notes": [
            "优先检查止损、仓位和相关持仓暴露",
            "若仍有持仓，避免在风险信号未解除前继续加仓",
        ],
        "freshness": _now_iso(),
    }


# ---------- overview ---------------------------------------------------------


@router.get("/overview")
def advisor_overview() -> dict[str, Any]:
    """组合总览 — 对接 assistant frontend 资产总览 tab."""
    try:
        from quantbot.stocks.api.portfolio import _manager  # type: ignore[attr-defined]

        current_prices: dict[str, float] = {
            slot.session.symbol: slot.session.positions.get(
                slot.session.symbol,
                type("_P", (), {"avg_price": 0.0})(),
            ).avg_price
            for slot in _manager.strategies.values()
        }
        summary = _manager.summary(current_prices)
        net_worth = float(summary.get("total_portfolio_value", 0.0))
        total_cash = float(summary.get("total_cash", 0.0))
        cash_ratio = total_cash / net_worth if net_worth > 0 else 1.0
        positions = [
            {
                "name": s.get("name", ""),
                "symbol": s.get("symbol", ""),
                "allocation": s.get("allocation", 0.0),
                "pnl": s.get("pnl", 0.0),
                "portfolio_value": s.get("portfolio_value", 0.0),
            }
            for s in summary.get("strategies", [])
        ]
    except Exception:
        net_worth = 0.0
        cash_ratio = 1.0
        positions = []

    return {
        "net_worth": net_worth,
        "cash_ratio": cash_ratio,
        "positions": positions,
        "generated_at": _now_iso(),
    }


# ---------- opportunities ----------------------------------------------------


@router.get("/opportunities")
def stock_opportunities(
    symbols: str | None = Query(default=None, description="Comma-separated US stock symbols"),
) -> dict[str, Any]:
    """美股机会卡片 — 基于综合技术、相对强弱、EPS 修正和分析师共识."""
    items: list[dict[str, Any]] = []
    for symbol in _split_symbols(symbols):
        try:
            evidence = _get_stock_evidence(symbol)
            card = _stock_opportunity_card(symbol, evidence)
        except Exception:
            card = None
        if card is not None:
            items.append(card)
    return {"items": items}


@router.get("/risks")
def stock_risks(
    symbols: str | None = Query(default=None, description="Comma-separated US stock symbols"),
) -> dict[str, Any]:
    """美股风险卡片 — 基于综合技术、相对强弱、EPS 修正和分析师共识."""
    items: list[dict[str, Any]] = []
    for symbol in _split_symbols(symbols):
        try:
            evidence = _get_stock_evidence(symbol)
            card = _stock_risk_card(symbol, evidence)
        except Exception:
            card = None
        if card is not None:
            items.append(card)
    return {"items": items}




# ---------- risks ------------------------------------------------------------


