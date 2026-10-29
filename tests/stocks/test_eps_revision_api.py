"""Tests for EPS Revision API endpoints."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.eps_revision.engine import (
    AnalystTargets,
    EpsRevisionMomentum,
    EpsRevisionPeriod,
)


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


# Shared fixtures

PERIOD_OK = EpsRevisionPeriod(
    period="0q",
    period_label="本季度",
    up_7d=5,
    down_7d=1,
    up_30d=10,
    down_30d=2,
    revision_score_7d=0.667,
    revision_score_30d=0.667,
    direction="upgrade",
)

TARGETS_OK = AnalystTargets(
    current_price=175.0,
    target_mean=210.0,
    target_median=205.0,
    target_high=250.0,
    target_low=150.0,
    upside_pct=0.2,
)

REVISION_OK = EpsRevisionMomentum(
    ticker="AAPL",
    periods=[PERIOD_OK],
    targets=TARGETS_OK,
    overall_direction="upgrade",
    as_of_date=date(2026, 4, 29),
)


# ---------------------------------------------------------------------------
# GET /api/eps-revision/summary
# ---------------------------------------------------------------------------


class TestSummaryEndpoint:
    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=REVISION_OK,
    )
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/eps-revision/summary?ticker=AAPL")
        assert resp.status_code == 200

    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=REVISION_OK,
    )
    def test_response_has_required_fields(self, mock_fn, client):
        data = client.get("/api/eps-revision/summary?ticker=AAPL").json()
        assert "ticker" in data
        assert "periods" in data
        assert "targets" in data
        assert "overall_direction" in data
        assert "as_of_date" in data

    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=REVISION_OK,
    )
    def test_periods_not_empty(self, mock_fn, client):
        data = client.get("/api/eps-revision/summary?ticker=AAPL").json()
        assert len(data["periods"]) > 0

    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=None,
    )
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/eps-revision/summary?ticker=FAKE")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        resp = client.get("/api/eps-revision/summary")
        assert resp.status_code == 422

    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=REVISION_OK,
    )
    def test_ticker_uppercased(self, mock_fn, client):
        data = client.get("/api/eps-revision/summary?ticker=aapl").json()
        assert data["ticker"] == "AAPL"


# ---------------------------------------------------------------------------
# GET /api/eps-revision/targets
# ---------------------------------------------------------------------------


class TestTargetsEndpoint:
    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=REVISION_OK,
    )
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/eps-revision/targets?ticker=AAPL")
        assert resp.status_code == 200

    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=REVISION_OK,
    )
    def test_response_has_target_fields(self, mock_fn, client):
        data = client.get("/api/eps-revision/targets?ticker=AAPL").json()
        assert "target_mean" in data
        assert "target_median" in data
        assert "upside_pct" in data

    @patch(
        "quantbot.stocks.api.eps_revision.compute_eps_revision_momentum",
        return_value=None,
    )
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/eps-revision/targets?ticker=FAKE")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/eps-revision/targets").status_code == 422
