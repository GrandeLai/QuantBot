from copy import deepcopy

import pytest

from quantbot.ai.evaluation import replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.factors.mechanism_probes import (
    DividendReplanProvider, KINDS, ProbeProvider, entry_targets, feature_panel, run_probe,
)
from quantbot.factors.signal_diagnostics import signal_funnel
from quantbot.factors.conditional_study import file_ref, save
from quantbot.factors.replan_study import run
from tests.test_mechanism_probes import card, fixture_snapshot


def dividend_fixture(indices=(181,)):
    _, _, base = fixture_snapshot("stocks")
    actions = [{"id": "div"+str(i), "symbol": "IEF", "type": "dividend",
                "date": base.times[i].date().isoformat(), "pay_date": base.times[i+3].date().isoformat(),
                "amount_per_share": ".1", "currency": "USD", "distribution": "ordinary_cash",
                "available_at": None} for i in indices]
    return fixture_snapshot("stocks", actions=actions)


def execute(tmp_path, config, spec, snap, *, retry=True, name="probe"):
    item = card(KINDS[2], config["market"]["symbols"])
    path = tmp_path/(name+".jsonl")
    result = run_probe(snap, item, feature_panel(snap), snap.times[180], config, spec,
                       path, candidate=True, replan=retry)
    accounts = [r["payload"] for r in inspect_replay(path)["records"] if r["kind"] == "account"]
    return result, accounts


def test_dividend_replan_uses_actual_cancel_and_next_open_once(tmp_path):
    config, spec, snap = dividend_fixture()
    baseline, _ = execute(tmp_path, config, spec, snap, retry=False, name="control")
    result, accounts = execute(tmp_path, config, spec, snap)
    assert baseline["buy_fills"] == 0 and result["buy_fills"] == 5
    assert len(result["replans"]) == 1 and result["replans"][0]["as_of"] == snap.times[181].isoformat()
    assert result["reconciliation"]["status"] == "reconciled"
    assert all(o["status"] == "canceled" for o in accounts[1]["orders"])
    buys = [(i, o) for i, a in enumerate(accounts) for o in a["orders"] if o["side"] == "buy" and o["status"] == "filled"]
    assert len(buys) == 5 and {i for i, _ in buys} == {2}
    assert all(o["created_at"] == snap.times[181].isoformat() for _, o in buys)
    assert all(o["stop_loss_pct"] == .03 and o["take_profit_pct"] == .06 for _, o in buys)
    assert all(a["available_at"] is None for a in snap.actions)


def test_second_dividend_does_not_retry_again(tmp_path):
    config, spec, snap = dividend_fixture((181, 182))
    result, accounts = execute(tmp_path, config, spec, snap)
    assert len(result["replans"]) == 1 and result["buy_fills"] == 0
    assert any(o.get("reason") == "corporate_action_replan" for o in accounts[2]["orders"])


@pytest.mark.parametrize("failure", ["entry_limit", "protective_exit", "split"])
def test_other_execution_events_never_trigger_reentry(tmp_path, failure):
    config, spec, snap = fixture_snapshot("stocks")
    day = snap.times[181].date().isoformat()
    for row in snap.rows:
        if row["date"] == day:
            if failure == "entry_limit":
                row.update(open=row["open"]*1.02, high=row["high"]*1.02)
            elif failure == "protective_exit":
                row["low"] *= .9
    if failure == "split":
        _, _, snap = fixture_snapshot("stocks", actions=[{"id": "split", "symbol": "IEF",
            "type": "split", "date": day, "ratio": "2", "available_at": None}])
    snap = snap.with_config(config)
    result, _ = execute(tmp_path, config, spec, snap)
    assert not result["replans"]
    assert result["buy_fills"] == (5 if failure == "protective_exit" else 0)
    if failure == "protective_exit":
        assert result["exit_reasons"] == ["protective_stop"]*5


def test_replan_recomputes_signal_at_cancel_close(tmp_path):
    config, spec, snap = dividend_fixture()
    item = card(KINDS[2], config["market"]["symbols"])
    panel = feature_panel(snap)
    for s in item["symbols"]:
        panel[(snap.times[181].isoformat(), s)]["tr63"] = -.1
    result = run_probe(snap, item, panel, snap.times[180], config, spec, tmp_path/'probe.jsonl',
                       candidate=True, replan=True)
    assert result["replans"][0]["targets"] == {} and result["buy_fills"] == 0


def test_observation_is_isolated_from_execution_ledger(tmp_path):
    config, spec, snap = dividend_fixture()
    item = card(KINDS[2], config["market"]["symbols"])
    panel = feature_panel(snap)
    targets, _ = entry_targets(item, {s: panel[(snap.times[180].isoformat(), s)] for s in item["symbols"]}, config, candidate=True)

    class MutatingObserver(ProbeProvider):
        def observe_execution(self, context, orders):
            for order in orders:
                order["status"] = "filled"

    result = replay(snap, spec, config, MutatingObserver(item, panel, snap.times[180].isoformat(), targets),
                    times=snap.times[180:201], journal_path=tmp_path/'mutating.jsonl')
    assert result["fills"] == 0 and result["account"]["positions"] == {}
    assert sum(o["status"] == "canceled" for o in result["orders"]) == 5
    assert not any(o["status"] == "filled" for o in result["orders"])


def test_no_cancellation_report_means_no_retry_even_on_ex_date():
    config, spec, snap = dividend_fixture()
    item = card(KINDS[2], config["market"]["symbols"])
    panel = feature_panel(snap)
    targets, _ = entry_targets(item, {s: panel[(snap.times[180].isoformat(), s)] for s in item["symbols"]}, config, candidate=True)
    provider = DividendReplanProvider(item, panel, snap.times[180].isoformat(), targets, snap.actions)
    for at in snap.times[180:182]:
        provider.observe_execution(snap.context(at), ())
    assert provider.retry is None


def test_signal_diagnostic_keeps_thresholds_and_does_not_evaluate_returns():
    config, _, snap = fixture_snapshot("crypto")
    times = snap.times[130:230]
    window = {"id": "w", "start": times[0].date().isoformat(), "end": times[-1].date().isoformat(),
              "first_close": times[0].isoformat(), "last_close": times[-1].isoformat(), "expected_bars": len(times)}
    before = deepcopy(snap.rows)
    report = signal_funnel(snap, card(KINDS[1], config["market"]["symbols"]), [window])
    assert not report["returns_computed"] and not report["threshold_search"]
    assert all(r["daily_sessions"] == 60 and r["grid_sessions"] == 6 for r in report["reports"])
    assert all(r["counts"]["daily"]["all_conditions"] == 0 for r in report["reports"])
    assert snap.rows == before


def test_changed_frozen_source_blocks_before_creating_output(tmp_path):
    protocol, frozen = tmp_path/'protocol.json', tmp_path/'freeze.json'
    save(protocol, {"id": "test"})
    save(frozen, {"protocol": file_ref(protocol), "source_hash": "changed"})
    with pytest.raises(ValueError, match="冻结代码"):
        run(protocol, frozen, tmp_path/'must_not_exist')
    assert not (tmp_path/'must_not_exist').exists()
