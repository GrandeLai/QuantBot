from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.factors.paired_study import finalize, screen_market


def fixture(*, delta=.002, net=.004, kept=30):
    calendars, events = {}, []
    for year in (2020, 2021, 2022):
        window = str(year)
        times = [datetime(year, 1, 1, tzinfo=UTC)+timedelta(days=i) for i in range(160)]
        calendars[window] = times
        for cost in (15, 30):
            for i in range(40):
                events.append({"window": window, "cost_bps": cost, "session_index": i*4,
                    "signal_at": times[i*4].isoformat(), "factor_valid": True,
                    "kept": i < kept, "kept_filled": i < kept,
                    "candidate_contribution": net, "baseline_contribution": net-delta,
                    "incremental_contribution": delta, "risk_hold": None,
                    "candidate_open": False, "baseline_open": False})
    return {"market": "crypto", "hypothesis": "btc_retest_confirmation", "events": events}, calendars


def test_all_opportunities_including_rejected_enter_both_statistic_denominators():
    analysis, calendars = fixture()
    row = screen_market(analysis, calendars)
    assert row["reasons"] == []
    for cost in row["costs"]:
        assert cost["totals"]["opportunities"] == 120
        assert cost["totals"]["kept"] == 90
        assert cost["bootstrap"]["net_contribution_per_opportunity"]["observed"] == pytest.approx(.004)
        assert cost["bootstrap"]["increment_per_opportunity"]["observed"] == pytest.approx(.002)
    stock = deepcopy(row)
    stock.update(market="stocks", hypothesis="etf_switch_cost_coverage")
    result = finalize([row, stock], [{"p_value": 1.0}]*124)
    assert result["correction_family_size"] == 126 and all(result["pool_by_market"].values())
    assert not result["formal_admission"] and not result["trading_enabled"]


@pytest.mark.parametrize("delta,net", [(-.002,.004),(.002,-.004),(0,.004)])
def test_positive_net_or_delta_alone_cannot_pass(delta, net):
    analysis, calendars = fixture(delta=delta, net=net)
    row = screen_market(analysis, calendars)
    assert row["reasons"] and row["p_value"] == 1


def test_insufficient_retained_and_cancelled_buys_do_not_count_as_trades():
    analysis, calendars = fixture(kept=4)
    row = screen_market(analysis, calendars)
    assert row["status"] == "insufficient_evidence" and row["p_value"] == 1
    assert row["costs"][0]["bootstrap"]["status"] == "insufficient_evidence"
    analysis, calendars = fixture()
    for event in analysis["events"]:
        event["kept_filled"] = False
    row = screen_market(analysis, calendars)
    assert "cost_15:insufficient_total_kept_filled" in row["reasons"]


def test_stress_failures_and_risk_holds_cannot_be_hidden_by_other_cost():
    analysis, calendars = fixture()
    for event in analysis["events"]:
        if event["cost_bps"] == 30:
            event["incremental_contribution"] = -.001
    row = screen_market(analysis, calendars)
    assert not row["costs"][0]["reasons"] and row["p_value"] == 1
    analysis, calendars = fixture()
    analysis["events"][0]["risk_hold"] = "drawdown_limit"
    assert "cost_15:branch_risk_hold" in screen_market(analysis, calendars)["reasons"]


def test_incomplete_family_and_wrong_time_mapping_fail_closed():
    analysis, calendars = fixture()
    row = screen_market(analysis, calendars)
    with pytest.raises(ValueError):
        finalize([row], [{"p_value": 1}]*124)
    analysis["events"][0]["session_index"] = 1
    with pytest.raises(ValueError, match="timestamp"):
        screen_market(analysis, calendars)
