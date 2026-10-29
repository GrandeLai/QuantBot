"""Tests for the paper-only hedged maker strategy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.hedged_maker import HedgedMakerStrategyService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_hedged_maker_emits_paper_quote_and_hedge_opportunity() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    opportunities = HedgedMakerStrategyService(settings, ExchangeFactory(settings)).scan(
        symbol="BTC/USDT",
        maker_exchange="mock",
        hedge_exchange="mock_alt",
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.strategy_type == "hedged-maker"
    assert opportunity.net_profit > Decimal("0")
    assert opportunity.metadata["read_only"] is True
    assert opportunity.metadata["paper_only"] is True
    assert opportunity.metadata["maker_order_type"] == "limit_post_only"
    assert opportunity.metadata["hedge_order_type"] == "taker_market_preview"
    assert opportunity.metadata["maker_quote"]["side"] == "buy"
    assert opportunity.metadata["hedge_preview"]["side"] == "sell"
    assert {leg["role"] for leg in opportunity.metadata["legs"]} == {"maker_quote", "taker_hedge"}


def test_hedged_maker_diagnostics_explain_weak_edge() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.hedged_maker.min_edge_pct = Decimal("10")

    service = HedgedMakerStrategyService(settings, ExchangeFactory(settings))

    assert service.scan(symbol="BTC/USDT", maker_exchange="mock", hedge_exchange="mock_alt") == []
    diagnostics = service.diagnose(symbol="BTC/USDT", maker_exchange="mock", hedge_exchange="mock_alt")
    assert diagnostics["approved"] is False
    assert "edge_below_minimum" in diagnostics["reasons"]
    assert diagnostics["paper_only"] is True
