"""Tests for Token Unlock API endpoints."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.crypto.token_unlock.engine import (
    TokenUnlockCalendar,
    TokenUnlockEvent,
)


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


TODAY = date.today()


def _make_event(
    symbol: str = "ARB",
    days_until: int = 7,
    score: float = 0.7,
    signal: str = "high_risk",
) -> TokenUnlockEvent:
    return TokenUnlockEvent(
        protocol="Test Protocol",
        symbol=symbol,
        unlock_date=TODAY + timedelta(days=days_until),
        days_until_unlock=days_until,
        unlock_tokens=1_000_000,
        unlock_usd=500_000,
        unlock_pct_circulating=5.0,
        category="investors",
        sell_pressure_score=score,
        signal=signal,  # type: ignore[arg-type]
    )


CAL_EMPTY = TokenUnlockCalendar(
    as_of_date=TODAY,
    events=[],
    total_events=0,
    high_risk_count=0,
)

CAL_WITH_EVENTS = TokenUnlockCalendar(
    as_of_date=TODAY,
    events=[
        _make_event("ARB", 7, 0.75, "high_risk"),
        _make_event("OP",  14, 0.4,  "moderate_risk"),
        _make_event("DOGE",30, 0.15, "low_risk"),
    ],
    total_events=3,
    high_risk_count=1,
)


# ---------------------------------------------------------------------------
# GET /api/token-unlocks/upcoming
# ---------------------------------------------------------------------------


class TestUpcomingEndpoint:
    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_EMPTY)
    def test_returns_200_even_empty(self, mock_fetch, client):
        resp = client.get("/api/token-unlocks/upcoming")
        assert resp.status_code == 200

    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_response_has_required_fields(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/upcoming").json()
        assert "events" in data
        assert "total_events" in data
        assert "high_risk_count" in data
        assert "as_of_date" in data

    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_returns_all_events(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/upcoming").json()
        assert data["total_events"] == 3

    def test_invalid_days_param_returns_422(self, client):
        resp = client.get("/api/token-unlocks/upcoming?days=0")
        assert resp.status_code == 422

    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_event_has_sell_pressure_score(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/upcoming").json()
        event = data["events"][0]
        assert "sell_pressure_score" in event
        assert "signal" in event


# ---------------------------------------------------------------------------
# GET /api/token-unlocks/by-symbol
# ---------------------------------------------------------------------------


class TestBySymbolEndpoint:
    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_filters_by_symbol(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/by-symbol?symbol=ARB").json()
        assert all(e["symbol"] == "ARB" for e in data)

    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_returns_empty_for_unknown_symbol(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/by-symbol?symbol=UNKNOWN").json()
        assert data == []

    def test_missing_symbol_returns_422(self, client):
        resp = client.get("/api/token-unlocks/by-symbol")
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/token-unlocks/high-risk
# ---------------------------------------------------------------------------


class TestHighRiskEndpoint:
    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_filters_high_risk_events(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/high-risk").json()
        # Only ARB has score 0.75 >= 0.6
        assert len(data) == 1
        assert data[0]["symbol"] == "ARB"

    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_EMPTY)
    def test_empty_calendar_returns_empty_list(self, mock_fetch, client):
        data = client.get("/api/token-unlocks/high-risk").json()
        assert data == []

    @patch("quantbot.crypto.api.token_unlock.fetch_upcoming_unlocks", return_value=CAL_WITH_EVENTS)
    def test_custom_min_score(self, mock_fetch, client):
        # min_score=0.3 → includes ARB (0.75) and OP (0.4)
        data = client.get("/api/token-unlocks/high-risk?min_score=0.3").json()
        assert len(data) == 2
