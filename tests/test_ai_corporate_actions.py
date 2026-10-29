from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.backtest import backtest
from quantbot.ai.evaluation import evaluate
from quantbot.ai.feeds import fetch_snapshot
from quantbot.ai.local_execution import account_view, advance, model_account, new_account, queue_plan
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.research import register_study, run_study
from quantbot.cli import main
from quantbot.web.app import create_app
from tests.test_ai_research import CARD, PROTOCOL
from tests.test_ai_vintages import versioned


def corporate_fixture(tmp_path, ratio="2"):
    store, job, path, payload = versioned(tmp_path, "stocks")
    payload["rows"] = payload["rows"][:100]
    rows = payload["rows"]
    payload["actions"] = [
        {"id": "split-1", "symbol": "SPY", "type": "split", "date": rows[92]["date"],
         "available_at": rows[91]["available_at"], "ratio": ratio},
        {"id": "dividend-1", "symbol": "SPY", "type": "dividend", "date": rows[95]["date"],
         "available_at": rows[94]["available_at"], "amount_per_share": "1", "currency": "USD",
         "distribution": "ordinary_cash", "pay_date": rows[98]["date"]},
    ]
    for index, row in enumerate(rows):
        if index >= 92:
            for field in ("open", "high", "low", "close"):
                row[field] /= float(ratio)
            row["volume"] *= float(ratio)
        if index >= 95:
            for field in ("open", "high", "low", "close"):
                row[field] -= 1
    path.write_text(json.dumps(payload))
    return store, job, path, payload


def hand_replay(store, job, *, end=99):
    config = deepcopy(job["config"])
    config["execution"]["cost_bps"] = 0
    config["execution"]["initial_cash"] = 10000
    snapshot = fetch_snapshot(job["spec"], config, datetime.now(UTC), store.root, fresh=False)
    state = new_account(10000)
    records, history = [], {}

    def record(kind, payload):
        records.append({"sequence": len(records), "recorded_at": datetime.now(UTC).isoformat(),
                        "kind": kind, "payload": payload})

    record("started", {"schema_version": 1, "config": config, "spec": job["spec"], "corporate_actions": snapshot.actions})
    for index in range(90, end + 1):
        context = snapshot.context(snapshot.times[index])
        count = len(state["corporate_action_events"])
        orders = advance(state, snapshot, context, config, job["spec"])
        view = account_view(state)
        record("account", {"as_of": context.as_of.isoformat(), "account": view, "orders": orders,
                           "valuation_prices": context.prices, "corporate_actions": state["corporate_action_events"][count:]})
        history[index] = deepcopy(state)
        if index == 90:
            queue_plan(state, {"orders": [{"client_order_id": "manual-entry", "symbol": "SPY", "side": "buy",
                                          "quantity": 4, "limit_price": 1000, "stop": 80}]})
        if index == 94:
            # 除息前的旧计划必须撤回；不能在除息价上继续执行旧目标。
            queue_plan(state, {"orders": [{"client_order_id": "stale-entry", "symbol": "SPY", "side": "buy",
                                          "quantity": 2, "limit_price": 1000, "stop": 1}]})
        if index == 95:
            queue_plan(state, {"orders": [{"client_order_id": "manual-exit", "symbol": "SPY", "side": "sell",
                                          "quantity": state["positions"]["SPY"]}]})
    record("completed", {"execution_completed": True, "account": account_view(state)})
    return snapshot, state, history, records


@pytest.mark.parametrize("ratio", ["2", "0.5"])
def test_hand_calculated_split_dividend_sale_and_payment_reconcile(tmp_path, ratio):
    store, job, _, payload = corporate_fixture(tmp_path, ratio)
    snap, state, history, records = hand_replay(store, job)
    quantity = Decimal(4) * Decimal(ratio)
    assert Decimal(history[92]["positions"]["SPY"]) == quantity
    assert Decimal(history[92]["average_cost"]["SPY"]) == Decimal(str(payload["rows"][91]["open"])) / Decimal(ratio)
    assert Decimal(history[92]["stops"]["SPY"]) == Decimal(80) / Decimal(ratio)
    assert history[95]["cash"] == history[94]["cash"]
    assert Decimal(history[95]["dividend_income"]) == quantity
    assert account_view(history[95])["dividend_receivable"] == float(quantity)
    assert any(o.get("reason") == "corporate_action_replan" for o in records[6]["payload"]["orders"])
    assert not history[96]["positions"] and history[96]["dividend_receivables"]
    assert not state["positions"] and not state["dividend_receivables"]
    expected_cash = Decimal(10000) - 4 * Decimal(str(payload["rows"][91]["open"]))
    expected_cash += quantity * Decimal(str(payload["rows"][96]["open"])) + quantity
    assert Decimal(state["cash"]) == expected_cash
    assert state["risk_latched"] is None
    ledger = reconcile_replay(records)
    assert ledger["status"] == "reconciled", ledger
    assert Decimal(ledger["closing_balances"]["dividend_income"]) == quantity
    account = model_account(history[95], snap.context(snap.times[95]), "stocks")
    assert account.broker.buying_power == float(history[95]["cash"])
    assert account.payload(snap.context(snap.times[95]))["dividend_receivable"] == float(quantity)


def test_unpaid_dividend_remains_a_receivable_and_tampering_is_detected(tmp_path):
    store, job, _, _ = corporate_fixture(tmp_path)
    _, state, _, records = hand_replay(store, job, end=97)
    assert not state["positions"] and account_view(state)["dividend_receivable"] == 8
    assert reconcile_replay(records)["status"] == "reconciled"
    damaged = deepcopy(records)
    damaged[6]["payload"]["corporate_actions"][0]["amount"] = "9"
    assert reconcile_replay(damaged)["status"] == "mismatch"
    damaged = deepcopy(records)
    damaged[6]["payload"]["corporate_actions"] = []
    with pytest.raises(ValueError, match="生效记录缺失"):
        reconcile_replay(damaged)


def test_splits_adjust_only_available_history_and_never_raw_fills(tmp_path):
    store, job, path, payload = corporate_fixture(tmp_path)
    snapshot = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    before = snapshot.context(snapshot.times[91])
    after = snapshot.context(snapshot.times[92])
    assert after.features["SPY"]["momentum"] > 0
    assert after.features["SPY"]["atr"] < 2
    assert after.features["SPY"]["history_price_basis"] == "split_adjusted_as_of"
    assert snapshot.bars(snapshot.times[91], snapshot.times[92])[0][1]["SPY"]["open"] == payload["rows"][92]["open"]
    payload["actions"][0]["ratio"] = "4"
    path.write_text(json.dumps(payload))
    changed = fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)
    assert changed.context(changed.times[91]) == before
    assert changed.context(changed.times[92]).snapshot_id != after.snapshot_id


def test_fractional_reverse_split_fails_without_inventing_cash(tmp_path):
    store, job, _, _ = corporate_fixture(tmp_path, "0.3")
    with pytest.raises(ValueError, match="碎股"):
        hand_replay(store, job)


@pytest.mark.parametrize("mutation, message", [
    (lambda p: p["actions"][0].update(ratio=0), "正有限"),
    (lambda p: p["actions"][0].update(available_at=p["rows"][92]["available_at"]), "开盘前"),
    (lambda p: p["actions"][1].update(pay_date=p["rows"][94]["date"]), "不能早于"),
    (lambda p: p["actions"][1].update(currency="EUR"), "USD"),
    (lambda p: p["actions"].append(deepcopy(p["actions"][0])), "重复"),
])
def test_ambiguous_or_unavailable_action_terms_are_rejected(tmp_path, mutation, message):
    store, job, path, payload = corporate_fixture(tmp_path)
    mutation(payload)
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=message):
        fetch_snapshot(job["spec"], job["config"], datetime.now(UTC), store.root, fresh=False)


def test_main_evaluation_and_frozen_research_support_actions(tmp_path):
    store, job, path, _ = corporate_fixture(tmp_path)
    report = evaluate(store, job["id"], last_bars=12)
    assert report["execution_completed"]
    assert report["candidate"]["account"]["dividend_income"] > 0
    for section in ("candidate", "baseline"):
        journal = inspect_replay(report[section]["journal_path"])
        assert reconcile_replay(journal["records"])["status"] == "reconciled"
    study = register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)
    path.write_text("original file no longer exists")
    result = run_study(store, study["id"])
    assert result["report"]["execution_completed"] and result["report"]["conclusion"] == "insufficient"
    assert len(result["input_snapshot"]["vintage_input"]["actions"]) == 2


def test_vintage_cli_web_and_direct_backtests_agree_and_keep_frozen_input(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("公司行动回测不能启动券商执行")

    monkeypatch.setattr("quantbot.ai.paper.run_paper", forbidden)
    monkeypatch.setattr("quantbot.web.app.run_spot", forbidden)
    _, job, path, payload = corporate_fixture(tmp_path)
    direct = backtest(job["config"], output=tmp_path / "direct", provider="demo", vintage_data=payload, last_bars=12)
    config = tmp_path / "config.toml"
    config.write_text("[strategy]\nmomentum_window = 20\n")
    output = io.StringIO()
    code = main(["backtest", "--config", str(config), "--provider", "demo", "--vintage-data", str(path),
                 "--last-bars", "12", "--output", str(tmp_path / "cli")], stdout=output, environ={})
    assert code == 0, output.getvalue()
    cli = json.loads(output.getvalue())
    client = TestClient(create_app(config=job["config"], runtime_dir=tmp_path / "web"))
    before = client.get("/api/quantbot/runtime").json()["jobs"]
    response = client.post("/api/quantbot/allocate", json={"provider": "demo", "mode": "simulate",
                           "vintage_data": payload, "last_bars": 12, "initial_cash": job["config"]["execution"]["initial_cash"]})
    assert response.status_code == 200, response.text
    web = response.json()
    assert client.get("/api/quantbot/runtime").json()["jobs"] == before
    for report in (cli, web):
        assert report["data_source"] == "vintage" and report["model_called"] is False
        assert report["candidate"]["equity_curve"] == direct["candidate"]["equity_curve"]
        assert report["candidate"]["corporate_action_events"] == direct["candidate"]["corporate_action_events"]
        assert report["input_snapshot"]["vintage_input"]["actions"] == payload["actions"]
        assert json.loads(Path(report["spec"]["data_path"]).read_text()) == payload
        assert json.loads(Path(report["report_path"]).read_text()) == report
    path.write_text("source removed")
    frozen = backtest(job["config"], output=tmp_path / "frozen", provider="demo",
                      vintage_data=web["input_snapshot"]["vintage_input"], last_bars=12)
    assert frozen["candidate"]["equity_curve"] == direct["candidate"]["equity_curve"]


def test_web_rejects_mixed_sources_and_keeps_unsupported_action_failure(tmp_path):
    _, job, _, payload = corporate_fixture(tmp_path)
    client = TestClient(create_app(config=job["config"], runtime_dir=tmp_path / "web"))
    request = {"provider": "demo", "mode": "simulate", "vintage_data": payload, "last_bars": 12}
    response = client.post("/api/quantbot/allocate", json={**request, "csv_data": "csv"})
    assert response.status_code == 400 and "只能选择一种" in response.json()["detail"]
    response = client.post("/api/quantbot/allocate", json={**request, "mode": "decide"})
    assert response.status_code == 400 and "仅支持历史回测" in response.json()["detail"]
    payload["actions"][0] = {key: payload["actions"][0][key] for key in ("symbol", "date", "type", "available_at")}
    response = client.post("/api/quantbot/allocate", json=request)
    assert response.status_code == 400
    report = client.get(response.json()["detail"]["download_url"]).json()
    assert report["execution_completed"] is False
    assert report["input_data"]["vintage_input"] == payload
    assert "缺少详情" in report["error"]


def test_reconciliation_rejects_malformed_action_and_out_of_window_payment(tmp_path):
    store, job, _, _ = corporate_fixture(tmp_path)
    _, _, _, records = hand_replay(store, job)
    malformed = deepcopy(records)
    malformed[6]["payload"]["corporate_actions"][0]["action"] = []
    with pytest.raises(ValueError, match="公司行动身份"):
        reconcile_replay(malformed)
    malformed = deepcopy(records)
    malformed[9]["payload"]["corporate_actions"][0]["at"] = "2027-01-01T21:00:00+00:00"
    with pytest.raises(ValueError, match="区间内"):
        reconcile_replay(malformed)


def test_unsupported_action_in_feature_history_is_rejected_before_decisions(tmp_path, monkeypatch):
    store, job, path, payload = corporate_fixture(tmp_path)
    payload["actions"].append({"symbol": "SPY", "date": payload["rows"][70]["date"], "type": "split",
                               "available_at": payload["rows"][69]["available_at"]})
    path.write_text(json.dumps(payload))

    def forbidden(*args, **kwargs):
        raise AssertionError("不能使用包含未知公司行动的预热特征")

    monkeypatch.setattr("quantbot.ai.models.DemoProvider.decide", forbidden)
    with pytest.raises(ValueError, match="缺少详情"):
        evaluate(store, job["id"], last_bars=12)
    with pytest.raises(ValueError, match="缺少详情"):
        register_study(store, job_ids=[job["id"]], card=CARD, protocol=PROTOCOL)


def rotation_opening(action_symbols, *, stop_touched=False):
    from types import SimpleNamespace
    from quantbot.ai.feeds import stock_calendar
    previous = datetime(2024, 6, 20, 20, tzinfo=UTC)
    at = datetime(2024, 6, 21, 20, tzinfo=UTC)
    state = new_account(1000)
    state.update(cash='700', positions={'SPY': '3'}, average_cost={'SPY': '100'},
                 stops={'SPY': '90'}, take_profits={'SPY': '110'}, as_of=previous.isoformat(),
                 prices={'SPY': '100', 'QQQ': '100', 'IWM': '100'})
    queue_plan(state, {'orders': [
        {'client_order_id': 'sell-spy', 'symbol': 'SPY', 'side': 'sell', 'quantity': 3},
        {'client_order_id': 'buy-qqq', 'symbol': 'QQQ', 'side': 'buy', 'quantity': 2,
         'limit_price': 101, 'stop': 90, 'stop_loss_pct': .03, 'take_profit_pct': .06},
    ]})
    actions = [{'id': f'{s}-dividend', 'symbol': s, 'type': 'dividend', 'date': '2024-06-21',
                'amount_per_share': '1', 'pay_date': '2024-06-28', 'distribution': 'ordinary_cash',
                'currency': 'USD'} for s in action_symbols]
    bars = {s: {'open': 99 if s in action_symbols else 100, 'close': 99 if s in action_symbols else 100,
                'high': 101, 'low': 80 if s == 'SPY' and stop_touched else 98, 'volume': 10000}
            for s in ('SPY', 'QQQ', 'IWM')}
    snapshot = SimpleNamespace(actions=actions, times=[previous, at],
        calendar=stock_calendar('2024-06-01', '2024-07-01'), bars=lambda start, end: [(at, bars)])
    context = SimpleNamespace(as_of=at, prices={s: b['close'] for s, b in bars.items()})
    orders = advance(state, snapshot, context, {'execution': {'cost_bps': 15}},
                     {'market': 'stocks', 'bar': '1d', 'max_daily_loss': .02, 'max_drawdown': .05})
    return state, orders


@pytest.mark.parametrize('symbols', [['SPY'], ['QQQ'], ['SPY', 'QQQ']])
def test_action_retracts_whole_rotation_plan_with_entitlements_preserved(symbols):
    state, orders = rotation_opening(symbols)
    assert len(orders) == 2 and all(o['status'] == 'canceled' for o in orders)
    assert {o['client_order_id'] for o in orders} == {'sell-spy', 'buy-qqq'}
    assert {o['reason'] for o in orders} == {'corporate_action_replan', 'corporate_action_plan_replan'}
    assert all(o['action_id'] == f'{symbols[0]}-dividend' for o in orders)
    assert state['positions'] == {'SPY': '3'} and state['cash'] == '700'
    assert not state['pending'] and state['costs'] == '0'
    assert state['stops']['SPY'] == ('89' if 'SPY' in symbols else '90')
    assert state['take_profits']['SPY'] == ('109' if 'SPY' in symbols else '110')
    assert account_view(state)['dividend_receivable'] == (3 if 'SPY' in symbols else 0)
    assert len(state['corporate_action_events']) == len(symbols)


@pytest.mark.parametrize('symbols', [[], ['IWM']])
def test_rotation_without_an_affected_pending_order_still_executes(symbols):
    state, orders = rotation_opening(symbols)
    assert [(o['side'], o['status']) for o in orders] == [('sell', 'filled'), ('buy', 'filled')]
    assert state['positions'] == {'QQQ': '2'}
    assert not state['pending']


def test_plan_retraction_does_not_disable_existing_protective_stop():
    state, orders = rotation_opening(['SPY'], stop_touched=True)
    assert sum(o['status'] == 'canceled' for o in orders) == 2
    fills = [o for o in orders if o['status'] == 'filled']
    assert len(fills) == 1 and fills[0]['symbol'] == 'SPY' and fills[0]['reason'] == 'protective_stop'
    assert fills[0]['reference_price'] == '89'
    assert not state['positions'] and account_view(state)['dividend_receivable'] == 3
