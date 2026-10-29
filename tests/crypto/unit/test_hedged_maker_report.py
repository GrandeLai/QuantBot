"""Tests for hedged-maker paper evaluation reports."""

from __future__ import annotations

import json
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.strategies.hedged_maker_report import HedgedMakerPaperEvaluationReportService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_hedged_maker_report_aggregates_paper_lifecycle_quality(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    journal_path = tmp_path / "strategy-events.jsonl"
    paper_state_path = tmp_path / "hedged-maker-paper-state.json"
    settings.strategy_runtime.journal_path = str(journal_path)
    settings.hedged_maker.paper_state_path = str(paper_state_path)
    _write_journal(journal_path)
    _write_state(paper_state_path)

    report = HedgedMakerPaperEvaluationReportService(settings).report(limit=10)
    payload = report.to_dict()

    assert payload["read_only"] is True
    assert payload["orders_sent"] is False
    assert payload["live_orders_sent"] is False
    assert payload["journal_path"] == str(journal_path)
    assert payload["paper_state_path"] == str(paper_state_path)
    assert payload["scanned_events"] == 3
    assert payload["summary"]["executed_events"] == 3
    assert payload["summary"]["lifecycle_counts"] == {
        "cancel_pending": 1,
        "filled_and_hedged": 1,
        "partially_filled_and_hedged": 1,
    }
    assert payload["summary"]["fill_events"] == 2
    assert payload["summary"]["partial_fill_events"] == 1
    assert payload["summary"]["adverse_selection_events"] == 1
    assert payload["summary"]["cancel_pending_events"] == 1
    assert payload["summary"]["total_expected_net_profit_usdt"] == "1.650000"
    assert payload["summary"]["total_realized_net_profit_usdt"] == "-0.100000"
    assert payload["summary"]["average_fill_ratio_pct"] == "62.50"
    assert payload["summary"]["max_hedge_slippage_multiplier"] == "3"
    assert payload["summary"]["state_order_counts"]["partial_open"] == 1
    assert payload["summary"]["active_state_orders"] == 2
    assert payload["entries"][0]["lifecycle_status"] == "partially_filled_and_hedged"
    assert payload["entries"][0]["fill_ratio_pct"] == "25"
    assert payload["entries"][1]["adverse_selection"] is True
    assert any("adverse" in recommendation for recommendation in payload["recommendations"])


def _write_journal(path: Path) -> None:
    events = [
        _event(
            status="partially_filled_and_hedged",
            net_profit="-0.05",
            expected="0.55",
            hedge_order={
                "quantity": "0.0005",
                "adverse_selection": False,
                "hedge_slippage_multiplier": "1",
                "fill_quality": {
                    "fill_ratio_pct": "25",
                    "filled_quantity": "0.0005",
                },
            },
            paper_order={"filled_quantity": "0.0005", "remaining_quantity": "0.0015"},
        ),
        _event(
            status="filled_and_hedged",
            net_profit="-0.05",
            expected="0.55",
            hedge_order={
                "quantity": "0.002",
                "adverse_selection": True,
                "hedge_slippage_multiplier": "3",
                "fill_quality": {
                    "fill_ratio_pct": "100",
                    "filled_quantity": "0.002",
                },
            },
            paper_order={"filled_quantity": "0.002", "remaining_quantity": "0"},
        ),
        _event(
            status="cancel_pending",
            net_profit="0",
            expected="0.55",
            hedge_order=None,
            paper_order={"cancel_effective_at": "2026-05-16T00:00:07+00:00"},
        ),
    ]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")


def _event(
    *,
    status: str,
    net_profit: str,
    expected: str,
    hedge_order: dict | None,
    paper_order: dict,
) -> dict:
    return {
        "event": "strategy_cycle",
        "execution_mode": "paper",
        "strategy_name": "hedged-maker",
        "decision": "executed",
        "selected_opportunity_id": f"hm-{status}",
        "net_profit": net_profit,
        "execution": {
            "expected_net_profit": expected,
            "net_profit": net_profit,
            "orders_sent": False,
            "live_orders_sent": False,
            "hedged_maker_lifecycle": {
                "status": status,
                "active_order_count": 1,
                "expected_net_profit_usdt": expected,
                "realized_net_profit_usdt": net_profit,
                "paper_order": paper_order,
                "hedge_order": hedge_order,
                "canceled_order_ids": ["seed-buy"] if status == "cancel_pending" else [],
            },
        },
    }


def _write_state(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "orders": [
                    {"order_id": "open-a", "status": "open"},
                    {"order_id": "partial-a", "status": "partial_open"},
                    {"order_id": "filled-a", "status": "filled"},
                    {"order_id": "canceled-a", "status": "canceled"},
                ]
            }
        ),
        encoding="utf-8",
    )
