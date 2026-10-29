from copy import deepcopy
import io
import json
from pathlib import Path

import pytest

from quantbot.factors.conditional_study import file_ref, read, save
from quantbot.intraday import attribution, data, research
from tests.test_intraday_execution import model, section  # noqa: F401
from tests.test_intraday_freeze import frozen_missing_data


@pytest.fixture
def archived_protocol(tmp_path, monkeypatch, model, section):  # noqa: F811
    path, _ = frozen_missing_data(tmp_path, monkeypatch)
    monkeypatch.setattr(research, "checked_protocol", data.checked_protocol)
    original = tmp_path / "policy.py"
    original.write_text("MAX_DRAWDOWN = 0.05\n")
    old_ref = file_ref(original)
    snapshot = tmp_path / "archived-policy.py"
    snapshot.write_bytes(original.read_bytes())
    original.write_text("# Shared matching implementation\nMAX_DRAWDOWN = 0.05\n")
    mappings = [{"original": old_ref, "snapshot": file_ref(snapshot), "current": file_ref(original)}]
    mapping_path = tmp_path / "policy-snapshots.json"
    save(mapping_path, mappings)
    prior = tmp_path / "prior-protocol.json"
    save(prior, {"markets": {market: {
        "config": {key: section[key] for key in ("risk", "strategy", "execution")},
        "spec": {key: section[key] for key in ("max_daily_loss", "max_drawdown")},
    } for market in ("crypto", "stocks")}})
    protocol = read(path)
    protocol.update(
        trading_enabled=False, formal_admission=False, sealed_holdout_evaluated=False,
        model_calls_authorized=0, policies=[old_ref], prior_protocol=file_ref(prior),
        protected_intervals=[], windows=[], range={"start": "2024-11-01", "end_exclusive": "2026-07-01"},
        markets={market: deepcopy(section) for market in ("crypto", "stocks")},
        sampling={"execution_bar_minutes": 5, "signal_bar_minutes": 15, "opening_range_minutes": 30,
                  "entry_end_minutes_before_close": 135, "max_holding_minutes": 120,
                  "flatten_minutes_before_close": 15},
        execution_model={**model, "quantity_stocks": "1"},
    )
    protocol["statistics"].update(
        minimum_feature_coverage=0.95, minimum_filled_per_window=5, minimum_total_filled=30,
        minimum_positive_windows=2, bootstrap_replicates=4999, block_sessions=20, seed=1729,
        purge_sessions=20, embargo_sessions=20,
    )
    path.write_text(json.dumps(protocol))
    save(path.with_suffix(".freeze.json"), {"protocol": file_ref(path)})
    manifest = read(tmp_path / "manifest.json")
    manifest["protocol"] = file_ref(path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return path, mapping_path, mappings


def test_snapshot_is_explicit_and_retains_original_protocol(archived_protocol):
    path, _, mappings = archived_protocol
    with pytest.raises(ValueError, match="冻结输入"):
        data.checked_protocol(path)
    assert data.checked_protocol(path, policy_snapshots=mappings) == read(path)


def test_unmapped_policy_keeps_original_hash_check(archived_protocol, tmp_path):
    path, _, mappings = archived_protocol
    second = tmp_path / "second-policy.py"
    second.write_text("original")
    protocol = read(path)
    protocol["policies"].append(file_ref(second))
    path.write_text(json.dumps(protocol))
    path.with_suffix(".freeze.json").write_text(json.dumps({"protocol": file_ref(path)}))
    second.write_text("changed")
    with pytest.raises(ValueError, match="冻结输入"):
        data.checked_protocol(path, policy_snapshots=mappings)


def test_policy_mapping_does_not_redirect_nonpolicy_references(archived_protocol):
    path, _, mappings = archived_protocol
    Path(read(path)["prior_protocol"]["path"]).write_text("{}")
    with pytest.raises(ValueError, match="冻结输入"):
        data.checked_protocol(path, policy_snapshots=mappings)


@pytest.mark.parametrize("change,reason", [
    ("duplicate", "重复"), ("unrelated", "policies"), ("snapshot_sha", "摘要"),
    ("current_path", "路径"), ("snapshot_bytes", "冻结输入"), ("current_bytes", "冻结输入"),
])
def test_invalid_policy_mapping_fails_closed(archived_protocol, change, reason):
    path, _, mappings = archived_protocol
    if change == "duplicate":
        mappings += deepcopy(mappings)
    elif change == "unrelated":
        mappings[0]["original"] = file_ref(path)
    elif change == "snapshot_sha":
        mappings[0]["snapshot"]["sha256"] = "wrong"
    elif change == "current_path":
        mappings[0]["current"]["path"] = str(path)
    else:
        Path(mappings[0][change.removesuffix("_bytes")]["path"]).write_text("changed")
    with pytest.raises(ValueError, match=reason):
        data.checked_protocol(path, policy_snapshots=mappings)


def test_snapshots_propagate_to_frozen_parent_protocol(archived_protocol, tmp_path):
    path, _, mappings = archived_protocol
    parent_ref = file_ref(path)
    parent = read(path)
    old_attribution = tmp_path / "old-attribution.json"
    save(old_attribution, {"protocol": parent_ref, "created_at": "2026-10-22T00:00:00+00:00"})
    parent.update(id="dual_market_intraday_v2", original_control_required=True, new_outcomes_seen=False,
                  parent_intraday_protocol=parent_ref, attribution=file_ref(old_attribution),
                  factor_parameters={"relative_volume_min": 1.5, "opening_range_cost_multiple": 1.0,
                                     "maximum_extension_range_fraction": 0.5, "persistence_minutes": 15})
    parent["factors"] = [{"market": market, "factor": factor, "control": control}
                         for market in ("crypto", "stocks")
                         for factor, control in (("volume_cost_room", "volume_confirmation"),
                                                 ("retest_persistence", "retest_confirmation"))]
    parent["statistics"].update(minimum_family_size=147, historical_trials=143, reserved_new_trials=4)
    child = tmp_path / "child.json"
    save(child, parent)
    save(child.with_suffix(".freeze.json"), {"protocol": file_ref(child)})
    assert data.checked_protocol(child, policy_snapshots=mappings) == parent


def test_cli_freeze_run_and_attribute_bind_policy_snapshots(archived_protocol, tmp_path):
    path, mapping_path, mappings = archived_protocol
    output = tmp_path / "archived-execution"
    assert research.main(["freeze", "--protocol", str(path), "--data-manifest", str(tmp_path / "manifest.json"),
                          "--output", str(output), "--policy-snapshots", str(mapping_path)],
                         stdout=io.StringIO()) == 0
    frozen = read(output / "execution-freeze.json")
    assert frozen["policy_snapshots"] == mappings
    assert frozen["policy_snapshots_input"] == file_ref(mapping_path)
    report = research.run(path, output / "execution-freeze.json", tmp_path / "study")
    assert report["factor_pool"] == {"crypto": [], "stocks": []}
    explained = attribution.attribute(path, tmp_path / "study/report.json", tmp_path / "attribution.json")
    assert explained["unique_replays"] == 0


@pytest.mark.parametrize("target", ["mapping", "snapshot", "current"])
def test_run_rejects_changed_mapping_or_policy_before_prices(archived_protocol, tmp_path, target):
    path, mapping_path, mappings = archived_protocol
    research.freeze(path, tmp_path / "manifest.json", tmp_path / "archived-execution",
                    policy_snapshots=mapping_path)
    changed = mapping_path if target == "mapping" else Path(mappings[0][target]["path"])
    changed.write_text("[]" if target == "mapping" else "changed")
    with pytest.raises(ValueError, match="冻结输入"):
        research.run(path, tmp_path / "archived-execution/execution-freeze.json", tmp_path / "study")
    assert not (tmp_path / "study").exists()


@pytest.mark.parametrize("target", ["mapping", "snapshot", "current"])
def test_run_rechecks_policy_bindings_before_publishing(archived_protocol, tmp_path, monkeypatch, target):
    path, mapping_path, mappings = archived_protocol
    research.freeze(path, tmp_path / "manifest.json", tmp_path / "archived-execution",
                    policy_snapshots=mapping_path)
    changed = mapping_path if target == "mapping" else Path(mappings[0][target]["path"])
    def change_after_screen(output, content):
        save(output, content)
        if output.name.endswith("-screen.json"):
            changed.write_text("[]" if target == "mapping" else "changed")
    monkeypatch.setattr(research, "save", change_after_screen)
    with pytest.raises(ValueError, match="冻结输入"):
        research.run(path, tmp_path / "archived-execution/execution-freeze.json", tmp_path / "study")
    assert (tmp_path / "study/failure.json").exists()
    assert not (tmp_path / "study/report.json").exists()
