"""Synthetic share-unit and action-boundary checks; no vendor or profitability claims."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pandas as pd
import pytest

from quantbot.intraday import data, research
from quantbot.intraday.execution import IntradayAccount
from tests.test_intraday_execution import model, section  # noqa: F401


@pytest.fixture
def history():
    protocol = {"range": {"start": "2025-01-02", "end_exclusive": "2025-02-14"},
                "markets": {"stocks": {"symbols": ["QQQ", "SPY"]}}}
    sessions = data.session_grid(protocol, "stocks")
    day = list(sessions)[21]
    rows = []
    for session, (opening, closing) in sessions.items():
        for i, stamp in enumerate(pd.date_range(opening, closing, freq="5min", inclusive="left")):
            for symbol in ("QQQ", "SPY"):
                price = 100 + min(i, 24) * 0.02
                rows.append({"timestamp": stamp, "symbol": symbol, "open": price,
                             "high": price + 0.02, "low": price - 0.02, "close": price + 0.01,
                             "volume": 10000})
    actions = [{"symbol": symbol, "date": day, "type": "split", "id": symbol + "-split",
                "available_at": (sessions[day][0] - pd.Timedelta(hours=1)).isoformat(), "ratio": ratio}
               for symbol, ratio in (("SPY", "2"), ("QQQ", "0.5"))]
    return pd.DataFrame(rows), sessions, actions


def split_frame(frame, sessions, actions):
    changed = frame.copy()
    for action in actions:
        mask = ((changed["symbol"] == action["symbol"])
                & (changed["timestamp"] >= sessions[action["date"]][0]))
        ratio = float(action["ratio"])
        for field in ("open", "high", "low", "close"):
            changed[field] = changed[field].astype(float)
            changed.loc[mask, field] /= ratio
        changed["volume"] = changed["volume"].astype(float)
        changed.loc[mask, "volume"] *= ratio
    return changed


def test_forward_reverse_split_preserves_causal_features_and_raw_prices(history):
    frame, sessions, actions = history
    before = research.prepare_sessions(frame, sessions, ["QQQ", "SPY"])
    changed = split_frame(frame, sessions, actions)
    after = research.prepare_sessions(changed, sessions, ["QQQ", "SPY"], actions=actions)
    day = actions[0]["date"]
    price_features = {"atr", "close", "opening_high", "opening_low", "session_vwap", "prior_hour_low"}
    for session in sessions:
        if session < day:
            assert before[session] == after[session]  # Future action cannot rewrite earlier sessions.
            continue
        for stamp, values in before[session]["signals"].items():
            for symbol, original in values.items():
                ratio = 2 if symbol == "SPY" else 0.5
                actual = after[session]["signals"][stamp][symbol]
                for key, value in original.items():
                    expected = value / ratio if key in price_features and value is not None else value
                    if isinstance(expected, float):
                        assert actual[key] == pytest.approx(expected), (session, symbol, key)
                    else:
                        assert actual[key] == expected
        for (_, raw), (_, adjusted) in zip(before[session]["bars"], after[session]["bars"], strict=True):
            for symbol, ratio in (("SPY", 2), ("QQQ", 0.5)):
                assert adjusted[symbol]["close"] == raw[symbol]["close"] / ratio
                assert adjusted[symbol]["volume"] == raw[symbol]["volume"] * ratio
    assert after[day]["feature_valid"]
    assert next(iter(after[day]["signals"].values()))["SPY"]["relative_volume"] == 1


def test_split_orders_keep_integer_shares_and_economic_exposure(history, section, model):  # noqa: F811
    frame, sessions, actions = history
    original = research.prepare_sessions(frame, sessions, ["QQQ", "SPY"])
    changed = research.prepare_sessions(split_frame(frame, sessions, actions), sessions, ["QQQ", "SPY"], actions=actions)
    day = actions[0]["date"]
    for symbol, ratio in (("SPY", Decimal("2")), ("QQQ", Decimal("0.5"))):
        accounts = []
        for prepared in (original, changed):
            account = IntradayAccount("stocks", section, model, cost_bps=15, latency_seconds=1, sessions=prepared)
            at, bars = prepared[day]["bars"][0]
            account.previous_volume = {symbol: Decimal("10000")}
            account.advance(at, bars, day=day, session_close=prepared[day]["closing"])
            signal_at = next(iter(prepared[day]["signals"]))
            value = prepared[day]["signals"][signal_at][symbol]
            assert account.enter(symbol, signal_at, day, value["close"], value["atr"], identity="units")
            order = next(iter(account.orders.values()))
            quantity = Decimal(order["quantity"])
            assert quantity == quantity.to_integral_value()
            accounts.append((quantity, Decimal(order["limit_price"])))
        old_quantity, old_limit = accounts[0]
        new_quantity, new_limit = accounts[1]
        assert abs(new_quantity - old_quantity * ratio) < max(Decimal("1"), ratio)
        assert abs(old_quantity * old_limit - new_quantity * new_limit) < max(old_limit, new_limit)


def test_cash_dividend_keeps_raw_gap_future_atr_and_zero_entitlement(history, section, model):  # noqa: F811
    frame, sessions, actions = history
    action = {k: v for k, v in actions[0].items() if k != "ratio"}
    action.update(type="dividend", amount_per_share="1", pay_date="2025-02-12",
                  currency="USD", distribution="ordinary_cash")
    day = action["date"]
    changed = frame.copy()
    mask = (changed["symbol"] == "SPY") & (changed["timestamp"] >= sessions[day][0])
    for field in ("open", "high", "low", "close"):
        changed.loc[mask, field] -= 1
    raw = research.prepare_sessions(changed, sessions, ["QQQ", "SPY"])
    prepared = research.prepare_sessions(changed, sessions, ["QQQ", "SPY"], actions=[action])
    next_day = list(sessions)[list(sessions).index(day) + 1]
    assert prepared[next_day] == raw[next_day]
    undropped = research.prepare_sessions(frame, sessions, ["QQQ", "SPY"])
    assert next(iter(prepared[next_day]["signals"].values()))["SPY"]["atr"] > next(
        iter(undropped[next_day]["signals"].values()))["SPY"]["atr"]
    account = IntradayAccount("stocks", section, model, cost_bps=15, latency_seconds=1, sessions=prepared)
    for at, bars in prepared[day]["bars"]:
        account.advance(at, bars, day=day, session_close=prepared[day]["closing"])
    report = account.report()
    assert report["account"]["cash"] == section["execution"]["initial_cash"]
    assert report["net_return"] == 0 and report["corporate_actions"][0]["cash_change"] == "0"


@pytest.mark.parametrize("pending_only", [False, True])
def test_action_open_rejects_pending_or_partially_unliquidated_inventory_before_new_prices(
        history, section, model, pending_only):  # noqa: F811
    from tests.test_intraday_execution import T, bar, enter, step

    _, _, actions = history
    actions[0]["date"] = "2025-01-03"
    sessions = {"2025-01-02": {}, "2025-01-03": {"actions": [actions[0]]}}
    account = IntradayAccount("stocks", section, model, cost_bps=15, latency_seconds=1, sessions=sessions)
    enter(account)
    if not pending_only:
        step(account, 5)
        step(account, 10)
        step(account, 15)
        # A real partially filled entry, then no sell liquidity before the close.
        step(account, 370, bar(volume="0"))
        step(account, 385, bar(volume="0"))
        assert account.state["positions"] and account.session_violations and account.orders
    state, orders, events = deepcopy(account.state), deepcopy(account.orders), deepcopy(account.events)
    with pytest.raises(ValueError, match="开盘前必须空仓且无待单"):
        account.advance(T + timedelta(days=1), bar(open="50", high="50", low="50", close="50"),
                        day="2025-01-03", session_close=T + timedelta(days=1, hours=6, minutes=30))
    assert account.state == state and account.orders == orders and account.events == events


@pytest.mark.parametrize("amount", ["25", "30"])
def test_special_cash_dividend_is_still_rejected(history, amount):
    frame, sessions, actions = history
    action = {k: v for k, v in actions[0].items() if k != "ratio"}
    action.update(type="dividend", amount_per_share=amount)
    # Prior close is 100.49, so equality at its exact 25% is the first blocked amount.
    previous_day = list(sessions)[20]
    previous = frame.loc[(frame["symbol"] == "SPY") & (frame["timestamp"] < sessions[action["date"]][0]), "close"].iloc[-1]
    assert previous_day < action["date"]
    action["amount_per_share"] = str(Decimal(str(previous)) * Decimal(amount) / 100)
    with pytest.raises(ValueError, match="25%"):
        research.prepare_sessions(frame, sessions, ["QQQ", "SPY"], actions=[action])


@pytest.fixture
def bound_history(history, section, model, tmp_path, monkeypatch):  # noqa: F811
    from quantbot.ai.research_catalog import write_revision
    from quantbot.factors.conditional_study import file_ref, read, save
    from quantbot.intraday import attribution, evidence

    frame, sessions, actions = history
    frame = split_frame(frame, sessions, actions)
    symbols = ["QQQ", "SPY"]
    empty = {"version": 1, "scope": "offline_development_index", "trials": [], "outcomes": {},
             "datasets": [], "accesses": [], "policies": [], "protected_intervals": [],
             "formal_admission": False, "trading_enabled": False}
    write_revision(empty, tmp_path / "prior.json")
    protocol = {"id": "synthetic-action-integration", "frozen_at": "2025-03-01T00:00:00+00:00",
                "range": {"start": "2025-01-02", "end_exclusive": "2025-02-14"},
                "markets": {"stocks": section | {"symbols": symbols},
                            "crypto": section | {"symbols": ["BTC/USDT"]}},
                "execution_model": model | {"one_way_cost_bps": [15], "latency_seconds": [1]},
                "windows": [{"id": "fixture", "start": "2025-01-02", "end": "2025-02-13"}],
                "protected_intervals": [], "prior_catalog": file_ref(tmp_path / "prior.json"),
                "factors": [{"id": "stocks_fixture", "market": "stocks", "factor": "control", "control": "control"}],
                "statistics": {"minimum_family_size": 1, "familywise_alpha": 0.05, "historical_trials": 0,
                    "purge_sessions": 1, "embargo_sessions": 1, "minimum_filled_per_window": 1,
                    "minimum_total_filled": 1, "minimum_positive_windows": 1, "minimum_feature_coverage": 0.95}}
    prices = tmp_path / "prices.csv"
    frame.to_csv(prices, index=False)
    bundle = {"schema_version": 1, "market": "stocks", "symbols": symbols, "range": protocol["range"],
              "data": file_ref(prices), "data_kind": "synthetic", "price_basis": "raw"}
    header = {k: v for k, v in bundle.items() if k != "price_basis"}
    header.update(provider="test", source="synthetic action regression", captured_at=protocol["frozen_at"])
    coverage = [{"session": day, "symbol": symbol, "available_at": opening.isoformat(),
                 "observed_at": opening.isoformat()}
                for day, (opening, _) in sessions.items() for symbol in symbols]
    documents = {
        "corporate_actions": header | {"price_basis": "raw", "coverage": coverage, "actions": actions},
        "historical_availability": header | {"price_basis": "raw", "rows": [
            row | {"timestamp": row["timestamp"].isoformat(),
                   "available_at": (row["timestamp"] + timedelta(minutes=5)).isoformat(),
                   "observed_at": (row["timestamp"] + timedelta(minutes=5)).isoformat()}
            for row in frame.to_dict("records")]},
        "execution_costs": header | {"rows": [row | {"sample_count": 10, "venue": "synthetic",
            "fee_bps": 10, "half_spread_bps": 1, "slippage_bps": 1, "impact_bps": 0.1,
            "latency_seconds": 1} for row in coverage]},
    }
    for name, document in documents.items():
        save(tmp_path / (name + ".json"), document)
        bundle[name] = file_ref(tmp_path / (name + ".json"))
    save(tmp_path / "evidence.json", bundle)
    save(tmp_path / "protocol.json", protocol)
    manifest = {"protocol": file_ref(tmp_path / "protocol.json"),
                "stocks": {"data": bundle["data"], "evidence": file_ref(tmp_path / "evidence.json")},
                "crypto": {"data": None, "reasons": ["synthetic_stocks_only"]}}
    save(tmp_path / "manifest.json", manifest)
    monkeypatch.setattr(research, "checked_protocol", lambda path, **kwargs: read(path))
    monkeypatch.setattr(attribution, "checked_protocol", lambda path, **kwargs: read(path))
    return protocol, manifest, bundle, evidence


def test_frozen_action_research_and_attribution_use_same_effective_history(bound_history, tmp_path):
    from quantbot.factors.conditional_study import read
    from quantbot.intraday import attribution

    _, _, bundle, evidence = bound_history
    frozen = research.freeze(tmp_path / "protocol.json", tmp_path / "manifest.json", tmp_path / "freeze")
    assert frozen["corporate_actions"]["stocks"]["document"] == bundle["corporate_actions"]
    report = research.run(tmp_path / "protocol.json", tmp_path / "freeze/execution-freeze.json", tmp_path / "study")
    assert report["verification"]["fills"] > 0
    explained = attribution.attribute(tmp_path / "protocol.json", tmp_path / "study/report.json", tmp_path / "attribution.json")
    assert explained["episodes"] and not explained["trading_enabled"]
    from quantbot.intraday.diagnostics import diagnose

    diagnostic = diagnose(tmp_path / "attribution.json", tmp_path / "diagnostics.json")
    assert diagnostic["verification"]["flat_to_flat_episodes"] == len(explained["episodes"])
    assert all(row["completed_close_evidence_version"] == 1 for row in explained["episodes"])
    verification = evidence.verify_market_evidence(read(tmp_path / "protocol.json"), "stocks", read(tmp_path / "manifest.json")["stocks"])
    assert verification["consistency_verified"] and verification["execution_compatible"]
    assert not verification["evidence_verified"] and not verification["authenticity_verified"]


@pytest.mark.parametrize("consumer", ["run", "attribute"])
def test_nested_action_tamper_rejected_before_research_or_attribution_prices(bound_history, tmp_path, monkeypatch, consumer):
    from pathlib import Path
    from quantbot.factors.conditional_study import file_ref, save
    from quantbot.intraday import attribution

    _, _, bundle, _ = bound_history
    research.freeze(tmp_path / "protocol.json", tmp_path / "manifest.json", tmp_path / "freeze")
    study = tmp_path / "study"
    study.mkdir()
    save(study / "started.json", {"freeze": file_ref(tmp_path / "freeze/execution-freeze.json")})
    save(study / "report.json", {"artifacts": []})
    path = Path(bundle["corporate_actions"]["path"])
    path.write_text(path.read_text() + " ")
    monkeypatch.setattr(research, "load_bars", lambda *args: pytest.fail("tampered action opened prices"))
    monkeypatch.setattr(attribution, "load_bars", lambda *args: pytest.fail("tampered action opened prices"))
    with pytest.raises(ValueError, match="冻结输入"):
        if consumer == "run":
            research.run(tmp_path / "protocol.json", tmp_path / "freeze/execution-freeze.json", tmp_path / "rejected")
        else:
            attribution.attribute(tmp_path / "protocol.json", study / "report.json", tmp_path / "rejected.json")


def test_failed_freeze_keeps_pre_price_access_when_action_semantics_fail(bound_history, tmp_path):
    from quantbot.ai.research_catalog import check_window, load
    from tests.test_intraday_evidence import rewrite
    from quantbot.factors.conditional_study import save

    protocol, manifest, _, _ = bound_history
    rewrite(manifest["stocks"], "corporate_actions", lambda document: document["actions"][0].update(ratio="1"))
    (tmp_path / "manifest.json").unlink()
    save(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="ratio 不能等于 1"):
        research.freeze(tmp_path / "protocol.json", tmp_path / "manifest.json", tmp_path / "failed-freeze")
    catalog = load(tmp_path / "failed-freeze/catalog-accessed-stocks.json")
    assert not check_window(catalog, "stocks", protocol["markets"]["stocks"]["symbols"],
                            "2025-01-02", "2025-02-13", role="independent")["allowed_by_index"]
    assert not (tmp_path / "failed-freeze/execution-freeze.json").exists()
