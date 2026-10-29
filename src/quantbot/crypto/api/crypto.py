"""Crypto 交易 API 路由 — OKX 现货 / 合约 / 期权.

现货
  GET  /crypto/status
  GET  /crypto/pairs
  GET  /crypto/ticker?symbols=BTC-USDT,ETH-USDT
  GET  /crypto/price/{symbol}
  GET  /crypto/account
  GET  /crypto/orders/open
  GET  /crypto/orders/history?symbol=BTC-USDT
  POST /crypto/orders
  DELETE /crypto/orders/{order_id}?symbol=BTC-USDT

永续合约
  GET  /crypto/futures/tickers
  GET  /crypto/futures/positions
  GET  /crypto/futures/orders/open
  POST /crypto/futures/orders
  DELETE /crypto/futures/orders/{order_id}?inst_id=BTC-USDT-SWAP

期权
  GET  /crypto/options/underlyings
  GET  /crypto/options/expiries?uly=BTC-USD
  GET  /crypto/options/chain?uly=BTC-USD&exp_time=YYYYMMDD
  GET  /crypto/options/positions?uly=BTC-USD
  POST /crypto/options/orders
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from quantbot.crypto.broker.okx import get_okx_provider
from quantbot.crypto.broker.types import CryptoOrderRequest, SwapOrderRequest, TradingProviderKind
from quantbot.common.data.fetchers.okx_fetcher import (
    OPTIONS_UNDERLYINGS,
    POPULAR_PAIRS,
    display_symbol,
    normalize_symbol,
)

router = APIRouter(prefix="/crypto", tags=["加密货币"])


@router.get("/status")
def get_status() -> dict:
    p = get_okx_provider()
    return {
        "provider": TradingProviderKind.OKX,
        "testnet": p.demo,
        "configured": p.configured,
        "base_url": p.BASE_URL,
        "mode": "demo" if p.demo else "live",
        "setup_hint": (
            "在 .env 中设置 QUANTBOT_CRYPTO_OKX_API_KEY / QUANTBOT_CRYPTO_OKX_API_SECRET / QUANTBOT_CRYPTO_OKX_PASSPHRASE"
            if not p.configured else ""
        ),
    }


@router.get("/pairs")
def list_pairs(with_price: bool = Query(default=False)) -> dict:
    """热门交易对列表，可选含实时价格."""
    p = get_okx_provider()
    pairs = []
    for sym, name in POPULAR_PAIRS:
        price = p.get_price(sym) if with_price else 0.0
        pairs.append({
            "symbol": sym,
            "display": display_symbol(sym),
            "name": name,
            "price": price,
        })
    return {"pairs": pairs, "count": len(pairs)}


@router.get("/ticker")
def get_ticker(
    symbols: str = Query(..., description="逗号分隔的 OKX 交易对，如 BTC-USDT,ETH-USDT"),
) -> dict:
    sym_list = [normalize_symbol(s.strip()) for s in symbols.split(",") if s.strip()]
    p = get_okx_provider()
    raw = p.get_ticker_24h(sym_list)
    tickers = []
    for d in raw:
        last  = float(d.get("last",    0) or 0)
        open_ = float(d.get("open24h", 0) or 0)
        change_pct = (last - open_) / open_ * 100 if open_ else 0.0
        inst_id = str(d.get("instId", ""))
        tickers.append({
            "symbol":      inst_id,
            "display":     display_symbol(inst_id),
            "price":       last,
            "change_pct":  round(change_pct, 4),
            "volume_usdt": float(d.get("volCcy24h", 0) or 0),
            "high_24h":    float(d.get("high24h",   0) or 0),
            "low_24h":     float(d.get("low24h",    0) or 0),
        })
    return {"tickers": tickers}


@router.get("/price/{symbol}")
def get_price(symbol: str) -> dict:
    p = get_okx_provider()
    sym = normalize_symbol(symbol)
    return {"symbol": sym, "display": display_symbol(sym), "price": p.get_price(sym)}


@router.get("/account")
def get_account() -> dict:
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(
            400,
            "OKX API Key 未配置，请在 .env 设置 QUANTBOT_CRYPTO_OKX_API_KEY / QUANTBOT_CRYPTO_OKX_API_SECRET / QUANTBOT_CRYPTO_OKX_PASSPHRASE",
        )
    try:
        return p.get_account().model_dump()
    except Exception as exc:
        raise HTTPException(502, f"OKX API 错误: {exc}") from exc


@router.get("/orders/open")
def get_open_orders(symbol: str | None = Query(default=None)) -> dict:
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        orders = p.get_open_orders(symbol)
        return {"orders": [o.model_dump() for o in orders]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/orders/history")
def get_order_history(
    symbol: str = Query(...),
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        orders = p.get_order_history(symbol, limit=limit)
        return {"orders": [o.model_dump() for o in orders]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/orders")
def submit_order(req: CryptoOrderRequest) -> dict:
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        order = p.submit_order(req)
        return order.model_dump()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.delete("/orders/{order_id}")
def cancel_order(order_id: str, symbol: str = Query(...)) -> dict:
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        result = p.cancel_order(symbol, order_id)
        return result.model_dump()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


# ══════════════════════════════════════════════════════════════════════════════
# 永续合约 (SWAP)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/futures/tickers")
def get_futures_tickers() -> dict:
    """获取热门永续合约行情（含资金费率）."""
    p = get_okx_provider()
    try:
        tickers = p.get_swap_tickers()
        return {"tickers": [t.model_dump() for t in tickers]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/futures/positions")
def get_futures_positions() -> dict:
    """获取合约持仓（需要 API Key）."""
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        positions = p.get_positions("SWAP")
        return {"positions": [pos.model_dump() for pos in positions]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/futures/orders/open")
def get_futures_open_orders(inst_id: str | None = Query(default=None)) -> dict:
    """获取合约挂单."""
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        orders = p.get_open_futures_orders("SWAP", inst_id)
        return {"orders": [o.model_dump() for o in orders]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/futures/orders")
def submit_futures_order(req: SwapOrderRequest) -> dict:
    """提交永续合约订单."""
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        order = p.submit_swap_order(req)
        return order.model_dump()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.delete("/futures/orders/{order_id}")
def cancel_futures_order(order_id: str, inst_id: str = Query(...)) -> dict:
    """撤销合约订单."""
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        result = p.cancel_order(inst_id, order_id)
        return result.model_dump()
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


# ══════════════════════════════════════════════════════════════════════════════
# 期权 (OPTION)
# ══════════════════════════════════════════════════════════════════════════════

@router.get("/options/underlyings")
def get_options_underlyings() -> dict:
    """返回支持的期权标的列表."""
    return {"underlyings": OPTIONS_UNDERLYINGS}


@router.get("/options/expiries")
def get_options_expiries(uly: str = Query(..., description="如 BTC-USD")) -> dict:
    """获取指定标的的期权到期日列表."""
    p = get_okx_provider()
    try:
        expiries = p.get_options_expiries(uly)
        return {"uly": uly, "expiries": expiries}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/options/chain")
def get_options_chain(
    uly: str = Query(..., description="如 BTC-USD"),
    exp_time: str | None = Query(default=None, description="YYYYMMDD，不传则返回全部"),
) -> dict:
    """获取期权链（含希腊字母）."""
    p = get_okx_provider()
    try:
        chain = p.get_options_chain(uly, exp_time)
        return {"uly": uly, "exp_time": exp_time, "count": len(chain),
                "chain": [t.model_dump() for t in chain]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/options/positions")
def get_options_positions() -> dict:
    """获取期权持仓."""
    p = get_okx_provider()
    if not p.configured:
        raise HTTPException(400, "OKX API Key 未配置")
    try:
        positions = p.get_positions("OPTION")
        return {"positions": [pos.model_dump() for pos in positions]}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
