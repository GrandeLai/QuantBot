"""独立现金流核验：手算样例、实际回放和历史记录损坏。"""

from copy import deepcopy
from decimal import Decimal
import io
import json
from pathlib import Path

import pytest

from quantbot.ai.evaluation import evaluate
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.runtime_cli import main
from tests.test_ai_runtime import setup


def manual_records():
    records = []

    def record(kind, payload):
        records.append({"sequence": len(records), "recorded_at": "2025-01-10T00:00:00+00:00", "kind": kind, "payload": payload})

    record("started", {"schema_version": 1, "config": {"execution": {"initial_cash": 1000}}, "spec": {"market": "stocks"}})
    # 两次买入、部分卖出、全部卖出；手算平均成本 10.505，最终净利 29.65。
    examples = [
        (1000, 1000, 0, 0, 0, 0, 1000, 10, None),
        (899.9, 1001.9, 10, 0, 1.9, .1, 1001.9, 10.2, ("buy", "10", "10.01", "10", ".1")),
        (789.9, 999.9, 20, 0, -.1, .2, 1001.9, 10.5, ("buy", "10", "11", "10.99", ".1")),
        (849.8, 1029.8, 15, 7.375, 22.425, .3, 1029.8, 12, ("sell", "5", "11.98", "12", ".1")),
        (1029.65, 1029.65, 0, 29.65, 0, .45, 1029.8, 12, ("sell", "15", "11.99", "12", ".15")),
    ]
    for index, (cash, equity, quantity, realized, unrealized, costs, peak, price, fill) in enumerate(examples):
        account = {"cash": cash, "equity": equity, "positions": {"SPY": str(quantity)} if quantity else {},
                   "realized_pnl": realized, "unrealized_pnl": unrealized, "estimated_execution_cost": costs,
                   "drawdown_fraction": float(Decimal(str(equity)) / Decimal(str(peak)) - 1)}
        orders = []
        if fill:
            side, size, executed, reference, cost = fill
            orders.append({"client_order_id": str(index), "symbol": "SPY", "side": side, "quantity": size,
                           "price": executed, "reference_price": reference, "estimated_cost": cost, "status": "filled"})
        record("account", {"as_of": f"2025-01-0{index + 1}T21:00:00+00:00", "account": account,
                           "valuation_prices": {"SPY": price}, "orders": orders})
    record("completed", {"execution_completed": True, "account": deepcopy(account)})
    return records


def test_manual_cashflows_costs_partial_sales_and_terminal_liquidation():
    result = reconcile_replay(manual_records())
    assert result["status"] == "reconciled" and result["discrepancies"] == []
    assert result["account_snapshots"] == 5 and result["fills"] == 4
    closing = result["closing_balances"]
    assert Decimal(closing["cash"]) == Decimal("1029.65")
    assert Decimal(closing["realized_pnl"]) == Decimal("29.65")
    assert Decimal(closing["estimated_execution_cost"]) == Decimal(".45")
    assert closing["positions"] == {}


@pytest.mark.parametrize("field", ["cash", "equity", "positions", "realized_pnl", "estimated_execution_cost"])
def test_saved_account_corruption_is_reported_with_time_and_expected_amount(field):
    records = manual_records()
    records[1]["payload"]["account"][field] = {"SPY": "10"} if field == "positions" else 123
    result = reconcile_replay(records)
    assert result["status"] == "mismatch"
    difference = next(row for row in result["discrepancies"] if row["field"] == field)
    assert difference["as_of"] == records[1]["payload"]["as_of"]
    assert "expected" in difference and "reported" in difference


def test_duplicate_execution_and_overselling_cannot_form_a_valid_ledger():
    records = manual_records()
    records[3]["payload"]["orders"][0]["client_order_id"] = "1"
    with pytest.raises(ValueError, match="重复成交"):
        reconcile_replay(records)
    records = manual_records()
    records[4]["payload"]["orders"][0]["quantity"] = "21"
    with pytest.raises(ValueError, match="卖出超过持仓"):
        reconcile_replay(records)


def test_unverifiable_old_logs_and_incomplete_runs_are_never_marked_complete(tmp_path):
    records = manual_records()
    del records[1]["payload"]["valuation_prices"]
    assert reconcile_replay(records)["status"] == "unavailable"
    records = manual_records()[:-1]
    path = tmp_path / "unfinished.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    output = io.StringIO()
    assert main(["replay-show", str(path), "--reconcile"], stdout=output) == 2
    result = json.loads(output.getvalue())
    assert result["status"] == "unfinished" and result["accounting"]["status"] == "reconciled"


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_real_candidate_and_baseline_reconcile_without_changing_evidence(tmp_path, market):
    store, job, _, _ = setup(tmp_path, market)
    report = evaluate(store, job["id"], last_bars=8)
    for section in ("candidate", "baseline"):
        path = Path(report[section]["journal_path"])
        original = path.read_bytes()
        inspection = inspect_replay(path)
        result = reconcile_replay(inspection["records"])
        assert result["status"] == "reconciled", result
        assert result["fills"] == report[section]["fills"]
        output = io.StringIO()
        unused_root = tmp_path / "must-not-be-created"
        assert main(["--root", str(unused_root), "replay-show", str(path), "--reconcile"], stdout=output) == 0
        assert json.loads(output.getvalue())["accounting"]["status"] == "reconciled"
        assert path.read_bytes() == original and not unused_root.exists()
