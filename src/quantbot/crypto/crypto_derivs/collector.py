"""异步 collectors — 从 Binance Futures / OKX SWAP 公共 API 拉 funding + OI.

零 API key（公共端点）。一次性 fetch 用 fetch_aggregated_derivs，做仪表盘。
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from typing import Any

import httpx

from quantbot.crypto.crypto_derivs.models import FundingRate, OpenInterest

_BINANCE_BASE = "https://fapi.binance.com"
_OKX_BASE = "https://www.okx.com"

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def _binance_symbol(asset: str) -> str:
    return f"{asset.upper()}USDT"


def _okx_inst_id(asset: str) -> str:
    return f"{asset.upper()}-USDT-SWAP"


def _ts_from_ms(ms: int | str | None) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)


# --- Binance -----------------------------------------------------------------


async def fetch_binance_funding(
    asset: str = "BTC", *, client: httpx.AsyncClient | None = None
) -> FundingRate:
    """当前 funding rate（Binance Futures）."""
    raw = _binance_symbol(asset)
    url = f"{_BINANCE_BASE}/fapi/v1/premiumIndex"
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=_TIMEOUT))
        r = await client.get(url, params={"symbol": raw})
        r.raise_for_status()
        data: dict[str, Any] = r.json()
    return FundingRate(
        exchange="binance",
        symbol=asset.upper(),
        raw_symbol=raw,
        funding_rate=float(data["lastFundingRate"]),
        next_funding_time=_ts_from_ms(data.get("nextFundingTime")),
        timestamp=_ts_from_ms(data.get("time")) or datetime.now(tz=UTC),
    )


async def fetch_binance_open_interest(
    asset: str = "BTC", *, client: httpx.AsyncClient | None = None
) -> OpenInterest:
    """当前 open interest（Binance Futures）."""
    raw = _binance_symbol(asset)
    url = f"{_BINANCE_BASE}/fapi/v1/openInterest"
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=_TIMEOUT))
        r = await client.get(url, params={"symbol": raw})
        r.raise_for_status()
        data: dict[str, Any] = r.json()
    return OpenInterest(
        exchange="binance",
        symbol=asset.upper(),
        raw_symbol=raw,
        open_interest=float(data["openInterest"]),
        open_interest_value=None,  # Binance 不直出，需要乘价格
        timestamp=_ts_from_ms(data.get("time")) or datetime.now(tz=UTC),
    )


async def fetch_binance_funding_history(
    asset: str = "BTC",
    limit: int = 100,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[FundingRate]:
    """历史 funding rate（Binance Futures，最多 1000 条）."""
    raw = _binance_symbol(asset)
    url = f"{_BINANCE_BASE}/fapi/v1/fundingRate"
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=_TIMEOUT))
        r = await client.get(url, params={"symbol": raw, "limit": min(limit, 1000)})
        r.raise_for_status()
        rows: list[dict[str, Any]] = r.json()
    return [
        FundingRate(
            exchange="binance",
            symbol=asset.upper(),
            raw_symbol=raw,
            funding_rate=float(row["fundingRate"]),
            next_funding_time=None,
            timestamp=_ts_from_ms(row["fundingTime"]) or datetime.now(tz=UTC),
        )
        for row in rows
    ]


# --- OKX ---------------------------------------------------------------------


async def fetch_okx_funding(
    asset: str = "BTC", *, client: httpx.AsyncClient | None = None
) -> FundingRate:
    """当前 funding rate（OKX SWAP）."""
    inst_id = _okx_inst_id(asset)
    url = f"{_OKX_BASE}/api/v5/public/funding-rate"
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=_TIMEOUT))
        r = await client.get(url, params={"instId": inst_id})
        r.raise_for_status()
        body: dict[str, Any] = r.json()
    if body.get("code") != "0":
        raise httpx.HTTPStatusError(
            f"OKX error: {body.get('msg')}", request=r.request, response=r
        )
    row = body["data"][0]
    return FundingRate(
        exchange="okx",
        symbol=asset.upper(),
        raw_symbol=inst_id,
        funding_rate=float(row["fundingRate"]),
        next_funding_time=_ts_from_ms(row.get("nextFundingTime")),
        timestamp=_ts_from_ms(row.get("ts")) or datetime.now(tz=UTC),
    )


async def fetch_okx_open_interest(
    asset: str = "BTC", *, client: httpx.AsyncClient | None = None
) -> OpenInterest:
    """当前 open interest（OKX SWAP）."""
    inst_id = _okx_inst_id(asset)
    url = f"{_OKX_BASE}/api/v5/public/open-interest"
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=_TIMEOUT))
        r = await client.get(url, params={"instType": "SWAP", "instId": inst_id})
        r.raise_for_status()
        body: dict[str, Any] = r.json()
    if body.get("code") != "0":
        raise httpx.HTTPStatusError(
            f"OKX error: {body.get('msg')}", request=r.request, response=r
        )
    row = body["data"][0]
    return OpenInterest(
        exchange="okx",
        symbol=asset.upper(),
        raw_symbol=inst_id,
        open_interest=float(row["oi"]),
        open_interest_value=float(row["oiCcy"]) if row.get("oiCcy") else None,
        timestamp=_ts_from_ms(row.get("ts")) or datetime.now(tz=UTC),
    )


async def fetch_okx_funding_history(
    asset: str = "BTC",
    limit: int = 100,
    *,
    client: httpx.AsyncClient | None = None,
) -> list[FundingRate]:
    """历史 funding rate（OKX SWAP，最多 100 条/请求）."""
    inst_id = _okx_inst_id(asset)
    url = f"{_OKX_BASE}/api/v5/public/funding-rate-history"
    async with AsyncExitStack() as stack:
        if client is None:
            client = await stack.enter_async_context(httpx.AsyncClient(timeout=_TIMEOUT))
        r = await client.get(url, params={"instId": inst_id, "limit": min(limit, 100)})
        r.raise_for_status()
        body: dict[str, Any] = r.json()
    if body.get("code") != "0":
        raise httpx.HTTPStatusError(
            f"OKX error: {body.get('msg')}", request=r.request, response=r
        )
    return [
        FundingRate(
            exchange="okx",
            symbol=asset.upper(),
            raw_symbol=inst_id,
            funding_rate=float(row["realizedRate"]),
            next_funding_time=None,
            timestamp=_ts_from_ms(row.get("fundingTime")) or datetime.now(tz=UTC),
        )
        for row in body["data"]
    ]


# --- 一站式聚合 --------------------------------------------------------------


async def fetch_aggregated_derivs(asset: str = "BTC") -> dict[str, Any]:
    """并发拉 binance + okx 的 funding + OI；单失败不阻塞其它项.

    Returns:
        {
          "asset": "BTC",
          "timestamp": ISO,
          "funding": {"binance": FundingRate | None, "okx": FundingRate | None},
          "open_interest": {"binance": OI | None, "okx": OI | None},
          "errors": {"binance_funding": "msg", ...}  # 仅失败的项
        }
    """
    asset = asset.upper()
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        results = await asyncio.gather(
            fetch_binance_funding(asset, client=client),
            fetch_binance_open_interest(asset, client=client),
            fetch_okx_funding(asset, client=client),
            fetch_okx_open_interest(asset, client=client),
            return_exceptions=True,
        )

    keys = ("binance_funding", "binance_oi", "okx_funding", "okx_oi")
    funding: dict[str, Any] = {"binance": None, "okx": None}
    oi: dict[str, Any] = {"binance": None, "okx": None}
    errors: dict[str, str] = {}

    for key, res in zip(keys, results, strict=True):
        if isinstance(res, BaseException):
            errors[key] = f"{type(res).__name__}: {res}"
            continue
        if key == "binance_funding":
            funding["binance"] = res
        elif key == "binance_oi":
            oi["binance"] = res
        elif key == "okx_funding":
            funding["okx"] = res
        elif key == "okx_oi":
            oi["okx"] = res

    return {
        "asset": asset,
        "timestamp": datetime.now(tz=UTC),
        "funding": funding,
        "open_interest": oi,
        "errors": errors,
    }
