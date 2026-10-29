"""Unit tests for controlled autonomous live-agent trading gates."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.crypto.runtime.arbitrage.opportunity import ArbitrageOpportunity
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import AgentTradingConfig, ExchangeConfig, Settings, TradingConfig
from quantbot.crypto.runtime.execution.live_agent import AgentLiveTradingGate
from quantbot.crypto.runtime.risk.manager import RiskDecision


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def _opportunity(buy_exchange: str = "okx", sell_exchange: str = "okx") -> ArbitrageOpportunity:
    return ArbitrageOpportunity(
        opportunity_id="okx-cross",
        strategy_type="cross-exchange",
        symbol="BTC/USDT",
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        expected_profit=Decimal("2.00"),
        expected_profit_pct=Decimal("2.00"),
        estimated_fee=Decimal("0.20"),
        estimated_slippage=Decimal("0.01"),
        required_capital=Decimal("100"),
        net_profit=Decimal("1.79"),
        risk_score=Decimal("0.20"),
        confidence=Decimal("0.85"),
        metadata={
            "execution_quality": {
                "spread_persistence": {"passed": True},
                "depth_fill": {
                    "buy": {"complete": True},
                    "sell": {"complete": True},
                },
            }
        },
    )


def _approved_risk() -> RiskDecision:
    return RiskDecision(approved=True, violations=[], risk_score=Decimal("0.20"))


def test_agent_live_gate_blocks_default_config() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    readiness = AgentLiveTradingGate(settings).evaluate(_opportunity("mock", "mock_alt"), _approved_risk())

    assert readiness.ready is False
    assert "agent_trading_disabled" in readiness.reasons
    assert "agent_live_orders_disabled" in readiness.reasons
    assert "live_trading_disabled" in readiness.reasons
    assert "dry_run_enabled" in readiness.reasons
    assert "manual_confirmation_required" in readiness.reasons
    assert "mock_exchange_involved" in readiness.reasons


def test_agent_live_gate_allows_only_after_all_controls_are_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "test-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "test-secret")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_OPERATOR_ID", "operator-1")

    settings = Settings(
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
            audit_log_path=str(tmp_path / "agent-audit.jsonl"),
            policy_id="test-policy",
        ),
    )

    readiness = AgentLiveTradingGate(settings).evaluate(_opportunity(), _approved_risk())

    assert readiness.ready is True
    assert readiness.reasons == []
    assert readiness.checks["operator_id_present"] is True
    assert readiness.checks["credentials_from_environment"]["okx"] is True
    assert readiness.checks["audit_log_configured"] is True


def test_agent_live_gate_allows_okx_demo_orders_without_live_trading(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "demo-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "demo-secret")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "demo-passphrase")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_OPERATOR_ID", "operator-1")

    settings = Settings(
        trading=TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False),
        exchanges={
            "okx": ExchangeConfig(
                enabled=True,
                sandbox=True,
                adapter="okx",
                okx_demo=True,
                api_key_env="QUANTBOT_CRYPTO_OKX_API_KEY",
                api_secret_env="QUANTBOT_CRYPTO_OKX_API_SECRET",
                passphrase_env="QUANTBOT_CRYPTO_OKX_PASSPHRASE",
            )
        },
        agent_trading=AgentTradingConfig(
            enabled=True,
            allow_demo_orders=True,
            allow_live_orders=False,
            strategy_allowlist=["cross-exchange"],
            allowed_exchanges=["okx"],
            max_autonomous_order_value_usdt=Decimal("200"),
            max_autonomous_orders_per_day=3,
            audit_log_path=str(tmp_path / "agent-demo-audit.jsonl"),
        ),
    )

    readiness = AgentLiveTradingGate(settings).evaluate(_opportunity(), _approved_risk())

    assert readiness.ready is True
    assert readiness.reasons == []
    assert readiness.checks["operation_mode"] == "demo"
    assert readiness.checks["agent_demo_orders_allowed"] is True
    assert readiness.checks["agent_live_orders_allowed"] is False


def test_agent_live_gate_kill_switch_blocks_even_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "test-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "test-secret")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "test-passphrase")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_OPERATOR_ID", "operator-1")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_LIVE_KILL_SWITCH", "true")

    settings = Settings(
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
            audit_log_path=str(tmp_path / "agent-audit.jsonl"),
        ),
    )

    readiness = AgentLiveTradingGate(settings).evaluate(_opportunity(), _approved_risk())

    assert readiness.ready is False
    assert "agent_live_kill_switch_enabled" in readiness.reasons


def test_agent_live_audit_log_redacts_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_KEY", "secret-key")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_API_SECRET", "secret-value")
    monkeypatch.setenv("QUANTBOT_CRYPTO_OKX_PASSPHRASE", "secret-passphrase")

    audit_path = tmp_path / "audit" / "agent-live.jsonl"
    settings = Settings(
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
        agent_trading=AgentTradingConfig(audit_log_path=str(audit_path)),
    )

    gate = AgentLiveTradingGate(settings)
    gate.append_audit_event(
        {
            "event": "readiness_checked",
            "api_key": "secret-key",
            "api_secret": "secret-value",
            "passphrase": "secret-passphrase",
            "opportunity_id": "okx-cross",
        }
    )

    content = audit_path.read_text(encoding="utf-8")
    row = json.loads(content)
    assert row["event"] == "readiness_checked"
    assert "secret-key" not in content
    assert "secret-value" not in content
    assert "secret-passphrase" not in content
    assert "***redacted***" in content
