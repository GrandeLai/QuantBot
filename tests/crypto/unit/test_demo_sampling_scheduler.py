"""Unit tests for same-size OKX demo sampling schedules."""

from __future__ import annotations

from typing import Any

from quantbot.crypto.runtime.validation.demo_sampling_scheduler import StrategyDemoSamplingScheduler


def test_demo_sampling_scheduler_runs_same_size_windows_with_interval() -> None:
    runner = _WindowRunner(
        [
            _executed_window(orders_attempted=True),
            _executed_window(orders_attempted=True),
        ]
    )
    sleeps: list[int] = []
    scheduler = StrategyDemoSamplingScheduler(window_runner=runner, sleeper=sleeps.append)

    payload = scheduler.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        windows=2,
        cycles_per_window=3,
        symbol="BTC/USDT",
        interval_seconds=60,
        size_stage={"demo_order_size_multiplier": "2", "demo_max_order_value_usdt": "20"},
    )

    assert payload["status"] == "Completed"
    assert payload["completed"] is True
    assert payload["windows_requested"] == 2
    assert payload["windows_completed"] == 2
    assert payload["cycles_per_window"] == 3
    assert payload["same_size_enforced"] is True
    assert payload["size_stage"]["demo_order_size_multiplier"] == "2"
    assert payload["orders_attempted"] is True
    assert payload["live_orders_sent"] is False
    assert payload["stop_reason"] is None
    assert [window["window_index"] for window in payload["windows"]] == [1, 2]
    assert sleeps == [60]


def test_demo_sampling_scheduler_stops_after_blocked_precheck() -> None:
    runner = _WindowRunner(
        [
            {
                "strategy_demo_window_orchestration": {
                    "status": "Blocked",
                    "completed": False,
                    "orders_attempted": False,
                    "live_orders_sent": False,
                    "block_reasons": ["runtime_guard_cooldown"],
                }
            },
            _executed_window(orders_attempted=True),
        ]
    )
    scheduler = StrategyDemoSamplingScheduler(window_runner=runner, sleeper=lambda seconds: None)

    payload = scheduler.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        windows=3,
        cycles_per_window=3,
        symbol="BTC/USDT",
        interval_seconds=0,
        size_stage={"demo_order_size_multiplier": "2", "demo_max_order_value_usdt": "20"},
    )

    assert payload["status"] == "Stopped"
    assert payload["completed"] is False
    assert payload["windows_completed"] == 0
    assert payload["stop_reason"] == "precheck_blocked:runtime_guard_cooldown"
    assert len(payload["windows"]) == 1
    assert runner.calls == 1


def test_demo_sampling_scheduler_stops_when_window_attempts_no_orders() -> None:
    runner = _WindowRunner([_executed_window(orders_attempted=False)])
    scheduler = StrategyDemoSamplingScheduler(window_runner=runner, sleeper=lambda seconds: None)

    payload = scheduler.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        windows=2,
        cycles_per_window=3,
        symbol="BTC/USDT",
        interval_seconds=0,
        size_stage={"demo_order_size_multiplier": "2", "demo_max_order_value_usdt": "20"},
    )

    assert payload["status"] == "Stopped"
    assert payload["completed"] is False
    assert payload["windows_completed"] == 0
    assert payload["stop_reason"] == "no_demo_orders_attempted"
    assert runner.calls == 1


class _WindowRunner:
    """Return preconfigured demo-window payloads."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self.payloads = payloads
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        payload = self.payloads[self.calls]
        self.calls += 1
        return payload


def _executed_window(orders_attempted: bool) -> dict[str, Any]:
    return {
        "strategy_demo_window_orchestration": {
            "status": "Executed",
            "completed": True,
            "orders_attempted": orders_attempted,
            "live_orders_sent": False,
            "block_reasons": [],
            "demo_window": {
                "demo_window_validation": {"status": "Needs More Samples", "metrics": {"executed": 1}},
                "strategy_run": {
                    "results": [
                        {
                            "decision": "executed" if orders_attempted else "skipped",
                            "execution": {"demo_orders_sent": orders_attempted, "live_orders_sent": False},
                        }
                    ]
                },
            },
        }
    }
