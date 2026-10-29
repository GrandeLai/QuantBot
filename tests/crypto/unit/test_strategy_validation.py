"""Unit tests for three-layer strategy validation and promotion gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import Settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.models import ExecutionMode, StrategyCycleResult, StrategyRunResult
from quantbot.crypto.runtime.validation.service import StrategyValidationService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_validate_local_passes_with_profitable_paper_runner(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    fake_runner = FakeRunner(_run_result("paper", [_cycle("paper", Decimal("0.25"))]))
    service = StrategyValidationService(
        settings,
        ExchangeFactory(settings),
        runner_factory=lambda _settings, _exchanges: fake_runner,
    )

    payload = service.validate_local(strategy_name="triangular", cycles=1, symbol="BTC/USDT")

    assert payload["local_validation"]["status"] == "Pass"
    assert payload["local_validation"]["metrics"]["executed"] == 1
    assert fake_runner.calls[0]["execution_mode"] == "paper"


def test_validate_demo_window_requires_clean_demo_evidence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.strategy_runtime.validation_min_demo_executions = 1
    fake_runner = FakeRunner(_run_result("demo", [_cycle("demo", Decimal("0.42"), execution=_demo_execution())]))
    service = StrategyValidationService(
        settings,
        ExchangeFactory(settings),
        runner_factory=lambda _settings, _exchanges: fake_runner,
        open_risk_checker=lambda: {
            "checked": True,
            "configured": True,
            "demo": True,
            "open_spot_orders": 0,
            "open_swap_orders": 0,
            "swap_positions": 0,
        },
    )

    payload = service.validate_demo_window(strategy_name="triangular", cycles=1, symbol="BTC/USDT")

    assert payload["demo_window_validation"]["status"] == "Pass"
    assert payload["demo_window_validation"]["reasons"] == []
    assert payload["post_run_open_risk"]["demo"] is True


def test_validate_demo_window_fails_when_orders_remain_open(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.strategy_runtime.validation_min_demo_executions = 1
    fake_runner = FakeRunner(_run_result("demo", [_cycle("demo", Decimal("0.42"), execution=_demo_execution())]))
    service = StrategyValidationService(
        settings,
        ExchangeFactory(settings),
        runner_factory=lambda _settings, _exchanges: fake_runner,
        open_risk_checker=lambda: {
            "checked": True,
            "configured": True,
            "demo": True,
            "open_spot_orders": 1,
            "open_swap_orders": 0,
            "swap_positions": 0,
        },
    )

    payload = service.validate_demo_window(strategy_name="triangular", cycles=1, symbol="BTC/USDT")

    assert payload["demo_window_validation"]["status"] == "Fail"
    assert "open_spot_orders_after_run" in payload["demo_window_validation"]["reasons"]


def test_validate_demo_window_fast_blocks_when_target_guard_is_cooling_down(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _write_guard_cooldown(Path(settings.strategy_runtime.runtime_guard_path), "triangular-multi-route")
    fake_runner = FakeRunner(_run_result("demo", [_cycle("demo", Decimal("0.42"), execution=_demo_execution())]))
    service = StrategyValidationService(
        settings,
        ExchangeFactory(settings),
        runner_factory=lambda _settings, _exchanges: fake_runner,
        open_risk_checker=lambda: {"checked": True, "open_spot_orders": 1},
    )

    payload = service.validate_demo_window(strategy_name="triangular-multi-route", cycles=3, symbol="BTC/USDT")

    assert fake_runner.calls == []
    assert payload["demo_window_validation"]["status"] == "Fail"
    assert payload["demo_window_validation"]["reasons"] == ["runtime_guard_cooldown"]
    assert payload["demo_window_validation"]["metrics"]["cycles_completed"] == 0
    assert payload["demo_window_validation"]["metrics"]["total"] == 0
    assert payload["strategy_run"]["completed"] is False
    assert payload["strategy_run"]["stopped_reason"] == "runtime_guard_cooldown"
    assert payload["post_run_open_risk"] == {"checked": False, "reason": "runtime_guard_cooldown"}


def test_validate_demo_window_fails_when_residual_inventory_exceeds_tolerance(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.strategy_runtime.validation_min_demo_executions = 1
    execution = _demo_execution()
    execution["pnl_validation"]["residual_inventory_within_tolerance"] = False
    fake_runner = FakeRunner(_run_result("demo", [_cycle("demo", Decimal("0.42"), execution=execution)]))
    service = StrategyValidationService(
        settings,
        ExchangeFactory(settings),
        runner_factory=lambda _settings, _exchanges: fake_runner,
        open_risk_checker=lambda: {
            "checked": True,
            "configured": True,
            "demo": True,
            "open_spot_orders": 0,
            "open_swap_orders": 0,
            "swap_positions": 0,
        },
    )

    payload = service.validate_demo_window(strategy_name="triangular", cycles=1, symbol="BTC/USDT")

    assert payload["demo_window_validation"]["status"] == "Fail"
    assert "residual_inventory_above_tolerance" in payload["demo_window_validation"]["reasons"]


def test_promotion_status_blocks_live_canary_by_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    settings.strategy_runtime.validation_min_local_executions = 2
    settings.strategy_runtime.validation_min_demo_executions = 2
    _write_strategy_events(
        Path(settings.strategy_runtime.journal_path),
        [
            {"execution_mode": "paper", "strategy_name": "triangular", "net_profit": "0.20"},
            {"execution_mode": "paper", "strategy_name": "triangular", "net_profit": "0.25"},
            {"execution_mode": "demo", "strategy_name": "triangular", "net_profit": "0.30"},
            {"execution_mode": "demo", "strategy_name": "triangular", "net_profit": "0.35"},
        ],
    )
    service = StrategyValidationService(settings, ExchangeFactory(settings))

    payload = service.promotion_status(strategy_name="triangular")

    status = payload["strategy_promotion_status"][0]
    assert status["local"]["status"] == "Pass"
    assert status["demo"]["status"] == "Pass"
    assert status["live"]["status"] == "Fail"
    assert status["promotable_to_live_canary"] is False
    assert "live_canary_promotion_disabled" in status["live"]["reasons"]
    assert "agent_live_orders_disabled" in status["live"]["reasons"]


class FakeRunner:
    """Tiny runner double for validation service tests."""

    def __init__(self, result: StrategyRunResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def run(self, **kwargs: Any) -> StrategyRunResult:
        self.calls.append(kwargs)
        return self.result


def _settings(tmp_path: Path) -> Settings:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.validation_min_local_executions = 1
    settings.strategy_runtime.validation_min_demo_executions = 1
    settings.strategy_runtime.validation_min_win_rate_pct = Decimal("60")
    settings.strategy_runtime.validation_min_net_profit_usdt = Decimal("0")
    settings.strategy_runtime.validation_max_drawdown_usdt = Decimal("1")
    return settings


def _cycle(
    execution_mode: ExecutionMode,
    net_profit: Decimal,
    execution: dict[str, Any] | None = None,
) -> StrategyCycleResult:
    return StrategyCycleResult(
        cycle=1,
        strategy_name="triangular",
        decision="executed",
        execution_mode=execution_mode,
        opportunities_found=1,
        selected_opportunity_id="test-opportunity",
        risk_approved=True,
        risk_reasons=[],
        budget=None,
        lifecycle=None,
        execution=execution or {"dry_run": True, "status": "simulated"},
        net_profit=net_profit,
        message="validated",
    )


def _run_result(execution_mode: ExecutionMode, results: list[StrategyCycleResult]) -> StrategyRunResult:
    return StrategyRunResult(
        completed=True,
        strategy_name="triangular",
        execution_mode=execution_mode,
        cycles_completed=1,
        results=results,
        journal_path="memory",
        summary="fake validation run",
        demo_cumulative_net_pnl=sum((item.net_profit for item in results), Decimal("0")) if execution_mode == "demo" else None,
        demo_max_drawdown_usdt=Decimal("0") if execution_mode == "demo" else None,
    )


def _demo_execution() -> dict[str, Any]:
    return {
        "demo_orders_sent": True,
        "live_orders_sent": False,
        "provider_demo": True,
        "status": "closed",
        "net_pnl_usdt": "0.42",
        "pnl_validation": {
            "within_tolerance": True,
            "residual_inventory_within_tolerance": True,
            "exchange_receipts": {"complete": True},
        },
    }


def _write_strategy_events(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        json.dumps({"event": "strategy_cycle", "decision": "executed", **row}, ensure_ascii=False)
        for row in rows
    ]
    path.write_text("\n".join(events) + "\n", encoding="utf-8")


def _write_guard_cooldown(path: Path, strategy_name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(tz=UTC)
    path.write_text(
        json.dumps(
            {
                f"demo:{strategy_name}": {
                    "strategy_name": strategy_name,
                    "execution_mode": "demo",
                    "consecutive_failures": 0,
                    "consecutive_losses": 0,
                    "consecutive_market_data_failures": 3,
                    "consecutive_rate_limit_failures": 0,
                    "cooldown_until": (now + timedelta(minutes=5)).isoformat(),
                    "cooldown_seconds": 300,
                    "last_reason": "market_data_error:fixture",
                    "last_net_profit": "0",
                    "updated_at": now.isoformat(),
                }
            }
        ),
        encoding="utf-8",
    )
