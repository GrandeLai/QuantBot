"""Unit tests for detached autopilot runtime orchestration."""

from __future__ import annotations

import json
from pathlib import Path

from quantbot.crypto.runtime.application import TradingAssistantApp
from quantbot.crypto.runtime.autopilot.models import AutopilotState
from quantbot.crypto.runtime.autopilot.runtime import AutopilotRuntime
from quantbot.crypto.runtime.autopilot.state import AutopilotStateStore
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import TradingConfig


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_autopilot_state_store_returns_never_run_when_missing(tmp_path: Path) -> None:
    store = AutopilotStateStore(tmp_path / "autopilot-state.json")

    state = store.read()

    assert state.status == "NeverRun"
    assert state.cycles_completed == 0
    assert state.live_orders_sent is False


def test_autopilot_state_store_writes_json_atomically(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "autopilot-state.json"
    store = AutopilotStateStore(path)
    state = AutopilotState(
        status="Completed",
        mode="paper",
        strategy_name="all",
        symbol="BTC/USDT",
        cycles_requested=1,
        cycles_completed=1,
        stopped_reason=None,
        consecutive_blocked_cycles=0,
        live_orders_sent=False,
        orders_attempted=False,
        last_payload_summary={"decision_counts": {"executed": 1}},
    )

    store.write(state)

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["status"] == "Completed"
    assert raw["mode"] == "paper"
    assert raw["last_payload_summary"]["decision_counts"]["executed"] == 1
    assert not list(path.parent.glob("*.tmp"))


def test_paper_autopilot_completes_one_cycle_and_persists_state(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.autopilot_state_path = str(tmp_path / "autopilot-state.json")
    runtime = AutopilotRuntime(
        settings=settings,
        state_store=AutopilotStateStore(settings.strategy_runtime.autopilot_state_path),
        paper_runner=lambda: {
            "strategy_run": {
                "completed": True,
                "results": [
                    {"decision": "executed", "risk_reasons": [], "execution": {"dry_run": True, "live_orders_sent": False}}
                ],
            }
        },
        validation_reporter=lambda mode, strategy, limit: {"strategy_validation_report": {"total": {"executed": 1}}},
        operator_brief=lambda mode, strategy, limit: {"strategy_operator_brief": {"read_only": True, "live_orders_sent": False}},
        guard_status=lambda mode, strategy: {"strategy_guard_status": {"entries": []}},
        promotion_status=lambda strategy: {"strategy_promotion_status": []},
        sleeper=lambda seconds: None,
    )

    result = runtime.run(
        mode="paper",
        strategy_name="all",
        symbol="BTC/USDT",
        cycles=1,
        interval_seconds=0,
        demo_cycles_per_window=1,
        report_limit=10,
    )

    assert result.status == "Completed"
    assert result.cycles_completed == 1
    assert result.live_orders_sent is False
    assert result.orders_attempted is False
    state = AutopilotStateStore(settings.strategy_runtime.autopilot_state_path).read()
    assert state.status == "Completed"
    assert state.last_payload_summary["decision_counts"]["executed"] == 1


def test_paper_autopilot_refuses_live_trading_config(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.autopilot_state_path = str(tmp_path / "autopilot-state.json")
    settings.trading = TradingConfig(live_trading=True, dry_run=False, require_confirm_before_order=False)
    runtime = AutopilotRuntime(
        settings=settings,
        state_store=AutopilotStateStore(settings.strategy_runtime.autopilot_state_path),
        paper_runner=lambda: {"strategy_run": {"completed": True, "results": []}},
        validation_reporter=lambda mode, strategy, limit: {"strategy_validation_report": {}},
        operator_brief=lambda mode, strategy, limit: {"strategy_operator_brief": {}},
        guard_status=lambda mode, strategy: {"strategy_guard_status": {"entries": []}},
        promotion_status=lambda strategy: {"strategy_promotion_status": []},
        sleeper=lambda seconds: None,
    )

    result = runtime.run(
        mode="paper",
        strategy_name="all",
        symbol="BTC/USDT",
        cycles=1,
        interval_seconds=0,
        demo_cycles_per_window=1,
        report_limit=10,
    )

    assert result.status == "Stopped"
    assert result.stopped_reason == "paper_live_trading_enabled"
    assert AutopilotStateStore(settings.strategy_runtime.autopilot_state_path).read().status == "Stopped"


def test_demo_autopilot_refuses_live_trading_config(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.autopilot_state_path = str(tmp_path / "autopilot-state.json")
    settings.trading = TradingConfig(live_trading=True, dry_run=False, require_confirm_before_order=False)
    runtime = AutopilotRuntime(
        settings=settings,
        state_store=AutopilotStateStore(settings.strategy_runtime.autopilot_state_path),
        paper_runner=lambda: {"strategy_run": {"completed": True, "results": []}},
        demo_runner=lambda: {
            "strategy_demo_window_orchestration": {
                "status": "Executed",
                "completed": True,
                "orders_attempted": True,
                "live_orders_sent": False,
                "block_reasons": [],
            }
        },
        validation_reporter=lambda mode, strategy, limit: {"strategy_validation_report": {}},
        operator_brief=lambda mode, strategy, limit: {"strategy_operator_brief": {}},
        guard_status=lambda mode, strategy: {"strategy_guard_status": {"entries": []}},
        promotion_status=lambda strategy: {"strategy_promotion_status": []},
        sleeper=lambda seconds: None,
    )

    result = runtime.run(
        mode="demo",
        strategy_name="triangular-multi-route",
        symbol="BTC/USDT",
        cycles=1,
        interval_seconds=0,
        demo_cycles_per_window=1,
        report_limit=10,
    )

    assert result.status == "Stopped"
    assert result.stopped_reason == "demo_live_trading_enabled"
    assert result.cycles_completed == 0


def test_demo_autopilot_stops_on_live_order_signal(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.autopilot_state_path = str(tmp_path / "autopilot-state.json")
    runtime = AutopilotRuntime(
        settings=settings,
        state_store=AutopilotStateStore(settings.strategy_runtime.autopilot_state_path),
        paper_runner=lambda: {"strategy_run": {"completed": True, "results": []}},
        demo_runner=lambda: {
            "strategy_demo_window_orchestration": {
                "status": "Executed",
                "completed": True,
                "orders_attempted": True,
                "live_orders_sent": True,
                "block_reasons": [],
            }
        },
        validation_reporter=lambda mode, strategy, limit: {"strategy_validation_report": {"total": {"executed": 1}}},
        operator_brief=lambda mode, strategy, limit: {"strategy_operator_brief": {"read_only": True}},
        guard_status=lambda mode, strategy: {"strategy_guard_status": {"entries": []}},
        promotion_status=lambda strategy: {"strategy_promotion_status": []},
        sleeper=lambda seconds: None,
    )

    result = runtime.run(
        mode="demo",
        strategy_name="triangular-multi-route",
        symbol="BTC/USDT",
        cycles=3,
        interval_seconds=0,
        demo_cycles_per_window=1,
        report_limit=10,
    )

    assert result.status == "Stopped"
    assert result.stopped_reason == "live_order_detected"
    assert result.cycles_completed == 1
    assert result.live_orders_sent is True


def test_autopilot_stops_after_consecutive_blocked_cycles(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.autopilot_state_path = str(tmp_path / "autopilot-state.json")
    settings.strategy_runtime.autopilot_max_consecutive_blocked_cycles = 2
    runtime = AutopilotRuntime(
        settings=settings,
        state_store=AutopilotStateStore(settings.strategy_runtime.autopilot_state_path),
        paper_runner=lambda: {
            "strategy_run": {
                "completed": True,
                "results": [{"decision": "skipped", "risk_reasons": ["no_opportunity"], "execution": None}],
            }
        },
        validation_reporter=lambda mode, strategy, limit: {"strategy_validation_report": {"total": {"executed": 0}}},
        operator_brief=lambda mode, strategy, limit: {"strategy_operator_brief": {"read_only": True}},
        guard_status=lambda mode, strategy: {"strategy_guard_status": {"entries": []}},
        promotion_status=lambda strategy: {"strategy_promotion_status": []},
        sleeper=lambda seconds: None,
    )

    result = runtime.run(
        mode="paper",
        strategy_name="all",
        symbol="BTC/USDT",
        cycles=5,
        interval_seconds=0,
        demo_cycles_per_window=1,
        report_limit=10,
    )

    assert result.status == "Stopped"
    assert result.stopped_reason == "max_consecutive_blocked_cycles"
    assert result.cycles_completed == 2
    assert result.cycles[-1].block_reasons == ["no_opportunity"]


def test_application_autopilot_run_status_and_report(tmp_path: Path) -> None:
    config_path = tmp_path / "autopilot.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "strategy-events.jsonl"}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  retrospective_path: {tmp_path / "strategy-retrospective.md"}
  retrospective_state_path: {tmp_path / "strategy-retrospective.state.json"}
  evolution_state_path: {tmp_path / "strategy-evolution.state.json"}
  evolution_report_path: {tmp_path / "strategy-evolution.md"}
  autopilot_state_path: {tmp_path / "autopilot-state.json"}
  review_min_samples: 1
""".strip(),
        encoding="utf-8",
    )
    app = TradingAssistantApp(config_path=config_path)

    run_payload = app.autopilot_run(
        mode="paper",
        strategy_name="cross-exchange",
        symbol="BTC/USDT",
        cycles=1,
        interval_seconds=0,
        demo_cycles_per_window=1,
        target_exchange="okx",
        report_limit=10,
        include_private_health=False,
    )
    status_payload = app.autopilot_status()
    report_payload = app.autopilot_report(mode="paper", strategy_name="cross-exchange", report_limit=10)

    assert run_payload["autopilot_run"]["status"] == "Completed"
    assert status_payload["autopilot_status"]["status"] == "Completed"
    assert report_payload["autopilot_report"]["state"]["status"] == "Completed"
    assert report_payload["autopilot_report"]["live_orders_sent"] is False
