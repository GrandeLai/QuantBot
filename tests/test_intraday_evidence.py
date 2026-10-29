"""Evidence consistency tests use generated prices; they do not certify a data vendor."""

import csv
from datetime import datetime, timedelta

import pytest

from quantbot.factors.conditional_study import file_ref, read, save
from quantbot.intraday import evidence
from quantbot.intraday.data import FIELDS, session_grid


@pytest.fixture(params=["crypto", "stocks"])
def dataset(tmp_path, request):
    market = request.param
    symbols = ["BTC/USDT"] if market == "crypto" else ["SPY"]
    protocol = {
        "range": {"start": "2025-01-02", "end_exclusive": "2025-01-03"},
        "markets": {market: {"symbols": symbols}}, "protected_intervals": [],
        "execution_model": {"fee_bps": [10, 20], "half_spread_bps": [2.5, 5],
                            "slippage_bps": [2.5, 5], "impact_bps_at_participation_cap": 5,
                            "latency_seconds": [1, 301]},
    }
    sessions = session_grid(protocol, market)
    opening, closing = sessions["2025-01-02"]
    now = opening.to_pydatetime()
    bars = []
    while now < closing:
        bars.append({"timestamp": now.isoformat(), "symbol": symbols[0], "open": "100",
                     "high": "102", "low": "99", "close": "101", "volume": "5000"})
        now += timedelta(minutes=5)
    data = tmp_path / "prices.csv"
    with data.open("w") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(bars)
    # historical_unverified describes the schema branch under test, not the
    # origin of this fixture. A separate test verifies the synthetic branch.
    bundle = {"schema_version": 1, "market": market, "symbols": symbols,
              "range": protocol["range"], "data": file_ref(data),
              "data_kind": "historical_unverified", "price_basis": "raw"}
    header = {k: v for k, v in bundle.items() if k != "price_basis"}
    header.update(provider="test-provider", source="test fixture, not real historical evidence",
                  captured_at="2025-01-04T00:00:00+00:00")
    coverage = [{"session": "2025-01-02", "symbol": symbols[0],
                 "available_at": opening.isoformat(), "observed_at": opening.isoformat()}]
    docs = {
        "historical_availability": {**header, "price_basis": "raw", "rows": [
            {**bar, "available_at": (datetime.fromisoformat(bar["timestamp"]) + timedelta(minutes=5)).isoformat(),
             "observed_at": (datetime.fromisoformat(bar["timestamp"]) + timedelta(minutes=5)).isoformat()}
            for bar in bars]},
        "corporate_actions": {**header, "price_basis": "raw", "coverage": coverage, "actions": []},
        "execution_costs": {**header, "rows": [{**coverage[0], "sample_count": 10, "venue": "test",
            "fee_bps": 10, "half_spread_bps": 1, "slippage_bps": 1, "impact_bps": 0.1, "latency_seconds": 1}]},
    }
    for name, document in docs.items():
        save(tmp_path / (name + ".json"), document)
        bundle[name] = file_ref(tmp_path / (name + ".json"))
    save(tmp_path / "evidence.json", bundle)
    return protocol, market, {"data": bundle["data"], "evidence": file_ref(tmp_path / "evidence.json")}


def rewrite(section, component, mutate):
    """Rebind deliberately changed fixtures so semantic checks, not just SHA, run."""
    bundle = read(section["evidence"]["path"])
    path = bundle[component]["path"] if component else section["evidence"]["path"]
    value = read(path)
    mutate(value)
    from pathlib import Path
    Path(path).unlink()
    save(path, value)
    if component:
        bundle[component] = file_ref(path)
        Path(section["evidence"]["path"]).unlink()
        save(section["evidence"]["path"], bundle)
    section["evidence"] = file_ref(section["evidence"]["path"])


def test_complete_evidence_has_working_internal_verification_without_authentication(dataset):
    protocol, market, section = dataset
    report = evidence.verify_market_evidence(protocol, market, section)
    assert report["evidence_verified"] and report["pit_verified"] and report["costs_verified"]
    assert report["status"] == "evidence_verified_authenticity_unverified"
    assert not report["authenticity_verified"] and not report["admission_ready"]
    assert report["reasons"] == ["authenticity_unverified"]


def test_metadata_inspection_does_not_open_observation_or_price_files(dataset, monkeypatch):
    protocol, market, section = dataset
    allowed = section["evidence"]["path"]
    original_check, original_ref = evidence.check_ref, evidence.file_ref
    def checked(ref):
        assert ref["path"] == allowed
        return original_check(ref)
    def referenced(path):
        assert str(path) == allowed
        return original_ref(path)
    monkeypatch.setattr(evidence, "check_ref", checked)
    monkeypatch.setattr(evidence, "file_ref", referenced)
    result = evidence.inspect_evidence(section["evidence"], market, protocol["markets"][market]["symbols"],
                                      "2025-01-02", "2025-01-02", protocol["execution_model"])
    assert result["metadata_ready"] and result["data_ref"] == section["data"]
    assert not result["prices_read"] and not result["evidence_verified"]


@pytest.mark.parametrize("change", ["market", "range", "data", "synthetic"])
def test_bundle_rejects_misbinding_before_price_read_or_synthetic_as_financial_evidence(dataset, monkeypatch, change):
    protocol, market, section = dataset
    if change == "synthetic":
        for name in evidence.COMPONENTS:
            rewrite(section, name, lambda doc: doc.update(data_kind="synthetic"))
        rewrite(section, None, lambda doc: doc.update(data_kind="synthetic"))
        report = evidence.verify_market_evidence(protocol, market, section)
        assert report["consistency_verified"] and not report["evidence_verified"]
        assert "synthetic_data_not_financial_evidence" in report["reasons"]
        return
    rewrite(section, None, lambda doc: doc.update({change: {
        "market": "stocks" if market == "crypto" else "crypto",
        "range": {"start": "2025-01-01", "end_exclusive": "2025-01-03"},
        "data": {"path": section["data"]["path"], "sha256": "0" * 64},
    }[change]}))
    monkeypatch.setattr(evidence, "load_bars", lambda *a: pytest.fail("misbound input opened prices"))
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["prices_read"] and report["reasons"][0].startswith("invalid_evidence_metadata")


@pytest.mark.parametrize("change", ["late", "wrong_price", "missing_row", "missing_available", "duplicate"])
def test_rebound_bad_historical_observations_fail(dataset, change):
    protocol, market, section = dataset
    def mutate(doc):
        if change == "late":
            # Third 5m bar ends exactly at the 15m decision; one second late is rejected.
            stamp = datetime.fromisoformat(doc["rows"][2]["available_at"]) + timedelta(seconds=1)
            doc["rows"][2].update(available_at=stamp.isoformat(), observed_at=stamp.isoformat())
        elif change == "wrong_price":
            doc["rows"][0]["close"] = "103"
        elif change == "missing_row":
            doc["rows"].pop()
        elif change == "duplicate":
            doc["rows"].append(doc["rows"][0])
        else:
            del doc["rows"][0]["available_at"]
    rewrite(section, "historical_availability", mutate)
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["pit_verified"] and not report["evidence_verified"]
    assert "invalid_historical_availability" in report["reasons"]


@pytest.mark.parametrize("component", ["historical_availability", "corporate_actions", "execution_costs"])
def test_component_sha_tamper_is_rejected(dataset, component):
    protocol, market, section = dataset
    from pathlib import Path
    path = Path(read(section["evidence"]["path"])[component]["path"])
    path.write_text(path.read_text() + " ")
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["evidence_verified"]
    assert "invalid_" + component in report["reasons"]


@pytest.mark.parametrize("change", ["late", "missing", "underpriced"])
def test_cost_coverage_and_causal_calibration(dataset, change):
    protocol, market, section = dataset
    def mutate(doc):
        if change == "missing":
            doc["rows"] = []
        elif change == "underpriced":
            doc["rows"][0]["fee_bps"] = 12  # Base uses 10; stress alone is insufficient.
        else:
            stamp = datetime.fromisoformat(doc["rows"][0]["observed_at"]) + timedelta(seconds=1)
            doc["rows"][0]["observed_at"] = stamp.isoformat()
    rewrite(section, "execution_costs", mutate)
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["costs_verified"] and not report["evidence_verified"]


def test_company_action_coverage_cannot_be_asserted_with_empty_list(dataset):
    protocol, market, section = dataset
    rewrite(section, "corporate_actions", lambda doc: doc.update(coverage=[]))
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["checks"]["corporate_actions"]["verified"]


def test_protected_or_missing_data_is_rejected_before_reading_any_price(dataset, monkeypatch):
    protocol, market, section = dataset
    monkeypatch.setattr(evidence, "check_ref", lambda *a: pytest.fail("metadata block opened a referenced file"))
    missing = evidence.verify_market_evidence(protocol, market, {"data": None, "pit_verified": True})
    assert not missing["prices_read"] and "minute_data_missing" in missing["reasons"]
    protocol["protected_intervals"] = [{"market": market, "symbols": ["*"],
                                        "start": "2025-01-02", "end": "2025-02-01"}]
    blocked = evidence.verify_market_evidence(protocol, market, section)
    assert not blocked["prices_read"] and blocked["reasons"] == ["invalid_or_protected_range"]


def test_data_sha_tampering_cannot_leave_a_passing_report(dataset):
    protocol, market, section = dataset
    from pathlib import Path
    path = Path(section["data"]["path"])
    path.write_text(path.read_text().replace("5000", "6000", 1))
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["evidence_verified"] and report["reasons"][0].startswith("invalid_raw_minutes")


@pytest.mark.parametrize("dataset", ["stocks"], indirect=True)
def test_stock_action_requires_details_and_preopen_publication(dataset):
    protocol, market, section = dataset
    rewrite(section, "corporate_actions", lambda doc: doc["actions"].append({
        "symbol": "SPY", "date": "2025-01-02", "type": "split",
        "available_at": "2025-01-02T14:00:00+00:00"}))
    report = evidence.verify_market_evidence(protocol, market, section)
    assert not report["evidence_verified"] and "invalid_corporate_actions" in report["reasons"]


@pytest.mark.parametrize("dataset", ["stocks"], indirect=True)
@pytest.mark.parametrize("action_type", ["split", "dividend"])
def test_well_formed_split_supported_but_first_day_dividend_needs_prior_close(dataset, action_type):
    protocol, market, section = dataset
    action = {"symbol": "SPY", "date": "2025-01-02", "type": action_type,
              "available_at": "2025-01-02T14:00:00+00:00", "id": "action-1"}
    action.update({"ratio": "2"} if action_type == "split" else {
        "amount_per_share": "1", "pay_date": "2025-01-06", "currency": "USD", "distribution": "ordinary_cash"})
    rewrite(section, "corporate_actions", lambda doc: doc["actions"].append(action))
    report = evidence.verify_market_evidence(protocol, market, section)
    if action_type == "split":
        assert report["checks"]["corporate_actions"]["verified"] and report["consistency_verified"]
        assert report["execution_compatible"] and report["evidence_verified"]
    else:
        assert not report["execution_compatible"] and not report["evidence_verified"]
        assert "此前完整会话收盘价" in report["checks"]["corporate_actions"]["reason"]


def test_source_change_during_wrapper_check_refuses_a_success_artifact(tmp_path, monkeypatch):
    from quantbot.ai.research_catalog import write_revision
    catalog = {"version": 1, "scope": "offline_development_index", "trials": [], "outcomes": {},
               "datasets": [], "accesses": [], "policies": [], "protected_intervals": [],
               "formal_admission": False, "trading_enabled": False}
    write_revision(catalog, tmp_path / "catalog.json")
    protocol = {"range": {"start": "2025-01-02", "end_exclusive": "2025-01-03"},
                "markets": {"crypto": {"symbols": ["BTC/USDT"]}, "stocks": {"symbols": ["SPY"]}},
                "protected_intervals": [], "prior_catalog": file_ref(tmp_path / "catalog.json")}
    save(tmp_path / "protocol.json", protocol)
    save(tmp_path / "manifest.json", {"protocol": file_ref(tmp_path / "protocol.json"),
         "crypto": {"data": None}, "stocks": {"data": None}})
    monkeypatch.setattr(evidence, "checked_protocol", lambda *a, **k: protocol)
    hashes = iter(["before", "after"])
    monkeypatch.setattr(evidence, "source_tree_hash", lambda: next(hashes))
    with pytest.raises(ValueError, match="source changed"):
        evidence.evidence_check(tmp_path / "protocol.json", tmp_path / "manifest.json", tmp_path / "result.json")
    assert not (tmp_path / "result.json").exists()


def test_component_changed_after_validation_cannot_report_verified(dataset, monkeypatch):
    protocol, market, section = dataset
    original = evidence._costs
    def mutate(*args):
        result = original(*args)
        from pathlib import Path
        path = Path(read(section["evidence"]["path"])["historical_availability"]["path"])
        path.write_text(path.read_text() + " ")
        return result
    monkeypatch.setattr(evidence, "_costs", mutate)
    result = evidence.verify_market_evidence(protocol, market, section)
    assert not result["evidence_verified"] and not result["checks"]["input_integrity"]["verified"]


def test_handwritten_pit_flag_does_not_replace_observation_documents(dataset):
    protocol, market, section = dataset
    result = evidence.verify_market_evidence(protocol, market, {"data": section["data"], "pit_verified": True})
    assert result["checks"]["raw_minutes"]["complete"]
    assert not result["evidence_verified"] and not result["pit_verified"]
    assert "historical_availability_missing" in result["reasons"]


def test_metadata_reports_missing_component_before_independent_price_access(dataset):
    from pathlib import Path
    protocol, market, section = dataset
    bundle = read(section["evidence"]["path"])
    Path(bundle["execution_costs"]["path"]).unlink()
    result = evidence.inspect_evidence(section["evidence"], market, protocol["markets"][market]["symbols"],
                                      "2025-01-02", "2025-01-02", protocol["execution_model"])
    assert not result["metadata_ready"] and not result["prices_read"]
    assert "missing_file:execution_costs" in result["reasons"]


@pytest.mark.parametrize("fail_price_load", [False, True])
@pytest.mark.parametrize("explicit_catalog", [False, True])
def test_development_price_access_is_registered_before_read_and_survives_failure(
        dataset, tmp_path, monkeypatch, fail_price_load, explicit_catalog):
    from quantbot.ai.research_catalog import check_window, load, write_revision
    protocol, market, section = dataset
    other = "stocks" if market == "crypto" else "crypto"
    protocol["markets"][other] = {"symbols": ["SPY"] if other == "stocks" else ["BTC/USDT"]}
    catalog = {"version": 1, "scope": "offline_development_index", "trials": [], "outcomes": {},
               "datasets": [], "accesses": [], "policies": [], "protected_intervals": [],
               "formal_admission": False, "trading_enabled": False}
    catalog_path = tmp_path / "catalog.json"
    write_revision(catalog, catalog_path)
    protocol["prior_catalog"] = file_ref(catalog_path)
    selected_catalog = catalog_path
    if explicit_catalog:
        catalog["accesses"].append({"id": "previous-study-access", "market": market,
                                    "symbols": protocol["markets"][market]["symbols"],
                                    "start": "2024-01-01", "end": "2024-12-31", "role": "development"})
        selected_catalog = tmp_path / "catalog-completed.json"
        write_revision(catalog, selected_catalog, parent=catalog_path)
    save(tmp_path / "protocol.json", protocol)
    save(tmp_path / "manifest.json", {"protocol": file_ref(tmp_path / "protocol.json"),
                                     market: section, other: {"data": None}})
    monkeypatch.setattr(evidence, "checked_protocol", lambda *a, **k: protocol)
    monkeypatch.setattr(evidence, "source_tree_hash", lambda: "stable-test-source")
    original_load = evidence.load_bars

    def assert_registered(*args):
        claimed = load(tmp_path / f"result.catalog-{market}.json")
        assert claimed["parent"] == file_ref(selected_catalog)
        record = claimed["accesses"][-1]
        assert (record["market"], record["symbols"], record["start"], record["end"], record["role"]) == (
            market, protocol["markets"][market]["symbols"], "2025-01-02", "2025-01-02", "development")
        if fail_price_load:
            raise ValueError("simulated failed minute load after exposure registration")
        return original_load(*args)

    monkeypatch.setattr(evidence, "load_bars", assert_registered)
    result = evidence.evidence_check(tmp_path / "protocol.json", tmp_path / "manifest.json", tmp_path / "result.json",
                                    catalog_path=selected_catalog if explicit_catalog else None)
    claimed = load(result["catalog"]["path"])
    assert not check_window(claimed, market, protocol["markets"][market]["symbols"],
                            "2025-01-02", "2025-01-02", role="independent")["allowed_by_index"]
    assert len(claimed["accesses"]) == 1 + int(explicit_catalog)  # Missing-data market was not exposed.
    if explicit_catalog:
        assert claimed["accesses"][0] == catalog["accesses"][0]
    assert result["markets"][market]["prices_read"]
    assert result["markets"][market]["evidence_verified"] is not fail_price_load


@pytest.mark.parametrize("invalid_catalog", ["ancestor", "unrelated"])
def test_evidence_catalog_must_preserve_protocol_ancestry_before_price_read(
        dataset, tmp_path, monkeypatch, invalid_catalog):
    from quantbot.ai.research_catalog import write_revision
    protocol, market, section = dataset
    other = "stocks" if market == "crypto" else "crypto"
    protocol["markets"][other] = {"symbols": ["SPY"] if other == "stocks" else ["BTC/USDT"]}
    catalog = {"version": 1, "scope": "offline_development_index", "trials": [], "outcomes": {},
               "datasets": [], "accesses": [], "policies": [], "protected_intervals": [],
               "formal_admission": False, "trading_enabled": False}
    ancestor_path = tmp_path / "ancestor.json"
    write_revision(catalog, ancestor_path)
    prior_path = tmp_path / "prior-catalog.json"
    write_revision(catalog, prior_path, parent=ancestor_path)
    protocol["prior_catalog"] = file_ref(prior_path)
    if invalid_catalog == "unrelated":
        selected = tmp_path / "unrelated.json"
        write_revision(catalog, selected)
    else:
        selected = ancestor_path
    save(tmp_path / "protocol.json", protocol)
    save(tmp_path / "manifest.json", {"protocol": file_ref(tmp_path / "protocol.json"),
                                     market: section, other: {"data": None}})
    monkeypatch.setattr(evidence, "checked_protocol", lambda *a, **k: protocol)
    monkeypatch.setattr(evidence, "load_bars", lambda *a: pytest.fail("invalid catalog opened prices"))
    with pytest.raises(ValueError, match="catalog must descend"):
        evidence.evidence_check(tmp_path / "protocol.json", tmp_path / "manifest.json", tmp_path / "result.json",
                                catalog_path=selected)
    assert not (tmp_path / "result.json").exists()
    assert not list(tmp_path.glob("result.catalog-*.json"))
