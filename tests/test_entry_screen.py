"""Synthetic event contracts only: no market returns, models or trading calls."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json

import numpy as np
import pytest

from quantbot.factors.entry_context import FACTOR_IDS
from quantbot.factors.entry_screen import PROTOCOL, _bootstrap, _circular_sums, evaluate_market, finalize_pool


def fixture_events(*, dropped=-0.002, all_keep=False, sessions=160):
    calendars, events = {}, []
    for year in (2020, 2021, 2022):
        window = str(year)
        start = datetime(year, 1, 1, tzinfo=UTC)
        calendars[window] = [(start+timedelta(days=i)).isoformat() for i in range(sessions)]
        for i in range(40):
            kept = all_keep or i % 2 == 0
            contribution = 0.004 if i % 2 == 0 else dropped
            factors = dict(zip(FACTOR_IDS, (1.2, 0.9, 0.8, 0.9) if kept else (0.8, 0.4, 1.2, 0.5), strict=True))
            events.append({"window": window, "signal_at": calendars[window][i*4],
                           "account_contribution": contribution, "net_return": contribution*10,
                           "factors": factors, "market_state": "synthetic"})
    return {15: events, 30: deepcopy(events)}, calendars


def test_sparse_sessions_never_dilute_event_denominators_and_bootstrap_is_deterministic():
    events, calendars = fixture_events()
    first = evaluate_market("crypto", events, calendars)
    assert first == evaluate_market("crypto", events, calendars)
    longer = {window: values + [(datetime.fromisoformat(values[0])+timedelta(days=i)).isoformat()
                               for i in range(160, 320)] for window, values in calendars.items()}
    second = evaluate_market("crypto", events, longer)
    for record, expanded in zip(first, second, strict=True):
        assert record["reasons"] == []
        cost, more_dates = record["costs"][0], expanded["costs"][0]
        assert cost["totals"]["baseline_events"] == 120
        assert cost["totals"]["kept_events"] == 60
        assert cost["totals"]["filtered_events"] == 60
        for result in (cost, more_dates):
            bootstrap = result["bootstrap"]
            assert bootstrap["net_contribution_per_kept_event"]["observed"] == pytest.approx(0.004)
            assert bootstrap["increment_per_baseline_event"]["observed"] == pytest.approx(0.001)
            assert bootstrap["replicates"] == 4999 and bootstrap["block_sessions"] == 20
        assert cost["folds"][0]["foregone_profit_contribution"] == 0
        assert cost["folds"][0]["avoided_loss_contribution"] == pytest.approx(0.04)
    # Evidence output can be written as strict JSON, including all bootstrap values.
    json.dumps(first, allow_nan=False)


def test_higher_kept_trade_mean_cannot_hide_discarded_positive_profit():
    events, calendars = fixture_events(dropped=0.002)
    records = evaluate_market("crypto", events, calendars)
    for record in records:
        cost = record["costs"][0]
        assert cost["folds"][0]["kept_mean_net_return"] > cost["folds"][0]["baseline_mean_net_return"]
        assert cost["totals"]["kept_contribution"] > 0
        assert cost["totals"]["incremental_contribution"] < 0
        assert cost["totals"]["foregone_profit_contribution"] == pytest.approx(0.12)
        assert record["status"] == "rejected"
        assert "cost_15:nonpositive_mean_window_increment" in record["reasons"]


@pytest.mark.parametrize("empty", [False, True])
def test_no_filter_or_no_events_cannot_pass_as_increment(empty):
    events, calendars = fixture_events(all_keep=True)
    if empty:
        events = {15: [], 30: []}
    records = evaluate_market("crypto", events, calendars)
    assert all(row["status"] == "insufficient_evidence" and row["p_value"] == 1 for row in records)
    assert all("cost_15:insufficient_filtered_events" in row["reasons"] for row in records)


def test_missing_factor_values_reduce_coverage_and_do_not_change_other_filters():
    events, calendars = fixture_events()
    for cost in events:
        for i, row in enumerate(events[cost]):
            if i % 40 < 3:
                row["factors"][FACTOR_IDS[0]] = (None, float("nan"), float("inf"))[i % 40]
    records = evaluate_market("stocks", events, calendars)
    rejected = records[0]
    assert rejected["status"] == "insufficient_evidence" and rejected["p_value"] == 1
    for fold in rejected["costs"][0]["folds"]:
        assert fold["feature_coverage"] == pytest.approx(37/40)
        assert fold["missing_feature_events"] == 3
    assert all(row["reasons"] == [] for row in records[1:])


def test_each_cost_and_joint_positive_windows_are_required():
    events, calendars = fixture_events()
    for row in events[30]:
        if row["factors"][FACTOR_IDS[0]] < 1 and row["window"] in ("2021", "2022"):
            row["account_contribution"] = 0.002
    records = evaluate_market("crypto", events, calendars)
    for row in records:
        base, stress = row["costs"]
        assert base["reasons"] == []
        assert stress["jointly_positive_windows"] == 1
        assert row["status"] == "rejected"
        assert "cost_30:fewer_than_two_jointly_positive_windows" in row["reasons"]
        assert row["p_value"] == max(base["p_value"], stress["p_value"])


def test_failed_window_stability_keeps_small_raw_p_but_enters_holm_as_one():
    events, calendars = fixture_events()
    for row in events[30]:
        if row["factors"][FACTOR_IDS[0]] >= 1:
            row["account_contribution"] = 0.008 if row["window"] == "2020" else -0.0001
            row["net_return"] = row["account_contribution"]*10
    rejected = evaluate_market("crypto", events, calendars)
    for row in rejected:
        base, stress = row["costs"]
        assert base["reasons"] == [] and base["p_value"] < 0.001
        assert stress["totals"]["kept_events"] == 60
        assert stress["jointly_positive_windows"] == 1
        assert stress["raw_p_value"] == pytest.approx(1/5000)
        assert stress["bootstrap"]["p_value"] == stress["raw_p_value"]
        assert stress["p_value"] == 1 and row["p_value"] == 1
        assert row["raw_p_value"] == pytest.approx(1/5000)
    # Four failed small raw p values must not move these other hypotheses down
    # the Holm ranking. Their first multiplier stays 124, not 120.
    others = [row for row in family_rows(0.00041) if row["market"] == "stocks"]
    final = finalize_pool(rejected+others, [1.0]*116)
    assert all(row["holm_p_value"] == 1 for row in final["results"][:4])
    assert final["results"][4]["holm_p_value"] == pytest.approx(124*0.00041)
    assert final["pool_by_market"] == {"crypto": [], "stocks": []}


def test_calendar_block_sums_match_explicit_circular_indices():
    grid = np.arange(92, dtype=float).reshape(23, 4)
    for width in (1, 3, 20, 23):
        expected = np.array([grid[(np.arange(width)+start) % len(grid)].sum(axis=0)
                             for start in range(len(grid))])
        np.testing.assert_array_equal(_circular_sums(grid, width), expected)


def test_bootstrap_resamples_windows_separately_and_retains_multiple_same_session_events():
    # A full 20-session block always contains every event in its window, whatever its start.
    grids = [np.zeros((20, 4)) for _ in range(3)]
    for grid in grids:
        grid[0] = (2, 1, 0.004, 0.002)
    result = _bootstrap(grids)
    assert result["net_contribution_per_kept_event"]["observed"] == pytest.approx(0.004)
    assert result["increment_per_baseline_event"]["observed"] == pytest.approx(0.001)
    assert result["increment_per_baseline_event"]["ci_low"] == pytest.approx(0.001)
    assert result["p_value"] == pytest.approx(1/5000)


def test_bootstrap_replicates_without_events_are_failed_evidence():
    grids = [np.zeros((200, 4)) for _ in range(3)]
    for grid in grids:
        grid[0] = (1, 1, 0.004, 0)
        grid[1] = (1, 0, 0, 0.002)
    result = _bootstrap(grids)
    assert result["net_contribution_per_kept_event"]["invalid_replicates"] > 0
    assert result["net_contribution_per_kept_event"]["ci_low"] is None
    assert result["increment_per_baseline_event"]["ci_low"] is None


def family_rows(p_value=1.0):
    return [{"key": f"{market}:{factor}", "market": market, "factor": factor,
             "p_value": p_value, "reasons": [], "status": "pending_multiplicity"}
            for market in ("crypto", "stocks") for factor in FACTOR_IDS]


def test_holm_includes_all_eight_hypotheses_and_frozen_history_without_mutating_inputs():
    rows = family_rows()
    rows[0]["p_value"] = 0.0005
    saved = deepcopy(rows)
    alone = finalize_pool(rows, [])
    penalized = finalize_pool(rows, [1.0]*116)
    assert alone["pool_by_market"]["crypto"] == [FACTOR_IDS[0]]
    assert penalized["correction_family_size"] == 124
    assert penalized["results"][0]["holm_p_value"] == pytest.approx(0.062)
    assert penalized["pool_by_market"] == {"crypto": [], "stocks": []}
    assert rows == saved
    assert penalized["formal_admission"] is False and penalized["model_calls"] == 0


def test_pool_uses_fixed_priority_not_validation_p_value_or_input_order():
    rows = family_rows(0.00001)
    rows[2]["p_value"] = 0.000001
    result = finalize_pool(list(reversed(rows)), [1.0]*116)
    assert result["pool_by_market"] == {market: list(FACTOR_IDS[:2]) for market in ("crypto", "stocks")}
    assert result["results"][2]["status"] == "pool_limit"
    assert result["results"][2]["selected"] is False


@pytest.mark.parametrize("defect", ["market", "cost", "windows", "continuous", "unknown_window", "date",
                                      "factor", "missing_factor", "contribution", "net_return", "calendar_order"])
def test_invalid_evidence_is_rejected(defect):
    events, calendars = fixture_events()
    market = "crypto"
    if defect == "market":
        market = "unknown"
    elif defect == "cost":
        events.pop(30)
    elif defect == "windows":
        calendars.pop("2022")
    elif defect == "continuous":
        calendars["continuous"] = calendars.pop("2022")
    elif defect == "unknown_window":
        events[15][0]["window"] = "other"
    elif defect == "date":
        events[15][0]["signal_at"] = "2001-01-01T00:00:00+00:00"
    elif defect == "factor":
        events[15][0]["factors"]["unknown"] = 1
    elif defect == "missing_factor":
        events[15][0]["factors"].pop(FACTOR_IDS[0])
    elif defect in ("contribution", "net_return"):
        events[15][0]["account_contribution" if defect == "contribution" else defect] = float("nan")
    else:
        calendars["2020"] = list(reversed(calendars["2020"]))
    with pytest.raises(ValueError):
        evaluate_market(market, events, calendars)


def test_finalization_rejects_missing_market_hypotheses_or_invalid_prior_probability():
    with pytest.raises(ValueError, match="eight-hypothesis"):
        finalize_pool(family_rows()[:-1], [])
    with pytest.raises(ValueError, match="finite"):
        finalize_pool(family_rows(), [float("inf")])
    with pytest.raises(ValueError, match="must lie"):
        finalize_pool(family_rows(), [-0.1])
    assert PROTOCOL["minimum_total_kept"] == 30
