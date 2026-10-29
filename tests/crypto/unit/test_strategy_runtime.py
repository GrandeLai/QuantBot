"""Unit tests for production strategy runtime controls."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import AgentTradingConfig, ExchangeConfig, StrategyRuntimeConfig, TradingConfig
from quantbot.crypto.runtime.exceptions import ExchangeError
from quantbot.crypto.runtime.exchanges.base import Candle, utcnow
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.candidate_backtest import StrategyCandidateBacktestService
from quantbot.crypto.runtime.strategies.market_regime import classify_candle_regime
from quantbot.crypto.runtime.strategies.policy import StrategyPolicy
from quantbot.crypto.runtime.strategies.preflight_buffer import AdaptivePreflightBufferService
from quantbot.crypto.runtime.strategies.registry import StrategyRegistry
from quantbot.crypto.runtime.strategies.retrospective import MANUAL_END, MANUAL_START, StrategyRetrospectiveService
from quantbot.crypto.runtime.strategies.revival import StrategyRevivalWindowService
from quantbot.crypto.runtime.strategies.review import StrategyReviewService
from quantbot.crypto.runtime.strategies.runner import StrategyRunner
from quantbot.crypto.runtime.strategies.demo_execution import StrategyDemoExecutionService
from quantbot.crypto.runtime.strategies.demo_validation import StrategyDemoValidationService
from quantbot.crypto.runtime.strategies.evolution import StrategyEvolutionService
from quantbot.crypto.runtime.strategies.guard import StrategyRuntimeGuard
from quantbot.crypto.runtime.strategies.validation_report import StrategyValidationReportService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_strategy_registry_lists_existing_arbitrage_strategies() -> None:
    registry = StrategyRegistry()

    strategies = registry.list()

    assert [strategy.name for strategy in strategies][:5] == [
        "cross-exchange",
        "triangular-multi-route",
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
    ]
    assert {strategy.name for strategy in strategies} >= {
        "trend-breakout",
        "mean-reversion-spot",
        "volatility-squeeze-breakout",
        "momentum-rotation",
        "orderbook-imbalance-scalp",
    }
    assert all(strategy.default_execution_mode == "paper" for strategy in strategies)
    assert registry.get("triangular").scanner_type == "triangular-multi-route"
    assert registry.get("all").name == "all"


def test_strategy_policy_blocks_when_position_cap_is_too_low() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.max_position_value_usdt = Decimal("1")
    opportunity = StrategyRunner(settings, ExchangeFactory(settings)).scan_once("cross-exchange")[0]

    decision = StrategyPolicy(settings.strategy_runtime).evaluate("cross-exchange", opportunity)

    assert decision.approved is False
    assert "max_position_value_usdt" in decision.reasons


def test_strategy_policy_uses_projected_order_and_capital_budget() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.max_open_orders_per_strategy = 2
    settings.strategy_runtime.max_strategy_capital_usdt = Decimal("250")
    opportunity = StrategyRunner(settings, ExchangeFactory(settings)).scan_once("cross-exchange")[0]

    capital_block = StrategyPolicy(settings.strategy_runtime).evaluate(
        "cross-exchange",
        opportunity,
        active_orders=1,
        active_capital_usdt=Decimal("175"),
        additional_orders=1,
        additional_capital_usdt=Decimal("100"),
    )
    order_block = StrategyPolicy(settings.strategy_runtime).evaluate(
        "cross-exchange",
        opportunity,
        active_orders=2,
        active_capital_usdt=Decimal("0"),
        additional_orders=1,
        additional_capital_usdt=Decimal("0"),
    )

    assert capital_block.approved is False
    assert "max_strategy_capital_usdt" in capital_block.reasons
    assert capital_block.active_capital_usdt == Decimal("175")
    assert capital_block.projected_strategy_capital_usdt == Decimal("275")
    assert capital_block.projected_open_orders == 2
    assert order_block.approved is False
    assert "max_open_orders_per_strategy" in order_block.reasons
    assert order_block.projected_open_orders == 3


def test_strategy_runner_executes_all_strategies_and_records_journal(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    runner = StrategyRunner(settings, ExchangeFactory(settings))

    result = runner.run(strategy_name="all", max_cycles=1, interval_seconds=0, execution_mode="paper")

    assert result.completed is True
    assert result.execution_mode == "paper"
    assert result.cycles_completed == 1
    assert {item.strategy_name for item in result.results} >= {
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
    }
    assert any(item.decision == "executed" for item in result.results)
    assert (tmp_path / "strategy-events.jsonl").exists()
    assert "api_secret" not in (tmp_path / "strategy-events.jsonl").read_text(encoding="utf-8")


def test_strategy_runner_executes_demo_mode_through_demo_executor(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=FakeDemoExecutor())

    result = runner.run(strategy_name="cross-exchange", max_cycles=1, interval_seconds=0, execution_mode="demo")

    assert result.results[0].decision == "executed"
    assert result.results[0].risk_reasons == []
    assert result.results[0].execution is not None
    assert result.results[0].execution["demo_orders_sent"] is True
    assert result.results[0].execution["net_pnl_usdt"] == "0.42"
    assert result.results[0].execution["preflight"]["net_pnl_usdt"] == "0.42"
    assert "demo_strategy_broker_not_wired" not in (tmp_path / "strategy-events.jsonl").read_text(encoding="utf-8")


def test_strategy_runner_skips_unprofitable_demo_preflight_without_ordering(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    demo_executor = FakePreflightDemoExecutor(approved=False, net_pnl="-0.01")
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=demo_executor)

    result = runner.run(strategy_name="cross-exchange", max_cycles=1, interval_seconds=0, execution_mode="demo")

    assert result.results[0].decision == "skipped"
    assert result.results[0].risk_reasons == ["demo_preflight_not_profitable"]
    assert result.results[0].net_profit == Decimal("-0.01")
    assert demo_executor.execute_calls == []


def test_strategy_runner_blocks_demo_when_adaptive_preflight_buffer_not_met(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.demo_preflight_adaptive_buffer_enabled = True
    settings.strategy_runtime.demo_preflight_adaptive_buffer_min_samples = 2
    settings.strategy_runtime.demo_preflight_adaptive_buffer_quantile_pct = Decimal("100")
    settings.strategy_runtime.demo_preflight_adaptive_buffer_lookback = 10
    _write_preflight_gap_samples(Path(settings.strategy_runtime.journal_path), "cross-exchange")
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    demo_executor = FakePreflightDemoExecutor(approved=True, net_pnl="0.03")
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=demo_executor)

    result = runner.run(strategy_name="cross-exchange", max_cycles=1, interval_seconds=0, execution_mode="demo")

    preflight = result.results[0].execution["preflight"]
    assert result.results[0].decision == "skipped"
    assert result.results[0].risk_reasons == ["demo_preflight_buffer_not_met"]
    assert Decimal(str(preflight["min_required_net_pnl_usdt"])) == Decimal("0.040000")
    assert preflight["adaptive_preflight_buffer"]["sample_count"] == 3
    assert preflight["adaptive_preflight_buffer"]["buffer_usdt"] == "0.040000"
    assert demo_executor.execute_calls == []


def test_strategy_runner_allows_demo_when_adaptive_preflight_buffer_is_met(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.demo_preflight_adaptive_buffer_enabled = True
    settings.strategy_runtime.demo_preflight_adaptive_buffer_min_samples = 2
    settings.strategy_runtime.demo_preflight_adaptive_buffer_quantile_pct = Decimal("100")
    settings.strategy_runtime.demo_preflight_adaptive_buffer_lookback = 10
    _write_preflight_gap_samples(Path(settings.strategy_runtime.journal_path), "cross-exchange")
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    demo_executor = FakePreflightDemoExecutor(approved=True, net_pnl="0.05")
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=demo_executor)

    result = runner.run(strategy_name="cross-exchange", max_cycles=1, interval_seconds=0, execution_mode="demo")

    preflight = result.results[0].execution["preflight"]
    assert result.results[0].decision == "executed"
    assert result.results[0].risk_reasons == []
    assert Decimal(str(preflight["min_required_net_pnl_usdt"])) == Decimal("0.040000")
    assert preflight["adaptive_preflight_buffer"]["buffer_usdt"] == "0.040000"
    assert demo_executor.execute_calls == ["cross-exchange"]


def test_adaptive_preflight_buffer_skips_directional_lifecycle_previews(tmp_path: Path) -> None:
    config = StrategyRuntimeConfig(journal_path=str(tmp_path / "strategy-events.jsonl"))
    config.demo_preflight_adaptive_buffer_enabled = True
    config.demo_preflight_adaptive_buffer_min_samples = 1
    _write_preflight_gap_samples(Path(config.journal_path), "trend-breakout")
    service = AdaptivePreflightBufferService(config)

    preview = service.apply(
        "trend-breakout",
        {
            "strategy_name": "trend-breakout",
            "strategy_family": "directional",
            "approved": True,
            "reason": "directional_entry_gate_passed",
            "net_pnl_usdt": "0.000000",
            "min_required_net_pnl_usdt": "0.000000",
        },
    )

    assert preview["approved"] is True
    assert preview["reason"] == "directional_entry_gate_passed"
    assert preview["adaptive_preflight_buffer"]["applied"] is False
    assert preview["adaptive_preflight_buffer"]["reason"] == "directional_preview"


def test_strategy_runner_requires_local_validation_before_demo_orders(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.risk.max_order_value_usdt = Decimal("1")
    demo_executor = FakePreflightDemoExecutor(approved=True, net_pnl="0.42")
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=demo_executor)

    result = runner.run(strategy_name="cross-exchange", max_cycles=1, interval_seconds=0, execution_mode="demo")

    assert result.results[0].decision == "skipped"
    assert result.results[0].risk_reasons[0] == "local_validation_not_passed"
    assert demo_executor.execute_calls == []


def test_strategy_runner_stops_demo_loop_when_stop_loss_trips(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.demo_require_profitable_preflight = False
    settings.strategy_runtime.demo_stop_loss_usdt = Decimal("0.50")
    settings.strategy_runtime.demo_max_drawdown_usdt = Decimal("10")
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=FakeSequenceDemoExecutor(["-0.30", "-0.30", "1.00"]))

    result = runner.run(strategy_name="triangular", max_cycles=5, interval_seconds=0, execution_mode="demo")

    assert result.cycles_completed == 2
    assert result.stopped_reason == "demo_stop_loss"
    assert [item.net_profit for item in result.results] == [Decimal("-0.30"), Decimal("-0.30")]


def test_strategy_runner_persists_runtime_guard_after_consecutive_demo_losses(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.demo_require_profitable_preflight = False
    settings.strategy_runtime.demo_stop_loss_usdt = Decimal("10")
    settings.strategy_runtime.demo_max_drawdown_usdt = Decimal("10")
    settings.strategy_runtime.max_consecutive_losses = 2
    settings.strategy_runtime.failure_cooldown_seconds = 300
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)

    first_runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=FakeSequenceDemoExecutor(["-0.10", "-0.20"]))
    first_result = first_runner.run(strategy_name="triangular", max_cycles=2, interval_seconds=0, execution_mode="demo")

    assert [item.decision for item in first_result.results] == ["executed", "executed"]
    guard_status = StrategyRuntimeGuard(settings.strategy_runtime).status(strategy_names=["triangular"], execution_mode="demo")
    triangular_guard = guard_status["entries"][0]
    assert triangular_guard["consecutive_losses"] == 2
    assert triangular_guard["cooldown_active"] is True

    demo_executor = FakeSequenceDemoExecutor(["1.00"])
    second_runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=demo_executor)
    second_result = second_runner.run(strategy_name="triangular", max_cycles=1, interval_seconds=0, execution_mode="demo")

    assert second_result.results[0].decision == "skipped"
    assert second_result.results[0].risk_reasons == ["runtime_guard_cooldown"]
    assert second_result.results[0].execution is not None
    assert second_result.results[0].execution["runtime_guard"]["cooldown_active"] is True
    assert demo_executor.index == 0


def test_strategy_runtime_guard_resets_after_profitable_execution(tmp_path: Path) -> None:
    config = StrategyRuntimeConfig(
        runtime_guard_path=str(tmp_path / "strategy-runtime-guard.json"),
        max_consecutive_losses=2,
        failure_cooldown_seconds=300,
    )
    guard = StrategyRuntimeGuard(config)

    guard.record("triangular", "demo", decision="executed", net_profit=Decimal("-0.10"), reasons=["negative_pnl"])
    guard.record("triangular", "demo", decision="executed", net_profit=Decimal("0.20"), reasons=[])

    status = guard.status(strategy_names=["triangular"], execution_mode="demo")["entries"][0]
    assert status["consecutive_failures"] == 0
    assert status["consecutive_losses"] == 0
    assert status["cooldown_active"] is False


def test_strategy_runtime_guard_cools_down_after_rate_limit_scan_errors(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.max_consecutive_rate_limit_failures = 1
    settings.strategy_runtime.failure_cooldown_seconds = 300
    exchanges = FakeRateLimitedExchangeFactory()

    first_result = StrategyRunner(settings, exchanges).run(
        strategy_name="cross-exchange",
        max_cycles=1,
        interval_seconds=0,
        execution_mode="paper",
    )

    assert first_result.results[0].decision == "skipped"
    assert first_result.results[0].risk_reasons[0].startswith("exchange_rate_limit:")
    status = StrategyRuntimeGuard(settings.strategy_runtime).status(strategy_names=["cross-exchange"], execution_mode="paper")
    entry = status["entries"][0]
    assert entry["consecutive_market_data_failures"] == 1
    assert entry["consecutive_rate_limit_failures"] == 1
    assert entry["cooldown_active"] is True

    second_result = StrategyRunner(settings, exchanges).run(
        strategy_name="cross-exchange",
        max_cycles=1,
        interval_seconds=0,
        execution_mode="paper",
    )

    assert second_result.results[0].decision == "skipped"
    assert second_result.results[0].risk_reasons == ["runtime_guard_cooldown"]


def test_strategy_runtime_guard_resets_market_data_counters_after_profitable_execution(tmp_path: Path) -> None:
    config = StrategyRuntimeConfig(
        runtime_guard_path=str(tmp_path / "strategy-runtime-guard.json"),
        max_consecutive_market_data_failures=2,
    )
    guard = StrategyRuntimeGuard(config)

    guard.record(
        "triangular",
        "demo",
        decision="skipped",
        net_profit=Decimal("0"),
        reasons=["market_data_error:OKX API returned no ticker"],
    )
    guard.record("triangular", "demo", decision="executed", net_profit=Decimal("0.20"), reasons=[])

    status = guard.status(strategy_names=["triangular"], execution_mode="demo")["entries"][0]
    assert status["consecutive_market_data_failures"] == 0
    assert status["consecutive_rate_limit_failures"] == 0
    assert status["cooldown_active"] is False


def test_strategy_demo_execution_service_summarizes_filled_roundtrip_pnl() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoExecutionService(settings, provider=FakeFilledDemoProvider())

    result = service.execute("cross-exchange")

    assert result["strategy_name"] == "cross-exchange"
    assert result["demo_orders_sent"] is True
    assert result["live_orders_sent"] is False
    assert result["status"] == "closed"
    assert result["net_pnl_usdt"] == "-0.032000"
    assert [order["status"] for order in result["orders"]] == ["filled", "filled"]


def test_strategy_demo_execution_service_reconciles_pnl_with_account_equity() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoExecutionService(settings, provider=FakeEquityDeltaDemoProvider())

    result = service.execute("cross-exchange")

    validation = result["pnl_validation"]
    assert validation["cash_flow_net_pnl_usdt"] == "-0.032000"
    assert validation["equity_delta_usdt"] == "-0.032000"
    assert validation["difference_usdt"] == "0.000000"
    assert validation["within_tolerance"] is True
    assert validation["residual_inventory_usdt"] == "0.000000"
    assert validation["residual_inventory_within_tolerance"] is True


def test_strategy_demo_execution_service_includes_exchange_fill_receipts() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoExecutionService(settings, provider=FakeReceiptDemoProvider())

    result = service.execute("cross-exchange")

    receipts = result["pnl_validation"]["exchange_receipts"]
    assert receipts["source"] == "okx_fills_history"
    assert receipts["complete"] is True
    assert receipts["orders_with_receipts"] == 2
    assert receipts["missing_order_suffixes"] == []
    assert receipts["fill_count"] == 2
    assert receipts["fee_expense_usdt"] == "0.024008"
    assert receipts["fill_pnl_usdt"] == "0.000000"
    assert receipts["receipt_net_pnl_usdt"] == "-0.024008"


def test_strategy_demo_execution_uses_accumulated_fill_size() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoExecutionService(settings, provider=FakeAccumulatedFillDemoProvider())

    result = service.execute("cross-exchange")

    assert result["orders"][0]["quantity"] == "0.0001"
    assert result["orders"][0]["executed_quantity"] == "0.0001"
    assert result["orders"][0]["status"] == "filled"


def test_strategy_demo_execution_aborts_and_unwinds_after_unfilled_leg() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoExecutionService(settings, provider=FakeTriangularSecondLegCanceledProvider())

    result = service.execute("triangular")

    assert result["status"] == "aborted_unwound"
    assert str(result["abort_reason"]).startswith("leg_not_filled:spot:ETH/BTC:buy:canceled")
    assert [order["symbol"] for order in result["orders"]] == ["BTC/USDT", "ETH/BTC", "BTC/USDT"]
    assert [order["side"] for order in result["orders"]] == ["buy", "buy", "sell"]
    assert "ETH/USDT" not in {order["instId"] for order in service.provider.order_details.values()}
    assert Decimal(str(result["net_pnl_usdt"])) > Decimal("-1")
    assert result["live_orders_sent"] is False


def test_strategy_demo_execution_uses_dynamic_triangular_inventory_buffer() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoExecutionService(settings, provider=FakeFilledDemoProvider())

    result = service.execute("triangular")

    assert result["status"] == "closed"
    assert [order["symbol"] for order in result["orders"]] == ["BTC/USDT", "ETH/BTC", "ETH/USDT"]
    assert Decimal(str(result["orders"][1]["quantity"])) == Decimal("0.001995")
    assert Decimal(str(result["orders"][2]["quantity"])) == Decimal("0.001993")
    assert Decimal(str(result["orders"][1]["quantity"])) < Decimal("0.001998")
    assert Decimal(str(result["orders"][2]["quantity"])) < Decimal(str(result["orders"][1]["quantity"]))


def test_strategy_demo_execution_uses_spot_orderbook_for_marketable_limits() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.demo_limit_price_buffer_pct = Decimal("0.002")
    service = StrategyDemoExecutionService(settings, provider=FakeSpotBookDemoProvider())

    result = service.execute("triangular")

    assert result["status"] == "closed"
    assert [order["submitted_price"] for order in result["orders"]] == ["80170.0", "0.050300", "3982.0"]
    assert Decimal(str(result["orders"][1]["quantity"])) < Decimal("0.00199")


def test_strategy_demo_execution_caps_spot_prices_to_okx_price_limits() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.demo_limit_price_buffer_pct = Decimal("0.002")
    service = StrategyDemoExecutionService(settings, provider=FakeSpotPriceLimitDemoProvider())

    result = service.execute("triangular")

    assert result["status"] == "closed"
    assert [order["submitted_price"] for order in result["orders"]] == ["80100.0", "0.050250", "3985.0"]


def test_strategy_demo_execution_refreshes_price_limit_after_rejection() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.demo_limit_price_buffer_pct = Decimal("0.002")
    provider = FakeMovingPriceLimitDemoProvider()
    service = StrategyDemoExecutionService(settings, provider=provider)

    result = service.execute("triangular")

    assert result["status"] == "closed"
    assert result["orders"][0]["submitted_price"] == "80100.0"
    assert provider.price_limit_rejection_count == 1


def test_strategy_demo_preflight_uses_orderbook_expected_fill_price_not_limit_cap() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.demo_limit_price_buffer_pct = Decimal("0.002")
    service = StrategyDemoExecutionService(settings, provider=FakeProfitableSpotBookDemoProvider())

    preview = service.preview("triangular")

    assert preview["approved"] is True
    assert Decimal(str(preview["net_pnl_usdt"])) > Decimal("0")
    assert preview["orders"][0]["submitted_limit_price"] == "80160.0"
    assert preview["orders"][0]["expected_fill_price"] == "80000"
    assert preview["orders"][2]["submitted_limit_price"] == "4021.9"
    assert preview["orders"][2]["expected_fill_price"] == "4030"
    assert Decimal(str(preview["orders"][0]["submitted_limit_notional_usdt"])) > Decimal(
        str(preview["orders"][0]["estimated_notional_usdt"])
    )
    assert Decimal(str(preview["orders"][2]["submitted_limit_notional_usdt"])) < Decimal(
        str(preview["orders"][2]["estimated_notional_usdt"])
    )


def test_strategy_demo_execution_service_uses_demo_order_size_multiplier() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.demo_order_size_multiplier = Decimal("2")
    settings.strategy_runtime.demo_max_order_value_usdt = Decimal("20")
    service = StrategyDemoExecutionService(settings, provider=FakeFilledDemoProvider())

    result = service.execute("cross-exchange")

    orders = result["orders"]
    assert orders[0]["quantity"] == "0.0002"
    assert orders[1]["quantity"] == "0.0002"
    assert result["net_pnl_usdt"] == "-0.064000"


def test_strategy_demo_execution_service_uses_strategy_size_override() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.demo_order_size_multiplier = Decimal("3")
    settings.strategy_runtime.demo_strategy_size_overrides = {
        "cross-exchange": {
            "order_size_multiplier": Decimal("1"),
            "max_order_value_usdt": Decimal("10"),
        }
    }
    service = StrategyDemoExecutionService(settings, provider=FakeFilledDemoProvider())

    result = service.execute("cross-exchange")

    orders = result["orders"]
    assert orders[0]["quantity"] == "0.0001"
    assert orders[1]["quantity"] == "0.0001"
    assert result["sizing_policy"]["order_size_multiplier"] == "1"
    assert result["sizing_policy"]["max_order_value_usdt"] == "10"


def test_strategy_demo_execution_service_supports_carry_and_basis_canaries() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(
        enabled=True,
        allow_demo_orders=True,
        strategy_allowlist=["funding-carry-hedged", "spot-perp-carry", "futures-perp-basis"],
    )
    service = StrategyDemoExecutionService(settings, provider=FakeFilledDemoProvider())

    funding = service.execute("funding-carry-hedged")
    spot_perp = service.execute("spot-perp-carry")
    futures_basis = service.execute("futures-perp-basis")

    assert [order["instrument_type"] for order in funding["orders"]] == ["spot", "swap", "swap", "spot"]
    assert [order["instrument_type"] for order in spot_perp["orders"]] == ["spot", "swap", "spot", "swap"]
    assert [order["instrument_type"] for order in futures_basis["orders"]] == ["swap", "futures", "futures", "swap"]
    assert all(result["demo_orders_sent"] is True for result in [funding, spot_perp, futures_basis])
    assert all(result["live_orders_sent"] is False for result in [funding, spot_perp, futures_basis])


def test_directional_demo_execution_opens_position_without_immediate_exit(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True, strategy_allowlist=["trend-breakout"])
    settings.directional.position_state_path = str(tmp_path / "directional-positions.json")
    service = StrategyDemoExecutionService(settings, provider=FakeDirectionalLifecycleProvider(price=Decimal("80000")))

    result = service.execute(
        "trend-breakout",
        context={"selected_opportunity_id": "directional-trend-breakout-btc-usdt"},
    )

    assert result["status"] == "open"
    assert result["demo_orders_sent"] is True
    assert result["live_orders_sent"] is False
    assert result["net_pnl_usdt"] == "0.000000"
    assert [order["side"] for order in result["orders"]] == ["buy"]
    state = json.loads(Path(settings.directional.position_state_path).read_text(encoding="utf-8"))
    position = state["positions"]["trend-breakout|BTC/USDT"]
    assert position["state"] == "open"
    assert position["quantity"] == result["orders"][0]["executed_quantity"]


def test_directional_demo_execution_closes_position_on_take_profit(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True, strategy_allowlist=["trend-breakout"])
    settings.directional.position_state_path = str(tmp_path / "directional-positions.json")
    provider = FakeDirectionalLifecycleProvider(price=Decimal("80000"))
    service = StrategyDemoExecutionService(settings, provider=provider)
    service.execute("trend-breakout", context={"selected_opportunity_id": "directional-trend-breakout-btc-usdt"})
    provider.price = Decimal("83000")

    preview = service.preview("trend-breakout", context={"selected_opportunity_id": "directional-trend-breakout-btc-usdt"})
    result = service.execute("trend-breakout", context={"selected_opportunity_id": "directional-trend-breakout-btc-usdt"})

    assert preview["approved"] is True
    assert preview["reason"] == "directional_exit_take_profit"
    assert result["status"] == "closed"
    assert [order["side"] for order in result["orders"]] == ["sell"]
    assert Decimal(str(result["net_pnl_usdt"])) > Decimal("0")
    state = json.loads(Path(settings.directional.position_state_path).read_text(encoding="utf-8"))
    assert state["positions"]["trend-breakout|BTC/USDT"]["state"] == "closed"


def test_directional_demo_execution_closes_position_on_reverse_signal_with_loss_audit(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True, strategy_allowlist=["trend-breakout"])
    settings.directional.position_state_path = str(tmp_path / "directional-positions.json")
    provider = FakeDirectionalLifecycleProvider(price=Decimal("80000"))
    service = StrategyDemoExecutionService(settings, provider=provider)
    context = {"selected_opportunity_id": "directional-trend-breakout-btc-usdt"}
    service.execute("trend-breakout", context=context)
    provider.price = Decimal("79950")
    exit_context = {
        **context,
        "directional_signal": {"symbol": "BTC/USDT", "signal": "sell"},
    }

    preview = service.preview("trend-breakout", context=exit_context)
    result = service.execute("trend-breakout", context=exit_context)

    assert preview["approved"] is True
    assert preview["reason"] == "directional_exit_reverse_signal"
    assert result["status"] == "closed"
    assert [order["side"] for order in result["orders"]] == ["sell"]
    assert result["position_lifecycle"]["exit_reason"] == "reverse_signal"
    assert Decimal(str(result["net_pnl_usdt"])) < Decimal("0")
    assert result["managed_exit_audit"]["exit_reason"] == "reverse_signal"
    assert result["managed_exit_audit"]["loss_cooldown_recommended"] is True
    assert result["managed_exit_audit"]["cooldown_reason"] == "managed_directional_loss"
    assert result["managed_exit_audit"]["runtime_guard_records_loss"] is True


def test_directional_demo_preview_holds_existing_position_before_exit(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True, strategy_allowlist=["trend-breakout"])
    settings.directional.position_state_path = str(tmp_path / "directional-positions.json")
    provider = FakeDirectionalLifecycleProvider(price=Decimal("80000"))
    service = StrategyDemoExecutionService(settings, provider=provider)
    service.execute("trend-breakout", context={"selected_opportunity_id": "directional-trend-breakout-btc-usdt"})
    provider.price = Decimal("80100")

    preview = service.preview("trend-breakout", context={"selected_opportunity_id": "directional-trend-breakout-btc-usdt"})

    assert preview["approved"] is False
    assert preview["reason"] == "directional_position_hold"
    assert preview["open_position"]["state"] == "open"


def test_strategy_runner_allows_directional_reverse_signal_exit_when_local_buy_gate_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    fake_executor = FakeDirectionalReverseExitExecutor()
    runner = StrategyRunner(settings, ExchangeFactory(settings), demo_executor=fake_executor)

    def local_sell_gate(_definition, _symbol: str) -> dict[str, object]:
        return {
            "approved": False,
            "layer": "local",
            "reasons": ["signal_sell"],
            "opportunities_found": 0,
            "selected_opportunity_id": None,
            "directional_signal": {"symbol": "BTC/USDT", "signal": "sell"},
        }

    monkeypatch.setattr(runner, "_demo_local_validation_gate", local_sell_gate)

    result = runner.run(strategy_name="trend-breakout", max_cycles=1, interval_seconds=0, execution_mode="demo")

    cycle = result.results[0]
    assert cycle.decision == "executed"
    assert cycle.risk_reasons == []
    assert cycle.execution is not None
    assert cycle.execution["preflight"]["reason"] == "directional_exit_reverse_signal"
    assert cycle.execution["net_pnl_usdt"] == "-0.100000"
    assert fake_executor.preview_contexts[0]["directional_signal"]["signal"] == "sell"


def test_strategy_review_suggests_learning_adjustments_from_journal(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                '{"event":"strategy_cycle","strategy_name":"cross-exchange","decision":"executed","net_profit":"-1.0"}',
                '{"event":"strategy_cycle","strategy_name":"cross-exchange","decision":"executed","net_profit":"0"}',
                '{"event":"strategy_cycle","strategy_name":"cross-exchange","decision":"blocked","net_profit":"0"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(journal_path=str(journal_path), review_min_samples=2)

    report = StrategyReviewService(config).review()

    assert report.total_events == 3
    assert report.strategy_stats["cross-exchange"].executed == 2
    assert report.strategy_stats["cross-exchange"].blocked == 1
    assert report.suggestions
    assert report.suggestions[0].action in {"increase_min_profit_threshold", "reduce_position_cap"}


def test_strategy_review_counts_only_executed_pnl_as_real_profit(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                '{"event":"strategy_cycle","strategy_name":"triangular","decision":"executed","net_profit":"0.5"}',
                '{"event":"strategy_cycle","strategy_name":"triangular","decision":"skipped","net_profit":"-99"}',
                '{"event":"strategy_cycle","strategy_name":"triangular","decision":"blocked","net_profit":"-88"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = StrategyReviewService(StrategyRuntimeConfig(journal_path=str(journal_path), review_min_samples=1)).review()

    stats = report.strategy_stats["triangular"]
    assert stats.executed == 1
    assert stats.skipped == 1
    assert stats.blocked == 1
    assert stats.net_profit == Decimal("0.5")


def test_strategy_review_can_filter_by_execution_mode(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                '{"event":"strategy_cycle","execution_mode":"paper","strategy_name":"triangular","decision":"executed","net_profit":"99"}',
                '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"triangular","decision":"executed","net_profit":"0.25"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = StrategyReviewService(StrategyRuntimeConfig(journal_path=str(journal_path))).review(execution_mode="demo")

    assert report.total_events == 1
    assert report.strategy_stats["triangular"].net_profit == Decimal("0.25")


def test_strategy_evolution_archives_sustained_unprofitable_strategy(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "strategy_cycle",
                    "execution_mode": "demo",
                    "strategy_name": "spot-perp-carry",
                    "decision": "executed",
                    "net_profit": "-0.10",
                    "risk_reasons": [],
                }
            )
            for _ in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        review_min_samples=3,
        min_review_win_rate_pct=Decimal("50"),
        evolution_state_path=str(tmp_path / "evolution.state.json"),
        evolution_report_path=str(tmp_path / "evolution.md"),
    )

    summary = StrategyEvolutionService(config).refresh()

    decision = next(item for item in summary["decisions"] if item["strategy_name"] == "spot-perp-carry")
    assert decision["status"] == "archived"
    assert decision["action"] == "archive"
    assert "net_profit_below_threshold" in decision["reason_codes"]
    state = json.loads(Path(config.evolution_state_path).read_text(encoding="utf-8"))
    assert state["strategies"]["spot-perp-carry"]["status"] == "archived"
    assert "Archived Strategies" in Path(config.evolution_report_path).read_text(encoding="utf-8")
    assert any(
        candidate["strategy_name"] == "spot-perp-carry"
        and candidate["candidate_id"].startswith("spot-perp-carry-")
        and "arbitrage.spot_perp_min_basis_pct" in candidate["parameter_overrides"]
        for candidate in summary["parameter_candidates"]
    )


def test_strategy_evolution_marks_archived_strategy_as_revival_candidate_after_profit(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "event": "strategy_cycle",
                    "execution_mode": "paper",
                    "strategy_name": "spot-perp-carry",
                    "decision": "executed",
                    "net_profit": "0.25",
                    "risk_reasons": [],
                }
            )
            for _ in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    state_path = tmp_path / "evolution.state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "strategies": {
                    "spot-perp-carry": {
                        "strategy_name": "spot-perp-carry",
                        "status": "archived",
                        "action": "archive",
                        "reason_codes": ["net_profit_below_threshold"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        review_min_samples=3,
        min_review_win_rate_pct=Decimal("50"),
        evolution_state_path=str(state_path),
        evolution_report_path=str(tmp_path / "evolution.md"),
    )

    summary = StrategyEvolutionService(config).refresh()

    decision = next(item for item in summary["decisions"] if item["strategy_name"] == "spot-perp-carry")
    assert decision["status"] == "revive_candidate"
    assert decision["action"] == "revive_candidate"
    assert "archived_strategy_recovered_in_simulation" in decision["reason_codes"]


def test_strategy_evolution_tags_market_regime_from_recent_journal(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    events = [
        {
            "event": "strategy_cycle",
            "execution_mode": "paper",
            "strategy_name": "trend-breakout",
            "decision": "executed",
            "net_profit": "0.25",
            "market_regime": {"tag": "trend_up", "volatility": "normal"},
        },
        {
            "event": "strategy_cycle",
            "execution_mode": "paper",
            "strategy_name": "momentum-rotation",
            "decision": "executed",
            "net_profit": "0.15",
            "market_regime": {"tag": "trend_up", "volatility": "normal"},
        },
        {
            "event": "strategy_cycle",
            "execution_mode": "paper",
            "strategy_name": "mean-reversion-spot",
            "decision": "skipped",
            "net_profit": "0",
            "market_regime": {"tag": "range", "volatility": "low"},
        },
    ]
    journal_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        evolution_state_path=str(tmp_path / "evolution.state.json"),
        evolution_report_path=str(tmp_path / "evolution.md"),
    )

    summary = StrategyEvolutionService(config).refresh(execution_mode="paper")

    assert summary["market_regime"]["tag"] == "trend_up"
    assert summary["market_regime"]["sample_count"] == 3
    assert summary["market_regime"]["strategy_tags"]["trend-breakout"] == "trend_up"
    assert "Market Regime" in Path(config.evolution_report_path).read_text(encoding="utf-8")


def test_candle_market_regime_classifier_uses_completed_candles_only() -> None:
    candles: list[Candle] = []
    price = Decimal("100")
    for index in range(80):
        price += Decimal("0.35")
        candles.append(
            Candle(
                exchange="mock",
                symbol="BTC/USDT",
                bar="15m",
                open=price - Decimal("0.20"),
                high=price + Decimal("0.40"),
                low=price - Decimal("0.40"),
                close=price,
                volume=Decimal("1000") + Decimal(index),
                timestamp=utcnow(),
                complete=True,
            )
        )
    candles.append(
        Candle(
            exchange="mock",
            symbol="BTC/USDT",
            bar="15m",
            open=Decimal("1"),
            high=Decimal("1"),
            low=Decimal("1"),
            close=Decimal("1"),
            volume=Decimal("1"),
            timestamp=utcnow(),
            complete=False,
        )
    )

    regime = classify_candle_regime(candles)

    assert regime["tag"] == "trend_up"
    assert regime["completed_candles"] == 80
    assert regime["ignored_incomplete_candles"] == 1
    assert Decimal(str(regime["medium_return_pct"])) > Decimal("1")


def test_candidate_backtest_materializes_temp_config_without_mutating_settings(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "evolution.state.json")
    settings.strategy_runtime.evolution_report_path = str(tmp_path / "evolution.md")
    original_min_profit_factor = settings.directional.min_profit_factor

    result = StrategyCandidateBacktestService(settings, ExchangeFactory(settings)).run(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        limit=10,
    )

    assert result["orders_sent"] is False
    assert result["live_orders_sent"] is False
    assert result["checked_in_config_modified"] is False
    assert settings.directional.min_profit_factor == original_min_profit_factor
    assert result["candidates"]
    candidate = result["candidates"][0]
    assert candidate["strategy_name"] == "trend-breakout"
    assert candidate["temporary_config_materialized"] is True
    assert candidate["temporary_config_persisted"] is False
    assert "candidate_net_profit_usdt" in candidate
    assert "baseline_net_profit_usdt" in candidate


def test_strategy_revival_window_runs_only_revival_candidates_after_local_pass(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "evolution.state.json")
    settings.strategy_runtime.evolution_report_path = str(tmp_path / "evolution.md")
    Path(settings.strategy_runtime.evolution_state_path).write_text(
        json.dumps(
            {
                "version": 1,
                "strategies": {
                    "spot-perp-carry": {"strategy_name": "spot-perp-carry", "status": "revive_candidate"},
                    "cross-exchange": {"strategy_name": "cross-exchange", "status": "archived"},
                },
            }
        ),
        encoding="utf-8",
    )
    fake_validation = FakeRevivalValidationService(local_status="Pass", demo_status="Needs More Samples")
    service = StrategyRevivalWindowService(
        settings,
        ExchangeFactory(settings),
        validation_service_factory=lambda _settings, _exchanges: fake_validation,
    )

    result = service.run(strategy_name="all", cycles=2, symbol="BTC/USDT")

    assert result["local_first"] is True
    assert result["demo_window_attempted"] is True
    assert result["orders_sent"] is False
    assert result["live_orders_sent"] is False
    assert result["candidates"] == ["spot-perp-carry"]
    assert fake_validation.local_calls == [("spot-perp-carry", 1, "BTC/USDT")]
    assert fake_validation.demo_calls == [("spot-perp-carry", 2, "BTC/USDT")]
    assert result["results"][0]["demo_window_validation"]["status"] == "Needs More Samples"


def test_strategy_revival_window_skips_demo_when_local_validation_fails(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "evolution.state.json")
    Path(settings.strategy_runtime.evolution_state_path).write_text(
        json.dumps({"version": 1, "strategies": {"spot-perp-carry": {"status": "revive_candidate"}}}),
        encoding="utf-8",
    )
    fake_validation = FakeRevivalValidationService(local_status="Fail", demo_status="Pass")
    service = StrategyRevivalWindowService(
        settings,
        ExchangeFactory(settings),
        validation_service_factory=lambda _settings, _exchanges: fake_validation,
    )

    result = service.run(strategy_name="all", cycles=1, symbol="BTC/USDT")

    assert result["orders_sent"] is False
    assert result["results"][0]["decision"] == "skipped"
    assert result["results"][0]["reason"] == "local_validation_not_passed"
    assert fake_validation.demo_calls == []


def test_strategy_retrospective_deduplicates_and_escalates_repeated_issues(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    retrospective_path = tmp_path / "retro.md"
    state_path = tmp_path / "retro.state.json"
    journal_path.write_text(
        "\n".join(
            [
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"spot-perp-carry",'
                    '"decision":"skipped","risk_reasons":["demo_preflight_not_profitable"],"net_profit":"-0.1"}'
                ),
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"spot-perp-carry",'
                    '"decision":"skipped","risk_reasons":["demo_preflight_not_profitable"],"net_profit":"-0.2"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        retrospective_path=str(retrospective_path),
        retrospective_state_path=str(state_path),
        retrospective_repeat_warning_threshold=2,
    )

    summary = StrategyRetrospectiveService(config).refresh_from_journal(execution_mode="demo")

    assert summary["open_issue_count"] == 1
    issue = summary["top_open_issues"][0]
    assert issue["category"] == "preflight_not_profitable"
    assert issue["occurrences"] == 2
    assert issue["severity"] == "warning"
    assert retrospective_path.exists()
    assert state_path.exists()


def test_strategy_retrospective_refresh_does_not_increment_existing_snapshot(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"cross-exchange",'
                    '"decision":"skipped","risk_reasons":["demo_preflight_not_profitable"],"net_profit":"-0.1"}'
                ),
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"cross-exchange",'
                    '"decision":"skipped","risk_reasons":["demo_preflight_not_profitable"],"net_profit":"-0.2"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        retrospective_path=str(tmp_path / "retro.md"),
        retrospective_state_path=str(tmp_path / "retro.state.json"),
    )
    service = StrategyRetrospectiveService(config)

    first = service.refresh_from_journal(execution_mode="demo")
    second = service.refresh_from_journal(execution_mode="demo")

    assert first["top_open_issues"][0]["occurrences"] == 2
    assert second["top_open_issues"][0]["occurrences"] == 2


def test_strategy_retrospective_treats_transient_market_data_failure_as_improved_after_profit(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"triangular-multi-route",'
                    '"decision":"skipped","risk_reasons":["market_data_error:fixture"],"net_profit":"0"}'
                ),
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"triangular-multi-route",'
                    '"decision":"executed","risk_reasons":[],"net_profit":"0.25"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        retrospective_path=str(tmp_path / "retro.md"),
        retrospective_state_path=str(tmp_path / "retro.state.json"),
    )

    summary = StrategyRetrospectiveService(config).refresh_from_journal(execution_mode="demo")

    assert summary["open_issue_count"] == 0
    assert summary["top_open_issues"] == []


def test_strategy_retrospective_does_not_improve_unobserved_strategy_issues(tmp_path: Path) -> None:
    state_path = tmp_path / "retro.state.json"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-05-11T00:00:00+00:00",
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
                        "first_seen_at": "2026-05-11T00:00:00+00:00",
                        "last_seen_at": "2026-05-11T00:00:00+00:00",
                        "recommendation": "Do not force demo orders.",
                        "last_context": {"net_profit": "-0.258630"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(tmp_path / "strategy-events.jsonl"),
        retrospective_path=str(tmp_path / "retro.md"),
        retrospective_state_path=str(state_path),
    )
    payload = {
        "strategy_run": {
            "completed": True,
            "execution_mode": "demo",
            "results": [
                {
                    "strategy_name": "triangular-multi-route",
                    "execution_mode": "demo",
                    "decision": "executed",
                    "risk_reasons": [],
                    "net_profit": "0.25",
                }
            ],
        },
        "demo_window_validation": {
            "strategy_name": "triangular-multi-route",
            "status": "Pass",
            "reasons": [],
            "metrics": {"executed": 1, "skipped": 0, "net_profit": "0.25"},
        },
    }

    summary = StrategyRetrospectiveService(config).update_after_payload("strategy_validate_demo_window", payload)

    assert summary["open_issue_count"] == 1
    assert summary["top_open_issues"][0]["strategy_name"] == "spot-perp-carry"


def test_strategy_retrospective_reports_optimization_pressure_for_repeated_preflight(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    retrospective_path = tmp_path / "retro.md"
    journal_path.write_text(
        "\n".join(
            [
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"spot-perp-carry",'
                    '"decision":"skipped","risk_reasons":["demo_preflight_not_profitable"],"net_profit":"-0.25"}'
                ),
                (
                    '{"event":"strategy_cycle","execution_mode":"demo","strategy_name":"spot-perp-carry",'
                    '"decision":"skipped","risk_reasons":["demo_preflight_not_profitable"],"net_profit":"-0.30"}'
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(journal_path),
        retrospective_path=str(retrospective_path),
        retrospective_state_path=str(tmp_path / "retro.state.json"),
        retrospective_repeat_warning_threshold=2,
    )

    summary = StrategyRetrospectiveService(config).refresh_from_journal(execution_mode="demo")

    pressure = summary["optimization_pressure"][0]
    assert pressure["strategy_name"] == "spot-perp-carry"
    assert pressure["execution_mode"] == "demo"
    assert pressure["priority"] == "high"
    assert pressure["observed_net_profit_usdt"] == "-0.30"
    assert pressure["break_even_gap_usdt"] == "0.30"
    assert "arbitrage.basis_holding_hours" in pressure["suggested_config_fields"]
    assert "do_not_force_demo_orders_when_preflight_is_negative" in pressure["guardrails"]
    assert "Strategy Optimization Pressure" in retrospective_path.read_text(encoding="utf-8")


def test_strategy_retrospective_classifies_receipt_pnl_residual_and_open_risk(tmp_path: Path) -> None:
    config = StrategyRuntimeConfig(
        journal_path=str(tmp_path / "strategy-events.jsonl"),
        retrospective_path=str(tmp_path / "retro.md"),
        retrospective_state_path=str(tmp_path / "retro.state.json"),
    )
    payload = {
        "strategy_run": {
            "completed": True,
            "cycles_completed": 1,
            "execution_mode": "demo",
            "results": [
                {
                    "cycle": 1,
                    "strategy_name": "triangular-multi-route",
                    "execution_mode": "demo",
                    "decision": "executed",
                    "risk_reasons": [],
                    "selected_opportunity_id": "okx-demo-triangular",
                    "net_profit": "-0.01",
                    "message": "filled",
                    "execution": {
                        "pnl_validation": {
                            "within_tolerance": False,
                            "residual_inventory_within_tolerance": False,
                            "exchange_receipts": {"complete": False},
                        }
                    },
                }
            ],
        },
        "post_run_open_risk": {
            "checked": True,
            "open_spot_orders": 1,
            "open_swap_orders": 0,
            "swap_positions": 0,
        },
    }

    summary = StrategyRetrospectiveService(config).update_after_payload("strategy_validate_demo_window", payload)

    categories = {issue["category"] for issue in summary["top_open_issues"]}
    assert {
        "execution_loss",
        "pnl_out_of_tolerance",
        "residual_inventory_out_of_tolerance",
        "receipt_incomplete",
        "open_risk_after_run",
    } <= categories
    assert summary["highest_severity"] == "critical"


def test_strategy_validation_report_summarizes_recent_receipt_pnl_and_residual_evidence(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "triangular-multi-route",
                        "decision": "executed",
                        "risk_reasons": [],
                        "net_profit": "0.20",
                        "execution": {
                            "pnl_validation": {
                                "within_tolerance": True,
                                "residual_inventory_usdt": "0.01",
                                "residual_inventory_within_tolerance": True,
                                "exchange_receipts": {"complete": True},
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "triangular-multi-route",
                        "decision": "executed",
                        "risk_reasons": [],
                        "net_profit": "-0.05",
                        "execution": {
                            "pnl_validation": {
                                "within_tolerance": False,
                                "residual_inventory_usdt": "0.12",
                                "residual_inventory_within_tolerance": False,
                                "exchange_receipts": {"complete": False},
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "spot-perp-carry",
                        "decision": "skipped",
                        "risk_reasons": ["demo_preflight_not_profitable"],
                        "net_profit": "-0.25",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = StrategyValidationReportService(StrategyRuntimeConfig(journal_path=str(journal_path))).report(
        execution_mode="demo",
        limit=50,
    )

    assert report.scanned_events == 3
    assert report.total.executed == 2
    assert report.total.skipped == 1
    assert report.total.wins == 1
    assert report.total.losses == 1
    assert report.total.realized_net_profit_usdt == Decimal("0.15")
    assert report.total.skipped_preflight_net_pnl_usdt == Decimal("-0.25")
    assert report.total.max_drawdown_usdt == Decimal("0.05")
    assert report.total.receipt_incomplete == 1
    assert report.total.pnl_out_of_tolerance == 1
    assert report.total.residual_inventory_out_of_tolerance == 1
    assert report.total.max_residual_inventory_usdt == Decimal("0.12")
    assert report.total.executed_preflight_missing == 2
    assert report.total.execution_quality_passed is False
    assert report.by_strategy["spot-perp-carry"].reason_counts["demo_preflight_not_profitable"] == 1
    assert any("Resolve receipt" in item for item in report.recommendations)


def test_strategy_validation_report_does_not_mark_open_directional_position_as_loss(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        json.dumps(
            {
                "event": "strategy_cycle",
                "execution_mode": "demo",
                "strategy_name": "trend-breakout",
                "decision": "executed",
                "risk_reasons": [],
                "net_profit": "0",
                "execution": {
                    "strategy_family": "directional",
                    "position_lifecycle": {"state": "open", "symbol": "BTC/USDT"},
                    "pnl_validation": {
                        "method": "managed_directional_open_position",
                        "equity_delta_usdt": None,
                        "within_tolerance": True,
                        "residual_inventory_usdt": "16",
                        "residual_inventory_within_tolerance": True,
                        "exchange_receipts": {"complete": True},
                    },
                    "preflight": {"net_pnl_usdt": "0"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report = StrategyValidationReportService(StrategyRuntimeConfig(journal_path=str(journal_path))).report(
        execution_mode="demo",
        limit=50,
    )

    assert report.total.executed == 1
    assert report.total.wins == 0
    assert report.total.losses == 0
    assert report.total.realized_net_profit_usdt == Decimal("0")
    assert report.total.execution_quality_passed is True


def test_strategy_validation_report_reconciles_expected_cash_flow_and_account_equity(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "triangular-multi-route",
                        "decision": "executed",
                        "risk_reasons": [],
                        "net_profit": "0.20",
                        "execution": {
                            "preflight": {"net_pnl_usdt": "0.18"},
                            "pnl_validation": {
                                "cash_flow_net_pnl_usdt": "0.20",
                                "equity_delta_usdt": "0.23",
                                "difference_usdt": "0.03",
                                "within_tolerance": True,
                                "residual_inventory_usdt": "0.01",
                                "residual_inventory_within_tolerance": True,
                                "exchange_receipts": {"complete": True},
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "triangular-multi-route",
                        "decision": "executed",
                        "risk_reasons": [],
                        "net_profit": "0.10",
                        "execution": {
                            "preflight": {"net_pnl_usdt": "0.11"},
                            "pnl_validation": {
                                "cash_flow_net_pnl_usdt": "0.10",
                                "equity_delta_usdt": "-0.02",
                                "difference_usdt": "-0.12",
                                "within_tolerance": False,
                                "residual_inventory_usdt": "0.00",
                                "residual_inventory_within_tolerance": True,
                                "exchange_receipts": {"complete": True},
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = StrategyValidationReportService(StrategyRuntimeConfig(journal_path=str(journal_path))).report(
        execution_mode="demo",
        limit=50,
    )

    assert report.total.expected_actual_sample_count == 2
    assert report.total.expected_preflight_net_pnl_usdt == Decimal("0.29")
    assert report.total.actual_cash_flow_net_pnl_usdt == Decimal("0.30")
    assert report.total.expected_actual_gap_usdt == Decimal("0.01")
    assert report.total.max_abs_expected_actual_gap_usdt == Decimal("0.02")
    assert report.total.account_equity_sample_count == 2
    assert report.total.account_equity_delta_usdt == Decimal("0.21")
    assert report.total.account_reconciliation_gap_usdt == Decimal("-0.09")
    assert report.total.max_abs_account_reconciliation_gap_usdt == Decimal("0.12")
    assert report.total.positive_cash_flow_negative_equity_delta == 1
    assert any("Account equity moved negative" in item for item in report.recommendations)


def test_strategy_retrospective_preserves_manual_notes(tmp_path: Path) -> None:
    retrospective_path = tmp_path / "retro.md"
    retrospective_path.write_text(
        f"# Existing\n\n{MANUAL_START}\nKeep this operator note.\n{MANUAL_END}\n",
        encoding="utf-8",
    )
    config = StrategyRuntimeConfig(
        journal_path=str(tmp_path / "strategy-events.jsonl"),
        retrospective_path=str(retrospective_path),
        retrospective_state_path=str(tmp_path / "retro.state.json"),
    )

    StrategyRetrospectiveService(config).update_after_payload(
        "strategy_run",
        {
            "strategy_run": {
                "completed": True,
                "cycles_completed": 1,
                "execution_mode": "demo",
                "results": [
                    {
                        "cycle": 1,
                        "strategy_name": "cross-exchange",
                        "execution_mode": "demo",
                        "decision": "skipped",
                        "risk_reasons": ["demo_preflight_not_profitable"],
                        "net_profit": "-0.1",
                    }
                ],
            }
        },
    )

    assert "Keep this operator note." in retrospective_path.read_text(encoding="utf-8")


def test_strategy_retrospective_resolves_relative_paths_from_repo_root() -> None:
    config = StrategyRuntimeConfig(retrospective_path="runtime/crypto/reports/28-strategy-retrospective.md")

    summary = StrategyRetrospectiveService(config).before_summary()

    assert summary["retrospective_path"] == str(ROOT / "runtime/crypto/reports/28-strategy-retrospective.md")


def test_strategy_demo_validation_service_runs_strategies_in_order() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    service = StrategyDemoValidationService(settings, provider=FakeDemoProvider())

    result = service.validate(["cross-exchange", "triangular", "funding-rate", "spot-perp"])

    assert [item.strategy_name for item in result.results] == ["cross-exchange", "triangular", "funding-rate", "spot-perp"]
    assert result.completed is True
    assert all(item.ok for item in result.results)
    assert all(item.live_orders_sent is False for item in result.results)
    assert result.results[2].orders[0]["instrument_type"] == "swap"


def test_strategy_demo_validation_service_supports_new_carry_and_basis_paths() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    provider = FakeAccountModeProvider(account_level="3")
    service = StrategyDemoValidationService(settings, provider=provider)

    result = service.validate(["funding-carry-hedged", "spot-perp-carry", "futures-perp-basis"])

    assert result.completed is True
    assert [item.strategy_name for item in result.results] == ["funding-carry-hedged", "spot-perp-carry", "futures-perp-basis"]
    assert all(item.ok for item in result.results)
    assert [order["instrument_type"] for order in result.results[0].orders] == ["spot", "swap"]
    assert result.results[0].orders[0]["quantity"] == Decimal("0.0001")
    assert result.results[0].orders[0]["price"] == Decimal("79200.0")
    assert result.results[0].orders[1]["quantity"] == Decimal("0.01")
    assert result.results[0].orders[1]["price"] == Decimal("80800.0")
    assert all(order["submitted"] and order["canceled"] and not order["open_after_cancel"] for order in result.results[0].orders)
    assert result.results[2].orders[1]["instrument_type"] == "futures"
    assert result.results[2].orders[1]["symbol"] == "BTC-USDT-260626"


def test_strategy_demo_validation_blocks_swap_when_demo_account_is_spot_mode() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    provider = FakeAccountModeProvider(account_level="1")
    service = StrategyDemoValidationService(settings, provider=provider)

    result = service.validate(["funding-rate"])

    assert result.completed is False
    assert result.account_mode["ok"] is False
    assert result.account_mode["before"] == "1"
    assert "--allow-account-mode-switch" in str(result.results[0].error)
    assert provider.posts == []


def test_strategy_demo_validation_can_switch_demo_account_mode_for_swap_validation() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    provider = FakeAccountModeProvider(account_level="1")
    service = StrategyDemoValidationService(settings, provider=provider, allow_account_mode_switch=True)

    result = service.validate(["funding-rate"])

    assert result.completed is True
    assert result.account_mode["switch_attempted"] is True
    assert result.account_mode["switched"] is True
    assert result.account_mode["after"] == "2"
    assert provider.posts == [("/api/v5/account/set-account-level", {"acctLv": "2"})]
    assert result.results[0].orders[0]["instrument_type"] == "swap"


def test_strategy_demo_validation_uses_cross_td_mode_for_spot_in_multicurrency_mode() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.trading = TradingConfig(live_trading=False, dry_run=False, require_confirm_before_order=False)
    settings.exchanges["okx"] = ExchangeConfig(enabled=True, sandbox=True, adapter="okx", okx_demo=True)
    settings.agent_trading = AgentTradingConfig(enabled=True, allow_demo_orders=True)
    provider = FakeAccountModeProvider(account_level="3")
    service = StrategyDemoValidationService(settings, provider=provider)

    result = service.validate(["cross-exchange"])

    assert result.completed is True
    assert result.account_mode["before"] == "3"
    assert provider.trade_posts[0][1]["tdMode"] == "cross"
    assert result.results[0].orders[0]["instrument_type"] == "spot"


class FakeRevivalValidationService:
    """Fake local/demo validation service for revival-window tests."""

    def __init__(self, local_status: str, demo_status: str) -> None:
        self.local_status = local_status
        self.demo_status = demo_status
        self.local_calls: list[tuple[str, int, str]] = []
        self.demo_calls: list[tuple[str, int, str]] = []

    def validate_local(self, strategy_name: str, cycles: int, symbol: str) -> dict:
        """Record local validation and return a configured status."""
        self.local_calls.append((strategy_name, cycles, symbol))
        return {
            "local_validation": {
                "status": self.local_status,
                "strategy_name": strategy_name,
                "reasons": [] if self.local_status == "Pass" else ["fixture_local_fail"],
                "metrics": {"executed": 1 if self.local_status == "Pass" else 0, "net_profit": "0.10"},
            },
            "strategy_run": {"execution_mode": "paper", "results": []},
        }

    def validate_demo_window(self, strategy_name: str, cycles: int, symbol: str) -> dict:
        """Record demo validation and return a configured status."""
        self.demo_calls.append((strategy_name, cycles, symbol))
        return {
            "demo_window_validation": {
                "status": self.demo_status,
                "strategy_name": strategy_name,
                "reasons": [],
                "metrics": {"executed": 1, "net_profit": "0.05"},
            },
            "strategy_run": {"execution_mode": "demo", "results": []},
            "post_run_open_risk": {"checked": True, "open_spot_orders": 0, "open_swap_orders": 0, "swap_positions": 0},
        }


class FakeDemoProvider:
    """Fake OKX demo provider for strategy demo validation tests."""

    configured = True
    demo = True

    def __init__(self) -> None:
        self.counter = 0
        self.open_spot: set[str] = set()
        self.open_swap: set[str] = set()

    def get_price(self, symbol: str) -> float:
        return {
            "BTC/USDT": 80000.0,
            "ETH/USDT": 4000.0,
            "ETH/BTC": 0.05,
        }.get(symbol, 80000.0)

    def submit_order(self, request):
        self.counter += 1
        order_id = f"spot-{self.counter}"
        self.open_spot.add(order_id)
        return FakeProviderOrder(order_id, request.symbol, request.side, request.order_type, request.quantity, request.price, "submitted")

    def submit_swap_order(self, request):
        self.counter += 1
        order_id = f"swap-{self.counter}"
        self.open_swap.add(order_id)
        return FakeProviderOrder(order_id, request.inst_id, request.side, request.order_type, request.sz, request.price, "submitted")

    def cancel_order(self, symbol: str, order_id: str):
        self.open_spot.discard(order_id)
        self.open_swap.discard(order_id)
        return FakeProviderOrder(order_id, symbol, "buy", "limit", 0.0, None, "canceled")

    def get_open_orders(self, symbol: str | None = None):
        return [FakeProviderOrder(order_id, symbol or "BTC/USDT", "buy", "limit", 0.0, None, "submitted") for order_id in self.open_spot]

    def get_open_futures_orders(self, inst_type: str = "SWAP", inst_id: str | None = None):
        return [FakeProviderOrder(order_id, inst_id or "BTC-USDT-SWAP", "sell", "limit", 0.0, None, "submitted") for order_id in self.open_swap]


class FakeRateLimitedExchangeFactory:
    """Fake exchange factory that simulates OKX market-data throttling."""

    def list_enabled(self) -> list[str]:
        return ["okx", "mock_alt"]

    def get(self, name: str):
        return FakeRateLimitedExchange(name)


class FakeRateLimitedExchange:
    """Fake exchange that fails market reads with a rate-limit error."""

    def __init__(self, name: str) -> None:
        self.name = name

    def get_ticker(self, symbol: str):
        raise ExchangeError("OKX API 429 rate limit while fetching ticker")


class FakeAccountModeProvider(FakeDemoProvider):
    """Fake demo provider with account-level switch support."""

    def __init__(self, account_level: str) -> None:
        super().__init__()
        self.account_level = account_level
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.trade_posts: list[tuple[str, dict[str, str]]] = []

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/account/config":
            return {"code": "0", "data": [{"acctLv": self.account_level}]}
        if path == "/api/v5/account/set-account-switch-precheck":
            return {"code": "0", "data": [{"sCode": "0", "acctLv": (params or {}).get("acctLv", "2")}]}
        if path == "/api/v5/public/instruments":
            return {"code": "0", "data": [{"instId": "BTC-USDT-260626", "expTime": "1782432000000"}]}
        if path == "/api/v5/market/ticker":
            return {"code": "0", "data": [{"instId": (params or {}).get("instId", "BTC-USDT-260626"), "last": "80100"}]}
        raise AssertionError(f"unexpected _get path: {path}")

    def _post(self, path: str, body: dict[str, str]):
        if path == "/api/v5/trade/order":
            self.counter += 1
            order_id = f"spot-{self.counter}"
            self.open_spot.add(order_id)
            self.trade_posts.append((path, body))
            return {"code": "0", "data": [{"ordId": order_id}]}
        self.posts.append((path, body))
        if path == "/api/v5/account/set-account-level":
            self.account_level = body["acctLv"]
            return {"code": "0", "data": [{"acctLv": self.account_level}]}
        raise AssertionError(f"unexpected _post path: {path}")

    def get_positions(self, inst_type: str = "SWAP"):
        return []


def _write_preflight_gap_samples(path: Path, strategy_name: str) -> None:
    """Write executed demo samples whose preflight estimate exceeded cash-flow PnL."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        ("0.060000", "0.020000"),
        ("0.050000", "0.010000"),
        ("0.040000", "0.020000"),
    ]
    events = [
        {
            "event": "strategy_cycle",
            "strategy_name": strategy_name,
            "execution_mode": "demo",
            "decision": "executed",
            "net_profit": actual,
            "execution": {
                "preflight": {"net_pnl_usdt": expected},
                "pnl_validation": {"cash_flow_net_pnl_usdt": actual, "within_tolerance": True},
            },
        }
        for expected, actual in rows
    ]
    path.write_text("\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n", encoding="utf-8")


class FakeDemoExecutor:
    """Fake demo executor for runner tests."""

    def preview(self, strategy_name: str):
        return {
            "strategy_name": strategy_name,
            "approved": True,
            "net_pnl_usdt": "0.42",
            "reason": "demo_preflight_profitable",
        }

    def execute(self, strategy_name: str):
        return {
            "strategy_name": strategy_name,
            "status": "closed",
            "demo_orders_sent": True,
            "live_orders_sent": False,
            "net_pnl_usdt": "0.42",
            "orders": [],
            "account_before": {},
            "account_after": {},
        }


class FakePreflightDemoExecutor:
    """Fake demo executor that can reject orders before execution."""

    def __init__(self, approved: bool, net_pnl: str) -> None:
        self.approved = approved
        self.net_pnl = net_pnl
        self.execute_calls: list[str] = []

    def preview(self, strategy_name: str):
        return {
            "strategy_name": strategy_name,
            "approved": self.approved,
            "net_pnl_usdt": self.net_pnl,
            "reason": "demo_preflight_profitable" if self.approved else "demo_preflight_not_profitable",
        }

    def execute(self, strategy_name: str):
        self.execute_calls.append(strategy_name)
        return {
            "strategy_name": strategy_name,
            "status": "closed",
            "demo_orders_sent": True,
            "live_orders_sent": False,
            "net_pnl_usdt": self.net_pnl,
            "orders": [],
            "account_before": {},
            "account_after": {},
        }


class FakeSequenceDemoExecutor:
    """Fake demo executor that returns a configured PnL sequence."""

    def __init__(self, pnls: list[str]) -> None:
        self.pnls = [Decimal(value) for value in pnls]
        self.index = 0

    def preview(self, strategy_name: str):
        return {
            "strategy_name": strategy_name,
            "approved": True,
            "net_pnl_usdt": str(self.pnls[min(self.index, len(self.pnls) - 1)]),
            "reason": "demo_preflight_profitable",
        }

    def execute(self, strategy_name: str):
        pnl = self.pnls[min(self.index, len(self.pnls) - 1)]
        self.index += 1
        return {
            "strategy_name": strategy_name,
            "status": "closed",
            "demo_orders_sent": True,
            "live_orders_sent": False,
            "net_pnl_usdt": str(pnl),
            "orders": [],
            "account_before": {},
            "account_after": {},
        }


class FakeFilledDemoProvider(FakeAccountModeProvider):
    """Fake provider that fills account-mode-aware raw demo orders."""

    def __init__(self) -> None:
        super().__init__(account_level="3")
        self.order_details: dict[str, dict[str, str]] = {}

    def _post(self, path: str, body: dict[str, str]):
        if path == "/api/v5/trade/order":
            self.counter += 1
            order_id = f"filled-{self.counter}"
            quantity = str(body["sz"])
            price = str(body["px"])
            self.order_details[order_id] = {
                "ordId": order_id,
                "instId": body["instId"],
                "side": body["side"],
                "ordType": body["ordType"],
                "state": "filled",
                "sz": quantity,
                "fillSz": quantity,
                "px": price,
                "avgPx": price,
                "cTime": "1",
                "uTime": "2",
            }
            return {"code": "0", "data": [{"ordId": order_id}]}
        return super()._post(path, body)

    def submit_swap_order(self, request):
        self.counter += 1
        order_id = f"filled-{self.counter}"
        quantity = str(request.sz)
        price = str(request.price)
        self.order_details[order_id] = {
            "ordId": order_id,
            "instId": request.inst_id,
            "side": str(request.side),
            "ordType": str(request.order_type),
            "state": "filled",
            "sz": quantity,
            "fillSz": quantity,
            "px": price,
            "avgPx": price,
            "cTime": "1",
            "uTime": "2",
        }
        return FakeProviderOrder(order_id, request.inst_id, request.side, request.order_type, request.sz, request.price, "filled")

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/trade/order":
            order_id = (params or {})["ordId"]
            return {"code": "0", "data": [self.order_details[order_id]]}
        return super()._get(path, params=params, signed=signed)

    def get_account(self):
        return type(
            "Account",
            (),
            {
                "balances": [
                    type("Balance", (), {"asset": "USDT", "free": 1000.0, "locked": 0.0, "total": 1000.0})(),
                    type("Balance", (), {"asset": "BTC", "free": 1.0, "locked": 0.0, "total": 1.0})(),
                ]
            },
        )()


class FakeEquityDeltaDemoProvider(FakeFilledDemoProvider):
    """Fake filled provider with account snapshots that match order PnL."""

    def __init__(self) -> None:
        super().__init__()
        self.account_reads = 0

    def get_account(self):
        self.account_reads += 1
        usdt_total = Decimal("1000.000000") if self.account_reads == 1 else Decimal("999.968000")
        return type(
            "Account",
            (),
            {
                "balances": [
                    type("Balance", (), {"asset": "USDT", "free": usdt_total, "locked": Decimal("0"), "total": usdt_total})(),
                    type("Balance", (), {"asset": "BTC", "free": Decimal("1"), "locked": Decimal("0"), "total": Decimal("1")})(),
                ]
            },
        )()


class FakeReceiptDemoProvider(FakeEquityDeltaDemoProvider):
    """Fake provider that returns OKX fills-history receipts."""

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/trade/fills-history":
            order_id = (params or {})["ordId"]
            detail = self.order_details[order_id]
            fee = "-0.0000001" if detail["side"] == "buy" else "-0.016000"
            fee_ccy = "BTC" if detail["side"] == "buy" else "USDT"
            return {
                "code": "0",
                "data": [
                    {
                        "instType": "SPOT",
                        "instId": detail["instId"],
                        "ordId": order_id,
                        "tradeId": f"trade-{order_id}",
                        "fillPx": detail["avgPx"],
                        "fillSz": detail["fillSz"],
                        "fillPnl": "0",
                        "fee": fee,
                        "feeCcy": fee_ccy,
                        "side": detail["side"],
                        "execType": "T",
                        "fillTime": "10",
                    }
                ],
            }
        return super()._get(path, params=params, signed=signed)


class FakeAccumulatedFillDemoProvider(FakeReceiptDemoProvider):
    """Fake provider whose order detail reports last fill and accumulated fill separately."""

    def _post(self, path: str, body: dict[str, str]):
        response = super()._post(path, body)
        if path == "/api/v5/trade/order":
            order_id = response["data"][0]["ordId"]
            quantity = self.order_details[order_id]["sz"]
            self.order_details[order_id]["fillSz"] = str(Decimal(quantity) / Decimal("2"))
            self.order_details[order_id]["accFillSz"] = quantity
        return response


class FakeTriangularSecondLegCanceledProvider(FakeReceiptDemoProvider):
    """Fake provider that cancels the middle triangular leg to test unwind safety."""

    def _post(self, path: str, body: dict[str, str]):
        if path == "/api/v5/trade/order" and body["instId"] == "ETH-BTC":
            self.counter += 1
            order_id = f"canceled-{self.counter}"
            self.order_details[order_id] = {
                "ordId": order_id,
                "instId": body["instId"],
                "side": body["side"],
                "ordType": body["ordType"],
                "state": "canceled",
                "sz": str(body["sz"]),
                "fillSz": "0",
                "px": str(body["px"]),
                "avgPx": "0",
                "cTime": "1",
                "uTime": "2",
            }
            return {"code": "0", "data": [{"ordId": order_id}]}
        return super()._post(path, body)


class FakeSpotBookDemoProvider(FakeReceiptDemoProvider):
    """Fake provider exposing OKX spot books for marketable-limit pricing tests."""

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/market/books":
            inst_id = (params or {})["instId"]
            books = {
                "BTC-USDT": ("79990", "80010"),
                "ETH-BTC": ("0.049800", "0.050200"),
                "ETH-USDT": ("3990", "4010"),
            }
            bid, ask = books[inst_id]
            return {"code": "0", "data": [{"bids": [[bid, "10"]], "asks": [[ask, "10"]]}]}
        return super()._get(path, params=params, signed=signed)


class FakeSpotPriceLimitDemoProvider(FakeSpotBookDemoProvider):
    """Fake provider exposing OKX spot price limits around marketable prices."""

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/public/price-limit":
            inst_id = (params or {})["instId"]
            limits = {
                "BTC-USDT": {"buyLmt": "80100.0", "sellLmt": "79800.0"},
                "ETH-BTC": {"buyLmt": "0.050250", "sellLmt": "0.049700"},
                "ETH-USDT": {"buyLmt": "4100.0", "sellLmt": "3985.0"},
            }
            return {"code": "0", "data": [limits[inst_id]]}
        return super()._get(path, params=params, signed=signed)


class FakeMovingPriceLimitDemoProvider(FakeSpotBookDemoProvider):
    """Fake provider whose OKX price limit tightens between preview and submit."""

    def __init__(self) -> None:
        super().__init__()
        self.price_limit_reads = 0
        self.price_limit_rejection_count = 0

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/public/price-limit":
            inst_id = (params or {})["instId"]
            if inst_id == "BTC-USDT":
                self.price_limit_reads += 1
                buy_limit = "80200.0" if self.price_limit_reads == 1 else "80100.0"
                return {"code": "0", "data": [{"buyLmt": buy_limit, "sellLmt": "79800.0"}]}
            return {"code": "0", "data": [{"buyLmt": "4100.0", "sellLmt": "0.000001"}]}
        return super()._get(path, params=params, signed=signed)

    def _post(self, path: str, body):
        if path == "/api/v5/trade/order" and body["instId"] == "BTC-USDT" and Decimal(str(body["px"])) > Decimal("80100.0"):
            self.price_limit_rejection_count += 1
            raise ValueError("OKX: The highest price limit for the buy leg is 80,100.0.  (code=1)")
        return super()._post(path, body)


class FakeProfitableSpotBookDemoProvider(FakeReceiptDemoProvider):
    """Fake provider with a positive top-of-book triangular edge."""

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/market/books":
            inst_id = (params or {})["instId"]
            books = {
                "BTC-USDT": ("79990", "80000"),
                "ETH-BTC": ("0.049900", "0.050000"),
                "ETH-USDT": ("4030", "4035"),
            }
            bid, ask = books[inst_id]
            return {"code": "0", "data": [{"bids": [[bid, "10"]], "asks": [[ask, "10"]]}]}
        return super()._get(path, params=params, signed=signed)


class FakeDirectionalLifecycleProvider(FakeReceiptDemoProvider):
    """Fake provider with mutable spot prices for directional lifecycle tests."""

    def __init__(self, price: Decimal) -> None:
        super().__init__()
        self.price = price

    def get_price(self, symbol: str) -> float:
        if symbol == "BTC/USDT":
            return float(self.price)
        return super().get_price(symbol)

    def _get(self, path: str, params=None, *, signed: bool = False):
        if path == "/api/v5/market/books":
            inst_id = (params or {})["instId"]
            if inst_id == "BTC-USDT":
                bid = self.price * Decimal("0.9999")
                ask = self.price * Decimal("1.0001")
                return {
                    "code": "0",
                    "data": [{"bids": [[str(bid), "10"]], "asks": [[str(ask), "10"]]}],
                }
        return super()._get(path, params=params, signed=signed)


class FakeDirectionalReverseExitExecutor:
    """Fake demo executor that previews and executes a managed directional reverse-signal exit."""

    def __init__(self) -> None:
        self.preview_contexts: list[dict[str, object]] = []
        self.execute_contexts: list[dict[str, object]] = []

    def preview(self, strategy_name: str, context: dict[str, object] | None = None) -> dict[str, object]:
        self.preview_contexts.append(dict(context or {}))
        return {
            "strategy_name": strategy_name,
            "strategy_family": "directional",
            "approved": True,
            "reason": "directional_exit_reverse_signal",
            "gross_pnl_usdt": "-0.080000",
            "estimated_fee_usdt": "0.020000",
            "net_pnl_usdt": "-0.100000",
            "orders": [{"side": "sell"}],
        }

    def execute(self, strategy_name: str, context: dict[str, object] | None = None) -> dict[str, object]:
        self.execute_contexts.append(dict(context or {}))
        return {
            "strategy_name": strategy_name,
            "strategy_family": "directional",
            "status": "closed",
            "demo_orders_sent": True,
            "live_orders_sent": False,
            "net_pnl_usdt": "-0.100000",
            "orders": [{"side": "sell"}],
            "managed_exit_audit": {
                "exit_reason": "reverse_signal",
                "loss_cooldown_recommended": True,
                "runtime_guard_records_loss": True,
            },
        }


class FakeProviderOrder:
    """Fake provider order object."""

    def __init__(self, order_id, symbol, side, order_type, quantity, price, status) -> None:
        self.order_id = order_id
        self.symbol = symbol
        self.side = side
        self.order_type = order_type
        self.quantity = quantity
        self.submitted_price = price
        self.status = status
