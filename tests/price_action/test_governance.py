"""价格行为政策、实验契约与发布门禁测试。"""

from __future__ import annotations

import copy
import subprocess
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

import quantbot.price_action.governance.release as release_mod
from quantbot.price_action.config import config_hash
from quantbot.price_action.governance import (
    CandidateBundleVerification,
    DecisionSource,
    ExperimentResult,
    HypothesisSpec,
    PolicyViolation,
    ProductionPolicyError,
    ReleaseManifest,
    evaluate_release,
    source_fingerprint,
    validate_exposure_change,
    validate_production_config,
)
from quantbot.price_action.review import (
    PaperCampaignSpec,
    PaperDayAssessment,
    summarize_paper_evidence,
)

from .conftest import make_cfg


def test_only_price_action_can_create_or_increase_exposure():
    change = validate_exposure_change(
        symbol="aapl",
        before_shares=0,
        after_shares=20,
        source=DecisionSource.PRICE_ACTION,
        reason="55 日新高突破",
        evidence_id="evidence-1",
    )
    assert change.symbol == "AAPL"
    assert change.increases_exposure

    with pytest.raises(PolicyViolation, match="只能来自 PRICE_ACTION"):
        validate_exposure_change(
            symbol="AAPL",
            before_shares=10,
            after_shares=20,
            source=DecisionSource.RISK_OVERRIDE,
            reason="风险层试图增仓",
        )


def test_risk_override_can_only_reduce_without_reversing():
    change = validate_exposure_change(
        symbol="AAPL",
        before_shares=20,
        after_shares=5,
        source=DecisionSource.RISK_OVERRIDE,
        reason="组合风险收缩",
    )
    assert change.reduces_exposure

    with pytest.raises(PolicyViolation, match="只能在不反向的前提下严格减仓"):
        validate_exposure_change(
            symbol="AAPL",
            before_shares=20,
            after_shares=20,
            source=DecisionSource.RISK_OVERRIDE,
            reason="无变化",
        )


def test_price_action_change_requires_evidence():
    with pytest.raises(PolicyViolation, match="evidence_id"):
        validate_exposure_change(
            symbol="AAPL",
            before_shares=20,
            after_shares=0,
            source=DecisionSource.PRICE_ACTION,
            reason="通道离场",
        )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("strategy", "earnings_avoid_days", 1),
        ("strategy", "time_stop_days", 10),
        ("shorts", "enabled", True),
        ("base_position", "enabled", True),
        ("leverage", "enabled", True),
    ],
)
def test_production_policy_rejects_non_price_or_disproven_modules(
    section, field, value,
):
    cfg = make_cfg()
    cfg.setdefault(section, {})[field] = value

    with pytest.raises(ProductionPolicyError):
        validate_production_config(cfg)


def test_source_fingerprint_is_explicit_without_git_head(tmp_path):
    source = tmp_path / "strategy.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    first = source_fingerprint(tmp_path, files=[source])
    assert first.git_head is None
    assert first.dirty is True
    assert first.code_version == f"uncommitted+{first.source_hash}"
    assert first.file_count == 1

    source.write_text("VALUE = 2\n", encoding="utf-8")
    second = source_fingerprint(tmp_path, files=[source])
    assert second.source_hash != first.source_hash


def test_ignored_runtime_source_cannot_be_reported_as_clean_git(tmp_path):
    package = tmp_path / "quantbot.price_action"
    package.mkdir()
    (package / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("quantbot.price_action/ignored.py\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "quantbot.price_action/tracked.py"],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "-c", "user.name=PABot Test",
            "-c", "user.email=quantbot.price_action@example.invalid", "commit", "-qm", "baseline",
        ],
        check=True,
    )
    (package / "ignored.py").write_text("VALUE = 2\n", encoding="utf-8")

    fingerprint = source_fingerprint(tmp_path)

    assert fingerprint.git_head is not None
    assert fingerprint.dirty is True
    assert "+dirty." in fingerprint.code_version


def test_tracked_runtime_symlink_is_rejected(tmp_path):
    package = tmp_path / "quantbot.price_action"
    package.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (package / "linked.py").symlink_to(outside)

    with pytest.raises(ValueError, match="源码集合不得包含软链接"):
        source_fingerprint(tmp_path)


def test_runtime_symlink_directory_is_rejected(tmp_path):
    package = tmp_path / "quantbot.price_action"
    package.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-package"
    outside.mkdir()
    (outside / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "external").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="源码集合不得包含软链接"):
        source_fingerprint(tmp_path)


def _release_fixture():
    baseline = make_cfg()
    candidate = copy.deepcopy(baseline)
    candidate["strategy"]["atr_trail_mult"] = 4.0
    candidate["data"]["snapshot"] = "snapshot-20260809"
    baseline_hash = config_hash(baseline)
    candidate_hash = config_hash(candidate)

    hypothesis = HypothesisSpec(
        hypothesis_id="hyp-1",
        title="更紧的价格吊灯止损",
        price_action_rationale="价格跌破更近的波动结构时更早退出",
        baseline_config_hash=baseline_hash,
        candidate_config_hash=candidate_hash,
        data_snapshot="snapshot-20260809",
        single_change={"strategy.atr_trail_mult": {"from": 4.5, "to": 4.0}},
        created_at="2026-08-09T00:00:00Z",
        evaluation_windows=("2015-2025", "2000-2014"),
        failure_conditions=("最大回撤恶化超过 2pp",),
    )
    experiment = ExperimentResult(
        experiment_id="exp-1",
        hypothesis_id="hyp-1",
        baseline_config_hash=baseline_hash,
        candidate_config_hash=candidate_hash,
        data_snapshot="snapshot-20260809",
        completed_at="2026-08-31T00:00:00Z",
        price_action_policy_passed=True,
        snapshot_verified=True,
        paired_cagr_diff=0.007,
        paired_ci_low=0.001,
        paired_ci_high=0.013,
        paired_p_value=0.06,
        walk_forward_passed=True,
        stress_test_passed=True,
        tests_passed=True,
        shadow_passed=True,
        paper_passed=True,
        paper_trading_days=60,
        artifacts={"report": "artifacts/exp-1/report.json"},
    )
    git_head = "b" * 40
    bundle_sha256 = "c" * 64
    snapshot_manifest_sha256 = "d" * 64
    trading_dates = tuple(
        (date(2026, 9, 1) + timedelta(days=offset)).isoformat()
        for offset in range(60)
    )
    campaign = PaperCampaignSpec(
        campaign_id="paper-hyp-1",
        candidate_config_hash=candidate_hash,
        source_hash="a" * 64,
        account_fingerprint="e" * 64,
        expected_trading_dates=trading_dates,
        expected_signal_dates=("2026-08-31", *trading_dates[:-1]),
    )
    paper_evidence = summarize_paper_evidence(
        campaign,
        tuple(
            PaperDayAssessment(
                execution_date=trading_day,
                eligible=True,
                reason_codes=(),
                facts_sha256="f" * 64,
                attempt_id=f"attempt-{trading_day}",
                reconciliation_categories=("account", "positions", "working_orders"),
            )
            for trading_day in trading_dates
        ),
    )
    bundle = CandidateBundleVerification(
        path=Path("/verified/candidate"),
        candidate_config_hash=candidate_hash,
        parent_config_hash=baseline_hash,
        experiment_id="exp-1",
        bundle_sha256=bundle_sha256,
        source_hash="a" * 64,
        git_head=git_head,
        data_snapshot="snapshot-20260809",
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        eligible=True,
        ineligibility_reasons=(),
        file_sha256={},
        experiment=experiment,
        hypothesis=hypothesis,
        forward_evidence_verified=True,
    )
    manifest = ReleaseManifest(
        release_id="release-1",
        hypothesis_id="hyp-1",
        experiment_id="exp-1",
        parent_config_hash=baseline_hash,
        candidate_config_hash=candidate_hash,
        code_version=git_head,
        source_hash="a" * 64,
        data_snapshot="snapshot-20260809",
        rollback_config_hash=baseline_hash,
        created_at="2026-11-09T00:00:00Z",
        effective_date="2026-11-10",
        human_approved=True,
        approved_by="owner",
        git_head=git_head,
        candidate_bundle_sha256=bundle_sha256,
        snapshot_manifest_sha256=snapshot_manifest_sha256,
        paper_campaign_sha256=campaign.sha256,
        paper_evidence_sha256=paper_evidence.evidence_sha256,
    )
    return (
        candidate,
        hypothesis,
        experiment,
        manifest,
        bundle,
        campaign,
        paper_evidence,
    )


def _trust_fixture_bundle(bundle):
    """单元测试只隔离发布规则；真实入口使用默认磁盘全量复核。"""
    def verify(path, *, project_root=None):
        assert path == bundle.path
        return bundle

    return verify


def _trust_fixture_paper(paper, campaign):
    """规则单测隔离 SQLite 建档；集成路径必须读取真实只读账本。"""
    def audit(path, supplied_campaign):
        assert Path(path) == Path("/verified/paper.sqlite")
        assert supplied_campaign == campaign
        return paper

    return audit


def test_contracts_round_trip_through_json():
    _, hypothesis, experiment, manifest, *_ = _release_fixture()

    assert HypothesisSpec.from_json(hypothesis.to_json()) == hypothesis
    assert ExperimentResult.from_json(experiment.to_json()) == experiment
    assert ReleaseManifest.from_json(manifest.to_json()) == manifest


def test_release_gate_approves_only_complete_evidence_without_mutating_config(
    monkeypatch,
):
    candidate, hypothesis, experiment, manifest, bundle, campaign, paper = \
        _release_fixture()
    before = copy.deepcopy(candidate)
    monkeypatch.setattr(
        release_mod, "verify_candidate_bundle", _trust_fixture_bundle(bundle)
    )
    monkeypatch.setattr(
        release_mod, "_audit_paper_journal", _trust_fixture_paper(paper, campaign)
    )

    decision = evaluate_release(
        hypothesis,
        experiment,
        manifest,
        candidate,
        candidate_bundle=bundle,
        paper_campaign=campaign,
        paper_journal_path="/verified/paper.sqlite",
    )

    assert decision.approved
    assert not decision.failures
    assert candidate == before


def test_release_gate_is_directional_and_requires_forward_evidence(monkeypatch):
    candidate, hypothesis, experiment, manifest, bundle, campaign, paper = \
        _release_fixture()
    experiment = replace(
        experiment,
        paired_cagr_diff=-0.007,
        paired_ci_low=-0.013,
        paired_ci_high=-0.001,
        paper_trading_days=59,
    )
    bundle = replace(bundle, experiment=experiment)
    manifest = replace(manifest, human_approved=False, approved_by="")
    monkeypatch.setattr(
        release_mod, "verify_candidate_bundle", _trust_fixture_bundle(bundle)
    )
    monkeypatch.setattr(
        release_mod, "_audit_paper_journal", _trust_fixture_paper(paper, campaign)
    )

    decision = evaluate_release(
        hypothesis,
        experiment,
        manifest,
        candidate,
        candidate_bundle=bundle,
        paper_campaign=campaign,
        paper_journal_path="/verified/paper.sqlite",
    )

    assert not decision.approved
    assert {check.code for check in decision.failures} == {
        "paired_directional", "human_approval",
    }


@pytest.mark.parametrize(
    "code_version",
    ["uncommitted+" + "a" * 64, "b" * 40 + "+dirty." + "a" * 64],
)
def test_release_gate_rejects_uncommitted_or_dirty_source(code_version, monkeypatch):
    candidate, hypothesis, experiment, manifest, bundle, campaign, paper = \
        _release_fixture()
    monkeypatch.setattr(
        release_mod, "verify_candidate_bundle", _trust_fixture_bundle(bundle)
    )
    monkeypatch.setattr(
        release_mod, "_audit_paper_journal", _trust_fixture_paper(paper, campaign)
    )

    decision = evaluate_release(
        hypothesis,
        experiment,
        replace(manifest, code_version=code_version),
        candidate,
        candidate_bundle=bundle,
        paper_campaign=campaign,
        paper_journal_path="/verified/paper.sqlite",
    )

    assert not decision.approved
    assert {check.code for check in decision.failures} == {"provenance"}


def test_release_gate_does_not_trust_legacy_paper_booleans_or_loose_manifest():
    candidate, hypothesis, experiment, manifest, *_ = _release_fixture()

    decision = evaluate_release(hypothesis, experiment, manifest, candidate)

    assert not decision.approved
    assert {"candidate_bundle", "snapshot", "paper", "provenance"} \
        <= {check.code for check in decision.failures}


def test_release_gate_reverifies_bundle_path_instead_of_trusting_dataclass():
    candidate, hypothesis, experiment, manifest, bundle, campaign, paper = \
        _release_fixture()

    decision = evaluate_release(
        hypothesis,
        experiment,
        manifest,
        candidate,
        candidate_bundle=bundle,
        paper_campaign=campaign,
    )

    assert not decision.approved
    assert "candidate_bundle" in {check.code for check in decision.failures}


def test_release_gate_rejects_experiment_result_not_read_from_bundle(monkeypatch):
    candidate, hypothesis, experiment, manifest, bundle, campaign, paper = \
        _release_fixture()
    forged = replace(
        experiment,
        paired_cagr_diff=0.50,
        paired_ci_low=0.40,
        paired_ci_high=0.60,
        paired_p_value=0.0,
    )
    monkeypatch.setattr(
        release_mod, "verify_candidate_bundle", _trust_fixture_bundle(bundle)
    )
    monkeypatch.setattr(
        release_mod, "_audit_paper_journal", _trust_fixture_paper(paper, campaign)
    )

    decision = evaluate_release(
        hypothesis,
        forged,
        manifest,
        candidate,
        candidate_bundle=bundle,
        paper_campaign=campaign,
        paper_journal_path="/verified/paper.sqlite",
    )

    assert not decision.approved
    assert {check.code for check in decision.failures} == {"experiment_bundle"}


def test_release_gate_rejects_hypothesis_threshold_not_read_from_bundle(monkeypatch):
    candidate, hypothesis, experiment, manifest, bundle, campaign, paper = \
        _release_fixture()
    forged = replace(hypothesis, minimum_effect=0.0001, maximum_p_value=0.99)
    monkeypatch.setattr(
        release_mod, "verify_candidate_bundle", _trust_fixture_bundle(bundle)
    )
    monkeypatch.setattr(
        release_mod, "_audit_paper_journal", _trust_fixture_paper(paper, campaign)
    )

    decision = evaluate_release(
        forged,
        experiment,
        manifest,
        candidate,
        candidate_bundle=bundle,
        paper_campaign=campaign,
        paper_journal_path="/verified/paper.sqlite",
    )

    assert not decision.approved
    assert {check.code for check in decision.failures} == {"hypothesis_bundle"}
