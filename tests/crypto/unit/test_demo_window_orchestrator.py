"""Unit tests for OKX demo-window orchestration prechecks."""

from __future__ import annotations

from typing import Any

from quantbot.crypto.runtime.validation.demo_window_orchestrator import StrategyDemoWindowOrchestrator


def test_demo_window_orchestrator_blocks_health_exception_before_runner() -> None:
    runner = _Runner()
    orchestrator = StrategyDemoWindowOrchestrator(
        health_checker=lambda: (_ for _ in ()).throw(RuntimeError("dns unavailable")),
        guard_checker=_clean_guard,
        market_compare_checker=_candidate_market_compare,
        validation_report_checker=_clean_validation_report,
        demo_window_runner=runner,
    )

    payload = orchestrator.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        cycles=3,
        symbol="BTC/USDT",
    )

    assert payload["status"] == "Blocked"
    assert payload["completed"] is False
    assert payload["orders_attempted"] is False
    assert payload["block_reasons"] == ["health_check_failed"]
    assert payload["prechecks"]["health"]["ok"] is False
    assert payload["prechecks"]["health"]["error_type"] == "RuntimeError"
    assert runner.calls == 0


def test_demo_window_orchestrator_blocks_guard_cooldown_before_runner() -> None:
    runner = _Runner()
    orchestrator = StrategyDemoWindowOrchestrator(
        health_checker=_healthy_sandbox,
        guard_checker=lambda: {
            "strategy_guard_status": {
                "entries": [
                    {
                        "strategy_name": "triangular-multi-route",
                        "execution_mode": "demo",
                        "cooldown_active": True,
                    }
                ]
            }
        },
        market_compare_checker=_candidate_market_compare,
        validation_report_checker=_clean_validation_report,
        demo_window_runner=runner,
    )

    payload = orchestrator.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        cycles=3,
        symbol="BTC/USDT",
    )

    assert payload["status"] == "Blocked"
    assert payload["block_reasons"] == ["runtime_guard_cooldown"]
    assert payload["prechecks"]["guard"]["cooldown_active"] is True
    assert runner.calls == 0


def test_demo_window_orchestrator_requires_market_compare_candidate() -> None:
    runner = _Runner()
    orchestrator = StrategyDemoWindowOrchestrator(
        health_checker=_healthy_sandbox,
        guard_checker=_clean_guard,
        market_compare_checker=lambda: {
            "strategy_market_compare": {
                "orders_sent": False,
                "comparisons": [
                    {
                        "strategy_name": "triangular-multi-route",
                        "verdict": "observe_only",
                        "reasons": ["target_no_opportunity"],
                    }
                ],
            }
        },
        validation_report_checker=_clean_validation_report,
        demo_window_runner=runner,
    )

    payload = orchestrator.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        cycles=3,
        symbol="BTC/USDT",
    )

    assert payload["status"] == "Blocked"
    assert payload["block_reasons"] == ["missing_demo_preflight_candidate"]
    assert payload["prechecks"]["market_compare"]["candidate_strategies"] == []
    assert runner.calls == 0


def test_demo_window_orchestrator_runs_after_clean_prechecks() -> None:
    runner = _Runner(
        {
            "demo_window_validation": {"status": "Needs More Samples", "metrics": {"executed": 3}},
            "strategy_run": {
                "completed": True,
                "results": [
                    {"decision": "executed", "execution": {"demo_orders_sent": True, "live_orders_sent": False}},
                    {"decision": "executed", "execution": {"demo_orders_sent": True, "live_orders_sent": False}},
                ],
            },
            "post_run_open_risk": {
                "checked": True,
                "open_spot_orders": 0,
                "open_swap_orders": 0,
                "swap_positions": 0,
            },
        }
    )
    orchestrator = StrategyDemoWindowOrchestrator(
        health_checker=_healthy_sandbox,
        guard_checker=_clean_guard,
        market_compare_checker=_candidate_market_compare,
        validation_report_checker=_clean_validation_report,
        demo_window_runner=runner,
    )

    payload = orchestrator.run(
        strategy_name="triangular-multi-route",
        target_strategy_names=["triangular-multi-route"],
        cycles=3,
        symbol="BTC/USDT",
    )

    assert payload["status"] == "Executed"
    assert payload["completed"] is True
    assert payload["orders_attempted"] is True
    assert payload["live_orders_sent"] is False
    assert payload["block_reasons"] == []
    assert payload["demo_window"]["demo_window_validation"]["metrics"]["executed"] == 3
    assert runner.calls == 1


class _Runner:
    """Small callable test double for demo-window execution."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}
        self.calls = 0

    def __call__(self) -> dict[str, Any]:
        self.calls += 1
        return self.payload


def _healthy_sandbox() -> dict[str, Any]:
    return {
        "sandbox_check": {
            "ok": True,
            "live_orders_sent": False,
            "checks": {
                "ping": {"ok": True},
                "ticker": {"ok": True},
                "orderbook": {"ok": True},
                "spot_perp_quote": {"ok": True},
                "account": {"ok": True},
            },
        }
    }


def _clean_guard() -> dict[str, Any]:
    return {
        "strategy_guard_status": {
            "entries": [
                {
                    "strategy_name": "triangular-multi-route",
                    "execution_mode": "demo",
                    "cooldown_active": False,
                }
            ]
        }
    }


def _candidate_market_compare() -> dict[str, Any]:
    return {
        "strategy_market_compare": {
            "orders_sent": False,
            "comparisons": [
                {
                    "strategy_name": "triangular-multi-route",
                    "verdict": "demo_preflight_candidate",
                    "reasons": ["target_has_positive_edge"],
                }
            ],
        }
    }


def _clean_validation_report() -> dict[str, Any]:
    return {
        "strategy_validation_report": {
            "total": {
                "execution_quality_passed": True,
                "positive_cash_flow_negative_equity_delta": 0,
                "max_drawdown_usdt": "0",
            },
            "recommendations": ["Keep collecting same-size samples."],
        }
    }
