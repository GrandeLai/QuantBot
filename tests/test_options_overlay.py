from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
import io
import json

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.config import load_config
from quantbot.ai.feeds import stock_calendar
from quantbot.cli import main
from quantbot.stocks.options.overlay import OverlayAccount, OverlayPolicy, execute_plan, replay_overlay
from quantbot.stocks.options.overlay_data import OverlayData
from quantbot.stocks.options.overlay_report import options_backtest
from quantbot.web.app import create_app


PORTFOLIO = {"cash": 1000, "positions": {"AAPL": 100}}
POLICY = {"min_dte": 1, "target_dte": 5, "max_dte": 30, "roll_dte": 0,
          "call_moneyness": 1.1, "put_moneyness": 0.9, "max_premium_fraction": 0.25}


def history(prices=None, *, expiry_index=4, second_expiry=False, call_strike=110, time_value=2):
    prices = prices or [100] * 10
    calendar = stock_calendar("2025-01-02", "2025-02-28")
    days = [x.date() for x in calendar.sessions_in_range("2025-01-02", "2025-02-28")[:len(prices)]]
    data = {"schema_version": 1, "source": "synthetic-test", "data_kind": "synthetic",
            "captured_at": "2026-01-01T00:00:00+00:00", "price_basis": "raw",
            "bars": [], "contracts": [], "quotes": [], "dividends": [], "actions": []}
    for day, spot in zip(days, prices, strict=True):
        data["bars"].append({"date": str(day), "symbol": "AAPL", "open": spot, "high": spot + 1,
                             "low": spot - 1, "close": spot, "volume": 1000000,
                             "available_at": calendar.session_close(str(day)).isoformat()})
    for index in [expiry_index, *([8] if second_expiry else [])]:
        for kind, strike in (("call", call_strike), ("put", 90)):
            cid = f"AAPL-{kind}-{index}"
            data["contracts"].append({"id": cid, "symbol": "AAPL", "kind": kind, "strike": strike,
                "expiry": str(days[index]), "last_trade_date": str(days[index]), "multiplier": 100,
                "deliverable_shares": 100, "exercise_style": "american", "settlement": "physical", "currency": "USD",
                "available_at": "2024-12-01T00:00:00+00:00"})
            for i, (day, spot) in enumerate(zip(days, prices, strict=True)):
                if i > index:
                    continue
                intrinsic = max(0, spot - strike if kind == "call" else strike - spot)
                for phase in ("open", "close"):
                    at = calendar.session_open(str(day)) if phase == "open" else calendar.session_close(str(day))
                    mid = intrinsic + (time_value if i < index or phase == "open" else 0)
                    data["quotes"].append({"contract_id": cid, "date": str(day), "phase": phase,
                        "bid": max(0, mid - 0.1), "ask": mid + 0.1, "bid_size": 100, "ask_size": 100,
                        "open_interest": 100, "iv": 0.25, "quote_at": at.isoformat(), "available_at": at.isoformat()})
    return data


def replay(payload, strategy, *, portfolio=None, policy=None, scenario="expiration_only"):
    return replay_overlay(OverlayData(payload), portfolio or PORTFOLIO, strategy, OverlayPolicy(**{**POLICY, **(policy or {})}), scenario)


@pytest.mark.parametrize("strategy,prices,expected", [
    ("covered_call", [100, 105, 110, 115, 120, 120, 120, 120, 120, 120], 12189.35),
    ("protective_put", [100, 95, 90, 85, 80, 80, 80, 80, 80, 80], 9789.35),
    ("collar", [100] * 10, 10978.7),
])
def test_exact_cashflows_expiry_and_pnl_reconcile(strategy, prices, expected):
    result = replay(history(prices), strategy)
    assert result["execution_completed"] and result["final_equity"] == pytest.approx(expected)
    assert result["research_conclusion"] == "not_assessed"
    for row in result["equity_curve"]:
        assert row["equity"] - result["initial_equity"] == pytest.approx(sum(row[k] for k in ("stock_pnl", "option_pnl", "dividend_income", "fees")))
        assert row["buying_power"] >= 0
        greeks = row["greeks"]
        assert greeks["complete"]
    trades = [e for e in result["events"] if e["type"] == "option_trade"]
    assert all(e["at"] > e["decision_at"] for e in trades)
    assert result["ending_positions"]["options"] == {}
    if strategy == "covered_call":
        assert result["ending_positions"]["stocks"]["AAPL"] == 0
        assert result["equity_curve"][1]["cash"] == pytest.approx(1189.35)
        assert result["equity_curve"][1]["buying_power"] == 1000  # 正现金 T+1 才能再花。
        assert result["equity_curve"][2]["buying_power"] == pytest.approx(1189.35)


def test_dividend_receivable_is_earned_on_ex_day_and_paid_later():
    payload = history([100, 100, 98, 98, 98, 98, 98, 98, 98, 98])
    payload["dividends"] = [{"id": "D1", "symbol": "AAPL", "ex_date": payload["bars"][2]["date"],
                             "pay_date": payload["bars"][4]["date"], "amount": 2, "available_at": "2025-01-01T00:00:00+00:00"}]
    result = replay(payload, "hold")
    assert result["execution_completed"] and result["final_equity"] == 11000
    assert result["equity_curve"][2]["dividend_receivable"] == 200
    assert result["equity_curve"][2]["cash"] == 1000
    assert result["equity_curve"][4]["dividend_receivable"] == 0
    assert result["equity_curve"][4]["cash"] == 1200


def test_early_assignment_is_separate_scenario_and_removes_dividend_entitlement():
    payload = history([100, 107, 109, 107, 103, 100, 100, 100, 100, 100], expiry_index=8, call_strike=105, time_value=1)
    payload["dividends"] = [{"id": "D", "symbol": "AAPL", "ex_date": payload["bars"][3]["date"],
                             "pay_date": payload["bars"][5]["date"], "amount": 2, "available_at": "2025-01-01T00:00:00+00:00"}]
    ordinary = replay(payload, "covered_call")
    stressed = replay(payload, "covered_call", scenario="ex_dividend_stress")
    assert ordinary["execution_completed"] and stressed["execution_completed"]
    assert ordinary["assignments"] == 0 and stressed["assignments"] == 1
    assert ordinary["equity_curve"][-1]["dividend_income"] == 200
    assert stressed["equity_curve"][-1]["dividend_income"] == 0
    assignment = next(e for e in stressed["events"] if e["type"] == "early_assignment_scenario")
    assert assignment["settlement_date"] == payload["dividends"][0]["ex_date"]


def test_roll_closes_old_leg_before_expiry_with_net_cash_and_fees():
    result = replay(history(second_expiry=True), "covered_call", policy={"roll_dte": 1, "min_dte": 2})
    assert result["execution_completed"]
    trades = [e for e in result["events"] if e["type"] == "option_trade"]
    assert [e["action"] for e in trades] == ["sell_to_open", "buy_to_close", "sell_to_open"]
    assert trades[1]["at"] == trades[2]["at"] and trades[1]["reason"] == "roll"
    assert result["fees"] == pytest.approx(1.95)


def test_future_quote_revisions_do_not_change_decisions_or_previous_curve():
    original = history()
    changed = deepcopy(original)
    future = deepcopy(next(q for q in changed["quotes"] if q["phase"] == "close"))
    future.update(bid=50, ask=51, available_at="2025-01-10T22:00:00+00:00")
    changed["quotes"].append(future)
    a, b = replay(original, "covered_call"), replay(changed, "covered_call")
    assert a["decisions"] == b["decisions"] and a["equity_curve"] == b["equity_curve"]


def test_missing_held_quote_fails_instead_of_inventing_theoretical_price(tmp_path):
    payload = history()
    missing_day = payload["bars"][2]["date"]
    payload["quotes"] = [q for q in payload["quotes"] if not (q["phase"] == "close" and q["date"] == missing_day)]
    result = options_backtest(payload=payload, portfolio=PORTFOLIO, policy=POLICY, output=tmp_path / "report")
    assert not result["execution_completed"] and result["results"][0]["execution_completed"]
    call = result["results"][1]
    assert call["final_equity"] is None and "理论价格" in call["errors"][0]["error"]
    assert json.loads((tmp_path / "report" / "report.json").read_text())["results"][1]["events"]


@pytest.mark.parametrize("change,reason", [
    ("late", "missing_or_wide_quote"), ("size", "insufficient_displayed_size"),
    ("spread", "missing_or_wide_quote"), ("cash", "settled_cash_limit"),
])
def test_atomic_collar_rejects_whole_package_when_either_leg_is_unfillable(change, reason):
    payload = history()
    day = payload["bars"][1]["date"]
    for q in payload["quotes"]:
        if q["phase"] == "open" and q["date"] == day and "put" in q["contract_id"]:
            if change == "late":
                q["available_at"] = (datetime.fromisoformat(q["available_at"]) + timedelta(hours=1)).isoformat()
            elif change == "size":
                q["ask_size"] = 0
            elif change == "spread":
                q["ask"] = 50
    result = replay(payload, "collar", portfolio={**PORTFOLIO, "cash": 0} if change == "cash" else None)
    rejection = next(e for e in result["events"] if e["type"] == "plan_rejected")
    assert rejection["reason"] == reason
    assert not [e for e in result["events"] if e["type"] == "option_trade" and e["at"][:10] == day]
    assert result["equity_curve"][1]["option_positions"] == {}


def test_contract_multiplier_and_independent_coverage_limit():
    payload = history()
    result = replay(payload, "covered_call", portfolio={"cash": 1000, "positions": {"AAPL": 250}})
    trades = [e for e in result["events"] if e["type"] == "option_trade"]
    assert trades[0]["quantity"] == 2 and trades[0]["multiplier"] == 100
    data, policy = OverlayData(payload), OverlayPolicy(**POLICY)
    state, events = OverlayAccount(data, PORTFOLIO), []
    planned = {"symbol": "AAPL", "from": {}, "target": {"AAPL-call-4": -2}, "reason": "test", "decision_at": data.at(data.days[0], "close").isoformat()}
    assert not execute_plan(data, state, planned, data.days[1], "open", policy, events, D(1000))
    assert events[0]["reason"] == "uncovered_or_overhedged_position" and state.options == {}


def test_missing_iv_is_unknown_greeks_while_cashflow_remains_valid():
    payload = history()
    for q in payload["quotes"]:
        q["iv"] = None
    result = replay(payload, "covered_call")
    assert result["execution_completed"]
    assert result["equity_curve"][1]["greeks"]["complete"] is False
    assert result["equity_curve"][1]["greeks"]["dollar_delta"] is None


@pytest.mark.parametrize("mutation,match", [
    (lambda p: p.update(price_basis="adjusted"), "raw"),
    (lambda p: p["contracts"][0].update(deliverable_shares=150), "调整后合约"),
    (lambda p: p["quotes"].append(deepcopy(p["quotes"][0])), "重复"),
    (lambda p: p["quotes"][0].update(bid=5, ask=1), "bid"),
    (lambda p: p["quotes"][0].update(available_at="2025-01-01T00:00:00+00:00"), "时间"),
    (lambda p: p["bars"].pop(3), "完整交易日"),
    (lambda p: p["bars"][0].update(available_at="2025-01-02T21:00:00"), "时区"),
    (lambda p: p["actions"].append({"type": "split"}), "专用会计"),
])
def test_invalid_or_unsupported_historical_input_is_rejected(mutation, match):
    payload = history()
    mutation(payload)
    with pytest.raises(ValueError, match=match):
        OverlayData(payload)


def test_policy_rejects_unknown_inconsistent_and_nonfinite_values():
    for change in ({"min_dte": True}, {"max_dte": 3}, {"risk_free_rate": float("nan")}, {"max_spread_fraction": "0.2"}):
        with pytest.raises(ValueError):
            replace(OverlayPolicy(), **change)


def test_cli_and_web_export_same_complete_portfolio_evidence(tmp_path):
    data_file, portfolio_file, policy_file = [tmp_path / name for name in ("data.json", "portfolio.json", "policy.json")]
    for path, payload in ((data_file, history()), (portfolio_file, PORTFOLIO), (policy_file, POLICY)):
        path.write_text(json.dumps(payload))
    output = io.StringIO()
    assert main(["options-backtest", "--data", str(data_file), "--portfolio", str(portfolio_file),
                 "--policy", str(policy_file), "--output", str(tmp_path / "cli")], stdout=output, environ={}) == 0
    result = json.loads((tmp_path / "cli" / "report.json").read_text())
    assert result["execution_completed"] and len(result["results"]) == 6
    assert result["research_conclusion"] == "insufficient" and result["model_calls"] == result["broker_calls"] == 0
    client = TestClient(create_app(config=load_config(environ={}), runtime_dir=tmp_path / "web"))
    body = {"data": history(), "portfolio": PORTFOLIO, "policy": POLICY}
    assert client.post("/api/quantbot/options/backtest", json={**body, "broker": "real"}).status_code == 422
    response = client.post("/api/quantbot/options/backtest", json=body)
    assert response.status_code == 200 and "input_data" not in response.json()
    exported = client.get(response.json()["download_url"])
    assert exported.status_code == 200
    assert exported.json()["input_data"] == body["data"]
    assert exported.json()["results"] == result["results"]
    assert client.get("/api/quantbot/options/reports/invalid").status_code == 404
    sample = client.get("/api/quantbot/options/example")
    assert sample.status_code == 200 and sample.json()["data"]["data_kind"] == "synthetic"
    assert len(sample.json()["data"]["bars"]) == 45
    policy_file.write_text('{"unknown": 1}')
    assert main(["options-backtest", "--data", str(data_file), "--portfolio", str(portfolio_file),
                 "--policy", str(policy_file), "--output", str(tmp_path / "invalid")], stdout=io.StringIO(), environ={}) == 2
    assert not (tmp_path / "invalid").exists()
    policy_file.write_text(json.dumps(POLICY))
    invalid = history()
    invalid["bars"][0]["date"] = None
    data_file.write_text(json.dumps(invalid))
    assert main(["options-backtest", "--data", str(data_file), "--portfolio", str(portfolio_file),
                 "--policy", str(policy_file), "--output", str(tmp_path / "invalid-data")], stdout=io.StringIO(), environ={}) == 2
    assert not (tmp_path / "invalid-data").exists()


def test_premium_budget_is_shared_across_symbols_in_same_session():
    payload = history()
    for key in ("bars", "contracts", "quotes"):
        second = deepcopy(payload[key])
        for row in second:
            if "symbol" in row:
                row["symbol"] = "MSFT"
            for field in ("id", "contract_id"):
                if field in row:
                    row[field] = row[field].replace("AAPL", "MSFT")
        payload[key].extend(second)
    result = replay(payload, "protective_put", portfolio={"cash": 1000, "positions": {"AAPL": 100, "MSFT": 100}},
                    policy={"max_premium_fraction": 0.015})
    first_day = result["equity_curve"][1]
    assert len(first_day["option_positions"]) == 1  # 单腿 210，日总预算 315，不能两腿各用一遍预算。
    assert any(e["type"] == "plan_rejected" and e["reason"] == "premium_budget" for e in result["events"])


def test_terminal_liquidation_cannot_fabricate_a_fill():
    payload = history(expiry_index=8)
    last = payload["bars"][3]["date"]
    payload["bars"] = payload["bars"][:4]
    payload["quotes"] = [q for q in payload["quotes"] if q["date"] <= last]
    for q in payload["quotes"]:
        if q["date"] == last and q["phase"] == "close":
            q["ask_size"] = q["bid_size"] = 0
    result = replay(payload, "covered_call", policy={"liquidate_at_end": True})
    assert not result["execution_completed"] and result["final_equity"] is None
    assert result["ending_positions"]["options"]
    assert "终点预定平仓未完成" in result["errors"][0]["error"]
