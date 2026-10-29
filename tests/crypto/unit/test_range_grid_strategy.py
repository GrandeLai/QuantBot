"""Tests for the paper-only range-grid strategy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import Settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.range_grid import RangeGridStrategyService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_range_grid_emits_paper_opportunity_for_range_regime(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    _force_mock_btc_range(settings)

    opportunities = RangeGridStrategyService(settings, ExchangeFactory(settings)).scan(
        symbol="BTC/USDT",
        exchange="mock",
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.strategy_type == "range-grid"
    assert opportunity.net_profit > Decimal("0")
    assert opportunity.required_capital == settings.range_grid.total_quote_usdt
    assert opportunity.metadata["read_only"] is True
    assert opportunity.metadata["paper_only"] is True
    assert opportunity.metadata["regime"] == "range"
    assert len(opportunity.metadata["grid_levels"]) == settings.range_grid.grid_levels
    assert {leg["side"] for leg in opportunity.metadata["legs"]} == {"buy", "sell"}


def test_range_grid_diagnostics_explain_non_range_regime(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.universe.trend_return_threshold_pct = Decimal("1")

    service = RangeGridStrategyService(settings, ExchangeFactory(settings))

    assert service.scan(symbol="BTC/USDT", exchange="mock") == []
    diagnostics = service.diagnose(symbol="BTC/USDT", exchange="mock")
    assert diagnostics["approved"] is False
    assert "regime_not_range" in diagnostics["reasons"]
    assert diagnostics["regime"] == "trend"


def _force_mock_btc_range(settings: Settings) -> None:
    settings.universe.trend_return_threshold_pct = Decimal("100")
    settings.universe.range_volatility_max_pct = Decimal("10")
    settings.range_grid.max_range_width_pct = Decimal("20")
    settings.range_grid.min_grid_spacing_pct = Decimal("0.50")


def _use_temp_runtime_paths(settings: Settings, tmp_path: Path) -> None:
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.retrospective_path = str(tmp_path / "strategy-retrospective.md")
    settings.strategy_runtime.retrospective_state_path = str(tmp_path / "strategy-retrospective.state.json")
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "strategy-evolution.state.json")
    settings.strategy_runtime.evolution_report_path = str(tmp_path / "strategy-evolution.md")
