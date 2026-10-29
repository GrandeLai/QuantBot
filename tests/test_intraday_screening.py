from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal

import pytest

from quantbot.intraday.research import screen_scenario


STATISTICS = {
    "minimum_filled_per_window": 5,
    "minimum_total_filled": 30,
    "minimum_positive_windows": 2,
    "minimum_feature_coverage": 0.95,
}


def account_report(window, market, *, control=False):
    """Synthetic, reconciled round trips; a stopped control keeps its cash curve."""
    symbol = "BTC/USDT" if market == "crypto" else "SPY"
    cash = Decimal("1000")
    costs = Decimal(0)
    events, curve = [], []
    entries = 50 if control else 60
    for index in range(60):
        day = date(2025, 1, 1) + timedelta(days=window * 100 + index)
        if index < entries:
            for side, clock, price in (
                ("buy", "10:00", Decimal("100.15")),
                ("sell", "11:00", Decimal("99.15" if control else "101.15")),
            ):
                at = f"{day}T{clock}:00+00:00"
                cash += price if side == "sell" else -price
                costs += Decimal("0.15")
                events.append({
                    "status": "filled", "client_order_id": f"{day}:{side}",
                    "at": at, "bar_open": at, "remaining": "0", "symbol": symbol,
                    "quantity": "1", "price": str(price), "side": side,
                    "bar_capacity": "100", "capacity_used_after": "1",
                    "estimated_cost": "0.15", "cost_components": {"fee": "0.15"},
                })
        curve.append({"day": str(day), "equity": float(cash)})
    return {
        "window": f"synthetic{window}", "market": market,
        "initial_equity": 1000, "daily_curve": curve, "events": events,
        "account": {"cash": str(cash), "positions": {}, "estimated_execution_cost": str(costs)},
        "net_return": float(cash / 1000 - 1), "entry_orders_filled": entries,
        "partial_fills": 0, "feature_coverage": 1.0,
        "risk_hold": "drawdown_limit" if control else None, "session_violations": [],
    }


def pairs(market="crypto"):
    return [(account_report(i, market), account_report(i, market, control=True)) for i in range(3)]


def screen(values):
    return screen_scenario({}, values, STATISTICS, cost=15, latency=1)


@pytest.mark.parametrize("market", ["crypto", "stocks"])
@pytest.mark.parametrize("hold", ["drawdown_limit", "daily_loss_limit"])
def test_normal_control_stop_cannot_veto_profitable_candidate(market, hold):
    values = pairs(market)
    for candidate, control in values:
        control["risk_hold"] = hold
        assert candidate["net_return"] == 0.06
        assert control["net_return"] == -0.05
        assert all(row["equity"] == 950 for row in control["daily_curve"][49:])
    stopped = screen(values)
    unlabelled = deepcopy(values)
    for _, control in unlabelled:
        control["risk_hold"] = None
    baseline = screen(unlabelled)
    assert stopped["reasons"] == baseline["reasons"] == []
    for field in ("net_daily_return", "increment_daily_return", "p_value"):
        assert stopped[field] == baseline[field]
    assert stopped["net_daily_return"]["ci_low"] > 0
    assert stopped["increment_daily_return"]["ci_low"] > 0
    assert all(row["control_risk_hold"] == hold for row in stopped["windows"])
    assert all(row["candidate_risk_hold"] is None for row in stopped["windows"])


@pytest.mark.parametrize("hold", ["drawdown_limit", "daily_loss_limit", "nonpositive_equity"])
def test_candidate_risk_stop_still_rejects(hold):
    values = pairs()
    values[0][0]["risk_hold"] = hold
    assert "synthetic0:risk_hold_or_session_residual" in screen(values)["reasons"]


@pytest.mark.parametrize("branch", [0, 1])
def test_session_residual_on_either_branch_still_rejects(branch):
    values = pairs()
    values[0][branch]["session_violations"] = [{"reason": "unliquidated_session_end"}]
    assert "synthetic0:risk_hold_or_session_residual" in screen(values)["reasons"]


@pytest.mark.parametrize("hold", ["nonpositive_equity", "unknown_execution_failure"])
def test_control_abnormal_risk_hold_still_rejects(hold):
    values = pairs()
    values[0][1]["risk_hold"] = hold
    assert "synthetic0:control_abnormal_risk_hold" in screen(values)["reasons"]


@pytest.mark.parametrize("branch", [0, 1])
@pytest.mark.parametrize("failure", ["ledger", "capacity"])
def test_accounting_or_execution_failure_on_either_branch_aborts(branch, failure):
    values = pairs()
    report = values[0][branch]
    if failure == "ledger":
        report["account"]["cash"] = "999"
        message = "落盘成交重算与账户不一致"
    else:
        report["events"][0]["bar_capacity"] = "0.5"
        message = "成交超过共享分钟量能预算"
    with pytest.raises(ValueError, match=message):
        screen(values)
