"""Tests for Short Interest + Squeeze Risk API endpoints."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.short_interest.engine import ShortInterestData


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


_MOCK_DATA = ShortInterestData(
    ticker="GME",
    short_pct_float=0.25,
    short_ratio=8.5,
    shares_short=60_000_000,
    shares_short_prior_month=55_000_000,
    short_change_pct=0.091,
    float_shares=240_000_000,
    avg_daily_volume=7_000_000,
    price_vs_52w_high=0.82,
    squeeze_risk_score=0.68,
    signal="high_short",
    as_of_date=date(2026, 4, 29),
)


# ---------------------------------------------------------------------------
# GET /api/short-interest/summary
# ---------------------------------------------------------------------------


class TestShortInterestSummary:
    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/short-interest/summary?ticker=GME")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/short-interest/summary?ticker=GME").json()
        assert "ticker" in data
        assert "short_pct_float" in data
        assert "squeeze_risk_score" in data
        assert "signal" in data
        assert "as_of_date" in data

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_signal_is_valid(self, mock_fn, client):
        data = client.get("/api/short-interest/summary?ticker=GME").json()
        assert data["signal"] in {"squeeze_setup", "high_short", "moderate", "low_short"}

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_ticker_uppercased(self, mock_fn, client):
        data = client.get("/api/short-interest/summary?ticker=gme").json()
        assert data["ticker"] == "GME"

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/short-interest/summary?ticker=NODATA")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/short-interest/summary").status_code == 422

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_score_in_range(self, mock_fn, client):
        data = client.get("/api/short-interest/summary?ticker=GME").json()
        assert 0.0 <= data["squeeze_risk_score"] <= 1.0


# ---------------------------------------------------------------------------
# GET /api/short-interest/squeeze-scan
# ---------------------------------------------------------------------------


class TestSqueezeScan:
    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_returns_list(self, mock_fn, client):
        resp = client.get("/api/short-interest/squeeze-scan?tickers=GME,AMC")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=None)
    def test_returns_empty_when_no_data(self, mock_fn, client):
        resp = client.get("/api/short-interest/squeeze-scan?tickers=X,Y")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("quantbot.stocks.api.short_interest.compute_short_interest", return_value=_MOCK_DATA)
    def test_sorted_by_score_desc(self, mock_fn, client):
        resp = client.get("/api/short-interest/squeeze-scan?tickers=GME,AMC,BB")
        data = resp.json()
        scores = [d["squeeze_risk_score"] for d in data]
        assert scores == sorted(scores, reverse=True)

    def test_exceeds_20_tickers_returns_400(self, client):
        tickers = ",".join([f"TK{i}" for i in range(21)])
        resp = client.get(f"/api/short-interest/squeeze-scan?tickers={tickers}")
        assert resp.status_code == 400

    def test_empty_tickers_returns_empty(self, client):
        resp = client.get("/api/short-interest/squeeze-scan?tickers=")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_missing_tickers_returns_422(self, client):
        assert client.get("/api/short-interest/squeeze-scan").status_code == 422
