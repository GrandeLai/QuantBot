"""候选配置证据包的闭环、不可覆盖与失败关闭测试。"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from quantbot.price_action.experiment import (
    SnapshotVerification,
    build_candidate_config,
    run_registered_experiment,
    write_experiment_artifact,
)
from quantbot.price_action.governance import HypothesisSpec, SourceFingerprint
from quantbot.price_action.governance.bundle import (
    BUNDLE_FILE,
    CANDIDATE_CONFIG_FILE,
    CandidateBundleError,
    EXPERIMENT_FILE,
    ROLLBACK_CONFIG_FILE,
    SNAPSHOT_FILE,
    SOURCE_FILE,
    create_candidate_bundle,
    verify_candidate_bundle,
)
from quantbot.price_action.config import canonical_config_json, config_hash

from .conftest import make_cfg


@dataclass
class _FakeResult:
    equity: pd.Series

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"pnl": 100.0, "ret": 0.01}, {"pnl": -25.0, "ret": -0.0025}]
        )


def _baseline(tmp_path) -> dict:
    config = make_cfg(tmp_path)
    config["data"]["snapshot"] = "frozen-001"
    return config


def _source(*, clean: bool = True, digest: str = "a") -> SourceFingerprint:
    source_hash = digest * 64
    if clean:
        git_head = "0123456789abcdef0123456789abcdef01234567"
        return SourceFingerprint(
            code_version=git_head,
            source_hash=source_hash,
            git_head=git_head,
            dirty=False,
            file_count=12,
        )
    return SourceFingerprint(
        code_version=f"uncommitted+{source_hash}",
        source_hash=source_hash,
        git_head=None,
        dirty=True,
        file_count=12,
    )


def _snapshot(digest: str = "b") -> SnapshotVerification:
    return SnapshotVerification(
        snapshot_id="frozen-001",
        verified=True,
        verification_mode="full_sha256",
        manifest_sha256=digest * 64,
        file_count=3,
        path="/frozen/snapshot",
    )


def _provider(source: SourceFingerprint):
    def provide(_root):
        return source

    return provide


def _verifier(snapshot: SnapshotVerification):
    def verify(_config, snapshot_id, *, project_root=None):
        assert snapshot_id == "frozen-001"
        return snapshot

    return verify


def _experiment_file(tmp_path, baseline: dict, source: SourceFingerprint):
    candidate, _ = build_candidate_config(
        baseline, {"strategy.breakout_window": 60}
    )
    hypothesis = HypothesisSpec(
        hypothesis_id="hyp-breakout-60",
        title="延长突破确认",
        price_action_rationale="更长的价格突破窗口应减少假突破",
        baseline_config_hash=config_hash(baseline),
        candidate_config_hash=config_hash(candidate),
        data_snapshot="frozen-001",
        single_change={"strategy.breakout_window": 60},
        created_at="2026-08-09T00:00:00+00:00",
    )
    index = pd.bdate_range("2025-01-01", periods=90)

    def runner(_data, config):
        daily = 0.0004 \
            if config["strategy"]["breakout_window"] == 55 else 0.0007
        equity = pd.Series(
            100_000 * np.cumprod(np.full(len(index), 1 + daily)), index=index
        )
        return _FakeResult(equity)

    artifact = run_registered_experiment(
        hypothesis,
        baseline,
        {},
        runner=runner,
        snapshot_verifier=_verifier(_snapshot()),
        provenance_provider=_provider(source),
        project_root=tmp_path,
        completed_at="2026-08-09T01:00:00+00:00",
        n_paths=100,
        block=10,
    )
    path = tmp_path / "experiment-input.json"
    write_experiment_artifact(artifact, path)
    return path, candidate


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_create_and_verify_bundle_closes_all_files_without_applying_config(tmp_path):
    baseline = _baseline(tmp_path)
    original = copy.deepcopy(baseline)
    source = _source(clean=True)
    experiment_path, candidate = _experiment_file(tmp_path, baseline, source)
    original_experiment = experiment_path.read_bytes()

    created = create_candidate_bundle(
        baseline,
        experiment_path,
        tmp_path / "candidates",
        project_root=tmp_path,
        source_provider=_provider(source),
        snapshot_verifier=_verifier(_snapshot()),
        created_at="2026-08-09T02:00:00+00:00",
    )

    assert baseline == original
    assert created.path.name == config_hash(candidate)
    assert created.eligible is True
    assert created.ineligibility_reasons == ()
    assert created.forward_evidence_verified is False
    assert {path.name for path in created.path.iterdir()} == {
        EXPERIMENT_FILE,
        CANDIDATE_CONFIG_FILE,
        ROLLBACK_CONFIG_FILE,
        SOURCE_FILE,
        SNAPSHOT_FILE,
        BUNDLE_FILE,
    }
    assert (created.path / EXPERIMENT_FILE).read_bytes() == original_experiment
    assert json.loads((created.path / CANDIDATE_CONFIG_FILE).read_text()) == candidate
    assert json.loads((created.path / ROLLBACK_CONFIG_FILE).read_text()) == baseline
    assert not (tmp_path / "config.yaml").exists()

    manifest = json.loads((created.path / BUNDLE_FILE).read_text())
    assert set(manifest["files"]) == {
        EXPERIMENT_FILE,
        CANDIDATE_CONFIG_FILE,
        ROLLBACK_CONFIG_FILE,
        SOURCE_FILE,
        SNAPSHOT_FILE,
    }
    for name, digest in manifest["files"].items():
        assert digest == _sha256(created.path / name)

    before_verify = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in created.path.iterdir()
    }
    checked = verify_candidate_bundle(
        created.path,
        project_root=tmp_path,
        source_provider=_provider(source),
        snapshot_verifier=_verifier(_snapshot()),
    )
    assert checked.candidate_config_hash == created.candidate_config_hash
    assert checked.hypothesis == created.hypothesis
    assert checked.bundle_sha256 == created.bundle_sha256
    assert checked.file_sha256 == created.file_sha256
    assert checked.forward_evidence_verified is False
    assert {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in created.path.iterdir()
    } == before_verify


def test_missing_git_head_and_dirty_source_generate_ineligible_audit_bundle(tmp_path):
    baseline = _baseline(tmp_path)
    source = _source(clean=False)
    experiment_path, _ = _experiment_file(tmp_path, baseline, source)

    created = create_candidate_bundle(
        baseline,
        experiment_path,
        tmp_path / "candidates",
        project_root=tmp_path,
        source_provider=_provider(source),
        snapshot_verifier=_verifier(_snapshot()),
    )

    assert created.eligible is False
    assert created.ineligibility_reasons == (
        "git_head_missing",
        "source_worktree_dirty",
    )
    checked = verify_candidate_bundle(
        created.path,
        project_root=tmp_path,
        source_provider=_provider(source),
        snapshot_verifier=_verifier(_snapshot()),
    )
    assert checked.eligible is False
    assert checked.ineligibility_reasons == created.ineligibility_reasons


def test_source_or_snapshot_drift_fails_before_candidate_is_published(tmp_path):
    baseline = _baseline(tmp_path)
    source = _source(clean=True)
    experiment_path, _ = _experiment_file(tmp_path, baseline, source)

    with pytest.raises(CandidateBundleError, match="源码指纹"):
        create_candidate_bundle(
            baseline,
            experiment_path,
            tmp_path / "source-drift",
            project_root=tmp_path,
            source_provider=_provider(_source(clean=True, digest="c")),
            snapshot_verifier=_verifier(_snapshot()),
        )
    assert not (tmp_path / "source-drift").exists()

    with pytest.raises(CandidateBundleError, match="manifest SHA-256"):
        create_candidate_bundle(
            baseline,
            experiment_path,
            tmp_path / "snapshot-drift",
            project_root=tmp_path,
            source_provider=_provider(source),
            snapshot_verifier=_verifier(_snapshot("d")),
        )
    assert not (tmp_path / "snapshot-drift").exists()


def test_paired_result_cannot_diverge_from_raw_experiment_section(tmp_path):
    baseline = _baseline(tmp_path)
    source = _source(clean=True)
    experiment_path, _ = _experiment_file(tmp_path, baseline, source)
    payload = json.loads(experiment_path.read_text(encoding="utf-8"))
    payload["result"]["paired_cagr_diff"] += 0.5
    experiment_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CandidateBundleError, match="paired_comparison.diff_annual"):
        create_candidate_bundle(
            baseline,
            experiment_path,
            tmp_path / "candidates",
            project_root=tmp_path,
            source_provider=_provider(source),
            snapshot_verifier=_verifier(_snapshot()),
        )


def test_existing_candidate_directory_is_never_overwritten(tmp_path):
    baseline = _baseline(tmp_path)
    source = _source(clean=True)
    experiment_path, _ = _experiment_file(tmp_path, baseline, source)
    output = tmp_path / "candidates"
    created = create_candidate_bundle(
        baseline,
        experiment_path,
        output,
        project_root=tmp_path,
        source_provider=_provider(source),
        snapshot_verifier=_verifier(_snapshot()),
    )
    before = {
        path.name: path.read_bytes() for path in created.path.iterdir()
    }

    with pytest.raises(FileExistsError, match="拒绝覆盖"):
        create_candidate_bundle(
            baseline,
            experiment_path,
            output,
            project_root=tmp_path,
            source_provider=_provider(source),
            snapshot_verifier=_verifier(_snapshot()),
        )

    assert {path.name: path.read_bytes() for path in created.path.iterdir()} == before
    assert not [path for path in output.iterdir() if path.name.startswith(".")]


def test_verify_fails_even_if_tampered_candidate_file_hash_is_rewritten(tmp_path):
    baseline = _baseline(tmp_path)
    source = _source(clean=True)
    experiment_path, _ = _experiment_file(tmp_path, baseline, source)
    created = create_candidate_bundle(
        baseline,
        experiment_path,
        tmp_path / "candidates",
        project_root=tmp_path,
        source_provider=_provider(source),
        snapshot_verifier=_verifier(_snapshot()),
    )

    candidate_path = created.path / CANDIDATE_CONFIG_FILE
    candidate = json.loads(candidate_path.read_text())
    candidate["strategy"]["breakout_window"] = 70
    candidate_path.write_text(canonical_config_json(candidate) + "\n", encoding="utf-8")

    bundle_path = created.path / BUNDLE_FILE
    bundle = json.loads(bundle_path.read_text())
    bundle["files"][CANDIDATE_CONFIG_FILE] = _sha256(candidate_path)
    bundle_path.write_text(
        json.dumps(
            bundle,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidateBundleError, match="实验重建候选配置"):
        verify_candidate_bundle(
            created.path,
            project_root=tmp_path,
            source_provider=_provider(source),
            snapshot_verifier=_verifier(_snapshot()),
        )
