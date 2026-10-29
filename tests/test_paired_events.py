from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from quantbot.ai.config import load_config
from quantbot.ai.development_strategies import development_provider
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot
from quantbot.ai.local_execution import account_view
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.factors.paired_events import (
    audit_branch, opportunity, parent_checkpoints, retest_state, run_branch, switch_coverage,
)


def test_confirmation_is_later_causal_bounded_and_anchored():
    anchor = {"level": 100, "atr": 2}
    values = {"close": 100.5, "trend_confirmed_2": True, "efficiency_ratio_10": .3}
    bar = {"low": 99, "open": 100}
    assert retest_state(anchor, values, bar, 0) == "waiting"
    assert retest_state(anchor, values, bar, 1) == "confirmed"
    assert retest_state(anchor, values, bar, 5) == "confirmed"
    assert retest_state(anchor, values, bar, 6) == "expired"
    assert retest_state(anchor, {**values, "close": 98.9}, bar, 1) == "invalidated"
    assert retest_state(anchor, values, {**bar, "low": 101.01}, 5) == "expired"
    assert retest_state(anchor, values, {**bar, "open": 100.5}, 1) == "waiting"


def test_switch_projection_charges_both_incremental_legs_at_each_cost():
    target, held = {"momentum": .052}, {"momentum": .04}
    assert switch_coverage(target, held, 15) == 1
    assert switch_coverage(target, held, 30) == .5
    assert switch_coverage(held, target, 15) == -1
    with pytest.raises(ValueError):
        switch_coverage(target, held, 0)


def test_rotation_only_includes_valid_incumbent_full_switch_not_cash_or_risk_trim():
    valid = {"uptrend": True, "momentum": .05, "channel_exit": False, "previous_rotation_eligible": True}
    context = SimpleNamespace(features={"SPY": {**valid, "momentum": .03}, "QQQ": valid})
    state = {"positions": {"SPY": "4"}}
    plan = {"orders": [{"side": "sell", "symbol": "SPY", "quantity": 4},
                       {"side": "buy", "symbol": "QQQ", "quantity": 3}]}
    event, reason = opportunity("stocks", state, context, plan, 15)
    assert reason is None and event["incumbent"] == "SPY" and event["target"] == "QQQ"
    assert opportunity("stocks", {"positions": {}}, context, plan, 15)[1] == "cash_entry"
    # An ex-dividend cancellation can leave both original symbols until the next plan.
    assert opportunity("stocks", {"positions": {"SPY": "3", "QQQ": "2"}}, context,
                       {"orders": plan["orders"][:1]}, 15) == (None, "multiple_incumbents_outside_scope")
    context.features["SPY"]["channel_exit"] = True
    assert opportunity("stocks", state, context, plan, 15)[1] == "invalid_incumbent_exit"
    context.features["SPY"]["channel_exit"] = False
    assert opportunity("stocks", state, context, {"orders": plan["orders"][:1]}, 15)[1] == "hold_or_risk_reduction"
    plan["orders"][0]["quantity"] = 2
    with pytest.raises(ValueError, match="full incumbent"):
        opportunity("stocks", state, context, plan, 15)


def synthetic_crypto():
    config = load_config(environ={})
    config["market"].update(symbols=["BTC/USDT"], benchmark="BTC/USDT")
    config["execution"].update(initial_cash=1000, cost_bps=15, limit_buffer_bps=200)
    config["risk"].update(crypto_stop_loss_pct=.03, crypto_take_profit_pct=.06)
    config["strategy"]["crypto_cooldown_bars"] = 3
    rows = [{"timestamp": (datetime(2023, 1, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
             "symbol": "BTC/USDT", "bar": "1d", "open": 100+i,
             "high": 101+i, "low": 99.8+i, "close": 100.8+i, "volume": 10000} for i in range(100)]
    snapshot = Snapshot("crypto", rows, config, "synthetic_paired_only")
    spec = {"market": "crypto", "bar": "1d", "max_daily_loss": .02, "max_drawdown": .05}
    return config, spec, snapshot


def test_actual_engine_forks_keep_original_prefix_and_skip_failed_retest(tmp_path):
    config, spec, snapshot = synthetic_crypto()
    path = tmp_path / "parent.jsonl"
    replay(snapshot, spec, config, development_provider("crypto", "btc_breakout20_filtered"),
           times=snapshot.times[40:80], journal_path=path)
    records = inspect_replay(path)["records"]
    checkpoints = parent_checkpoints(snapshot, records, config, spec)
    state, context, plan, prefix = next(checkpoints)
    before = deepcopy(state)
    event, reason = opportunity("crypto", state, context, plan, 15)
    assert reason is None
    contexts = [snapshot.context(at) for at in snapshot.times[40:61]]
    control = run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=False)
    candidate = run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=True)
    assert control["target_buy_filled"] and not candidate["target_buy_filled"]
    assert not candidate["factor_kept"] and candidate["net_pnl"] == 0
    assert control["net_pnl"] > 0  # Skipping profitable breakouts must count as negative increment.
    assert candidate["account_contribution"]-control["account_contribution"] < 0
    for branch in (control, candidate):
        assert audit_branch(records, prefix, branch)["status"] == "reconciled"
    assert state == before
    list(checkpoints)  # All later original account/stop/dividend fields must still reconstruct.
    corrupted = deepcopy(control)
    corrupted["records"][-1]["payload"]["account"]["cash"] += 1
    with pytest.raises(ValueError, match="reconciliation"):
        audit_branch(records, prefix, corrupted)


def test_cancelled_original_entry_is_still_a_research_opportunity(tmp_path):
    config, spec, snapshot = synthetic_crypto()
    config["execution"]["limit_buffer_bps"] = 0
    snapshot.config = config
    path = tmp_path / "parent.jsonl"
    result = replay(snapshot, spec, config, development_provider("crypto", "btc_breakout20_filtered"),
                    times=snapshot.times[40:65], journal_path=path)
    assert any(o.get("reason") == "open_above_entry_limit" for o in result["orders"])
    records = inspect_replay(path)["records"]
    state, context, plan, _ = next(parent_checkpoints(snapshot, records, config, spec))
    assert opportunity("crypto", state, context, plan, 15)[0] is not None


def test_retest_confirmation_places_order_only_at_confirmation_then_next_open(tmp_path):
    config, spec, snapshot = synthetic_crypto()
    rows = deepcopy(snapshot.rows)
    rows[42].update(open=140.3, low=140.2, close=141.0, high=142.2)
    snapshot = Snapshot("crypto", rows, config, "synthetic_retest_confirmation")
    path = tmp_path / "parent.jsonl"
    replay(snapshot, spec, config, development_provider("crypto", "btc_breakout20_filtered"),
           times=snapshot.times[40:65], journal_path=path)
    records = inspect_replay(path)["records"]
    state, context, plan, prefix = next(parent_checkpoints(snapshot, records, config, spec))
    event, _ = opportunity("crypto", state, context, plan, 15)
    contexts = [snapshot.context(at) for at in snapshot.times[40:61]]
    branch = run_branch(snapshot, state, contexts, config, spec, plan, event, candidate=True)
    assert branch["factor_kept"] and branch["target_buy_filled"]
    assert branch["confirmed_at"] == contexts[2].as_of.isoformat()
    buys = [(r["payload"]["as_of"], o) for r in branch["records"] if r["kind"] == "account"
            for o in r["payload"]["orders"] if o["side"] == "buy" and o["status"] == "filled"]
    assert len(buys) == 1
    assert buys[0][0] == contexts[3].as_of.isoformat()
    assert buys[0][1]["at"] == contexts[2].as_of.isoformat()  # UTC next open equals prior close.
    assert audit_branch(records, prefix, branch)["status"] == "reconciled"


def test_etf_rejected_switch_carries_existing_brackets_without_repurchase(tmp_path):
    from quantbot.ai.feeds import stock_calendar
    config = load_config(environ={})
    config["market"].update(symbols=["SPY", "QQQ"], benchmark="SPY")
    config["execution"].update(initial_cash=10000, cost_bps=15)
    config["risk"].update(stock_stop_loss_pct=.03, stock_take_profit_pct=.06)
    calendar = stock_calendar("2023-01-01", "2023-10-01")
    dates = calendar.sessions_in_range("2023-01-03", "2023-09-29")
    rows = [{"date": str(day.date()), "symbol": symbol, "open": 100+i*.05,
             "high": 100.15+i*.05, "low": 99.9+i*.05, "close": 100.1+i*.05, "volume": 100000}
            for i, day in enumerate(dates) for symbol in ("SPY", "QQQ")]
    snapshot = Snapshot("stocks", rows, config, "synthetic_etf_pair", calendar)
    snapshot.actions = [{"id": "spy-div", "symbol": "SPY", "type": "dividend", "distribution": "ordinary_cash",
        "currency": "USD", "amount_per_share": "1", "date": str(dates[134].date()),
        "pay_date": str(dates[140].date())}]
    # Explicit synthetic eligibility isolates carried account/brackets from swing-pattern detection.
    for at in snapshot.times[130:145]:
        context = snapshot.context(at)
        snapshot._contexts[at] = replace(context, features={symbol: {**values, "uptrend": True}
            for symbol, values in context.features.items()})
    class BuyAndHold:
        def decide(self, context, account, config):
            current = account.payload(context)["current_weights"]
            return DecisionBatch(tuple(Allocation(s, current.get(s, 0) or (.1 if s == "SPY" else 0), 1., "test")
                                       for s in context.features), "synthetic", "synthetic")
    spec = {"market": "stocks", "bar": "1d", "max_daily_loss": .02, "max_drawdown": .05}
    path = tmp_path / "parent.jsonl"
    replay(snapshot, spec, config, BuyAndHold(), times=snapshot.times[130:145], journal_path=path)
    records = inspect_replay(path)["records"]
    checkpoints = parent_checkpoints(snapshot, records, config, spec)
    next(checkpoints)
    state, context, plan, prefix = next(checkpoints)
    assert state["positions"]["SPY"]
    before = deepcopy(state)
    branch = run_branch(snapshot, state, [snapshot.context(at) for at in snapshot.times[131:137]],
        config, spec, plan, {"incumbent": "SPY", "target": "QQQ", "coverage_score": .5}, candidate=True)
    assert not branch["target_buy_filled"]
    assert not any(o["side"] == "buy" for r in branch["records"] if r["kind"] == "account" for o in r["payload"]["orders"])
    first_account = next(r["payload"]["account"] for r in branch["records"] if r["kind"] == "account")
    assert first_account["stops"] == account_view(before)["stops"]
    assert first_account["take_profits"] == account_view(before)["take_profits"]
    assert branch["final_account"]["dividend_receivable"] == float(before["positions"]["SPY"])
    assert float(branch["final_account"]["stops"]["SPY"]) == pytest.approx(float(before["stops"]["SPY"])-1)
    assert state == before and audit_branch(records, prefix, branch)["status"] == "reconciled"
    explicit = run_branch(snapshot, state, [snapshot.context(at) for at in snapshot.times[131:137]],
        config, spec, plan, {"incumbent": "SPY", "target": "QQQ", "coverage_score": 2},
        candidate=True, retain_original=False)
    assert explicit["final_account"] == branch["final_account"] and not explicit["factor_kept"]
    assert audit_branch(records, prefix, explicit)["status"] == "reconciled"
