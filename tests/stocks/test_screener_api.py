"""Screener API endpoint tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from quantbot.stocks.screener.market_review import MarketReview


def test_get_strategies(client: TestClient) -> None:
    """GET /api/screener/strategies returns 12 strategies."""
    r = client.get("/api/screener/strategies")
    assert r.status_code == 200
    data = r.json()
    assert "strategies" in data
    assert data["count"] == 12


def test_screen_empty_symbols_rejected(client: TestClient) -> None:
    """POST /api/screener/screen rejects empty symbol list."""
    r = client.post(
        "/api/screener/screen",
        json={"strategy_id": "bull_trend", "symbols": []},
    )
    assert r.status_code == 422


def test_screen_unknown_strategy(client: TestClient) -> None:
    """POST /api/screener/screen returns 404 for unknown strategy."""
    r = client.post(
        "/api/screener/screen",
        json={"strategy_id": "nonexistent", "symbols": ["000001"]},
    )
    assert r.status_code == 404


def test_get_market_returns_stance(client: TestClient) -> None:
    """GET /api/screener/market returns a stance field (mocked AKShare)."""
    stub = MarketReview(
        date="2024-01-01",
        advances=1500,
        declines=800,
        flat=100,
        advance_ratio=0.65,
        volume_ratio=1.0,
        stance="A",
        indices=[],
        top_sectors=[],
        bottom_sectors=[],
    )
    with patch(
        "quantbot.stocks.api.screener._market.get_review",
        new=AsyncMock(return_value=stub),
    ):
        r = client.get("/api/screener/market")
    assert r.status_code == 200
    data = r.json()
    assert "stance" in data
    assert data["stance"] in ("A", "B", "C")
