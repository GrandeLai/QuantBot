from copy import deepcopy
import json

import pytest

from quantbot.ai.research_catalog import (
    check_window, claim_development, dataset_record, load, reserve_plans, trial_registry, write_revision,
)
from quantbot.evidence import hash_payload
from quantbot.factors.conditional_study import file_ref


def trial(identity="failed-old"):
    return {"id": identity, "market": "crypto", "definition": {"hypothesis": "old", "p_value": 1},
            "registered_at": "2026-01-01T00:00:00+00:00"}


def seed(tmp_path):
    policy = tmp_path / "policy.txt"
    policy.write_text("unchanged gate")
    catalog = {"version": 1, "scope": "offline_development_index", "trials": [trial()],
        "datasets": [{"id": "old-prices", "market": "crypto", "symbols": ["BTC/USDT"],
                      "read_start": "2018-01-01", "read_end": "2026-07-01", "role": "exposed_development"}],
        "outcomes": {"failed-old": {"status": "rejected"}}, "accesses": [], "policies": [file_ref(policy)],
        "protected_intervals": [{"market": "crypto", "symbols": ["BTC/USDT", "ETH/USDT"],
                                 "start": "2026-07-03", "end": "2026-09-01", "reason": "sealed"}],
        "formal_admission": False, "trading_enabled": False}
    path = tmp_path / "v1.json"
    write_revision(catalog, path)
    return path


def test_failed_and_reserved_trials_still_count_and_duplicate_conflicts_fail():
    registry = trial_registry([trial(), trial("new")])
    assert registry.global_declared_trial_count == 2
    assert registry.global_completed_trial_count == 0  # import exposure is not invented execution proof
    with pytest.raises(ValueError, match="duplicate"):
        trial_registry([trial(), trial()])
    changed = trial()
    changed["definition"]["hypothesis"] = "changed"
    with pytest.raises(ValueError, match="conflicting"):
        trial_registry([trial(), changed])


@pytest.mark.parametrize("start,end", [("2018-01-01", "2018-01-05"), ("2026-06-01", "2026-07-01")])
def test_overlap_uses_symbols_and_full_read_interval_not_filename(tmp_path, start, end):
    catalog = load(seed(tmp_path))
    result = check_window(catalog, "crypto", ["BTC/USDT"], start, end, role="independent")
    assert result["allowed_by_index"] is False
    assert "previously_exposed_interval_including_warmup" in result["reasons"]
    assert check_window(catalog, "crypto", ["BTC/USDT"], start, end, role="development")["allowed_by_index"]


def test_sealed_window_denied_even_for_development_and_new_symbol_not_certified(tmp_path):
    catalog = load(seed(tmp_path))
    for role in ("development", "independent"):
        assert not check_window(catalog, "crypto", ["BTC/USDT"], "2026-07-03", "2026-07-04", role=role)["allowed_by_index"]
    result = check_window(catalog, "crypto", ["NEW/USDT"], "2020-01-01", "2020-02-01", role="independent")
    assert result["allowed_by_index"] and not result["independence_certified"]


def test_legacy_minute_access_preserves_history_and_blocks_reuse(tmp_path):
    original = seed(tmp_path)
    catalog = load(original)
    legacy = {"market": "crypto", "symbols": ["ETH/USDT"], "read_start": "2025-01-01",
              "read_end": "2025-06-30", "purpose": "old minute-data acquisition"}
    catalog["accesses"].append(legacy)
    path = tmp_path / "legacy.json"
    write_revision(catalog, path, parent=original)
    loaded = load(path)
    assert loaded["accesses"] == [legacy]
    assert check_window(loaded, "crypto", ["ETH/USDT"], "2025-02-01", "2025-02-02",
                        role="development")["allowed_by_index"]
    independent = check_window(loaded, "crypto", ["ETH/USDT"], "2025-02-01", "2025-02-02", role="independent")
    assert not independent["allowed_by_index"]
    assert independent["overlapping_exposures"][0].startswith("legacy:")
    successor = tmp_path / "new.json"
    claim_development(path, successor, market="crypto", symbols=["ETH/USDT"],
                      start="2025-02-01", end="2025-02-02", purpose="new development only")
    assert load(successor)["accesses"][0] == legacy


@pytest.mark.parametrize("interval", [
    {"read_start": "2025-01-01"},
    {"start": "2025-01-01", "end": "2025-02-01", "read_start": "2025-01-02", "read_end": "2025-02-01"},
])
def test_incomplete_or_conflicting_access_intervals_are_rejected(tmp_path, interval):
    catalog = load(seed(tmp_path))
    catalog["accesses"].append({"market": "crypto", "symbols": ["ETH/USDT"], **interval})
    with pytest.raises(ValueError, match="access interval"):
        write_revision(catalog, tmp_path / "invalid.json")


def test_claim_before_failure_preserves_exposure_and_rejects_sealed(tmp_path):
    original = seed(tmp_path)
    successor = tmp_path / "v2.json"
    claim_development(original, successor, market="crypto", symbols=["ETH/USDT"],
                      start="2020-01-01", end="2020-02-01", purpose="attempt that may fail")
    result = check_window(load(successor), "crypto", ["ETH/USDT"], "2020-01-02", "2020-01-03", role="independent")
    assert not result["allowed_by_index"]
    assert load(successor)["outcomes"] == load(original)["outcomes"]
    with pytest.raises(ValueError, match="denied"):
        claim_development(successor, tmp_path / "denied.json", market="crypto", symbols=["ETH/USDT"],
                          start="2026-07-03", end="2026-08-01", purpose="forbidden")
    assert not (tmp_path / "denied.json").exists()


def test_revisions_reject_history_loss_parent_tamper_and_overwrite(tmp_path):
    path = seed(tmp_path)
    catalog = load(path)
    changed = deepcopy(catalog)
    changed["trials"] = []
    changed["outcomes"] = {}
    with pytest.raises(ValueError, match="discard"):
        write_revision(changed, tmp_path / "bad.json", parent=path)
    with pytest.raises(FileExistsError):
        write_revision(catalog, path)
    successor = tmp_path / "v2.json"
    write_revision(catalog, successor, parent=path)
    original = json.loads(path.read_text())
    original["coverage"] = "tamper"
    original["digest"] = hash_payload({k: v for k, v in original.items() if k != "digest"})
    path.write_text(json.dumps(original))
    with pytest.raises(ValueError, match="冻结输入"):
        load(successor)


def test_crypto_open_timestamp_is_counted_as_next_close_and_bounds_checked(tmp_path):
    data = tmp_path / "bars.csv"
    data.write_text("timestamp,symbol,bar\n2026-06-30T00:00:00+00:00,BTC/USDT,1d\n")
    section = {"data": file_ref(data), "spec": {"bar": "1d", "symbols": ["BTC/USDT"]},
               "latest_allowed_close_date": "2026-07-01", "provenance": {"point_in_time_verified": False}}
    record = dataset_record("crypto", section)
    assert record["read_end"] == "2026-07-01"
    section["latest_allowed_close_date"] = "2026-06-30"
    with pytest.raises(ValueError, match="boundary"):
        dataset_record("crypto", section)


def test_reserving_plans_counts_both_markets_and_preserves_old_results(tmp_path):
    path = seed(tmp_path)
    hypotheses = [{"id": f"{m}-{i}", "market": m, "mechanism": "finite", "definition": {"lookback": 20},
                   "controls": ["cash"], "failure_conditions": ["nonpositive_net"],
                   "data_requirements": ["unmodified risk"]} for m in ("crypto", "stocks") for i in range(2)]
    plans = {"stage": "hypotheses_only", "model_calls_authorized": 0, "trading_enabled": False,
             "formal_admission": False, "preserved_policies": load(path)["policies"],
             "cost_bps": [15, 30], "hypotheses": hypotheses}
    source = tmp_path / "plans.json"
    source.write_text(json.dumps(plans))
    result = reserve_plans(path, source, tmp_path / "reserved.json")
    assert trial_registry(result["trials"]).global_declared_trial_count == 5
    assert result["outcomes"] == load(path)["outcomes"]
    assert len(result["outcomes"]) == 1
    plans["cost_bps"] = [5, 10]
    source.write_text(json.dumps(plans))
    with pytest.raises(ValueError, match="cost"):
        reserve_plans(path, source, tmp_path / "lower-cost.json")
