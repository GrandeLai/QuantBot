"""Tests for QuantBot crypto-only data API."""

from __future__ import annotations

from fastapi.testclient import TestClient

from quantbot.web.app import app

client = TestClient(app)


def test_universe_is_crypto_only() -> None:
    """Universe endpoint exposes crypto instruments only."""
    response = client.get("/api/data/universe?market=crypto")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] >= 3
    symbols = {item["symbol"] for item in body["instruments"]}
    assert {"BTC-USDT", "ETH-USDT", "SOL-USDT"}.issubset(symbols)
    assert "AAPL" not in symbols


def test_resolve_alias() -> None:
    """Common pair aliases resolve to canonical OKX symbols."""
    response = client.get("/api/data/resolve/BTCUSDT")
    assert response.status_code == 200
    assert response.json()["symbol"] == "BTC-USDT"
