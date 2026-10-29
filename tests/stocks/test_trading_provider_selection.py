"""Trading provider selection regression tests."""

from __future__ import annotations

import pytest

from quantbot.stocks.broker.types import TradingProviderKind


@pytest.fixture(autouse=True)
def clear_provider_cache() -> None:
    """Reset provider singleton cache around each test."""
    from quantbot.stocks.broker.provider import get_trading_provider

    get_trading_provider.cache_clear()
    try:
        yield
    finally:
        get_trading_provider.cache_clear()


def test_futu_provider_status_is_explicit_when_sdk_or_config_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forced futu mode should surface provider=futu instead of silently pretending to be another provider."""
    monkeypatch.setenv("QUANTBOT_TRADING_PROVIDER", "futu")
    monkeypatch.delenv("QUANTBOT_FUTU_HOST", raising=False)
    monkeypatch.delenv("QUANTBOT_FUTU_PORT", raising=False)

    from quantbot.stocks.broker.provider import get_trading_provider

    provider = get_trading_provider()
    status = provider.get_status()

    assert status.provider == TradingProviderKind.FUTU
    assert status.configured is False
    assert status.reason is not None
    assert "futu" in status.reason.lower()
    assert status.capabilities.supports_options is True


def test_futu_provider_account_returns_structured_unavailable_error(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forced futu mode should fail honestly on unavailable trading actions."""
    monkeypatch.setenv("QUANTBOT_TRADING_PROVIDER", "futu")
    monkeypatch.delenv("QUANTBOT_FUTU_HOST", raising=False)
    monkeypatch.delenv("QUANTBOT_FUTU_PORT", raising=False)

    from quantbot.stocks.broker.provider import get_trading_provider

    get_trading_provider.cache_clear()
    response = client.get("/api/trading/account")

    assert response.status_code == 503
    body = response.json()
    assert body["detail"]["code"] == "futu_unavailable"
    assert "Futu provider unavailable" in body["detail"]["message"]


def test_auto_mode_keeps_existing_longbridge_to_mock_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding futu should not change today's auto selection behavior."""
    monkeypatch.setenv("QUANTBOT_TRADING_PROVIDER", "auto")
    monkeypatch.delenv("QUANTBOT_LONGBRIDGE_APP_KEY", raising=False)
    monkeypatch.delenv("QUANTBOT_LONGBRIDGE_APP_SECRET", raising=False)
    monkeypatch.delenv("QUANTBOT_LONGBRIDGE_ACCESS_TOKEN", raising=False)

    from quantbot.stocks.broker.provider import get_trading_provider

    provider = get_trading_provider()
    status = provider.get_status()

    assert status.provider == TradingProviderKind.MOCK
    assert status.using_mock_fallback is True
    assert status.reason is not None
