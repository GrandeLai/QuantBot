"""Unit tests for strategy-level PnL attribution reports."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.schema import StrategyRuntimeConfig
from quantbot.crypto.runtime.strategies.pnl_attribution import StrategyPnlAttributionService


def test_pnl_attribution_separates_cash_flow_from_account_equity(tmp_path: Path) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    _write_demo_attribution_journal(journal_path)
    service = StrategyPnlAttributionService(StrategyRuntimeConfig(journal_path=str(journal_path)))

    report = service.report(execution_mode="demo", strategy_name="triangular-multi-route", limit=50)

    assert report.summary.total_entries == 2
    assert report.summary.executed_entries == 2
    assert report.summary.strategy_cash_flow_net_pnl_usdt == Decimal("0.30")
    assert report.summary.account_equity_delta_usdt == Decimal("0.21")
    assert report.summary.account_attribution_gap_usdt == Decimal("-0.09")
    assert report.summary.max_residual_inventory_usdt == Decimal("0.01")
    assert report.summary.positive_cash_flow_negative_equity_delta == 1
    assert report.entries[0].classification == "account_equity_matched"
    assert report.entries[1].classification == "positive_cash_flow_negative_equity_delta"
    assert report.entries[1].account_attribution_gap_usdt == Decimal("-0.12")
    assert any("Account equity moved negative" in item for item in report.recommendations)


def _write_demo_attribution_journal(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "triangular-multi-route",
                        "decision": "executed",
                        "selected_opportunity_id": "okx-demo-triangular-1",
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
                        "selected_opportunity_id": "okx-demo-triangular-2",
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
