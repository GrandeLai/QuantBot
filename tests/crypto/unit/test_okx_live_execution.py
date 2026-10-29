"""Unit tests for OKX live broker dispatch behind agent gates."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quantbot.crypto.runtime.arbitrage.opportunity import ArbitrageOpportunity
from quantbot.crypto.runtime.config.schema import AgentTradingConfig, ExchangeConfig, Settings, TradingConfig
from quantbot.crypto.runtime.exceptions import SafetyError
from quantbot.crypto.runtime.execution.live_broker import AgentLiveExecutionService
from quantbot.crypto.runtime.execution.okx_broker import OKXLiveBroker
from quantbot.crypto.runtime.risk.manager import RiskDecision


@dataclass
class FakeProviderOrder:
    """Small provider-order stand-in for OKXTradingProvider tests."""

    order_id: str
    symbol: str
    side: Any
    order_type: Any
    status: Any
    quantity: float
    submitted_price: float | None


class FakeOKXProvider:
    """Fake OKX provider that records submitted order requests."""

    configured = True

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def submit_order(self, request: Any) -> FakeProviderOrder:
        self.requests.append(request)
        return FakeProviderOrder(
            order_id=f"okx-{len(self.requests)}",
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            status="submitted",
            quantity=request.quantity,
            submitted_price=request.price,
        )


def _okx_opportunity() -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        opportunity_id="okx-spot-cross",
        strategy_type="cross-exchange",
        symbol="BTC/USDT",
        buy_exchange="okx",
        sell_exchange="okx",
        expected_profit=Decimal("2.00"),
        expected_profit_pct=Decimal("2.00"),
        estimated_fee=Decimal("0.20"),
        estimated_slippage=Decimal("0.01"),
        required_capital=Decimal("100"),
        net_profit=Decimal("1.79"),
        risk_score=Decimal("0.20"),
        confidence=Decimal("0.85"),
        metadata={
            "quantity": "0.002",
            "execution_quality": {
                "spread_persistence": {"passed": True},
                "depth_fill": {
                    "buy": {"complete": True},
                    "sell": {"complete": True},
                },
            },
            "legs": [
                {"exchange": "okx", "side": "buy", "market": "spot", "symbol": "BTC/USDT", "price": "50010"},
                {"exchange": "okx", "side": "sell", "market": "spot", "symbol": "BTC/USDT", "price": "50280"},
            ],
        },
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        trading=TradingConfig(live_trading=True, dry_run=False, require_confirm_before_order=False),
        exchanges={
            "okx": ExchangeConfig(
                enabled=True,
                sandbox=False,
                adapter="okx",
                api_key_env="QUANTBOT_CRYPTO_OKX_API_KEY",
                api_secret_env="QUANTBOT_CRYPTO_OKX_API_SECRET",
                passphrase_env="QUANTBOT_CRYPTO_OKX_PASSPHRASE",
            )
        },
        agent_trading=AgentTradingConfig(
            enabled=True,
            allow_live_orders=True,
            strategy_allowlist=["cross-exchange"],
            allowed_exchanges=["okx"],
            max_autonomous_order_value_usdt=Decimal("200"),
            max_autonomous_orders_per_day=3,
            audit_log_path=str(tmp_path / "okx-agent-audit.jsonl"),
            policy_id="okx-test-policy",
        ),
    )


def _risk() -> RiskDecision:
    return RiskDecision(approved=True, violations=[], risk_score=Decimal("0.20"))


def test_okx_live_broker_submits_spot_limit_orders() -> None:
    provider = FakeOKXProvider()
    broker = OKXLiveBroker(provider=provider)

    result = broker.execute(_okx_opportunity())

    assert result.live_orders_sent is True
    assert result.status == "submitted"
    assert [request.side.value for request in provider.requests] == ["buy", "sell"]
    assert [request.order_type.value for request in provider.requests] == ["limit", "limit"]
    assert provider.requests[0].symbol == "BTC/USDT"
    assert provider.requests[0].quantity == pytest.approx(0.002)
    assert provider.requests[0].price == pytest.approx(50010.0)
    assert [order.order_id for order in result.orders] == ["okx-1", "okx-2"]


def test_okx_live_broker_requires_configured_provider() -> None:
    provider = FakeOKXProvider()
    provider.configured = False

    with pytest.raises(SafetyError, match="OKX provider is not configured"):
        OKXLiveBroker(provider=provider).execute(_okx_opportunity())


def test_okx_live_broker_rejects_provider_demo_mode_mismatch() -> None:
    provider = FakeOKXProvider()
    provider.demo = True

    with pytest.raises(SafetyError, match="OKX provider demo mode does not match config"):
        OKXLiveBroker(provider=provider, expected_demo=False).execute(_okx_opportunity())


def test_agent_live_execution_service_dispatches_okx_after_gate_passes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "test-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "test-secret")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_OPERATOR_ID", "operator-1")
    provider = FakeOKXProvider()
    service = AgentLiveExecutionService(_settings(tmp_path), broker=OKXLiveBroker(provider=provider))

    result = service.execute(_okx_opportunity(), _risk())

    assert result.live_orders_sent is True
    assert len(provider.requests) == 2
    audit = (tmp_path / "okx-agent-audit.jsonl").read_text(encoding="utf-8")
    assert "agent_execute_live_submitted" in audit
    assert "test-secret" not in audit


def test_agent_live_execution_service_rejects_unsupported_perp_leg(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "test-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "test-secret")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_OPERATOR_ID", "operator-1")
    opportunity = _okx_opportunity()
    opportunity.metadata["legs"][1]["market"] = "perp"
    service = AgentLiveExecutionService(_settings(tmp_path), broker=OKXLiveBroker(provider=FakeOKXProvider()))

    with pytest.raises(SafetyError, match="only supports OKX spot limit orders"):
        service.execute(opportunity, _risk())
