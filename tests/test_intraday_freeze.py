import json

import pytest

from quantbot.ai.research_catalog import load, write_revision
from quantbot.factors.conditional_study import file_ref, read
from quantbot.intraday import research


def frozen_missing_data(tmp_path, monkeypatch, generation=1):
    timestamp = "2026-10-23T00:00:00+00:00"
    historical = {1: 139, 2: 143, 3: 147}[generation]
    trials = [
        {"id": f"prior_{i}", "market": "crypto", "definition": {}, "registered_at": timestamp}
        for i in range(historical)
    ]
    original = {
        "version": 1,
        "scope": "offline_development_index",
        "parent": None,
        "trials": trials,
        "outcomes": {t["id"]: {"p_value": 1.0, "qualified": False} for t in trials},
        "datasets": [],
        "accesses": [],
        "policies": [],
        "protected_intervals": [],
        "formal_admission": False,
        "trading_enabled": False,
    }
    prior = tmp_path / "prior.json"
    write_revision(original, prior)
    names = {1: ("volume_confirmation", "retest_confirmation"),
             2: ("volume_cost_room", "retest_persistence"),
             3: ("vwap_trend_reclaim", "failed_breakdown_reversal")}[generation]
    cards = [
        {"id": f"{m}_{f}", "market": m, "factor": f}
        for m in ("crypto", "stocks")
        for f in names
    ]
    protocol = {
        "id": f"dual_market_intraday_v{generation}",
        "frozen_at": timestamp,
        "prior_catalog": file_ref(prior),
        "factors": cards,
        "statistics": {"minimum_family_size": historical + 4, "familywise_alpha": 0.05,
                       "historical_trials": historical},
    }
    path = tmp_path / "protocol.json"
    path.write_text(json.dumps(protocol))
    monkeypatch.setattr(research, "checked_protocol", lambda _: protocol)
    manifest = {
        "protocol": file_ref(path),
        **{m: {"data": None, "reasons": ["fixture_missing_data"]} for m in ("crypto", "stocks")},
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    frozen = research.freeze(path, manifest_path, tmp_path / "execution")
    return path, frozen


def test_freeze_registers_all_trials_and_missing_data_never_passes(tmp_path, monkeypatch):
    path, frozen = frozen_missing_data(tmp_path, monkeypatch)
    catalog = load(frozen["catalog"]["path"])
    assert len(catalog["trials"]) == 143
    assert all(t["registered_at"] for t in catalog["trials"])
    report = research.run(path, tmp_path / "execution/execution-freeze.json", tmp_path / "study")
    assert report["family_size"] == 143
    assert report["factor_pool"] == {"crypto": [], "stocks": []}
    assert not report["execution_completed"] and not report["trading_enabled"]
    assert all(r["status"] == "blocked_data" for r in report["results"])
    completed = load(tmp_path / "study/catalog-completed.json")
    assert all(completed["outcomes"][k] == v for k, v in catalog["outcomes"].items())
    assert completed["trials"] == catalog["trials"]


def test_changed_source_rejected_before_any_replay(tmp_path, monkeypatch):
    path, _ = frozen_missing_data(tmp_path, monkeypatch)
    monkeypatch.setattr(research, "source_tree_hash", lambda: "changed")
    with pytest.raises(ValueError, match="冻结代码"):
        research.run(path, tmp_path / "execution/execution-freeze.json", tmp_path / "study")
    assert not (tmp_path / "study").exists()


def test_changed_data_manifest_rejected_before_any_replay(tmp_path, monkeypatch):
    path, _ = frozen_missing_data(tmp_path, monkeypatch)
    manifest = read(tmp_path / "manifest.json")
    manifest["stocks"]["data"] = {"path": "untrusted"}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="冻结输入"):
        research.run(path, tmp_path / "execution/execution-freeze.json", tmp_path / "study")
    assert not (tmp_path / "study").exists()


def test_followup_keeps_all_143_old_outcomes_and_registers_four_more(tmp_path, monkeypatch):
    path, frozen = frozen_missing_data(tmp_path, monkeypatch, generation=2)
    catalog = load(frozen["catalog"]["path"])
    report = research.run(path, tmp_path / "execution/execution-freeze.json", tmp_path / "study")
    completed = load(tmp_path / "study/catalog-completed.json")
    assert len(catalog["trials"]) == report["family_size"] == 147
    assert all(completed["outcomes"][k] == v for k, v in catalog["outcomes"].items())
    assert len(completed["outcomes"]) == 147
    assert all(r["status"] == "blocked_data" for r in report["results"])


def test_new_mechanisms_preserve_147_outcomes_and_record_all_blocked_trials(tmp_path, monkeypatch):
    path, frozen = frozen_missing_data(tmp_path, monkeypatch, generation=3)
    catalog = load(frozen["catalog"]["path"])
    report = research.run(path, tmp_path / "execution/execution-freeze.json", tmp_path / "study")
    completed = load(tmp_path / "study/catalog-completed.json")
    assert report["family_size"] == len(completed["trials"]) == len(completed["outcomes"]) == 151
    assert all(completed["outcomes"][k] == v for k, v in catalog["outcomes"].items())
    assert all(r["status"] == "blocked_data" for r in report["results"])
    assert report["model_calls"] == report["broker_calls"] == 0
