"""Economic diagnostics must retain accounting and frozen-evidence boundaries."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import gzip
import json

import pytest

from quantbot.factors.conditional_study import file_ref, save
from quantbot.intraday.attribution import describe, episodes
from quantbot.intraday.diagnostics import diagnose


def replay(factor, trades=((1, "1"),), *, market="crypto", window="w1"):
    symbol = "BTC/USDT" if market == "crypto" else "SPY"
    events = []
    cash, cost = D(10000), D(0)
    for day, gain in trades:
        at = datetime(2025, 1, day, 12, tzinfo=UTC)
        identity = f"{factor}-{day}"
        for minute, side, quantity, price, reference, order in [
            (5, "buy", D(1), D("100.1"), D(100), identity),
            (10, "buy", D(1), D("100.1"), D(100), identity),
            (25, "sell", D(2), D(100) + D(gain) - D("0.1"), D(100) + D(gain), identity + "-exit"),
        ]:
            own_cost = quantity * abs(price - reference)
            cash += (-1 if side == "buy" else 1) * quantity * price
            cost += own_cost
            stamp = (at + timedelta(minutes=minute)).isoformat()
            events.append({
                "status": "filled", "symbol": symbol, "side": side, "quantity": str(quantity),
                "price": str(price), "reference_price": str(reference), "estimated_cost": str(own_cost),
                "cost_components": {"fee": str(own_cost)}, "client_order_id": order,
                "created_at": at.isoformat(), "at": stamp, "remaining": "1" if minute == 5 else "0",
                "bar_open": stamp, "bar_capacity": "100", "capacity_used_after": str(quantity),
                "execution_time_basis": "bar_open",
                "reason": "holding_limit" if side == "sell" else "entry", "partial": minute == 5,
            })
    return {
        "market": market, "factor": factor, "cost_bps": 15, "latency_seconds": 1, "window": window,
        "events": events, "initial_equity": 10000,
        "account": {"cash": float(cash), "equity": float(cash), "positions": {},
                    "estimated_execution_cost": float(cost)},
        "net_return": float(cash / 10000 - 1), "attempts": [], "risk_hold": None,
    }


def frozen_case(tmp_path, candidate=None, control=None):
    candidate = candidate or replay("candidate")
    control = control or replay("control", market=candidate["market"], window=candidate["window"])
    market = candidate["market"]
    protocol = tmp_path / "protocol.json"
    save(protocol, {"windows": [{"id": candidate["window"]}], "markets": {"crypto": {}, "stocks": {}},
                    "factors": [{"id": "candidate-card", "market": market,
                                 "factor": "candidate", "control": "control"}]})
    freeze = tmp_path / "freeze.json"
    save(freeze, {"protocol": file_ref(protocol), "source_hash": "a" * 64,
                  "quality": {"crypto": {"complete": True}, "stocks": {"complete": market == "stocks"}}})
    save(tmp_path / "started.json", {"freeze": file_ref(freeze)})
    artifact = tmp_path / "pair.json.gz"
    with gzip.open(artifact, "wt") as f:
        json.dump({"candidate": candidate, "control": control, "original_control": control}, f)
    study = tmp_path / "report.json"
    save(study, {"artifacts": [file_ref(artifact)], "sealed_holdout_evaluated": False,
                 "results": [{"market": "stocks", "id": "stock-card", "status": "blocked_data",
                              "reasons": ["missing_minutes"]}] if market == "crypto" else []})
    rows, summaries = [], []
    for result in [candidate, control]:
        own = episodes(result)
        keys = {key: result[key] for key in ("market", "factor", "cost_bps", "latency_seconds", "window")}
        rows += [{**row, **keys, "relative_volume": 2, "signal_body_bps": 10, "entry_chase_bps": -1}
                 for row in own]
        summaries.append({**keys, **describe(own)})
    attribution = tmp_path / "attribution.json"
    save(attribution, {"study": file_ref(study), "freeze": file_ref(freeze), "protocol": file_ref(protocol),
                       "unique_replays": 2, "summaries": summaries, "episodes": rows})
    return attribution, artifact


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_reconciles_partial_fills_and_keeps_cost_and_source_identity_separate(tmp_path, market):
    attribution, _ = frozen_case(tmp_path, replay("candidate", market=market))
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    candidate = next(row for row in result["replays"] if row["factor"] == "candidate")
    metrics = candidate["metrics"]
    assert metrics["episodes"] == 1
    assert metrics["fill_count"] == 3
    assert metrics["net_pnl"] == pytest.approx(1.6)
    assert metrics["gross_contribution"] == pytest.approx(2)
    assert metrics["cost"] == pytest.approx(.4)
    assert metrics["break_even_round_trip_cost_bps"] == pytest.approx(100)
    assert metrics["weighted_recorded_round_trip_cost_bps"] == pytest.approx(20)
    assert metrics["break_even_recorded_cost_multiplier"] == pytest.approx(5)
    assert metrics["median_holding_minutes"] == 20
    assert result["source_identity"]["historical_execution_source_hash"] == "a" * 64
    assert result["source_identity"]["analyzer_source_hash"] != "a" * 64
    assert candidate["descriptive_groups"]["relative_volume"]["at_least_1.5"]["episodes"] == 1
    assert not result["qualified"] and not result["formal_admission"] and not result["trading_enabled"]
    assert not result["verification"]["raw_market_data_read"]
    if market == "crypto":
        assert result["market_coverage"]["stocks"]["status"] == "blocked_data"
        assert result["market_coverage"]["stocks"]["blocked_candidates"][0]["reasons"] == ["missing_minutes"]


def test_zero_fills_does_not_imply_profit_or_infinite_affordable_cost(tmp_path):
    attribution, _ = frozen_case(tmp_path, replay("candidate", ()), replay("control", ()))
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    assert result["verification"]["flat_to_flat_episodes"] == 0
    for row in result["replays"]:
        assert row["metrics"]["break_even_round_trip_cost_bps"] is None
        assert row["metrics"]["break_even_recorded_cost_multiplier"] is None
        assert not row["metrics"]["positive_gross_available"]


def test_pairing_keeps_missed_profit_and_avoided_loss_separate_and_noncausal(tmp_path):
    attribution, _ = frozen_case(tmp_path, replay("candidate", ((1, "1"), (4, "-1"))),
                                 replay("control", ((1, "1"), (2, "1"), (3, "-1"))))
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    comparison = result["comparisons"][0]
    assert comparison["matched_episodes"] == 1
    assert comparison["matched_net_pnl_difference"] == 0
    assert comparison["control_only_profitable"]["net_pnl"] == pytest.approx(1.6)
    assert comparison["control_only_losing"]["net_pnl"] == pytest.approx(-2.4)
    assert comparison["candidate_only"]["net_pnl"] == pytest.approx(-2.4)
    assert comparison["observed_account_net_pnl_difference"] == pytest.approx(-1.6)
    assert comparison["causal_increment"] is None
    assert "not compounded" in result["scenarios"][0]["aggregation"]


def test_cost_eroded_winner_is_not_a_profitable_episode(tmp_path):
    attribution, _ = frozen_case(tmp_path, replay("candidate", ((1, "0.1"),)))
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    metric = result["replays"][0]["metrics"]
    assert metric["gross_winners_erased_by_cost"] == 1
    assert metric["net_winners"] == 0
    assert metric["break_even_recorded_cost_multiplier"] == pytest.approx(.5)
    assert metric["cost_to_positive_gross_ratio"] == pytest.approx(2)


def rewrite(path, value):
    path.write_text(json.dumps(value))


def test_rejects_attribution_accounting_tamper_even_with_fresh_attribution_hash(tmp_path):
    attribution, _ = frozen_case(tmp_path)
    value = json.loads(attribution.read_text())
    value["episodes"][0]["net_pnl"] = "100"
    rewrite(attribution, value)
    with pytest.raises(ValueError, match="归因与成交工件不一致"):
        diagnose(attribution, tmp_path / "diagnostics.json")
    assert not (tmp_path / "diagnostics.json").exists()


def test_rejects_changed_frozen_artifact(tmp_path):
    attribution, artifact = frozen_case(tmp_path)
    artifact.write_bytes(artifact.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="冻结输入已改变"):
        diagnose(attribution, tmp_path / "diagnostics.json")


def test_rejects_residual_positions_after_cash_audit(tmp_path):
    attribution, artifact = frozen_case(tmp_path)
    with gzip.open(artifact, "rt") as f:
        value = json.load(f)
    own = value["candidate"]
    own["events"] = own["events"][:2]
    own["account"].update(cash=9799.8, positions={"BTC/USDT": "2"}, estimated_execution_cost=.2)
    with gzip.open(artifact, "wt") as f:
        json.dump(value, f)
    study = tmp_path / "report.json"
    payload = json.loads(study.read_text())
    payload["artifacts"] = [file_ref(artifact)]
    rewrite(study, payload)
    payload = json.loads(attribution.read_text())
    payload["study"] = file_ref(study)
    rewrite(attribution, payload)
    with pytest.raises(ValueError, match="残仓"):
        diagnose(attribution, tmp_path / "diagnostics.json")


def test_rejects_sealed_evidence_and_mixed_freeze(tmp_path):
    attribution, _ = frozen_case(tmp_path)
    payload = json.loads(attribution.read_text())
    payload["sealed_holdout_evaluated"] = True
    rewrite(attribution, payload)
    with pytest.raises(ValueError, match="只接受开发窗口"):
        diagnose(attribution, tmp_path / "diagnostics.json")
    payload["sealed_holdout_evaluated"] = False
    rewrite(attribution, payload)
    rewrite(tmp_path / "started.json", {"freeze": {"path": "different", "sha256": "x"}})
    with pytest.raises(ValueError, match="冻结方案引用不一致"):
        diagnose(attribution, tmp_path / "diagnostics.json")


def test_missing_context_is_explicit_not_imputed(tmp_path):
    attribution, _ = frozen_case(tmp_path)
    payload = json.loads(attribution.read_text())
    for row in payload["episodes"]:
        del row["relative_volume"]
    rewrite(attribution, payload)
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    assert result["replays"][0]["missing_context_episodes"] == {"relative_volume": 1}
    assert result["replays"][0]["descriptive_groups"]["relative_volume"]["unavailable"]["episodes"] == 1


@pytest.mark.parametrize("mutation,message", [
    ("duplicate", "同一回放身份"),
    ("window", "候选与对照窗口"),
])
def test_rejects_conflicting_control_or_unpaired_window(tmp_path, mutation, message):
    attribution, artifact = frozen_case(tmp_path)
    with gzip.open(artifact, "rt") as f:
        pair = json.load(f)
    if mutation == "duplicate":
        pair["original_control"]["account"]["cash"] += 1
    else:
        pair["control"]["window"] = "another-window"
    with gzip.open(artifact, "wt") as f:
        json.dump(pair, f)
    study = tmp_path / "report.json"
    payload = json.loads(study.read_text())
    payload["artifacts"] = [file_ref(artifact)]
    rewrite(study, payload)
    payload = json.loads(attribution.read_text())
    payload["study"] = file_ref(study)
    rewrite(attribution, payload)
    with pytest.raises(ValueError, match=message):
        diagnose(attribution, tmp_path / "diagnostics.json")


def single_entry_loss(*, intrabar_exit=False):
    result = replay("candidate", ((1, "-1"),))
    first = result["events"][0]
    first.update(quantity="2", estimated_cost="0.2", cost_components={"fee": "0.2"},
                 capacity_used_after="2", remaining="0", partial=False)
    del result["events"][1]
    if intrabar_exit:
        result["events"][-1].update(execution_time_basis="intrabar_time_unknown",
                                    bar_open="2025-01-01T12:20:00+00:00", reason="stop_loss")
    return result


@pytest.mark.parametrize("intrabar", [False, True])
def test_legacy_attribution_keeps_ledger_but_withholds_ambiguous_extrema(tmp_path, intrabar):
    attribution, _ = frozen_case(tmp_path, single_entry_loss(intrabar_exit=intrabar))
    payload = json.loads(attribution.read_text())
    for row in payload["episodes"]:
        row["max_completed_close_gain_bps"] = 150
        for field in ("entry_execution_time_basis", "exit_execution_time_basis", "exit_bar_open"):
            del row[field]
    rewrite(attribution, payload)
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    metrics = next(r["metrics"] for r in result["replays"] if r["factor"] == "candidate")
    assert metrics["net_pnl"] == pytest.approx(-2.4)
    assert metrics["cost"] == pytest.approx(.4)
    assert metrics["median_hindsight_completed_close_gain_bps"] == (None if intrabar else 150)
    assert metrics["losing_completed_close_cost_coverage"] == {
        "below_recorded_cost": 0, "reached_recorded_cost": 0 if intrabar else 1,
        "unknown": 1 if intrabar else 0,
        "unknown_reasons": {"execution_timing_ambiguous": 1} if intrabar else {},
    }


@pytest.mark.parametrize("gain,expected", [(19, "below_recorded_cost"), (20, "reached_recorded_cost"),
                                          (21, "reached_recorded_cost")])
def test_corrected_intrabar_context_uses_only_bounded_completed_closes(tmp_path, gain, expected):
    attribution, _ = frozen_case(tmp_path, single_entry_loss(intrabar_exit=True))
    payload = json.loads(attribution.read_text())
    row = payload["episodes"][0]
    row.update(completed_close_evidence_version=1, completed_close_count=3,
               completed_close_cutoff_at="2025-01-01T12:20:00+00:00",
               completed_close_timing_status="bounded_before_intrabar_exit",
               max_completed_close_at="2025-01-01T12:15:00+00:00", max_completed_close_gain_bps=gain)
    rewrite(attribution, payload)
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    assert result["replays"][0]["metrics"]["losing_completed_close_cost_coverage"][expected] == 1


@pytest.mark.parametrize("mutation", ["exit_basis", "cutoff", "maximum_time"])
def test_rejects_changed_time_evidence_even_when_attribution_hash_is_fresh(tmp_path, mutation):
    attribution, _ = frozen_case(tmp_path, single_entry_loss(intrabar_exit=True))
    payload = json.loads(attribution.read_text())
    row = payload["episodes"][0]
    row.update(completed_close_evidence_version=1, completed_close_count=3,
               completed_close_cutoff_at="2025-01-01T12:20:00+00:00",
               completed_close_timing_status="bounded_before_intrabar_exit",
               max_completed_close_at="2025-01-01T12:15:00+00:00", max_completed_close_gain_bps=150)
    field = {"exit_basis": "exit_execution_time_basis", "cutoff": "completed_close_cutoff_at",
             "maximum_time": "max_completed_close_at"}[mutation]
    row[field] = "bar_open" if mutation == "exit_basis" else "2025-01-01T12:25:00+00:00"
    rewrite(attribution, payload)
    with pytest.raises(ValueError, match="不一致"):
        diagnose(attribution, tmp_path / "diagnostics.json")


def test_partial_fills_and_absent_closes_remain_unknown_for_cost_coverage(tmp_path):
    attribution, _ = frozen_case(tmp_path, replay("candidate", ((1, "-1"),)),
                                 {**single_entry_loss(), "factor": "control"})
    payload = json.loads(attribution.read_text())
    payload["episodes"][0]["max_completed_close_gain_bps"] = 150
    rewrite(attribution, payload)
    result = diagnose(attribution, tmp_path / "diagnostics.json")
    assert result["replays"][0]["metrics"]["losing_completed_close_cost_coverage"]["unknown_reasons"] == {
        "multiple_fills": 1}
    assert result["replays"][1]["metrics"]["losing_completed_close_cost_coverage"]["unknown_reasons"] == {
        "no_completed_close_evidence": 1}
