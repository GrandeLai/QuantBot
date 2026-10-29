"""Tests for quant signals API endpoints.

All yfinance calls are mocked; no real network requests are made.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.quant_signals.engine import BeneishMScore, RussellMembership


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

BENEISH_OK = BeneishMScore(
    ticker="AAPL",
    m_score=-2.5,
    risk_level="safe",
    ratios={
        "DSRI": 1.02,
        "GMI": 0.98,
        "AQI": 1.01,
        "SGI": 1.10,
        "DEPI": 0.99,
        "SGAI": 1.00,
        "LVGI": 1.05,
        "TATA": -0.03,
    },
    interpretation="M-Score -2.500 < -2.22：财务健康，盈利操纵风险低。",
    as_of_date=date(2024, 12, 31),
)

RUSSELL_OK = RussellMembership(
    ticker="AAPL",
    market_cap_usd=3_000_000_000_000,
    estimated_rank=1,
    current_index="Russell 1000",
    proximity_score=0.0,
    rebalance_signal="stable",
)

BENEISH_MANIP = BeneishMScore(
    ticker="FRAUD",
    m_score=-1.0,
    risk_level="manipulator",
    ratios={
        "DSRI": 1.5, "GMI": 1.2, "AQI": 1.3,
        "SGI": 1.4, "DEPI": 0.8, "SGAI": 1.1,
        "LVGI": 1.3, "TATA": 0.15,
    },
    interpretation="M-Score -1.000 > -1.78：⚠ 高操纵风险",
    as_of_date=date(2024, 12, 31),
)


# ---------------------------------------------------------------------------
# GET /api/quant-signals/beneish
# ---------------------------------------------------------------------------


class TestBeneishEndpoint:
    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_OK)
    def test_returns_200_with_valid_ticker(self, mock_compute, client):
        resp = client.get("/api/quant-signals/beneish?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_OK)
    def test_response_has_required_fields(self, mock_compute, client):
        resp = client.get("/api/quant-signals/beneish?ticker=AAPL")
        data = resp.json()
        assert "ticker" in data
        assert "m_score" in data
        assert "risk_level" in data
        assert "ratios" in data
        assert "interpretation" in data
        assert "as_of_date" in data

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_OK)
    def test_ratios_has_8_keys(self, mock_compute, client):
        resp = client.get("/api/quant-signals/beneish?ticker=AAPL")
        assert len(resp.json()["ratios"]) == 8

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=None)
    def test_returns_404_when_no_data(self, mock_compute, client):
        resp = client.get("/api/quant-signals/beneish?ticker=FAKE")
        assert resp.status_code == 404

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_MANIP)
    def test_manipulator_risk_level(self, mock_compute, client):
        resp = client.get("/api/quant-signals/beneish?ticker=FRAUD")
        assert resp.json()["risk_level"] == "manipulator"

    def test_missing_ticker_param_returns_422(self, client):
        resp = client.get("/api/quant-signals/beneish")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/quant-signals/russell
# ---------------------------------------------------------------------------


class TestRussellEndpoint:
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=RUSSELL_OK)
    def test_returns_200_with_valid_ticker(self, mock_est, client):
        resp = client.get("/api/quant-signals/russell?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=RUSSELL_OK)
    def test_response_has_required_fields(self, mock_est, client):
        data = client.get("/api/quant-signals/russell?ticker=AAPL").json()
        assert "ticker" in data
        assert "market_cap_usd" in data
        assert "current_index" in data
        assert "proximity_score" in data
        assert "rebalance_signal" in data

    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=None)
    def test_returns_404_when_no_data(self, mock_est, client):
        resp = client.get("/api/quant-signals/russell?ticker=TINY")
        assert resp.status_code == 404

    def test_missing_ticker_param_returns_422(self, client):
        resp = client.get("/api/quant-signals/russell")
        assert resp.status_code == 422

    @patch(
        "quantbot.stocks.api.quant_signals.estimate_russell_membership",
        return_value=RussellMembership(
            ticker="SMLC",
            market_cap_usd=500_000_000,
            estimated_rank=2200,
            current_index="Russell 2000",
            proximity_score=0.3,
            rebalance_signal="stable",
        ),
    )
    def test_russell_2000_membership(self, mock_est, client):
        data = client.get("/api/quant-signals/russell?ticker=SMLC").json()
        assert data["current_index"] == "Russell 2000"


# ---------------------------------------------------------------------------
# GET /api/quant-signals/summary
# ---------------------------------------------------------------------------


class TestSummaryEndpoint:
    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_OK)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=RUSSELL_OK)
    def test_returns_200_both_present(self, mock_russell, mock_beneish, client):
        resp = client.get("/api/quant-signals/summary?ticker=AAPL")
        assert resp.status_code == 200
        data = resp.json()
        assert data["beneish"] is not None
        assert data["russell"] is not None

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=None)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=RUSSELL_OK)
    def test_beneish_none_still_200(self, mock_russell, mock_beneish, client):
        resp = client.get("/api/quant-signals/summary?ticker=NODATA")
        assert resp.status_code == 200
        data = resp.json()
        assert data["beneish"] is None
        assert data["russell"] is not None

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_OK)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=None)
    def test_russell_none_still_200(self, mock_russell, mock_beneish, client):
        resp = client.get("/api/quant-signals/summary?ticker=NODATA")
        assert resp.status_code == 200
        data = resp.json()
        assert data["beneish"] is not None
        assert data["russell"] is None

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=None)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=None)
    def test_both_none_still_200(self, mock_russell, mock_beneish, client):
        resp = client.get("/api/quant-signals/summary?ticker=GHOST")
        assert resp.status_code == 200
        data = resp.json()
        assert data["beneish"] is None
        assert data["russell"] is None

    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=BENEISH_OK)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=RUSSELL_OK)
    def test_ticker_uppercased(self, mock_russell, mock_beneish, client):
        data = client.get("/api/quant-signals/summary?ticker=aapl").json()
        assert data["ticker"] == "AAPL"

    def test_missing_ticker_returns_422(self, client):
        resp = client.get("/api/quant-signals/summary")
        assert resp.status_code == 422
