"""账户组合 API 测试."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_portfolio_summary_uses_trading_account_snapshot(client: TestClient) -> None:
    """组合概览应来自 broker 账户，而不是本地模拟盘策略槽位."""
    response = client.get("/api/portfolio/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["mode"] == "paper"
    assert body["strategies"] == []
    assert body["total_portfolio_value"] >= 0
    assert "positions_market_value" in body


def test_strategy_slots_are_removed(client: TestClient) -> None:
    """旧模拟盘策略槽位入口应明确不可用."""
    list_response = client.get("/api/portfolio/strategies")
    assert list_response.status_code == 200
    assert list_response.json() == {"count": 0, "strategies": []}

    create_response = client.post(
        "/api/portfolio/strategies",
        json={
            "name": "legacy",
            "strategy_class": "legacy",
            "symbol": "AAPL",
            "timeframe": "1d",
            "allocation": 1000,
            "params": {},
        },
    )
    assert create_response.status_code == 410
    assert "模拟策略槽位已移除" in create_response.json()["detail"]

