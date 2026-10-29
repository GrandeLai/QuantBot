"""Tests for DCF valuation API endpoints."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.dcf.engine import DCFResult, WACCComponents


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


WACC_OK = WACCComponents(
    cost_of_equity=0.111,
    cost_of_debt=0.040,
    tax_rate=0.21,
    debt_weight=0.08,
    equity_weight=0.92,
    wacc=0.105,
    beta=1.2,
    risk_free_rate=0.045,
)

DCF_OK = DCFResult(
    ticker="AAPL",
    current_price=185.0,
    fair_value_p5=160.0,
    fair_value_p50=220.0,
    fair_value_p95=310.0,
    wacc_components=WACC_OK,
    base_fcf=3_000_000_000.0,
    npv_fcf=12_000_000_000.0,
    terminal_value_pv=180_000_000_000.0,
    margin_of_safety=0.159,
    valuation="undervalued",
    projected_fcfs=[3.24e9, 3.5e9, 3.78e9, 4.08e9, 4.41e9],
    as_of_date=date(2026, 4, 29),
)


# ---------------------------------------------------------------------------
# GET /api/dcf/valuation
# ---------------------------------------------------------------------------


class TestValuationEndpoint:
    @patch("quantbot.stocks.api.dcf.compute_dcf", return_value=DCF_OK)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/dcf/valuation?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.dcf.compute_dcf", return_value=DCF_OK)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/dcf/valuation?ticker=AAPL").json()
        assert "fair_value_p50" in data
        assert "margin_of_safety" in data
        assert "valuation" in data
        assert "wacc_components" in data
        assert "projected_fcfs" in data

    @patch("quantbot.stocks.api.dcf.compute_dcf", return_value=DCF_OK)
    def test_fair_values_ordered(self, mock_fn, client):
        data = client.get("/api/dcf/valuation?ticker=AAPL").json()
        assert data["fair_value_p5"] <= data["fair_value_p50"] <= data["fair_value_p95"]

    @patch("quantbot.stocks.api.dcf.compute_dcf", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/dcf/valuation?ticker=FAKE")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/dcf/valuation").status_code == 422

    @patch("quantbot.stocks.api.dcf.compute_dcf", return_value=DCF_OK)
    def test_valuation_label_present(self, mock_fn, client):
        data = client.get("/api/dcf/valuation?ticker=AAPL").json()
        assert data["valuation"] in {
            "deep_value", "undervalued", "fair", "overvalued", "overheated"
        }


# ---------------------------------------------------------------------------
# GET /api/dcf/wacc
# ---------------------------------------------------------------------------


class TestWaccEndpoint:
    @patch("quantbot.stocks.api.dcf.compute_wacc", return_value=WACC_OK)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/dcf/wacc?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.dcf.compute_wacc", return_value=WACC_OK)
    def test_has_wacc_components(self, mock_fn, client):
        data = client.get("/api/dcf/wacc?ticker=AAPL").json()
        assert "wacc" in data
        assert "cost_of_equity" in data
        assert "beta" in data

    @patch("quantbot.stocks.api.dcf.compute_wacc", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        assert client.get("/api/dcf/wacc?ticker=FAKE").status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/dcf/wacc").status_code == 422
