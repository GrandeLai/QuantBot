"""Tests for crypto_derivs collectors using httpx.MockTransport (no network)."""
from __future__ import annotations

import json
from typing import Any
from collections.abc import Callable

import httpx
import pytest

from quantbot.crypto.crypto_derivs import (
    FundingRate,
    OpenInterest,
    fetch_aggregated_derivs,
    fetch_binance_funding,
    fetch_binance_funding_history,
    fetch_binance_open_interest,
    fetch_okx_funding,
    fetch_okx_funding_history,
    fetch_okx_open_interest,
)
from quantbot.crypto.crypto_derivs import collector as collector_module


# --- Helpers -----------------------------------------------------------------


def _json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )


def _route(routes: dict[str, Callable[[httpx.Request], httpx.Response]]) -> httpx.MockTransport:
    """Map URL path → handler. Unmatched path → 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        for path, fn in routes.items():
            if path in request.url.path:
                return fn(request)
        return httpx.Response(404, text=f"unrouted: {request.url}")

    return httpx.MockTransport(handler)


def _make_client(transport: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=transport, timeout=5.0)


BINANCE_FUNDING_OK = {
    "symbol": "BTCUSDT",
    "lastFundingRate": "0.00012345",
    "nextFundingTime": 1714000000000,
    "time": 1713990000000,
}
BINANCE_OI_OK = {"symbol": "BTCUSDT", "openInterest": "12345.67", "time": 1713990000000}
BINANCE_FUNDING_HISTORY_OK = [
    {"fundingRate": "0.0001", "fundingTime": 1713000000000},
    {"fundingRate": "-0.00005", "fundingTime": 1713028800000},
]

OKX_FUNDING_OK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "fundingRate": "0.00010000",
            "nextFundingTime": "1714000000000",
            "ts": "1713990000000",
        }
    ],
}
OKX_OI_OK = {
    "code": "0",
    "msg": "",
    "data": [
        {
            "instId": "BTC-USDT-SWAP",
            "instType": "SWAP",
            "oi": "5000.0",
            "oiCcy": "350000000.0",
            "ts": "1713990000000",
        }
    ],
}
OKX_FUNDING_HISTORY_OK = {
    "code": "0",
    "msg": "",
    "data": [
        {"realizedRate": "0.0001", "fundingTime": "1713000000000"},
        {"realizedRate": "-0.00005", "fundingTime": "1713028800000"},
    ],
}


# --- Binance -----------------------------------------------------------------


class TestBinanceCollectors:
    @pytest.mark.asyncio
    async def test_funding_happy_path(self) -> None:
        transport = _route({"premiumIndex": lambda _r: _json_response(BINANCE_FUNDING_OK)})
        async with _make_client(transport) as client:
            fr = await fetch_binance_funding("BTC", client=client)
        assert isinstance(fr, FundingRate)
        assert fr.exchange == "binance"
        assert fr.symbol == "BTC"
        assert fr.raw_symbol == "BTCUSDT"
        assert abs(fr.funding_rate - 0.00012345) < 1e-12
        assert fr.next_funding_time is not None

    @pytest.mark.asyncio
    async def test_funding_lowercase_asset_normalized(self) -> None:
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["symbol"] = request.url.params.get("symbol", "")
            return _json_response(BINANCE_FUNDING_OK)

        transport = _route({"premiumIndex": handler})
        async with _make_client(transport) as client:
            await fetch_binance_funding("btc", client=client)
        assert seen["symbol"] == "BTCUSDT"

    @pytest.mark.asyncio
    async def test_funding_4xx_raises(self) -> None:
        transport = _route(
            {"premiumIndex": lambda _r: _json_response({"code": -1121, "msg": "Invalid"}, 400)}
        )
        async with _make_client(transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_binance_funding("BTC", client=client)

    @pytest.mark.asyncio
    async def test_open_interest_happy_path(self) -> None:
        transport = _route({"openInterest": lambda _r: _json_response(BINANCE_OI_OK)})
        async with _make_client(transport) as client:
            oi = await fetch_binance_open_interest("BTC", client=client)
        assert isinstance(oi, OpenInterest)
        assert oi.exchange == "binance"
        assert oi.open_interest == 12345.67
        assert oi.open_interest_value is None

    @pytest.mark.asyncio
    async def test_funding_history(self) -> None:
        transport = _route({"fundingRate": lambda _r: _json_response(BINANCE_FUNDING_HISTORY_OK)})
        async with _make_client(transport) as client:
            history = await fetch_binance_funding_history("BTC", limit=2, client=client)
        assert len(history) == 2
        assert history[0].exchange == "binance"
        assert history[0].funding_rate == 0.0001
        assert history[1].funding_rate == -0.00005


# --- OKX ---------------------------------------------------------------------


class TestOKXCollectors:
    @pytest.mark.asyncio
    async def test_funding_happy_path(self) -> None:
        transport = _route({"funding-rate": lambda _r: _json_response(OKX_FUNDING_OK)})
        async with _make_client(transport) as client:
            fr = await fetch_okx_funding("BTC", client=client)
        assert isinstance(fr, FundingRate)
        assert fr.exchange == "okx"
        assert fr.raw_symbol == "BTC-USDT-SWAP"
        assert abs(fr.funding_rate - 0.0001) < 1e-12

    @pytest.mark.asyncio
    async def test_funding_okx_business_error_raises(self) -> None:
        # OKX 200 OK but code != "0" → should raise
        body = {"code": "51000", "msg": "Param error", "data": []}
        transport = _route({"funding-rate": lambda _r: _json_response(body)})
        async with _make_client(transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_okx_funding("BTC", client=client)

    @pytest.mark.asyncio
    async def test_open_interest_happy_path(self) -> None:
        transport = _route({"open-interest": lambda _r: _json_response(OKX_OI_OK)})
        async with _make_client(transport) as client:
            oi = await fetch_okx_open_interest("BTC", client=client)
        assert oi.exchange == "okx"
        assert oi.open_interest == 5000.0
        assert oi.open_interest_value == 350000000.0

    @pytest.mark.asyncio
    async def test_funding_history(self) -> None:
        transport = _route(
            {"funding-rate-history": lambda _r: _json_response(OKX_FUNDING_HISTORY_OK)}
        )
        async with _make_client(transport) as client:
            history = await fetch_okx_funding_history("BTC", limit=2, client=client)
        assert len(history) == 2
        assert history[0].exchange == "okx"
        assert history[0].funding_rate == 0.0001

    @pytest.mark.asyncio
    async def test_open_interest_business_error_raises(self) -> None:
        body = {"code": "51001", "msg": "Instrument not found", "data": []}
        transport = _route({"open-interest": lambda _r: _json_response(body)})
        async with _make_client(transport) as client:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_okx_open_interest("FAKE", client=client)


# --- Aggregated --------------------------------------------------------------


class TestAggregated:
    """fetch_aggregated_derivs internally constructs its own AsyncClient; we
    monkey-patch the four primitive fetchers to bypass HTTP entirely.
    """

    @pytest.mark.asyncio
    async def test_all_succeed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_binance_funding(asset: str = "BTC", *, client: Any = None) -> FundingRate:
            return FundingRate.model_validate(BINANCE_FUNDING_OK | {
                "exchange": "binance",
                "symbol": asset.upper(),
                "raw_symbol": "BTCUSDT",
                "funding_rate": 0.0001,
                "timestamp": "2026-04-29T00:00:00+00:00",
            })

        async def fake_binance_oi(asset: str = "BTC", *, client: Any = None) -> OpenInterest:
            return OpenInterest(
                exchange="binance",
                symbol=asset.upper(),
                raw_symbol="BTCUSDT",
                open_interest=12345.0,
                open_interest_value=None,
                timestamp="2026-04-29T00:00:00+00:00",  # type: ignore[arg-type]
            )

        async def fake_okx_funding(asset: str = "BTC", *, client: Any = None) -> FundingRate:
            return FundingRate(
                exchange="okx",
                symbol=asset.upper(),
                raw_symbol="BTC-USDT-SWAP",
                funding_rate=0.00012,
                next_funding_time=None,
                timestamp="2026-04-29T00:00:00+00:00",  # type: ignore[arg-type]
            )

        async def fake_okx_oi(asset: str = "BTC", *, client: Any = None) -> OpenInterest:
            return OpenInterest(
                exchange="okx",
                symbol=asset.upper(),
                raw_symbol="BTC-USDT-SWAP",
                open_interest=5000.0,
                open_interest_value=350_000_000.0,
                timestamp="2026-04-29T00:00:00+00:00",  # type: ignore[arg-type]
            )

        monkeypatch.setattr(collector_module, "fetch_binance_funding", fake_binance_funding)
        monkeypatch.setattr(collector_module, "fetch_binance_open_interest", fake_binance_oi)
        monkeypatch.setattr(collector_module, "fetch_okx_funding", fake_okx_funding)
        monkeypatch.setattr(collector_module, "fetch_okx_open_interest", fake_okx_oi)

        result = await fetch_aggregated_derivs("BTC")
        assert result["asset"] == "BTC"
        assert result["funding"]["binance"] is not None
        assert result["funding"]["okx"] is not None
        assert result["open_interest"]["binance"] is not None
        assert result["open_interest"]["okx"] is not None
        assert result["errors"] == {}

    @pytest.mark.asyncio
    async def test_partial_failure_collected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_ok_funding(asset: str = "BTC", *, client: Any = None) -> FundingRate:
            return FundingRate(
                exchange="binance",
                symbol="BTC",
                raw_symbol="BTCUSDT",
                funding_rate=0.0001,
                next_funding_time=None,
                timestamp="2026-04-29T00:00:00+00:00",  # type: ignore[arg-type]
            )

        async def fake_failing(asset: str = "BTC", *, client: Any = None) -> Any:
            raise RuntimeError("simulated network failure")

        monkeypatch.setattr(collector_module, "fetch_binance_funding", fake_ok_funding)
        monkeypatch.setattr(collector_module, "fetch_binance_open_interest", fake_failing)
        monkeypatch.setattr(collector_module, "fetch_okx_funding", fake_failing)
        monkeypatch.setattr(collector_module, "fetch_okx_open_interest", fake_failing)

        result = await fetch_aggregated_derivs("BTC")
        assert result["funding"]["binance"] is not None
        assert result["funding"]["okx"] is None
        assert result["open_interest"]["binance"] is None
        assert result["open_interest"]["okx"] is None
        assert "binance_oi" in result["errors"]
        assert "okx_funding" in result["errors"]
        assert "okx_oi" in result["errors"]
        assert "RuntimeError" in result["errors"]["binance_oi"]
