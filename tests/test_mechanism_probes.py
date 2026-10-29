from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.ai.config import load_config
from quantbot.ai.feeds import Snapshot, stock_calendar
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.factors.mechanism_probes import (
    KINDS, SECTORS, ResearchSnapshot, entry_targets, feature_panel, origins, run_probe, validate_actions,
)
from quantbot.factors.mechanism_screen import finalize, screen
from quantbot.factors.mechanism_study import METHOD, data_blockers, run, validate_protocol
from quantbot.factors.conditional_study import file_ref, save


def card(kind, symbols):
    crypto = kind in KINDS[:2]
    horizon = 10 if kind == KINDS[1] else 20
    return {"id": "test_"+kind, "mechanism": kind, "symbols": list(symbols),
            "market": "crypto" if crypto else "stocks",
            "definition": {"horizon_sessions": horizon, "origin_stride_sessions": horizon}}


def fixture_snapshot(market, symbols=None, actions=()):
    symbols = symbols or (["BTC/USDT", "ETH/USDT"] if market == "crypto" else ["SPY", "QQQ", "IWM", "IEF", "GLD"])
    config = load_config(environ={})
    config["market"].update(symbols=symbols, benchmark=symbols[0])
    config["risk"].update(crypto_stop_loss_pct=.03, crypto_take_profit_pct=.06,
                          stock_stop_loss_pct=.03, stock_take_profit_pct=.06)
    config["strategy"].update(crypto_cooldown_bars=3, stock_cooldown_bars=3)
    config["execution"].update(initial_cash=1000 if market == "crypto" else 10000,
                                cost_bps=15, limit_buffer_bps=50)
    calendar = stock_calendar("2023-01-01", "2024-12-31") if market == "stocks" else None
    days = calendar.sessions[:240] if calendar else [datetime(2023, 1, 1, tzinfo=UTC)+timedelta(days=i)
                                                    for i in range(240)]
    rows = []
    for i, day in enumerate(days):
        for j, symbol in enumerate(symbols):
            price = 100+i*(.1+j*.003)
            multiplier, dividend = 1, 0
            for action in actions:
                if action["symbol"] == symbol and action["date"] <= str(day.date()):
                    if action["type"] == "split":
                        multiplier *= float(action["ratio"])
                    else:
                        dividend += float(action["amount_per_share"])
            price = price/multiplier-dividend
            key = {"date": str(day.date())} if calendar else {"timestamp": day.isoformat(), "bar": "1d"}
            rows.append({**key, "symbol": symbol, "open": price, "high": price+.3,
                         "low": price-.3, "close": price+.05, "volume": 100000})
    snap = ResearchSnapshot(market, rows, config, "synthetic_mechanism_test", calendar, list(actions))
    spec = {"market": market, "bar": "1d", "max_daily_loss": .02, "max_drawdown": .05}
    return config, spec, snap


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_features_are_prefix_invariant_and_do_not_forward_fill(market):
    config, _, snapshot = fixture_snapshot(market)
    full = feature_panel(snapshot)
    rows = snapshot.rows[:180*len(config["market"]["symbols"]) ]
    prefix = Snapshot(market, rows, config, snapshot.source, snapshot.calendar)
    past = feature_panel(prefix)
    assert all(full[k] == v for k, v in past.items())
    broken = Snapshot(market, rows[:-1], config, snapshot.source, snapshot.calendar)
    with pytest.raises(ValueError):
        feature_panel(broken)


def test_signal_rules_controls_and_ties_have_fixed_semantics():
    config = load_config(environ={})
    values = {s: {"return20": .1, "return60": -.01, "close": 110, "sma120": 100} for s in ["BTC/USDT", "ETH/USDT"]}
    trend = card(KINDS[0], values)
    assert entry_targets(trend, values, config, candidate=True) == ({}, True)
    assert len(entry_targets(trend, values, config, candidate=False)[0]) == 2
    values = {s: {"zscore20": -2., "sma_gap": .02001, "efficiency20": .25} for s in values}
    reversal = card(KINDS[1], values)
    assert not entry_targets(reversal, values, config, candidate=True)[0]
    assert len(entry_targets(reversal, values, config, candidate=False)[0]) == 2
    values = {s: {"tr63": .2} for s in SECTORS}
    values["SPY"] = {"tr63": .1}
    sector = card(KINDS[3], values)
    target, valid = entry_targets(sector, values, config, candidate=True)
    control, _ = entry_targets(sector, values, config, candidate=False)
    assert valid and set(target) == {"XLB", "XLE"}
    assert set(control) == set(SECTORS) and sum(control.values()) == pytest.approx(sum(target.values()))
    with pytest.raises(ValueError, match="标的池"):
        entry_targets(sector, {"SPY": values["SPY"]}, config, candidate=True)


def test_zero_variance_is_invalid_not_an_entry():
    _, _, snapshot = fixture_snapshot("crypto")
    for row in snapshot.rows:
        row["close"] = 100.
    panel = feature_panel(snapshot)
    assert panel[(snapshot.times[-1].isoformat(), "BTC/USDT")]["zscore20"] is None
    assert panel[(snapshot.times[-1].isoformat(), "BTC/USDT")]["efficiency20"] is None


@pytest.mark.parametrize("kind", KINDS)
def test_all_four_probes_use_real_engine_and_independent_reconciliation(tmp_path, kind):
    market = "crypto" if kind in KINDS[:2] else "stocks"
    symbols = list(SECTORS)+["SPY"] if kind == KINDS[3] else None
    config, spec, snapshot = fixture_snapshot(market, symbols)
    item = card(kind, config["market"]["symbols"])
    result = run_probe(snapshot, item, feature_panel(snapshot), snapshot.times[180], config, spec,
                       tmp_path / "probe.jsonl", candidate=True)
    assert result["reconciliation"]["status"] == "reconciled"
    assert not result["risk_hold"]
    records = inspect_replay(tmp_path / "probe.jsonl")["records"]
    accounts = [r["payload"] for r in records if r["kind"] == "account"]
    assert accounts[0]["account"]["positions"] == {} and accounts[0]["orders"] == []
    assert all(o["side"] != "buy" for a in accounts[2:] for o in a["orders"] if o["status"] == "filled")
    assert len(accounts) == item["definition"]["horizon_sessions"]+1
    if kind == KINDS[3]:
        control = run_probe(snapshot, item, feature_panel(snapshot), snapshot.times[180], config, spec,
                            tmp_path / "control.jsonl", candidate=False)
        assert control["buy_fills"] <= config["risk"]["max_positions"]


def test_stop_is_first_and_no_reentry_after_protection(tmp_path):
    config, spec, snapshot = fixture_snapshot("crypto")
    for row in snapshot.rows[181*2:182*2]:
        row.update(high=150, low=80)  # Both protective levels touched on the entry bar.
    panel = feature_panel(snapshot)
    item = card(KINDS[0], config["market"]["symbols"])
    # Rebuild candle data after modifying fixture rows.
    snapshot = ResearchSnapshot("crypto", snapshot.rows, config, snapshot.source)
    result = run_probe(snapshot, item, panel, snapshot.times[180], config, spec, tmp_path / "stop.jsonl", candidate=True)
    assert result["buy_fills"] == 2 and result["net_return"] < 0
    assert result["exit_reasons"] == ["protective_stop", "protective_stop"]
    assert result["remaining_positions"] == {}


def test_split_and_dividend_units_reconcile_without_faking_publication_time(tmp_path):
    _, _, base = fixture_snapshot("stocks")
    actions = [{"id": "split", "symbol": "SPY", "type": "split", "date": base.times[185].date().isoformat(),
                "ratio": "2", "available_at": None},
               {"id": "div", "symbol": "SPY", "type": "dividend", "date": base.times[188].date().isoformat(),
                "pay_date": base.times[192].date().isoformat(), "amount_per_share": "1", "currency": "USD",
                "distribution": "ordinary_cash", "available_at": None}]
    config, spec, snapshot = fixture_snapshot("stocks", actions=actions)
    validate_actions(actions, set(config["market"]["symbols"]))
    panel = feature_panel(snapshot)
    assert panel[(snapshot.times[185].isoformat(), "SPY")]["tr63"] > 0
    result = run_probe(snapshot, card(KINDS[2], config["market"]["symbols"]), panel, snapshot.times[180],
                       config, spec, tmp_path / "actions.jsonl", candidate=True)
    assert result["reconciliation"]["status"] == "reconciled" and result["dividend_income"] > 0
    assert all(a["available_at"] is None for a in snapshot.actions)
    with pytest.raises(ValueError):
        validate_actions(actions+[deepcopy(actions[0])], set(config["market"]["symbols"]))


def test_origin_grid_purges_edges_and_keeps_entire_horizon():
    _, _, snap = fixture_snapshot("crypto")
    times = snap.times[130:230]
    window = {"id": "w", "start": times[0].date().isoformat(), "end": times[-1].date().isoformat(),
              "first_close": times[0].isoformat(), "last_close": times[-1].isoformat(), "expected_bars": len(times)}
    result = origins(card(KINDS[0], ["BTC/USDT", "ETH/USDT"]), snap.times, window)
    assert result == [times[i] for i in (20, 40, 60)]


STATS = {"minimum_feature_coverage": .95, "minimum_filled_per_window": 5, "minimum_total_filled": 30,
         "minimum_positive_windows": 2, "bootstrap_replicates": 4999, "block_sessions": 20, "seed": 1729,
         "minimum_family_size": 134, "familywise_alpha": .05}


def screen_fixture():
    calendars, rows = {}, []
    for i in range(3):
        times = [(datetime(2020+i, 1, 1, tzinfo=UTC)+timedelta(days=j)).isoformat() for j in range(120)]
        calendars[str(i)] = times
        for at in times[20:100:2]:
            branch = {"signal_at": at, "end_at": at, "feature_valid": True, "buy_fills": 1,
                      "risk_hold": None, "reconciliation": {"status": "reconciled"}}
            rows.append({"window": str(i), "signal_at": at, "candidate": {**branch, "net_return": .01},
                         "control": {**branch, "net_return": .005}})
    return calendars, {15: deepcopy(rows), 30: deepcopy(rows)}


def test_calendar_screen_requires_real_fills_pit_and_preserves_134_trials():
    calendars, events = screen_fixture()
    item = card(KINDS[0], ["BTC/USDT", "ETH/USDT"])
    result = screen(item, events, calendars, STATS, pit_verified=True)
    assert not result["reasons"]
    no_pit = screen(item, events, calendars, STATS, pit_verified=False)
    assert no_pit["p_value"] == 1 and "missing_point_in_time_inputs" in no_pit["reasons"]
    for rows in events.values():
        for row in rows:
            row["candidate"]["buy_fills"] = 0
    failed = screen(item, events, calendars, STATS, pit_verified=True)
    assert failed["p_value"] == 1 and any("buy_fills" in r for r in failed["reasons"])
    trials = [{"id": str(i)} for i in range(130)] + [{"id": str(i)} for i in range(130, 134)]
    catalog = {"trials": trials, "outcomes": {str(i): {"p_value": 1.} for i in range(130)}}
    results = [{**deepcopy(result), "id": str(i)} for i in range(130, 134)]
    report = finalize(results, catalog, STATS)
    assert report["family_size"] == 134 and all(r["qualified"] for r in report["results"])
    with pytest.raises(ValueError, match="全部"):
        finalize(results[:-1], catalog, STATS)
    events[30].pop()
    with pytest.raises(ValueError, match="配对"):
        screen(item, events, calendars, STATS, pit_verified=True)


def test_frozen_data_blocker_and_code_tamper_stop_before_execution(tmp_path):
    quality = tmp_path / "quality.json"
    save(quality, {"blocked_symbols": ["XLF"], "pit_verified": False})
    assert data_blockers({"quality": file_ref(quality)}, card(KINDS[3], [*SECTORS, "SPY"])) == ["invalid_ohlcv:XLF"]
    assert not data_blockers({"quality": file_ref(quality)}, card(KINDS[2], ["SPY", "QQQ", "IWM", "IEF", "GLD"]))
    protocol, freeze = tmp_path / "protocol.json", tmp_path / "freeze.json"
    save(protocol, {"id": "fixture"})
    save(freeze, {"protocol": file_ref(protocol), "source_hash": "changed"})
    with pytest.raises(ValueError, match="冻结代码"):
        run(protocol, freeze, tmp_path / "must_not_exist")
    assert not (tmp_path / "must_not_exist").exists()
    with pytest.raises(ValueError, match="禁止"):
        validate_protocol({"id": METHOD["version"], "method": METHOD, "stage": "development_only",
                           "model_calls_authorized": 0, "trading_enabled": True})
