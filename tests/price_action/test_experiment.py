"""预注册价格行为实验执行层。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest

from quantbot.price_action.config import config_hash
from quantbot.price_action.experiment import (
    ExperimentValidationError,
    SnapshotVerification,
    build_candidate_config,
    derive_experiment_id,
    run_registered_experiment,
    write_experiment_artifact,
)
from quantbot.price_action.governance import (
    HypothesisSpec,
    ReleaseManifest,
    SourceFingerprint,
)
from .conftest import make_cfg


@dataclass
class FakeResult:
    equity: pd.Series

    def trades_df(self) -> pd.DataFrame:
        return pd.DataFrame(
            [{"pnl": 100.0, "ret": 0.01}, {"pnl": -25.0, "ret": -0.0025}]
        )


def _baseline(tmp_path) -> dict:
    cfg = make_cfg(tmp_path)
    cfg["data"]["snapshot"] = "frozen-001"
    return cfg


def _hypothesis(cfg: dict, *, path: str = "strategy.breakout_window", value=60):
    candidate, _ = build_candidate_config(cfg, {path: value})
    return HypothesisSpec(
        hypothesis_id="hyp-breakout-001",
        title="延长突破窗口",
        price_action_rationale="更长的价格突破窗口应减少假突破",
        baseline_config_hash=config_hash(cfg),
        candidate_config_hash=config_hash(candidate),
        data_snapshot="frozen-001",
        single_change={path: value},
        created_at="2026-08-09T00:00:00+00:00",
    )


def _provenance(_root) -> SourceFingerprint:
    return SourceFingerprint(
        code_version="test-code",
        source_hash="a" * 64,
        git_head=None,
        dirty=True,
        file_count=12,
    )


def _verified(_cfg, snapshot_id: str) -> SnapshotVerification:
    return SnapshotVerification(
        snapshot_id=snapshot_id,
        verified=True,
        verification_mode="full_sha256",
        manifest_sha256="b" * 64,
        file_count=3,
        path="/frozen/snapshot",
    )


def test_build_candidate_is_single_change_and_does_not_mutate_baseline(tmp_path):
    baseline = _baseline(tmp_path)
    original = copy.deepcopy(baseline)

    candidate, change = build_candidate_config(
        baseline, {"risk.risk_per_trade": 0.02}
    )

    assert baseline == original
    assert candidate["risk"]["risk_per_trade"] == 0.02
    assert change.to_dict() == {
        "path": "risk.risk_per_trade",
        "before": 0.025,
        "after": 0.02,
    }


@pytest.mark.parametrize(
    "single_change",
    [
        {"data.snapshot": "another"},
        {"backtest.slippage_bps": 10},
        {"strategy.breakout_window": 60, "strategy.exit_window": 30},
        {"strategy.breakout_window": 55},
    ],
)
def test_build_candidate_rejects_unregistered_or_non_experiment_changes(
    tmp_path, single_change
):
    with pytest.raises(ExperimentValidationError):
        build_candidate_config(_baseline(tmp_path), single_change)


def test_run_registered_experiment_uses_same_data_and_produces_contract(tmp_path):
    baseline = _baseline(tmp_path)
    hypothesis = _hypothesis(baseline)
    data = {"opaque": pd.DataFrame({"close": [1.0]})}
    calls: list[tuple[int, int]] = []
    index = pd.bdate_range("2025-01-01", periods=90)

    def runner(received_data, cfg):
        calls.append((id(received_data), cfg["strategy"]["breakout_window"]))
        daily = 0.0004 if cfg["strategy"]["breakout_window"] == 55 else 0.0007
        equity = pd.Series(100_000 * np.cumprod(np.full(len(index), 1 + daily)),
                           index=index)
        return FakeResult(equity)

    artifact = run_registered_experiment(
        hypothesis,
        baseline,
        data,
        runner=runner,
        snapshot_verifier=_verified,
        provenance_provider=_provenance,
        completed_at="2026-08-09T01:00:00+00:00",
        n_paths=100,
        block=10,
    )

    assert calls == [(id(data), 55), (id(data), 60)]
    assert artifact.result.price_action_policy_passed is True
    assert artifact.result.snapshot_verified is True
    assert artifact.result.paper_passed is False
    assert artifact.result.paper_trading_days == 0
    assert artifact.result.candidate_config_hash == hypothesis.candidate_config_hash
    assert artifact.metric_deltas["cagr"] > 0
    assert artifact.paired_comparison["diff_annual"] > 0
    assert artifact.change.path == "strategy.breakout_window"
    assert artifact.experiment_id == derive_experiment_id(
        hypothesis, _provenance(None), n_paths=100, block=10
    )
    json.loads(artifact.to_json())


def test_hash_mismatch_fails_before_backtest(tmp_path):
    baseline = _baseline(tmp_path)
    hypothesis = _hypothesis(baseline)
    drifted = copy.deepcopy(baseline)
    drifted["risk"]["max_weight"] = 0.15
    called = False

    def runner(_data, _cfg):
        nonlocal called
        called = True
        raise AssertionError("不应执行")

    with pytest.raises(ExperimentValidationError, match="基线配置哈希"):
        run_registered_experiment(
            hypothesis,
            drifted,
            {},
            runner=runner,
            snapshot_verifier=_verified,
            provenance_provider=_provenance,
        )
    assert called is False


def test_snapshot_must_be_full_verified_before_backtest(tmp_path):
    baseline = _baseline(tmp_path)
    hypothesis = _hypothesis(baseline)
    called = False

    def runner(_data, _cfg):
        nonlocal called
        called = True
        raise AssertionError("不应执行")

    def sampled(_cfg, snapshot_id):
        return SnapshotVerification(
            snapshot_id=snapshot_id,
            verified=True,
            verification_mode="sampled_sha256",
            manifest_sha256="b" * 64,
            file_count=3,
        )

    with pytest.raises(ExperimentValidationError, match="full_sha256"):
        run_registered_experiment(
            hypothesis,
            baseline,
            {},
            runner=runner,
            snapshot_verifier=sampled,
            provenance_provider=_provenance,
        )
    assert called is False


def test_snapshot_evidence_must_bind_same_snapshot_id(tmp_path):
    baseline = _baseline(tmp_path)
    hypothesis = _hypothesis(baseline)

    def wrong_snapshot(_cfg, _snapshot_id):
        return SnapshotVerification(
            snapshot_id="another-snapshot",
            verified=True,
            verification_mode="full_sha256",
            manifest_sha256="b" * 64,
            file_count=3,
        )

    with pytest.raises(ExperimentValidationError, match="ID 不一致"):
        run_registered_experiment(
            hypothesis,
            baseline,
            {},
            runner=lambda _data, _cfg: pytest.fail("不应执行回测"),
            snapshot_verifier=wrong_snapshot,
            provenance_provider=_provenance,
        )


def test_rejects_multi_candidate_post_hoc_selection(tmp_path):
    baseline = _baseline(tmp_path)
    original = _hypothesis(baseline)
    hypothesis = HypothesisSpec.from_dict({
        **original.to_dict(),
        "parameter_candidates": [50, 60, 70],
    })

    with pytest.raises(ExperimentValidationError, match="parameter_candidates"):
        run_registered_experiment(
            hypothesis,
            baseline,
            {},
            runner=lambda _data, _cfg: pytest.fail("不应执行回测"),
            snapshot_verifier=_verified,
            provenance_provider=_provenance,
        )


def test_artifact_write_is_machine_readable_and_never_overwrites(tmp_path):
    baseline = _baseline(tmp_path)
    hypothesis = _hypothesis(baseline)
    index = pd.bdate_range("2025-01-01", periods=90)

    def runner(_data, cfg):
        daily = 0.0004 if cfg["strategy"]["breakout_window"] == 55 else 0.0007
        return FakeResult(pd.Series(
            100_000 * np.cumprod(np.full(len(index), 1 + daily)), index=index
        ))

    artifact = run_registered_experiment(
        hypothesis,
        baseline,
        {},
        runner=runner,
        snapshot_verifier=_verified,
        provenance_provider=_provenance,
        completed_at="2026-08-09T01:00:00+00:00",
        n_paths=100,
        block=10,
    )
    path = tmp_path / "artifacts" / f"{artifact.experiment_id}.json"

    assert write_experiment_artifact(artifact, path) == path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["experiment_id"] == artifact.experiment_id
    assert payload["snapshot_verification"]["verification_mode"] == "full_sha256"
    with pytest.raises(FileExistsError):
        write_experiment_artifact(artifact, path)


def test_optional_release_gate_remains_fail_closed_without_forward_evidence(tmp_path):
    baseline = _baseline(tmp_path)
    hypothesis = _hypothesis(baseline)
    provenance = _provenance(None)
    experiment_id = derive_experiment_id(
        hypothesis, provenance, n_paths=100, block=10
    )
    manifest = ReleaseManifest(
        release_id="release-001",
        hypothesis_id=hypothesis.hypothesis_id,
        experiment_id=experiment_id,
        parent_config_hash=hypothesis.baseline_config_hash,
        candidate_config_hash=hypothesis.candidate_config_hash,
        code_version=provenance.code_version,
        source_hash=provenance.source_hash,
        data_snapshot=hypothesis.data_snapshot,
        rollback_config_hash=hypothesis.baseline_config_hash,
        created_at="2026-08-09T02:00:00+00:00",
        effective_date="2026-12-01",
        human_approved=False,
    )
    index = pd.bdate_range("2025-01-01", periods=90)

    def runner(_data, cfg):
        daily = 0.0004 if cfg["strategy"]["breakout_window"] == 55 else 0.0007
        return FakeResult(pd.Series(
            100_000 * np.cumprod(np.full(len(index), 1 + daily)), index=index
        ))

    artifact = run_registered_experiment(
        hypothesis,
        baseline,
        {},
        runner=runner,
        snapshot_verifier=_verified,
        provenance_provider=_provenance,
        release_manifest=manifest,
        n_paths=100,
        block=10,
    )

    assert artifact.release_gate is not None
    assert artifact.release_gate.approved is False
    failed = {check.code for check in artifact.release_gate.failures}
    assert {"walk_forward", "stress_test", "test_suite", "shadow", "paper",
            "human_approval"}.issubset(failed)
