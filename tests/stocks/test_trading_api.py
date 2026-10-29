"""交易 API 回归测试 — Longbridge-first / mock-fallback."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_trading_provider(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """强制交易层使用 mock provider，便于本地闭环验证."""
    monkeypatch.setenv("QUANTBOT_TRADING_PROVIDER", "mock")

    from quantbot.stocks.broker.provider import get_trading_provider

    get_trading_provider.cache_clear()
    yield client
    get_trading_provider.cache_clear()


def test_trading_status_exposes_longbridge_boundaries(mock_trading_provider: TestClient) -> None:
    """状态接口应显式暴露当前 provider 与官方能力边界."""
    response = mock_trading_provider.get("/api/trading/status")
    assert response.status_code == 200

    body = response.json()
    assert body["provider"] == "mock"
    assert body["mode"] == "paper"
    assert body["capabilities"]["supports_options"] is False
    assert body["capabilities"]["supports_otc"] is False
    assert body["capabilities"]["supports_us_prepost"] is False


def test_security_search_supports_code_and_name(mock_trading_provider: TestClient) -> None:
    """搜索应支持代码和名称匹配."""
    by_code = mock_trading_provider.get("/api/trading/securities/search?q=AAPL")
    assert by_code.status_code == 200
    assert any(item["symbol"] == "AAPL.US" for item in by_code.json()["items"])

    by_name = mock_trading_provider.get("/api/trading/securities/search?q=apple")
    assert by_name.status_code == 200
    assert any(item["symbol"] == "AAPL.US" for item in by_name.json()["items"])


















def test_trading_risk_status_exposes_thresholds_and_halt_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """交易风控状态接口应返回当前阈值与是否已触发熔断."""
    monkeypatch.setenv("QUANTBOT_TRADING_PROVIDER", "mock")
    monkeypatch.setenv("QUANTBOT_TRADING_RISK_MAX_ORDER_VALUE", "5000")
    monkeypatch.setenv("QUANTBOT_TRADING_RISK_DAILY_LOSS_LIMIT_PCT", "0.05")

    from quantbot.stocks.broker.mock import MockTradingProvider
    from quantbot.stocks.broker.provider import get_trading_provider

    original_get_account = MockTradingProvider.get_account_overview

    def _patched_get_account(self: MockTradingProvider):  # type: ignore[override]
        overview = original_get_account(self)
        overview.today_pnl_pct = -0.06
        overview.today_pnl = -6000.0
        return overview

    monkeypatch.setattr(MockTradingProvider, "get_account_overview", _patched_get_account)
    get_trading_provider.cache_clear()

    response = client.get("/api/trading/risk")
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["halted"] is True
    assert body["max_order_value"] == 5000.0
    assert body["daily_loss_limit_pct"] == 0.05
    assert body["current_today_pnl_pct"] == -0.06
    assert body["open_position_count"] >= 0
    assert "largest_position_symbol" in body




def test_legacy_trading_http_writes_are_retired(mock_trading_provider):
    assert mock_trading_provider.post("/api/trading/orders",json={}).status_code == 403
    assert mock_trading_provider.delete("/api/trading/orders/old-id").status_code == 403
