from copy import deepcopy
from decimal import Decimal as D
import gzip
import hashlib
import json

import pytest

from quantbot.ai.entry_attribution import reconstruct_entries
from quantbot.ai.economic_attribution import analyze_run, episode_paths, _post_exit
from quantbot.ai.reconciliation import reconcile_replay


def _economic_records(records):
    records = deepcopy(records)
    account_by_time = {r["payload"]["as_of"]: r["payload"] for r in records if r["kind"] == "account"}
    for record in records:
        data = record["payload"]
        if record["kind"] == "decision_started":
            for symbol in data["market"]["features"]:
                data["market"]["features"][symbol]["close"] = account_by_time[data["as_of"]]["valuation_prices"][symbol]
        if record["kind"] == "decision":
            symbols = account_by_time[data["as_of"]]["valuation_prices"]
            data["allocations"] = [{"symbol": s, "target_weight": .2} for s in symbols]
            for order in data["orders"]:
                order.update(limit_price=200, stop=97)
    return records


def test_economic_waterfall_excursions_and_exposure(tmp_path):
    records = _economic_records(crypto_records())
    run, meta, _ = persist(tmp_path, records)
    result = analyze_run(run, meta, "crypto")
    event, = result["episodes"]
    assert event["observed_mfe_net_fraction"] == pytest.approx(19.58 / 200)
    assert event["observed_mae_net_fraction"] == pytest.approx(-.4 / 200)
    assert event["first_positive_observation"]["phase"] == "held_close"
    assert event["first_positive_observation"]["sessions_after_entry"] == 0
    assert event["intraday_extrema_verified"] is False
    assert event["post_exit"]["horizons"]["5"]["status"] == "right_censored"
    assert result["capital"]["mean_close_exposure"] == pytest.approx(204 / 1003.8 / 3)
    assert result["economics"]["gross_same_fills_pnl_including_dividends"] == pytest.approx(20)
    assert result["economics"]["net_liquidation_pnl_estimate"] == pytest.approx(19.58)
    assert result["funnel"]["filled_buy_plans"] == 1
    assert result["funnel"]["median_planned_fraction_of_requested"] == 1


def test_economic_partial_sells_dividends_and_open_episode(tmp_path):
    records = stock_records()
    run, meta, _ = persist(tmp_path, records)
    result = reconstruct_entries(run, meta, "stocks")
    paths = episode_paths(records, result["events"] + result["open_episodes"], 10, "stocks")
    assert paths[0]["observations"][-1]["net_liquidation_pnl"] == 47
    assert paths[1]["observations"][-1]["net_liquidation_pnl"] == pytest.approx(.898)
    assert len(paths) == 2
    assert paths[1]["post_exit"] is None


def test_same_bar_protective_exit_does_not_invent_held_closes(tmp_path):
    records = crypto_records()
    rows = [r["payload"] for r in records if r["kind"] == "account"]
    buy, sell = rows[1]["orders"][0], rows[2]["orders"][0]
    sell = {**sell, "reason": "protective_stop", "execution_time_basis": "intrabar_time_unknown"}
    rows[1]["orders"] = [buy, sell]
    rows[1]["account"] = deepcopy(rows[2]["account"])
    rows[2]["orders"] = []
    for record in records:
        if record["kind"] == "decision" and record["payload"]["as_of"] == rows[1]["as_of"]:
            record["payload"]["orders"] = []
    run, meta, _ = persist(tmp_path, records)
    rebuilt = reconstruct_entries(run, meta, "crypto")
    event, = episode_paths(records, rebuilt["events"], 10, "crypto")
    assert event["held_close_count"] == 0
    assert event["intrabar_exit_time_unknown"] is True
    assert event["intraday_extrema_verified"] is False


def test_post_exit_uses_issuer_dividends_after_sale_and_censors_boundary():
    rows = [{"as_of": f"2025-01-{i+1:02}T21:00:00+00:00", "valuation_prices": {"QQQ": 100 if i < 2 else 99},
             "diagnostic_dividends": {"QQQ": D(1)} if i == 2 else {}} for i in range(7)]
    result = _post_exit(rows, 0, "QQQ", "stocks")
    assert result["horizons"]["5"]["total_return"] == 0
    assert result["horizons"]["10"]["total_return"] is None
    assert result["hypothetical_price_diagnostic_not_trade_pnl"] is True


def account(cash, equity, positions, *, realized=0, unrealized=0, costs=0, dividends=0, receivable=0, peak=1000):
    return {"cash": float(cash), "equity": float(equity), "positions": positions,
            "realized_pnl": float(realized), "unrealized_pnl": float(unrealized),
            "estimated_execution_cost": costs, "dividend_income": dividends, "dividend_receivable": receivable,
            "drawdown_fraction": float(D(str(equity)) / D(str(peak)) - 1)}


def fill(identity, symbol, side, quantity, reference, price, cost, at, **extra):
    return {"client_order_id": identity, "symbol": symbol, "side": side, "quantity": str(quantity),
            "reference_price": str(reference), "price": str(price), "estimated_cost": str(cost),
            "at": at, "status": "filled", **extra}


def make_records(market, rows, orders, actions=()):
    records = [{"kind": "started", "payload": {"schema_version": 1, "code_hash": "synthetic-source",
        "spec": {"market": market}, "config": {"execution": {"initial_cash": 1000, "cost_bps": 10}},
        "corporate_actions": list(actions)}}]
    for index, row in enumerate(rows):
        records.append({"kind": "account", "payload": row})
        if index == len(rows) - 1:
            continue
        at, planned = row["as_of"], orders.get(index, [])
        snapshot_id = "snapshot-" + at
        symbols = {order["symbol"] for values in orders.values() for order in values}
        records.append({"kind": "decision_started", "payload": {"as_of": at, "market": {
            "as_of": at, "snapshot_id": snapshot_id, "features": {symbol: {} for symbol in symbols}}}})
        records.append({"kind": "decision", "payload": {"as_of": at, "snapshot_id": snapshot_id,
            "orders": planned, "adjustments": ["QQQ:max_weight_clamped"] if index == 2 else []}})
    records.append({"kind": "completed", "payload": {"account": rows[-1]["account"],
        "execution_completed": True, "checks": {"complete_window": True, "no_risk_hold": True}}})
    for index, record in enumerate(records):
        record.update(sequence=index, recorded_at="2026-01-01T00:00:00+00:00")
    return records


def persist(tmp_path, records, *, document=None):
    raw = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    compressed = gzip.compress(raw)
    path = tmp_path / "run.jsonl.gz"
    path.write_bytes(compressed)
    meta = {"path": str(path), "sha256": hashlib.sha256(compressed).hexdigest(),
            "uncompressed_sha256": hashlib.sha256(raw).hexdigest()}
    if document is None:
        rows = [r["payload"] for r in records if r["kind"] == "account"]
        final = rows[-1]["account"]
        fills = [o for row in rows for o in row["orders"]]
        recon = reconcile_replay(records)
        assert recon["status"] == "reconciled", recon
        liquidation = sum(float(q) * float(rows[-1]["valuation_prices"][s])
                          for s, q in final["positions"].items()) * .001
        document = {"summary": {"reconciliation": recon, "cost_bps": 10,
            "final_equity": final["equity"], "costs": final["estimated_execution_cost"],
            "dividend_income": final["dividend_income"], "unpaid_dividend": final["dividend_receivable"],
            "return_fraction": final["equity"] / 1000 - 1, "estimated_final_sell_cost": liquidation,
            "net_liquidation_return_estimate": (final["equity"] - liquidation) / 1000 - 1,
            "fills": len(fills), "sells": sum(o["side"] == "sell" for o in fills), "bars": len(rows),
            "decision_calls": len(rows) - 1, "remaining_positions": final["positions"], "risk_hold": None,
            "execution_checks": records[-1]["payload"]["checks"]},
            "inspection": {"status": "completed", "execution_completed": True}, "orders": fills,
            "equity_curve": [{"as_of": row["as_of"], **row["account"]} for row in rows]}
    run = tmp_path / "run.json"
    run.write_text(json.dumps(document))
    return run, meta, document


def crypto_records():
    times = [f"2025-01-0{day}T00:00:00+00:00" for day in (1, 2, 3)]
    buy = fill("entry", "BTC/USDT", "buy", 2, 100, 100.1, .2, times[0])
    sell = fill("exit", "BTC/USDT", "sell", 2, 110, 109.89, .22, times[1])
    rows = [{"as_of": times[0], "account": account(1000, 1000, {}), "orders": [],
             "valuation_prices": {"BTC/USDT": 100}},
            {"as_of": times[1], "account": account(799.8, 1003.8, {"BTC/USDT": "2"},
             unrealized=3.8, costs=.2, peak=1003.8), "orders": [buy], "valuation_prices": {"BTC/USDT": 102}},
            {"as_of": times[2], "account": account(1019.58, 1019.58, {}, realized=19.58, costs=.42,
             peak=1019.58), "orders": [sell], "valuation_prices": {"BTC/USDT": 110}}]
    return make_records("crypto", rows, {0: [buy], 1: [sell]})


def stock_records():
    dates = ["2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07", "2025-01-08", "2025-01-09", "2025-01-10"]
    closes = [day + "T21:00:00+00:00" for day in dates]
    buys_sells = [fill(identity, "QQQ", side, q, ref, price, fee, dates[i] + "T14:30:00+00:00",
                      created_at=closes[i - 1])
                  for i, identity, side, q, ref, price, fee in (
                      (1, "b1", "buy", 2, 100, 101, 2), (2, "b2", "buy", 1, 110, 111, 1),
                      (3, "partial", "sell", 1, 120, 119, 1), (4, "exit", "sell", 2, 120, 119, 2),
                      (5, "new", "buy", 1, 100, 101, 1))]
    action = {"id": "d1", "symbol": "QQQ", "type": "dividend", "date": dates[3], "pay_date": dates[6],
              "distribution": "ordinary_cash", "currency": "USD", "amount_per_share": "1"}
    entitlement = {"action": action, "kind": "dividend_entitlement", "amount": "3", "quantity": "3",
                   "at": dates[3] + "T14:30:00+00:00"}
    payment = {"action": action, "kind": "dividend_payment", "amount": "3", "at": closes[6]}
    views = [account(1000, 1000, {}), account(798, 998, {"QQQ": "2"}, unrealized=-2, costs=2),
        account(687, 1017, {"QQQ": "3"}, unrealized=17, costs=3, peak=1017),
        account(806, 1049, {"QQQ": "2"}, realized=D(44)/3, unrealized=D(94)/3, costs=4,
                dividends=3, receivable=3, peak=1049),
        account(1044, 1047, {}, realized=44, costs=6, dividends=3, receivable=3, peak=1049),
        account(943, 1046, {"QQQ": "1"}, realized=44, unrealized=-1, costs=7, dividends=3, receivable=3, peak=1049),
        account(946, 1048, {"QQQ": "1"}, realized=44, unrealized=1, costs=7, dividends=3, peak=1049)]
    prices = [100, 100, 110, 120, 120, 100, 102]
    rows = [{"as_of": at, "account": views[i], "valuation_prices": {"QQQ": prices[i]},
             "orders": [buys_sells[i - 1]] if 1 <= i <= 5 else [],
             "corporate_actions": [entitlement] if i == 3 else [payment] if i == 6 else []}
            for i, at in enumerate(closes)]
    return make_records("stocks", rows, {i: [order] for i, order in enumerate(buys_sells)}, [action])


def test_crypto_midnight_uses_exact_signal_and_keeps_files_read_only(tmp_path):
    run, meta, _ = persist(tmp_path, crypto_records())
    before = {path: path.read_bytes() for path in tmp_path.iterdir()}
    result = reconstruct_entries(run, meta, "crypto")
    event, = result["events"]
    assert event["signal_at"] == event["entry_at"] == "2025-01-01T00:00:00+00:00"
    assert event["net_pnl"] == pytest.approx(19.58)
    assert event["gross_pnl"] == 20
    assert event["costs"] == pytest.approx(.42)
    assert event["net_return"] == pytest.approx(19.58 / 200)
    assert event["account_contribution"] == pytest.approx(.01958)
    assert event["holding_bars"] == 1 and event["buy_fills"] == event["sell_fills"] == 1
    assert result["open_episodes"] == []
    assert sum(day["daily_pnl"] for day in result["daily"]) == pytest.approx(19.58)
    assert before == {path: path.read_bytes() for path in tmp_path.iterdir()}


def test_partial_average_cost_dividend_after_exit_and_open_episode(tmp_path):
    run, meta, _ = persist(tmp_path, stock_records())
    result = reconstruct_entries(run, meta, "stocks")
    closed, = result["events"]
    opened, = result["open_episodes"]
    assert closed["buy_fills"] == 2 and closed["sell_fills"] == 2
    assert closed["exit_reasons"] == ["risk_reduction", "signal_exit"]
    assert closed["holding_bars"] == 3 and closed["mixed_entry_signals"]
    assert closed["net_pnl"] == 47 and closed["gross_pnl"] == 53 and closed["costs"] == 6
    assert closed["gross_dividends"] == 3 and closed["entry_reference_notional"] == 310
    assert closed["signal_at"] == "2025-01-02T21:00:00+00:00"
    assert opened["net_pnl"] == 1 and opened["gross_dividends"] == 0
    assert opened["exit_at"] is None and opened["quantity"] == "1"
    assert opened["estimated_final_sell_cost"] == pytest.approx(.102)
    assert result["accounting"]["closed_net_pnl"] + result["accounting"]["open_mark_to_market_net_pnl"] == 48
    assert abs(result["accounting"]["difference"]) < 1e-20


def test_unknown_entry_identity_fails_instead_of_backdating(tmp_path):
    records = crypto_records()
    decision = next(r["payload"] for r in records if r["kind"] == "decision")
    decision["orders"] = [{**decision["orders"][0], "client_order_id": "unrelated"}]
    run, meta, _ = persist(tmp_path, records)
    with pytest.raises(ValueError, match="缺少确切入场信号"):
        reconstruct_entries(run, meta, "crypto")


@pytest.mark.parametrize("field", ["sha256", "uncompressed_sha256"])
def test_both_journal_hashes_are_required(tmp_path, field):
    run, meta, _ = persist(tmp_path, crypto_records())
    meta[field] = "0" * 64
    with pytest.raises(ValueError, match="哈希不一致"):
        reconstruct_entries(run, meta, "crypto")


def test_summary_tampering_fails(tmp_path):
    records = crypto_records()
    _, _, document = persist(tmp_path, records)
    document["summary"]["final_equity"] += 1
    run, meta, _ = persist(tmp_path, records, document=document)
    with pytest.raises(ValueError, match="final_equity"):
        reconstruct_entries(run, meta, "crypto")


def test_completed_risk_hold_remains_analyzable_and_final_bar_is_explicit(tmp_path):
    records = crypto_records()
    _, _, document = persist(tmp_path, records)
    records[-1]["payload"]["execution_completed"] = False
    records[-1]["payload"]["checks"]["no_risk_hold"] = False
    document = deepcopy(document)
    document["summary"]["risk_hold"] = "drawdown_limit"
    document["summary"]["execution_checks"] = records[-1]["payload"]["checks"]
    document["inspection"]["execution_completed"] = False
    run, meta, _ = persist(tmp_path, records, document=document)
    result = reconstruct_entries(run, meta, "crypto")
    assert result["accounting"]["risk_hold"] == "drawdown_limit"
    assert result["daily"][-1]["is_final_bar"] and not result["daily"][-1]["has_decision"]
