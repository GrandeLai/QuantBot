"""Synthetic integration and fail-closed checks; no historical holdout is read."""

from copy import deepcopy
import gzip
import io
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from quantbot.ai.research_catalog import load, write_revision
from quantbot.evidence import source_tree_hash
from quantbot.factors.conditional_study import file_ref, read, save
from quantbot.intraday import data, evidence, research, validation
from quantbot.intraday.execution import IntradayAccount
from tests.test_intraday_evidence import dataset, rewrite  # noqa: F401
from tests.test_intraday_execution import model, section  # noqa: F401


@pytest.fixture
def study(dataset, section, model, tmp_path, monkeypatch):  # noqa: F811
    evidence_protocol, market, supplied = dataset
    for component in (*evidence.COMPONENTS, None):
        rewrite(supplied, component, lambda doc: doc.update(data_kind="synthetic"))
    root = tmp_path / "development"
    root.mkdir()
    symbols = evidence_protocol["markets"][market]["symbols"]
    execution = model | {"one_way_cost_bps": [15], "latency_seconds": [1], "quantity_stocks": "1"}
    cards = [{"id": m + "_fixture", "market": m, "factor": "control", "control": "control"}
             for m in ("crypto", "stocks")]
    protocol = {"id": "fixture", "factors": cards, "policies": [], "sampling": {},
        "range": {"start": "2024-12-30", "end_exclusive": "2024-12-31"},
        "windows": [{"id": "development", "start": "2024-12-30", "end": "2024-12-30"}],
        "markets": {m: section | {"symbols": symbols if m == market else ["QQQ"]} for m in ("crypto", "stocks")},
        "execution_model": execution, "protected_intervals": [],
        "statistics": {"minimum_feature_coverage": 0.95, "minimum_filled_per_window": 5,
            "minimum_total_filled": 30, "minimum_positive_windows": 1, "familywise_alpha": 0.05,
            "minimum_family_size": 2, "purge_sessions": 0, "embargo_sessions": 0}}
    save(root / "PROTOCOL.json", protocol)
    # Only the production protocol parser is isolated; ledger, statistics, catalog,
    # evidence verification, execution, and CLI dispatch below are real.
    monkeypatch.setattr(validation, "checked_protocol", lambda path, **kwargs: read(path))
    frame, _, _ = data.load_bars(supplied["data"]["path"], evidence_protocol, market)
    frame["timestamp"] -= pd.Timedelta(days=3)
    prepared = research.prepare_sessions(frame, data.session_grid(protocol, market), symbols)
    replay = research.replay(protocol, market, prepared, protocol["windows"][0], "control", 15, 1)
    artifact = root / f"{market}_fixture-15-1-development.json.gz"
    with gzip.open(artifact, "wt") as handle:
        json.dump({"candidate": replay, "control": replay}, handle)
    scenario = research.screen_scenario(cards[0], [(replay, replay)], protocol["statistics"], cost=15, latency=1)
    report = {"family_size": 2, "artifacts": [file_ref(artifact)], "results": [
        {"id": c["id"], "market": c["market"], "scenarios": [scenario] if c["market"] == market else []}
        for c in cards]}
    save(root / "report.json", report)
    catalog = {"version": 1, "scope": "offline_development_index", "parent": None,
        "trials": [{"id": c["id"], "market": c["market"], "definition": c,
                    "registered_at": "2025-01-01T00:00:00+00:00"} for c in cards],
        "outcomes": {}, "datasets": [], "accesses": [{"id": "development", "market": market,
            "symbols": symbols, "start": "2024-12-30", "end": "2024-12-30"}],
        "policies": [], "protected_intervals": [], "formal_admission": False, "trading_enabled": False}
    write_revision(catalog, root / "catalog-frozen.json")
    save(root / "execution-freeze.json", {"protocol": file_ref(root / "PROTOCOL.json"),
        "catalog": file_ref(root / "catalog-frozen.json"), "source_hash": source_tree_hash()})
    save(root / "started.json", {"freeze": file_ref(root / "execution-freeze.json")})
    catalog["outcomes"] = {c["id"]: {"p_value": 1.0, "qualified": False,
        "evidence": file_ref(root / "report.json")} for c in cards}
    write_revision(catalog, root / "catalog-completed.json", parent=root / "catalog-frozen.json")
    request = {"version": 1, "mode": "synthetic", "selections": [{"market": market,
        "candidate_id": market + "_fixture", "range": evidence_protocol["range"],
        "windows": [{"id": "independent", "start": "2025-01-02", "end": "2025-01-02"}],
        "evidence": supplied["evidence"]["path"]}]}
    save(tmp_path / "request.json", request)
    return root, market, request, supplied


def test_development_rejection_precedes_new_price_or_window_access(study, tmp_path, monkeypatch):
    root, market, request, _ = study
    request["mode"] = "research"
    request["selections"] = [{"market": market, "candidate_id": market + "_fixture"}]
    save(tmp_path / "rejected.json", request)
    monkeypatch.setattr(validation, "_evidence", lambda *a: pytest.fail("blocked candidate inspected new evidence"))
    monkeypatch.setattr(validation, "load_bars", lambda *a: pytest.fail("blocked candidate read prices"))
    result = validation.prepare(root / "PROTOCOL.json", root / "report.json", tmp_path / "rejected.json", tmp_path / "blocked")
    assert result["status"] == "blocked" and result["eligible_candidate_count"] == {"crypto": 0, "stocks": 0}
    assert "development_numeric_gate_failed" in next(c for c in result["checks"] if c["market"] == market)["reasons"]
    assert not result["price_data_read"] and not (tmp_path / "blocked/validation-freeze.json").exists()


def test_synthetic_cli_prepare_run_assess_and_single_access(study, tmp_path, monkeypatch):
    root, market, _, supplied = study
    plan, output = tmp_path / "plan", tmp_path / "validation"
    stream = io.StringIO()
    with monkeypatch.context() as guard:
        guard.setattr(evidence, "load_bars", lambda *a: pytest.fail("prepare opened prices"))
        assert research.main(["validate-prepare", "--protocol", str(root / "PROTOCOL.json"),
            "--study", str(root / "report.json"), "--request", str(tmp_path / "request.json"),
            "--output", str(plan)], stdout=stream) == 0
    assert json.loads(stream.getvalue())["status"] == "ready"
    frozen = plan / "validation-freeze.json"
    assert research.main(["validate-run", "--freeze", str(frozen), "--output", str(output)], stdout=io.StringIO()) == 0
    report = read(output / "validation-report.json")
    assert report["family_size"] == 3 and len(report["results"]) == 1
    assert not report["results"][0]["strategy_qualified"] and not report["formal_admission"]
    assert not report["broker_paper_authorized"] and report["model_calls"] == report["broker_calls"] == 0
    assert report["results"][0]["data_evidence"]["consistency_verified"]
    stream = io.StringIO()
    assert research.main(["validate-assess", "--freeze", str(frozen), "--result",
                          str(output / "validation-report.json")], stdout=stream) == 0
    assert json.loads(stream.getvalue())["results"] == report["results"]
    with pytest.raises(ValueError, match="已经暴露"):
        validation.run(frozen, tmp_path / "another-output")
    again = validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", tmp_path / "another-plan")
    assert again["status"] == "blocked"
    assert "previously_exposed_interval_including_warmup" in next(
        c for c in again["checks"] if c["market"] == market)["reasons"]
    copied = tmp_path / "copy"
    shutil.copytree(plan, copied)
    with pytest.raises(ValueError, match="冻结计划已改变"):
        validation.run(copied / "validation-freeze.json", tmp_path / "copy-output")
    assert Path(supplied["data"]["path"]).exists()


def test_failed_run_keeps_access_and_trial_in_later_catalog(study, tmp_path, monkeypatch):
    root, _, _, _ = study
    plan = tmp_path / "plan"
    validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", plan)
    monkeypatch.setattr(evidence, "verify_market_evidence", lambda *a: {"reasons": ["fixture_missing_component"]})
    with pytest.raises(ValueError, match="fixture_missing_component"):
        validation.run(plan / "validation-freeze.json", tmp_path / "failed")
    failed = read(tmp_path / "failed/failure.json")
    assert failed["access_consumed"] and failed["type"] == "ValueError"
    catalog = load(tmp_path / "failed/catalog-completed.json")
    assert len(catalog["trials"]) == len(catalog["outcomes"]) == 3
    merged = load(root / "catalog-completed.json")
    validation._registry_trials(merged, root / "independent-validations")
    assert merged["outcomes"][catalog["trials"][-1]["id"]]["status"] == "independent_validation_failed"
    with pytest.raises(ValueError, match="已经暴露"):
        validation.run(plan / "validation-freeze.json", tmp_path / "failed-again")


def test_reservations_cannot_be_reset_by_repreparing_and_count_before_outcome(study, tmp_path):
    root, _, _, _ = study
    validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", tmp_path / "plan")
    with pytest.raises(ValueError, match="已经登记"):
        validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", tmp_path / "plan-again")
    catalog = load(root / "catalog-completed.json")
    validation._registry_trials(catalog, root / "independent-validations")
    assert len(catalog["trials"]) == 3
    assert catalog["outcomes"][catalog["trials"][-1]["id"]]["p_value"] == 1.0


@pytest.mark.parametrize("field", ["equity", "net_return", "partial_fills", "entry_orders_filled", "evaluated_end"])
def test_replayed_artifact_curve_must_match_the_ledger_and_dates(study, field):
    root, market, _, _ = study
    protocol = read(root / "PROTOCOL.json")
    report = read(root / "report.json")
    ref = report["artifacts"][0]
    with gzip.open(ref["path"], "rt") as handle:
        branches = json.load(handle)
    if field == "equity":
        branches["candidate"]["daily_curve"][0][field] += 100
    elif field == "evaluated_end":
        branches["candidate"][field] = "2026-01-01"
    else:
        branches["candidate"][field] += 100
    with gzip.open(ref["path"], "wt") as handle:
        json.dump(branches, handle)
    with pytest.raises(ValueError, match="权益曲线|净收益|起止标签"):
        validation._screens(protocol, next(c for c in protocol["factors"] if c["market"] == market), [file_ref(ref["path"])])


@pytest.mark.parametrize("cancellation", ["absent", "session_close", "after_close"])
def test_day_end_cannot_hide_unfinished_order_in_summary(study, cancellation):
    root, market, _, _ = study
    protocol = read(root / "PROTOCOL.json")
    ref = read(root / "report.json")["artifacts"][0]
    with gzip.open(ref["path"], "rt") as handle:
        branches = json.load(handle)
    result = branches["candidate"]
    sessions = data.session_grid(protocol, market)
    day = result["daily_curve"][0]["day"]
    account = IntradayAccount(market, protocol["markets"][market], protocol["execution_model"],
                              cost_bps=15, latency_seconds=1, sessions=sessions)
    assert account.enter(protocol["markets"][market]["symbols"][0], sessions[day][0].to_pydatetime(),
                         day, 100, 1, identity="unfinished-hidden-order")
    if cancellation != "absent":
        canceled_at = sessions[day][1] + pd.Timedelta(minutes=int(cancellation == "after_close"))
        account._cancel(next(iter(account.orders)), "session_end", canceled_at.to_pydatetime())
    result["events"].extend(account.events)
    assert not result["daily_curve"][-1]["pending_orders"]
    with gzip.open(ref["path"], "wt") as handle:
        json.dump(branches, handle)
    if cancellation == "session_close":
        validation._screens(protocol, next(c for c in protocol["factors"] if c["market"] == market),
                            [file_ref(ref["path"])])
    else:
        with pytest.raises(ValueError, match="未结束订单"):
            validation._screens(protocol, next(c for c in protocol["factors"] if c["market"] == market),
                                [file_ref(ref["path"])])


@pytest.mark.parametrize("market,symbol", [("crypto", "BTC/USDT"), ("stocks", "SPY")])
def test_day_end_order_audit_accepts_partial_fills_and_protective_exit(section, model, market, symbol):  # noqa: F811
    from tests.test_intraday_execution import T, account, bar, enter, step

    replay = account(section, model, market)
    enter(replay, symbol)
    for minute in (5, 10, 15, 20):
        step(replay, minute, bar(symbol))
    # Protection originates inside the matcher, then partially fills and closes.
    for minute in (25, 30):
        step(replay, minute, bar(symbol, open="96", high="96.1", low="95.9", close="96"))
    closing = T + pd.Timedelta(hours=6, minutes=30)
    step(replay, 385, bar(symbol), close=closing)
    result = replay.report() | {"evaluated_start": "2025-01-02", "evaluated_end": "2025-01-02"}
    assert result["partial_fills"] > 0 and not result["account"]["pending_orders"]
    assert any(e.get("reason") == "protective_stop" and e["status"] == "filled" for e in result["events"])
    validation._audit_curve(result, {"markets": {market: section}}, market, ["2025-01-02"],
                            {"2025-01-02": (pd.Timestamp(T), pd.Timestamp(closing))})


def test_missing_component_prevents_reservation_without_price_read(study, tmp_path, monkeypatch):
    root, market, _, supplied = study
    bundle = read(supplied["evidence"]["path"])
    Path(bundle["execution_costs"]["path"]).unlink()
    monkeypatch.setattr(evidence, "load_bars", lambda *a: pytest.fail("missing metadata read prices"))
    report = validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", tmp_path / "plan")
    assert report["status"] == "blocked"
    assert "missing_file:execution_costs" in next(c for c in report["checks"] if c["market"] == market)["reasons"]
    assert not (root / "independent-validations").exists()


def test_evidence_check_exposure_continues_into_independent_preparation(study, tmp_path, monkeypatch):
    root, market, request, supplied = study
    probe = read(root / "PROTOCOL.json")
    probe["range"] = request["selections"][0]["range"]
    probe["prior_catalog"] = file_ref(root / "catalog-frozen.json")
    save(tmp_path / "probe-protocol.json", probe)
    other = "stocks" if market == "crypto" else "crypto"
    save(tmp_path / "probe-manifest.json", {"protocol": file_ref(tmp_path / "probe-protocol.json"),
                                           market: supplied, other: {"data": None}})
    monkeypatch.setattr(evidence, "checked_protocol", lambda path, **kwargs: read(path))
    checked = evidence.evidence_check(tmp_path / "probe-protocol.json", tmp_path / "probe-manifest.json",
                                     tmp_path / "evidence-report.json", catalog_path=root / "catalog-completed.json")
    assert checked["markets"][market]["prices_read"]
    request["catalog"] = checked["catalog"]
    save(tmp_path / "continued-request.json", request)
    monkeypatch.setattr(validation, "load_bars", lambda *a: pytest.fail("exposed independent prices reopened"))
    monkeypatch.setattr(evidence, "load_bars", lambda *a: pytest.fail("exposed independent prices reopened"))
    prepared = validation.prepare(root / "PROTOCOL.json", root, tmp_path / "continued-request.json", tmp_path / "plan")
    assert prepared["status"] == "blocked" and not prepared["price_data_read"]
    assert "previously_exposed_interval_including_warmup" in next(
        row for row in prepared["checks"] if row["market"] == market)["reasons"]


def test_followup_catalog_is_the_new_freeze_parent(study, tmp_path):
    root, market, request, _ = study
    catalog = load(root / "catalog-completed.json")
    catalog["accesses"].append({"id": "prior-followup", "market": market,
                               "symbols": read(root / "PROTOCOL.json")["markets"][market]["symbols"],
                               "start": "2024-01-01", "end": "2024-01-02", "role": "development"})
    write_revision(catalog, tmp_path / "continued.json", parent=root / "catalog-completed.json")
    request["catalog"] = file_ref(tmp_path / "continued.json")
    save(tmp_path / "continued-request.json", request)
    prepared = validation.prepare(root / "PROTOCOL.json", root, tmp_path / "continued-request.json", tmp_path / "plan")
    assert prepared["status"] == "ready"
    assert load(tmp_path / "plan/catalog-frozen.json")["parent"] == request["catalog"]


def test_followup_catalog_cannot_replace_development_history(study, tmp_path, monkeypatch):
    root, _, request, _ = study
    request["catalog"] = file_ref(root / "catalog-frozen.json")
    save(tmp_path / "old-catalog-request.json", request)
    monkeypatch.setattr(validation, "_evidence", lambda *a: pytest.fail("invalid catalog inspected evidence"))
    with pytest.raises(ValueError, match="连续修订"):
        validation.prepare(root / "PROTOCOL.json", root, tmp_path / "old-catalog-request.json", tmp_path / "plan")
    assert not (tmp_path / "plan").exists()


def test_detached_rewritten_report_is_not_admission_evidence(study, tmp_path):
    root, _, _, _ = study
    plan, output = tmp_path / "plan", tmp_path / "validation"
    validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", plan)
    validation.run(plan / "validation-freeze.json", output)
    path = output / "validation-report.json"
    report = read(path)
    report["results"][0]["strategy_qualified"] = True
    path.unlink()
    save(path, report)
    # Even rewriting the local completion ledger cannot replace the canonical outcome.
    catalog = read(output / "catalog-completed.json")
    trial = report["results"][0]["id"]
    catalog["outcomes"][trial]["evidence"] = file_ref(path)
    (output / "catalog-completed.json").unlink()
    write_revision(catalog, output / "catalog-completed.json", parent=output / "catalog-accessed.json")
    with pytest.raises(ValueError, match="绑定产物"):
        validation.assess(plan / "validation-freeze.json", path)


def test_positive_internal_qualification_is_possible_but_never_broker_permission(tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    plan = {"registry": str(registry), "mode": "research", "statistics": {"familywise_alpha": 0.05},
            "selected": [{"trial_id": "trial", "evidence": {"synthetic": False}}]}
    catalog = {"trials": [{"id": "prior"}, {"id": "trial"}], "outcomes": {"prior": {"p_value": 1.0}}}
    row = {"id": "trial", "candidate_id": "selected", "p_value": 0.001, "reasons": [],
           "data_evidence": {"evidence_verified": True, "authenticity_verified": False,
                             "reasons": ["authenticity_unverified"]}}
    result = validation._qualification(plan, catalog, [deepcopy(row)])
    assert result["results"][0]["numeric_passed"] and result["results"][0]["strategy_qualified"]
    assert not result["formal_admission"] and not result["broker_paper_authorized"] and not result["live_authorized"]
    plan["selected"][0]["evidence"]["synthetic"] = True
    assert not validation._qualification(plan, catalog, [deepcopy(row)])["results"][0]["strategy_qualified"]


def test_later_registered_trials_cannot_relax_frozen_holm_threshold(tmp_path):
    registry = tmp_path / "registry"
    registry.mkdir()
    save(registry / "later-reservation.json", {"trial": {"id": "later"}})
    save(registry / "later-outcome.json", {"p_value": 0.00001})
    plan = {"registry": str(registry), "mode": "research", "statistics": {"familywise_alpha": 0.05},
            "selected": [{"trial_id": "trial", "evidence": {"synthetic": False}}]}
    catalog = {"trials": [{"id": "prior"}, {"id": "trial"}], "outcomes": {"prior": {"p_value": 1.0}}}
    row = {"id": "trial", "candidate_id": "selected", "p_value": 0.02, "reasons": [],
           "data_evidence": {"evidence_verified": True, "authenticity_verified": False, "reasons": []}}
    report = validation._qualification(plan, catalog, [row])
    assert report["family_size"] == 3 and row["holm_p_value"] == pytest.approx(0.06)
    assert not row["numeric_passed"] and not row["strategy_qualified"]


@pytest.mark.parametrize("dataset", ["stocks"], indirect=True)
@pytest.mark.parametrize("tamper", [False, True])
def test_independent_split_uses_same_action_contract_and_rejects_nested_tamper_before_prices(
        study, tmp_path, monkeypatch, tamper):
    root, market, _, supplied = study
    rewrite(supplied, "corporate_actions", lambda doc: doc["actions"].append({
        "symbol": "SPY", "date": "2025-01-02", "type": "split", "id": "synthetic-independent-split",
        "available_at": "2025-01-02T14:00:00+00:00", "ratio": "2"}))
    plan = tmp_path / "action-plan"
    validation.prepare(root / "PROTOCOL.json", root, tmp_path / "request.json", plan)
    frozen = plan / "validation-freeze.json"
    if tamper:
        action_path = Path(read(supplied["evidence"]["path"])["corporate_actions"]["path"])
        action_path.write_text(action_path.read_text() + " ")
        monkeypatch.setattr(evidence, "load_bars", lambda *args: pytest.fail("tampered action opened prices"))
        monkeypatch.setattr(validation, "load_bars", lambda *args: pytest.fail("tampered action opened prices"))
        with pytest.raises(ValueError, match="invalid_corporate_actions"):
            validation.run(frozen, tmp_path / "action-validation")
        assert read(tmp_path / "action-validation/failure.json")["access_consumed"]
    else:
        report = validation.run(frozen, tmp_path / "action-validation")
        assert report["results"][0]["data_evidence"]["execution_compatible"]
        for ref in report["artifacts"]:
            with gzip.open(ref["path"], "rt") as handle:
                for result in json.load(handle).values():
                    assert result["corporate_actions"][0]["action"]["id"] == "synthetic-independent-split"
                    assert result["corporate_actions"][0]["cash_change"] == "0"
        assert not report["formal_admission"]
