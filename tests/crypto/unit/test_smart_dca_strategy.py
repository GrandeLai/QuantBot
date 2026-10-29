"""Tests for the paper-only Smart DCA basket strategy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import Settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.smart_dca import SmartDcaStrategyService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_smart_dca_emits_paper_opportunity_for_drawdown_underweight_symbol(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    _force_btc_smart_dca_candidate(settings)

    opportunities = SmartDcaStrategyService(settings, ExchangeFactory(settings)).scan(
        symbol="BTC/USDT",
        exchange="mock",
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.strategy_type == "smart-dca-basket"
    assert opportunity.net_profit > Decimal("0")
    assert opportunity.required_capital >= settings.smart_dca.base_order_usdt
    assert opportunity.metadata["read_only"] is True
    assert opportunity.metadata["paper_only"] is True
    assert opportunity.metadata["demo_supported"] is False
    assert opportunity.metadata["live_supported"] is False
    assert opportunity.metadata["dca_order"]["symbol"] == "BTC/USDT"
    assert opportunity.metadata["dca_order"]["drawdown_pct"] > Decimal("1")
    assert opportunity.metadata["basket"]["rebalance_action"] == "accumulate_underweight"
    assert opportunity.metadata["basket"]["target_weight_pct"] == Decimal("60")
    assert opportunity.metadata["legs"] == [
        {
            "exchange": "mock",
            "symbol": "BTC/USDT",
            "side": "buy",
            "market": "spot",
            "price": opportunity.metadata["dca_order"]["price"],
            "quantity": opportunity.metadata["quantity"],
            "notional_usdt": opportunity.required_capital,
        }
    ]


def test_smart_dca_diagnostics_explain_low_drawdown_filter(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.smart_dca.min_drawdown_pct = Decimal("10")

    service = SmartDcaStrategyService(settings, ExchangeFactory(settings))

    assert service.scan(symbol="BTC/USDT", exchange="mock") == []
    diagnostics = service.diagnose(symbol="BTC/USDT", exchange="mock")
    assert diagnostics["approved"] is False
    assert "drawdown_below_minimum" in diagnostics["reasons"]
    assert diagnostics["symbol"] == "BTC/USDT"
    assert Decimal(str(diagnostics["drawdown_pct"])) < Decimal("10")
    assert diagnostics["read_only"] is True
    assert diagnostics["paper_only"] is True


def _force_btc_smart_dca_candidate(settings: Settings) -> None:
    settings.smart_dca.min_drawdown_pct = Decimal("1")
    settings.smart_dca.drawdown_tiers_pct = [Decimal("1"), Decimal("2"), Decimal("5")]
    settings.smart_dca.tier_multipliers = [Decimal("1"), Decimal("1.5"), Decimal("2")]
    settings.smart_dca.target_weights_pct = {
        "BTC/USDT": Decimal("60"),
        "ETH/USDT": Decimal("25"),
        "SOL/USDT": Decimal("15"),
    }
    settings.smart_dca.rebalance_band_pct = Decimal("5")
    settings.smart_dca.base_order_usdt = Decimal("25")
    settings.smart_dca.max_cycle_quote_usdt = Decimal("75")
    settings.smart_dca.min_depth_usdt = Decimal("1000")


def _use_temp_runtime_paths(settings: Settings, tmp_path: Path) -> None:
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.retrospective_path = str(tmp_path / "strategy-retrospective.md")
    settings.strategy_runtime.retrospective_state_path = str(tmp_path / "strategy-retrospective.state.json")
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "strategy-evolution.state.json")
    settings.strategy_runtime.evolution_report_path = str(tmp_path / "strategy-evolution.md")
