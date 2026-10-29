"""Unit tests for strategy platform catalog, scoring, and portfolio services."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import Settings
from quantbot.crypto.runtime.exchanges.base import AccountSnapshot, Balance, Exchange, OrderBook, SpotPerpQuote, Ticker, utcnow
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.crypto.runtime.arbitrage.route_discovery import TriangularRouteDiscoveryService
from quantbot.crypto.runtime.strategies.carry_basis_optimizer import CarryBasisOptimizationService
from quantbot.crypto.runtime.strategies.market_compare import StrategyMarketComparisonService
from quantbot.crypto.runtime.strategies.opportunity_density import OpportunityDensityService
from quantbot.crypto.runtime.strategies.platform import StrategyCatalog, StrategyController, StrategyPortfolioService, StrategyScoreService
from quantbot.crypto.runtime.strategies.registry import StrategyRegistry


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_strategy_catalog_registers_new_strategies_and_compatibility_aliases() -> None:
    catalog = StrategyCatalog().to_dict()
    registry = StrategyRegistry()

    names = {strategy["name"] for strategy in catalog["strategies"]}

    assert names >= {
        "triangular-multi-route",
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
        "range-grid",
        "hedged-maker",
        "smart-dca-basket",
    }
    assert catalog["aliases"]["triangular"] == "triangular-multi-route"
    assert catalog["aliases"]["funding-rate"] == "funding-carry-hedged"
    assert catalog["aliases"]["spot-perp"] == "spot-perp-carry"
    assert catalog["aliases"]["smart-dca"] == "smart-dca-basket"
    assert registry.get("triangular").scanner_type == "triangular-multi-route"
    assert registry.demo_validation_names()[:5] == [
        "cross-exchange",
        "triangular",
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
    ]
    assert "trend-breakout" in registry.demo_validation_names()
    assert "orderbook-imbalance-scalp" not in registry.demo_validation_names()
    assert "range-grid" not in registry.demo_validation_names()
    assert "hedged-maker" not in registry.demo_validation_names()
    assert "smart-dca-basket" not in registry.demo_validation_names()


def test_strategy_controller_scans_all_enabled_strategies(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    controller = StrategyController(settings, ExchangeFactory(settings))

    reports = controller.scan(strategy_name="all", symbol="BTC/USDT")

    assert {report.strategy_name for report in reports} >= {
        "cross-exchange",
        "triangular-multi-route",
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
        "trend-breakout",
        "mean-reversion-spot",
        "volatility-squeeze-breakout",
        "momentum-rotation",
        "orderbook-imbalance-scalp",
        "range-grid",
        "hedged-maker",
        "smart-dca-basket",
    }
    assert all(report.status == "active" for report in reports)
    assert sum(len(report.opportunities) for report in reports) >= 5


def test_strategy_controller_scans_range_grid_when_regime_is_range(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    _force_mock_btc_range(settings)

    report = StrategyController(settings, ExchangeFactory(settings)).scan(
        strategy_name="range-grid",
        symbol="BTC/USDT",
        exchange="mock",
    )[0]

    assert report.strategy_name == "range-grid"
    assert report.opportunities
    assert report.opportunities[0].metadata["paper_only"] is True
    assert report.diagnostics["approved"] is True
    assert report.diagnostics["regime"] == "range"


def test_strategy_controller_scans_smart_dca_basket(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.smart_dca.min_drawdown_pct = Decimal("1")
    settings.smart_dca.target_weights_pct = {
        "BTC/USDT": Decimal("60"),
        "ETH/USDT": Decimal("25"),
        "SOL/USDT": Decimal("15"),
    }

    report = StrategyController(settings, ExchangeFactory(settings)).scan(
        strategy_name="smart-dca-basket",
        symbol="BTC/USDT",
        exchange="mock",
    )[0]

    assert report.strategy_name == "smart-dca-basket"
    assert report.opportunities
    assert report.opportunities[0].metadata["paper_only"] is True
    assert report.diagnostics["approved"] is True
    assert report.diagnostics["basket"]["rebalance_action"] == "accumulate_underweight"


def test_triangular_route_discovery_expands_and_explains_mock_routes(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)

    report = TriangularRouteDiscoveryService(settings, ExchangeFactory(settings)).discover(
        exchange_name="mock",
        quote="USDT",
        route_limit=10,
    )
    scan = StrategyController(settings, ExchangeFactory(settings)).scan(
        strategy_name="triangular-multi-route",
        exchange="mock",
        route_mode="discovered",
    )[0]

    accepted_routes = [candidate.route for candidate in report.accepted_routes]
    filtered_reasons = [reason for candidate in report.filtered_routes for reason in candidate.reasons]
    assert ["USDT", "BTC", "ETH", "USDT"] in accepted_routes
    assert ["USDT", "BTC", "SOL", "USDT"] in accepted_routes
    assert any(reason.startswith("missing_symbol:") for reason in filtered_reasons)
    assert report.read_only is True
    assert report.orders_sent is False
    assert scan.opportunities


def test_opportunity_density_report_aggregates_expected_actual_and_break_even_gaps(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    journal_path = Path(settings.strategy_runtime.journal_path)
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "created_at": utcnow().isoformat(),
                        "strategy_name": "triangular-multi-route",
                        "execution_mode": "demo",
                        "decision": "executed",
                        "opportunities_found": 1,
                        "selected_opportunity_id": "okx-demo-triangular",
                        "risk_reasons": [],
                        "net_profit": "0.12",
                        "execution": {
                            "preflight": {"approved": True, "net_pnl_usdt": "0.10"},
                            "pnl_validation": {
                                "cash_flow_net_pnl_usdt": "0.12",
                                "equity_delta_usdt": "0.11",
                                "exchange_receipts": {"complete": True},
                                "within_tolerance": True,
                                "residual_inventory_within_tolerance": True,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "created_at": utcnow().isoformat(),
                        "strategy_name": "spot-perp-carry",
                        "execution_mode": "demo",
                        "decision": "skipped",
                        "opportunities_found": 1,
                        "selected_opportunity_id": "okx-demo-spot-perp-carry",
                        "risk_reasons": ["demo_preflight_not_profitable"],
                        "net_profit": "-0.08",
                        "execution": {"preflight": {"approved": False, "net_pnl_usdt": "-0.08"}},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = OpportunityDensityService(settings.strategy_runtime).report(window="24h")

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.scan_count == 2
    assert report.opportunity_count == 2
    assert report.demo_preflight_candidate_count == 1
    assert report.executed_count == 1
    assert report.expected_vs_actual_gap_usdt == Decimal("0.10")
    assert report.account_equity_delta_usdt == Decimal("0.11")
    assert report.by_strategy["spot-perp-carry"].break_even_gap_usdt == Decimal("0.08")
    assert report.demo_size_stage_readiness["stage_1_2x_eligible"] is False


def test_strategy_scan_reports_break_even_diagnostics_when_carry_filtered(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.arbitrage.min_net_profit_pct = Decimal("100")

    report = StrategyController(settings, ExchangeFactory(settings)).scan(
        strategy_name="spot-perp-carry",
        symbol="BTC/USDT",
    )[0]
    card = StrategyScoreService(settings, ExchangeFactory(settings)).score(
        strategy_name="spot-perp-carry",
        symbol="BTC/USDT",
    )[0]

    assert report.opportunities == []
    assert report.diagnostics["approved"] is False
    assert "net_profit_below_minimum" in report.diagnostics["reasons"]
    assert Decimal(str(report.diagnostics["break_even_gap_usdt"])) > 0
    assert "scan_diagnostic:net_profit_below_minimum" in card.reasons
    assert any(reason.startswith("scan_break_even_gap_usdt:") for reason in card.reasons)


def test_carry_basis_quality_gates_explain_basis_and_hedge_cost_filters(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.arbitrage.spot_perp_min_basis_pct = Decimal("99")
    settings.arbitrage.funding_max_basis_hedge_cost_pct = Decimal("0.01")
    controller = StrategyController(settings, ExchangeFactory(settings))

    spot_report = controller.scan(strategy_name="spot-perp-carry", symbol="BTC/USDT")[0]
    funding_report = controller.scan(strategy_name="funding-carry-hedged", symbol="BTC/USDT")[0]

    assert spot_report.opportunities == []
    assert "basis_below_minimum" in spot_report.diagnostics["reasons"]
    assert spot_report.diagnostics["min_basis_pct"] == Decimal("99")
    funding_candidates = funding_report.diagnostics["candidates"]
    assert any("basis_hedge_cost_above_maximum" in candidate["reasons"] for candidate in funding_candidates)
    assert all("basis_hedge_cost_pct" in candidate for candidate in funding_candidates)


def test_carry_basis_optimization_reports_break_even_gaps_without_trading(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.arbitrage.min_net_profit_pct = Decimal("100")

    report = CarryBasisOptimizationService(settings, ExchangeFactory(settings)).report(symbol="BTC/USDT")

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.live_orders_sent is False
    assert report.summary["blocked_count"] >= 1
    spot = next(card for card in report.cards if card.strategy_name == "spot-perp-carry")
    assert spot.demo_ready is False
    assert spot.break_even_gap_usdt > Decimal("0")
    assert "keep_demo_blocked_until_break_even_gap_closes" in spot.recommended_actions
    assert "arbitrage.spot_perp_min_basis_pct" in spot.suggested_config_fields
    assert "net_profit_below_minimum" in spot.reasons


def test_carry_basis_optimization_merges_target_market_unlock_diagnostics(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.exchanges["okx"].enabled = True

    report = CarryBasisOptimizationService(
        settings,
        ExchangeFactory(settings),
        market_compare_factory_cls=TargetOKXFactory,
    ).report(symbol="BTC/USDT", target_exchange="okx")
    spot = next(card for card in report.cards if card.strategy_name == "spot-perp-carry")

    assert report.target_exchange == "okx"
    assert spot.target_exchange == "okx"
    assert spot.target_verdict == "demo_preflight_candidate"
    assert spot.unlock_priority == "demo_candidate"
    assert spot.target_best_net_profit_usdt > Decimal("0")
    assert spot.target_delta_net_profit_usdt > Decimal("0")
    assert "target_has_positive_edge" in spot.target_reasons
    assert report.summary["target_demo_preflight_candidate_count"] >= 1
    assert report.summary["high_priority_unlock_count"] >= 1
    assert report.orders_sent is False
    assert report.live_orders_sent is False


def test_carry_basis_optimization_sweeps_multiple_symbols_and_ranks_unlocks(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.exchanges["okx"].enabled = True

    sweep = CarryBasisOptimizationService(
        settings,
        ExchangeFactory(settings),
        market_compare_factory_cls=TargetOKXFactory,
    ).sweep(symbols=["BTC/USDT", "ETH/USDT"], target_exchange="okx")
    payload = sweep.to_dict()

    assert payload["mode"] == "sweep"
    assert payload["read_only"] is True
    assert payload["orders_sent"] is False
    assert payload["live_orders_sent"] is False
    assert payload["symbols"] == ["BTC/USDT", "ETH/USDT"]
    assert payload["target_exchange"] == "okx"
    assert len(payload["reports"]) == 2
    assert payload["summary"]["symbol_count"] == 2
    assert payload["summary"]["card_count"] == 6
    assert payload["summary"]["target_demo_preflight_candidate_count"] >= 1
    assert payload["summary"]["high_priority_unlock_count"] >= 1
    assert payload["ranked_cards"][0]["unlock_priority"] == "demo_candidate"
    assert Decimal(payload["ranked_cards"][0]["quality_score"]) >= Decimal("80")
    assert payload["ranked_cards"][0]["quality_bucket"] == "high_quality"
    assert payload["summary"]["high_quality_candidate_count"] >= 1
    assert {card["symbol"] for card in payload["ranked_cards"]} == {"BTC/USDT", "ETH/USDT"}
    for report in payload["reports"]:
        assert {card["symbol"] for card in report["cards"]} == {report["symbol"]}

    filtered = CarryBasisOptimizationService(
        settings,
        ExchangeFactory(settings),
        market_compare_factory_cls=TargetOKXFactory,
    ).sweep(symbols=["BTC/USDT", "ETH/USDT"], target_exchange="okx", min_quality_score=Decimal("80"))
    filtered_payload = filtered.to_dict()

    assert filtered_payload["summary"]["min_quality_score"] == "80"
    assert filtered_payload["summary"]["filtered_out_count"] >= 1
    assert all(Decimal(card["quality_score"]) >= Decimal("80") for card in filtered_payload["ranked_cards"])


def test_carry_basis_optimization_sweep_respects_observation_budget(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)

    sweep = CarryBasisOptimizationService(
        settings,
        ExchangeFactory(settings),
    ).sweep(
        symbols=["BTC/USDT", "ETH/USDT"],
        request_budget_seconds=Decimal("0"),
        per_symbol_timeout_seconds=Decimal("0.01"),
    )
    payload = sweep.to_dict()

    assert payload["reports"] == []
    assert payload["ranked_cards"] == []
    assert payload["summary"]["requested_symbol_count"] == 2
    assert payload["summary"]["symbol_count"] == 0
    assert payload["summary"]["skipped_symbol_count"] == 2
    assert payload["summary"]["request_budget_seconds"] == "0"
    assert [observation["status"] for observation in payload["observations"]] == [
        "skipped_request_budget",
        "skipped_request_budget",
    ]
    assert all("request_budget_exhausted" in observation["reasons"] for observation in payload["observations"])


def test_carry_basis_optimization_sweep_classifies_symbol_timeouts(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    service = CarryBasisOptimizationService(settings, ExchangeFactory(settings))
    original_report = service.report

    def slow_report(symbol: str = "BTC/USDT", target_exchange: str | None = None):  # noqa: ANN202
        time.sleep(0.05)
        return original_report(symbol=symbol, target_exchange=target_exchange)

    service.report = slow_report  # type: ignore[method-assign]

    sweep = service.sweep(
        symbols=["BTC/USDT", "ETH/USDT"],
        request_budget_seconds=Decimal("1"),
        per_symbol_timeout_seconds=Decimal("0.001"),
    )
    payload = sweep.to_dict()

    assert payload["reports"] == []
    assert payload["summary"]["timeout_symbol_count"] == 2
    assert [observation["status"] for observation in payload["observations"]] == ["timed_out", "timed_out"]
    assert all("per_symbol_timeout_exceeded" in observation["reasons"] for observation in payload["observations"])


def test_strategy_market_compare_is_read_only_and_explains_target_delta(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.exchanges["okx"].enabled = True
    service = StrategyMarketComparisonService(settings, factory_cls=TargetOKXFactory)

    result = service.compare(strategy_name="spot-perp-carry", symbol="BTC/USDT")
    comparison = result.comparisons[0]

    assert result.read_only is True
    assert result.orders_sent is False
    assert result.baseline_exchange == "mock"
    assert result.target_exchange == "okx"
    assert comparison.target.exchange == "okx"
    assert comparison.delta_net_profit_usdt > Decimal("0")
    assert comparison.verdict == "demo_preflight_candidate"
    assert "target_has_positive_edge" in comparison.reasons


def test_strategy_score_and_portfolio_select_highest_scored_strategies(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.strategy_runtime.portfolio_max_concurrent_strategies = 2
    settings.strategy_runtime.portfolio_min_score = Decimal("50")
    factory = ExchangeFactory(settings)

    cards = StrategyScoreService(settings, factory).score(strategy_name="all", symbol="BTC/USDT")
    status = StrategyPortfolioService(settings, factory).status(symbol="BTC/USDT")

    assert len(cards) >= 5
    assert all(card.score >= Decimal("0") for card in cards)
    assert len(status.selected_strategies) == 2
    assert "triangular-multi-route" in status.selected_strategies
    assert status.blocked_strategies


def test_strategy_score_blocks_cross_exchange_when_sell_asset_is_not_free(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    factory = LockedSellBalanceFactory(settings)

    card = StrategyScoreService(settings, factory).score(strategy_name="cross-exchange", symbol="BTC/USDT")[0]

    assert card.sell_leg_available is False
    assert "sell_leg_unavailable" in card.reasons
    assert "sell_leg_no_free_balance:mock_alt:BTC" in card.balance_restrictions
    assert "balance_locked:mock_alt:BTC" in card.balance_restrictions


def test_strategy_score_penalizes_repeated_demo_preflight_pressure(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    state_path = tmp_path / "retrospective.state.json"
    _use_temp_runtime_paths(settings, tmp_path)
    settings.strategy_runtime.retrospective_state_path = str(state_path)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-05-10T00:00:00+00:00",
                "recent_runs": [],
                "issues": {
                    "spot-perp-carry|demo|preflight_not_profitable|demo_preflight_not_profitable": {
                        "issue_id": "ri-test",
                        "dedupe_key": "spot-perp-carry|demo|preflight_not_profitable|demo_preflight_not_profitable",
                        "strategy_name": "spot-perp-carry",
                        "execution_mode": "demo",
                        "category": "preflight_not_profitable",
                        "reason": "demo_preflight_not_profitable",
                        "severity": "warning",
                        "status": "open",
                        "occurrences": 10,
                        "first_seen_at": "2026-05-10T00:00:00+00:00",
                        "last_seen_at": "2026-05-10T00:00:00+00:00",
                        "recommendation": "Review spread and cost thresholds before another demo attempt.",
                        "last_context": {"net_profit": "-0.258630", "decision": "skipped"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    card = StrategyScoreService(settings, ExchangeFactory(settings)).score(
        strategy_name="spot-perp-carry",
        symbol="BTC/USDT",
    )[0]

    assert card.score < settings.strategy_runtime.portfolio_min_score
    assert "repeated_demo_preflight_not_profitable" in card.reasons
    assert "demo_break_even_gap_usdt:0.258630" in card.reasons


def test_strategy_score_and_portfolio_block_archived_strategy(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    state_path = tmp_path / "evolution.state.json"
    settings.strategy_runtime.evolution_state_path = str(state_path)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "strategies": {
                    "triangular-multi-route": {
                        "strategy_name": "triangular-multi-route",
                        "status": "archived",
                        "action": "archive",
                        "reason_codes": ["net_profit_below_threshold"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    card = StrategyScoreService(settings, ExchangeFactory(settings)).score(
        strategy_name="triangular-multi-route",
        symbol="BTC/USDT",
    )[0]
    status = StrategyPortfolioService(settings, ExchangeFactory(settings)).status(
        strategy_name="triangular-multi-route",
        symbol="BTC/USDT",
    )

    assert card.score == Decimal("0")
    assert "strategy_archived_by_evolution" in card.reasons
    assert status.selected_strategies == []
    assert status.blocked_strategies[0]["strategy_name"] == "triangular-multi-route"


def _use_temp_runtime_paths(settings: Settings, tmp_path: Path) -> None:
    strategy_runtime = settings.strategy_runtime
    strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    strategy_runtime.retrospective_path = str(tmp_path / "retrospective.md")
    strategy_runtime.retrospective_state_path = str(tmp_path / "retrospective.state.json")
    strategy_runtime.evolution_state_path = str(tmp_path / "evolution.state.json")
    strategy_runtime.evolution_report_path = str(tmp_path / "evolution.md")


def _force_mock_btc_range(settings: Settings) -> None:
    settings.universe.trend_return_threshold_pct = Decimal("100")
    settings.universe.range_volatility_max_pct = Decimal("10")
    settings.range_grid.max_range_width_pct = Decimal("20")
    settings.range_grid.min_grid_spacing_pct = Decimal("0.50")


class LockedSellBalanceFactory(ExchangeFactory):
    """Factory that returns a mock sell exchange with no free BTC."""

    def get(self, name: str) -> Exchange:
        if name == "mock_alt":
            return LockedSellMockExchange(name=name)
        return super().get(name)


class LockedSellMockExchange(MockExchange):
    """Mock exchange with sell asset locked instead of free."""

    def get_balances(self) -> AccountSnapshot:
        return AccountSnapshot(
            exchange=self.name,
            balances={
                "BTC": Balance(asset="BTC", total=Decimal("1"), free=Decimal("0"), locked=Decimal("1")),
                "USDT": Balance(asset="USDT", total=Decimal("10000"), free=Decimal("10000"), locked=Decimal("0")),
            },
            timestamp=utcnow(),
        )


class TargetOKXFactory(ExchangeFactory):
    """Factory that returns an OKX-like mock with a stronger spot/perp basis."""

    def get(self, name: str) -> Exchange:
        if name == "okx":
            return TargetOKXMockExchange(name=name)
        return super().get(name)


class TargetOKXMockExchange(MockExchange):
    """OKX-shaped exchange fixture for read-only market comparison."""

    def get_ticker(self, symbol: str) -> Ticker:
        ticker = MockExchange("mock").get_ticker(symbol)
        return replace(ticker, exchange=self.name)

    def get_orderbook(self, symbol: str) -> OrderBook:
        orderbook = MockExchange("mock").get_orderbook(symbol)
        return replace(orderbook, exchange=self.name)

    def get_spot_perp_quote(self, symbol: str) -> SpotPerpQuote:
        ticker = self.get_ticker(symbol)
        return SpotPerpQuote(
            exchange=self.name,
            symbol=ticker.symbol,
            spot_bid=ticker.bid,
            spot_ask=ticker.ask,
            perp_bid=ticker.bid + Decimal("550"),
            perp_ask=ticker.ask + Decimal("560"),
            funding_rate=Decimal("0.0008"),
        )
