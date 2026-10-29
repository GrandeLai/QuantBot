"""Unit tests for the end-to-end monetization workflow route."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.workflow.route import TradingRouteWorkflow


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_workflow_runs_mock_to_live_readiness_route_without_live_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "QUANTBOT_CRYPTO_AGENT_TRADING_ENABLED",
        "QUANTBOT_CRYPTO_AGENT_ALLOW_DEMO_ORDERS",
        "QUANTBOT_CRYPTO_AGENT_ALLOW_LIVE_ORDERS",
        "QUANTBOT_CRYPTO_AGENT_OPERATOR_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings(EXAMPLE_CONFIG)
    result = TradingRouteWorkflow(settings, ExchangeFactory(settings)).run(symbol="BTC/USDT")
    payload = result.to_dict()

    stage_names = [stage["name"] for stage in payload["stages"]]
    assert stage_names == [
        "config",
        "mock_market_data",
        "arbitrage_scan",
        "backtest",
        "paper_trading",
        "sandbox_readiness",
        "live_readiness",
        "agent_live_readiness",
        "report",
    ]
    assert payload["completed"] is True
    assert payload["live_ready"] is False
    assert payload["paper_trading"]["execution"]["status"] == "simulated"
    assert payload["paper_trading"]["ledger"]["trade_count"] == 1
    selected = payload["opportunities"]["cross-exchange"][0]
    assert selected["metadata"]["execution_quality"]["spread_persistence"]["passed"] is True
    assert selected["metadata"]["execution_quality"]["latency_drift_usdt"] != "0"
    assert payload["sandbox_readiness"]["ready"] is True
    assert payload["sandbox_readiness"]["checks"]["execution_quality_approved"] is True
    assert payload["live_readiness"]["ready"] is False
    assert "live_trading_disabled" in payload["live_readiness"]["reasons"]
    assert "dry_run_enabled" in payload["live_readiness"]["reasons"]
    assert payload["agent_live_readiness"]["ready"] is False
    assert "agent_trading_disabled" in payload["agent_live_readiness"]["reasons"]
