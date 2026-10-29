"""候选配置发布门禁；只读重验候选证据，不写生产配置。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from ..config import config_hash
from ..review.journal import ReviewJournal
from ..review.paper import (
    PaperCampaignSpec,
    PaperEvidenceSummary,
    audit_paper_campaign,
)
from .bundle import CandidateBundleVerification, verify_candidate_bundle
from .contracts import ExperimentResult, HypothesisSpec, ReleaseManifest
from .policy import DecisionSource, ProductionPolicyError, validate_production_config


@dataclass(frozen=True)
class ReleaseCriteria:
    """预先冻结的默认晋级门槛。"""

    minimum_cagr_improvement: float = 0.005
    maximum_p_value: float = 0.10
    minimum_paper_trading_days: int = 60

    def __post_init__(self) -> None:
        if self.minimum_cagr_improvement <= 0:
            raise ValueError("minimum_cagr_improvement 必须大于 0")
        if not 0 < self.maximum_p_value < 1:
            raise ValueError("maximum_p_value 必须位于 (0, 1)")
        if isinstance(self.minimum_paper_trading_days, bool) \
                or not isinstance(self.minimum_paper_trading_days, int) \
                or self.minimum_paper_trading_days < 0:
            raise ValueError("minimum_paper_trading_days 必须是非负整数")


@dataclass(frozen=True)
class GateCheck:
    code: str
    passed: bool
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ReleaseGateDecision:
    """可持久化的逐项门禁结论。"""

    approved: bool
    checks: tuple[GateCheck, ...]

    @property
    def failures(self) -> tuple[GateCheck, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": [check.to_dict() for check in self.checks],
        }


def _audit_paper_journal(
    journal_path: str | Path,
    campaign: PaperCampaignSpec,
) -> PaperEvidenceSummary:
    """从只读 SQLite 原始事实重新生成 Paper 摘要。"""
    with ReviewJournal(journal_path, readonly=True) as journal:
        records = {
            trading_day: journal.records_for_date(trading_day)
            for trading_day in campaign.expected_trading_dates
        }
    return audit_paper_campaign(campaign, records)


def evaluate_release(
    hypothesis: HypothesisSpec,
    experiment: ExperimentResult,
    manifest: ReleaseManifest,
    candidate_config: Mapping[str, Any],
    *,
    criteria: ReleaseCriteria | None = None,
    candidate_bundle: CandidateBundleVerification | None = None,
    paper_campaign: PaperCampaignSpec | None = None,
    paper_journal_path: str | Path | None = None,
    project_root: str | Path | None = None,
) -> ReleaseGateDecision:
    """评估候选版本能否发布；候选目录会在门禁内部重新只读验证。"""
    criteria = criteria or ReleaseCriteria()
    checks: list[GateCheck] = []

    def add(code: str, passed: bool, detail: str) -> None:
        checks.append(GateCheck(code=code, passed=bool(passed), detail=detail))

    try:
        actual_hash = config_hash(candidate_config)
        hash_detail = "假设、实验、发布清单及候选配置哈希必须闭环"
    except (TypeError, ValueError) as exc:
        actual_hash = None
        hash_detail = f"候选配置无法生成规范哈希: {exc}"
    links_match = (
        hypothesis.hypothesis_id == experiment.hypothesis_id == manifest.hypothesis_id
        and experiment.experiment_id == manifest.experiment_id
        and hypothesis.baseline_config_hash == experiment.baseline_config_hash
        == manifest.parent_config_hash == manifest.rollback_config_hash
        and hypothesis.candidate_config_hash == experiment.candidate_config_hash
        == manifest.candidate_config_hash == actual_hash
    )
    add("contract_links", links_match, hash_detail)

    verified_bundle = None
    bundle_error = "未提供候选包"
    if candidate_bundle is not None:
        try:
            verified_bundle = verify_candidate_bundle(
                candidate_bundle.path,
                project_root=project_root,
            )
            bundle_error = "候选目录已在发布判断时重新完成只读验证"
        except Exception as exc:  # noqa: BLE001 - 验证失败必须收敛为拒绝发布
            bundle_error = f"候选目录重新验证失败：{type(exc).__name__}: {exc}"
    candidate_bundle = verified_bundle
    bundle_ok = (
        candidate_bundle is not None
        and candidate_bundle.eligible
        and candidate_bundle.bundle_sha256 == manifest.candidate_bundle_sha256
        and candidate_bundle.candidate_config_hash == manifest.candidate_config_hash
        and candidate_bundle.parent_config_hash == manifest.parent_config_hash
        and candidate_bundle.experiment_id == manifest.experiment_id
    )
    add(
        "candidate_bundle",
        bundle_ok,
        bundle_error,
    )
    experiment_matches_bundle = (
        candidate_bundle is not None
        and candidate_bundle.experiment == experiment
    )
    add(
        "experiment_bundle",
        experiment_matches_bundle,
        "发布统计与前瞻状态必须来自刚刚重验的候选包 experiment.json",
    )
    hypothesis_matches_bundle = (
        candidate_bundle is not None
        and candidate_bundle.hypothesis == hypothesis
    )
    add(
        "hypothesis_bundle",
        hypothesis_matches_bundle,
        "发布阈值与单变量声明必须来自刚刚重验的候选包 experiment.json",
    )
    # 后续门禁一律消费候选包中刚解析出的契约；调用方传入的同 ID 伪造结果
    # 即使数值更好，也只能让 experiment_bundle 失败，不能影响实际判断。
    verified_experiment = (
        candidate_bundle.experiment
        if candidate_bundle is not None
        else experiment
    )
    verified_hypothesis = (
        candidate_bundle.hypothesis
        if candidate_bundle is not None
        else hypothesis
    )

    try:
        validate_production_config(candidate_config)
        production_policy_ok = True
        policy_detail = "候选配置满足价格行为生产政策"
    except (ProductionPolicyError, ValueError) as exc:
        production_policy_ok = False
        policy_detail = str(exc)
    doctrine_ok = (
        production_policy_ok
        and verified_hypothesis.signal_source is DecisionSource.PRICE_ACTION
        and verified_experiment.price_action_policy_passed
    )
    add("price_action_doctrine", doctrine_ok, policy_detail)

    data_section = candidate_config.get("data", {})
    configured_snapshot = data_section.get("snapshot") \
        if isinstance(data_section, Mapping) else None
    snapshot_ok = (
        verified_experiment.snapshot_verified
        and verified_hypothesis.data_snapshot
        == verified_experiment.data_snapshot
        == manifest.data_snapshot
        and configured_snapshot == hypothesis.data_snapshot
        and candidate_bundle is not None
        and candidate_bundle.data_snapshot == manifest.data_snapshot
        and candidate_bundle.snapshot_manifest_sha256
        == manifest.snapshot_manifest_sha256
    )
    add(
        "snapshot",
        snapshot_ok,
        "契约、候选包与发布清单必须绑定同一份全量校验快照及 manifest",
    )

    min_effect = max(
        criteria.minimum_cagr_improvement,
        verified_hypothesis.minimum_effect,
    )
    max_p = min(criteria.maximum_p_value, verified_hypothesis.maximum_p_value)
    paired_ok = (
        verified_hypothesis.primary_metric == "cagr"
        and verified_experiment.paired_cagr_diff >= min_effect
        and verified_experiment.paired_p_value < max_p
        and verified_experiment.paired_ci_low > 0
        and verified_experiment.paired_ci_high > 0
    )
    add(
        "paired_directional",
        paired_ok,
        f"候选-基线 CAGR 差需 >= {min_effect:.4f}、p < {max_p:.3f} 且区间全为正",
    )
    add(
        "walk_forward",
        verified_experiment.walk_forward_passed
        and candidate_bundle is not None
        and candidate_bundle.forward_evidence_verified,
        "Walk-forward 必须绑定候选包内已重验的原始产物",
    )
    add(
        "stress_test",
        verified_experiment.stress_test_passed
        and candidate_bundle is not None
        and candidate_bundle.forward_evidence_verified,
        "预注册压力测试必须绑定候选包内已重验的原始产物",
    )
    add(
        "test_suite",
        verified_experiment.tests_passed
        and candidate_bundle is not None
        and candidate_bundle.forward_evidence_verified,
        "完整测试必须绑定候选包内已重验的原始产物",
    )
    add(
        "shadow",
        verified_experiment.shadow_passed
        and candidate_bundle is not None
        and candidate_bundle.forward_evidence_verified,
        "Shadow 必须绑定候选包内已重验的原始产物",
    )
    review_section = candidate_config.get("review", {})
    configured_paper_days = criteria.minimum_paper_trading_days
    if isinstance(review_section, Mapping):
        raw_paper_days = review_section.get(
            "paper_min_trading_days", criteria.minimum_paper_trading_days
        )
        if isinstance(raw_paper_days, int) and not isinstance(raw_paper_days, bool):
            configured_paper_days = raw_paper_days
    required_paper_days = max(criteria.minimum_paper_trading_days, configured_paper_days)
    try:
        experiment_day = datetime.fromisoformat(
            verified_experiment.completed_at.replace("Z", "+00:00")
        ).date()
        manifest_day = datetime.fromisoformat(
            manifest.created_at.replace("Z", "+00:00")
        ).date()
        effective_day = date.fromisoformat(manifest.effective_date)
        paper_window_ok = (
            paper_campaign is not None
            and bool(paper_campaign.expected_trading_dates)
            and date.fromisoformat(paper_campaign.expected_trading_dates[0])
            > experiment_day
            and date.fromisoformat(paper_campaign.expected_trading_dates[-1])
            <= manifest_day
            <= effective_day
        )
    except ValueError:
        paper_window_ok = False
    paper_evidence = None
    paper_detail = "未提供冻结 campaign 与只读原始账本"
    if paper_campaign is not None and paper_journal_path is not None:
        try:
            paper_evidence = _audit_paper_journal(
                paper_journal_path,
                paper_campaign,
            )
            paper_detail = "已从只读 SQLite 原始事实重新审计 Paper campaign"
        except Exception as exc:  # noqa: BLE001 - 原始事实不可读必须拒绝发布
            paper_detail = f"Paper 原始账本重审失败：{type(exc).__name__}: {exc}"
    paper_ok = (
        paper_campaign is not None
        and paper_evidence is not None
        and paper_evidence.passed
        and paper_evidence.campaign_id == paper_campaign.campaign_id
        and paper_evidence.campaign_sha256 == paper_campaign.sha256
        and paper_evidence.evidence_sha256 == manifest.paper_evidence_sha256
        and paper_campaign.sha256 == manifest.paper_campaign_sha256
        and paper_campaign.candidate_config_hash == manifest.candidate_config_hash
        and paper_campaign.source_hash == manifest.source_hash
        and paper_evidence.consecutive_eligible_trading_days >= required_paper_days
        and paper_window_ok
    )
    add(
        "paper",
        paper_ok,
        f"{paper_detail}；必须在实验后、发布前闭环，且末尾连续合格至少 "
        f"{required_paper_days} 个交易日",
    )
    source_hash_ok = len(manifest.source_hash) == 64 \
        and all(char in "0123456789abcdef" for char in manifest.source_hash.lower())
    # SourceFingerprint 对无 HEAD / 脏工作区使用显式的 uncommitted+ / +dirty.
    # 这类身份可用于定位研究结果，但不能成为可回滚的生产发布身份。
    clean_version = (
        len(manifest.code_version) in {40, 64}
        and all(char in "0123456789abcdef" for char in manifest.code_version.lower())
    )
    provenance_links = (
        candidate_bundle is not None
        and candidate_bundle.eligible
        and candidate_bundle.source_hash == manifest.source_hash
        and candidate_bundle.git_head == manifest.git_head
        and manifest.code_version == manifest.git_head
    )
    add(
        "provenance",
        source_hash_ok and clean_version and provenance_links,
        "发布必须绑定干净且已提交的 Git HEAD 与完整 SHA-256 源码指纹",
    )
    approval_ok = manifest.human_approved and bool(manifest.approved_by.strip())
    add("human_approval", approval_ok, "生产发布必须有明确人工批准人")

    return ReleaseGateDecision(
        approved=all(check.passed for check in checks),
        checks=tuple(checks),
    )
