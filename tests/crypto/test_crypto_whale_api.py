"""Tests for Crypto Whale / CEX Inflow API endpoints."""

from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.crypto.crypto_whale.engine import CEXInflowData, WhaleTransfer


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


_MOCK_INFLOW = CEXInflowData(
    symbol="ETH",
    hours=24,
    min_eth=100.0,
    inflow_eth=1500.0,
    outflow_eth=400.0,
    net_flow_eth=1100.0,
    inflow_usd=4_500_000.0,
    pressure_score=0.79,
    signal="heavy_inflow",
    transfer_count=5,
    recent_transfers=[
        WhaleTransfer(
            tx_hash="0xabc123",
            from_address="0xdeadbeef",
            to_address="0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be",
            value_eth=500.0,
            value_usd=1_500_000.0,
            timestamp=datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc),
            exchange_name="Binance",
            direction="inflow",
        )
    ],
    as_of_date=date(2026, 4, 29),
    api_key_missing=False,
)

_MOCK_INFLOW_NO_KEY = CEXInflowData(
    symbol="ETH",
    hours=24,
    min_eth=100.0,
    inflow_eth=0.0,
    outflow_eth=0.0,
    net_flow_eth=0.0,
    inflow_usd=None,
    pressure_score=0.5,
    signal="neutral",
    transfer_count=0,
    recent_transfers=[],
    as_of_date=date(2026, 4, 29),
    api_key_missing=True,
)


# ---------------------------------------------------------------------------
# GET /api/crypto-whale/eth-inflow
# ---------------------------------------------------------------------------


class TestETHInflowEndpoint:
    @patch("quantbot.crypto.api.crypto_whale.compute_cex_inflow", return_value=_MOCK_INFLOW)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/crypto-whale/eth-inflow")
        assert resp.status_code == 200

    @patch("quantbot.crypto.api.crypto_whale.compute_cex_inflow", return_value=_MOCK_INFLOW)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/crypto-whale/eth-inflow").json()
        assert "pressure_score" in data
        assert "signal" in data
        assert "inflow_eth" in data
        assert "outflow_eth" in data
        assert "recent_transfers" in data

    @patch("quantbot.crypto.api.crypto_whale.compute_cex_inflow", return_value=_MOCK_INFLOW)
    def test_signal_is_valid(self, mock_fn, client):
        data = client.get("/api/crypto-whale/eth-inflow").json()
        assert data["signal"] in {
            "heavy_inflow", "elevated_inflow", "neutral",
            "accumulation", "heavy_accumulation",
        }

    @patch("quantbot.crypto.api.crypto_whale.compute_cex_inflow", return_value=_MOCK_INFLOW_NO_KEY)
    def test_returns_200_when_no_key(self, mock_fn, client):
        """Should return 200 even when API key is missing."""
        resp = client.get("/api/crypto-whale/eth-inflow")
        assert resp.status_code == 200
        assert resp.json()["api_key_missing"] is True

    @patch("quantbot.crypto.api.crypto_whale.compute_cex_inflow", return_value=_MOCK_INFLOW)
    def test_hours_param_accepted(self, mock_fn, client):
        resp = client.get("/api/crypto-whale/eth-inflow?hours=72")
        assert resp.status_code == 200

    def test_hours_too_large_returns_422(self, client):
        resp = client.get("/api/crypto-whale/eth-inflow?hours=9999")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/crypto-whale/recent-transfers
# ---------------------------------------------------------------------------


class TestRecentTransfersEndpoint:
    @patch("quantbot.crypto.api.crypto_whale.fetch_recent_whale_transfers", return_value=[])
    def test_returns_list(self, mock_fn, client):
        resp = client.get("/api/crypto-whale/recent-transfers")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @patch("quantbot.crypto.api.crypto_whale.fetch_recent_whale_transfers",
           return_value=[_MOCK_INFLOW.recent_transfers[0]])
    def test_transfer_has_fields(self, mock_fn, client):
        data = client.get("/api/crypto-whale/recent-transfers").json()
        assert len(data) == 1
        tx = data[0]
        assert "tx_hash" in tx
        assert "value_eth" in tx
        assert "direction" in tx
        assert "exchange_name" in tx

    def test_limit_too_large_returns_422(self, client):
        resp = client.get("/api/crypto-whale/recent-transfers?limit=9999")
        assert resp.status_code == 422
