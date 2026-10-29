"""Requirement-level completion audit."""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from .acceptance_policy import (
    ACCEPTANCE_POLICY_VERSION,
    FORMAL_ACCEPTANCE_REQUIREMENTS,
)
from .evidence_auth import (
    CANONICAL_VERIFIER_VERSION,
    FORMAL_EVIDENCE_GENESIS_HASH,
    STRATEGY_ADMISSION_REQUIREMENTS,
    authenticate_formal_payload,
    verify_canonical_verification_payload,
)


DESIGN_REQUIREMENTS = (
    "offline_tests",
    "live_api_smoke",
    "duckdb_parquet_runtime",
    "qlib_alpha158_runtime",
    "chronos_runtime",
    "rd_agent_runtime",
    "streamlit_dashboard_runtime",
    "apscheduler_runtime",
    "oos_backtest_acceptance",
    "normal_strategy_admission",
    "daytrade_strategy_admission",
    "paper_trading_4_weeks",
    "paper_reconciliation_60_sessions",
    "paper_vs_backtest_deviation",
    "live_trading_soak",
)

REQUIREMENT_RUN_TYPES = {
    "live_api_smoke": "live_api_smoke",
    "oos_backtest_acceptance": "historical_backtest",
    "normal_strategy_admission": "sealed_backtest",
    "daytrade_strategy_admission": "sealed_backtest",
    "paper_trading_4_weeks": "paper_trading",
    "paper_vs_backtest_deviation": "paper_trading",
    "paper_reconciliation_60_sessions": "paper_trading",
    "live_trading_soak": "live_trading",
    "daytrade_paper_soak": "paper_trading",
    "daytrade_live_soak": "live_trading",
}


@dataclass(frozen=True)
class RequirementEvidence:
    requirement_id: str
    passed: bool
    detail: str
    run_type: Optional[str] = None
    observed_at: Optional[str] = None
    dataset_hash: Optional[str] = None
    config_hash: Optional[str] = None
    cost_model_hash: Optional[str] = None
    code_hash: Optional[str] = None
    policy_version: Optional[str] = None
    input_materialization_hash: Optional[str] = None
    output_hash: Optional[str] = None
    report_hash: Optional[str] = None
    cost_suite_hash: Optional[str] = None
    run_id: Optional[str] = None
    experiment_id: Optional[str] = None
    permit_record_hash: Optional[str] = None
    execution_receipt_hash: Optional[str] = None
    execution_result_hash: Optional[str] = None
    execution_artifact_hash: Optional[str] = None
    canonical_verifier_version: Optional[int] = None
    canonical_verification_hash: Optional[str] = None
    formal_record_hash: Optional[str] = None
    _authenticated: bool = field(default=False, init=False, repr=False, compare=False)
    _canonical_verified: bool = field(
        default=False,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def authenticated(self) -> bool:
        """Whether the loader verified the formal HMAC/hash-chain record."""

        return self._authenticated

    @property
    def canonical_verified(self) -> bool:
        """Whether a verifier proof binds raw execution facts to this row."""

        return self._canonical_verified


@dataclass(frozen=True)
class CompletionAuditReport:
    complete: bool
    passed_ids: List[str]
    failed_ids: List[str]
    missing_ids: List[str]


class CompletionAudit:
    def __init__(self, requirement_ids: Iterable[str]):
        self.requirement_ids = list(requirement_ids)

    @classmethod
    def design_default(cls):
        return cls(DESIGN_REQUIREMENTS)

    def evaluate(self, evidence: Dict[str, RequirementEvidence]) -> CompletionAuditReport:
        passed = []
        failed = []
        missing = []
        for requirement_id in self.requirement_ids:
            item = evidence.get(requirement_id)
            if item is None:
                missing.append(requirement_id)
            elif item.passed is True and self._is_admissible(requirement_id, item):
                passed.append(requirement_id)
            else:
                failed.append(requirement_id)
        return CompletionAuditReport(
            complete=not failed and not missing,
            passed_ids=passed,
            failed_ids=failed,
            missing_ids=missing,
        )

    @staticmethod
    def _is_admissible(requirement_id: str, item: RequirementEvidence) -> bool:
        expected_run_type = REQUIREMENT_RUN_TYPES.get(requirement_id)
        if expected_run_type is None:
            return not _is_paper_or_live_requirement(requirement_id)
        if item.run_type != expected_run_type:
            return False
        if (
            requirement_id in FORMAL_ACCEPTANCE_REQUIREMENTS
            and item.policy_version != ACCEPTANCE_POLICY_VERSION
        ):
            return False
        if not _valid_observed_at(item.observed_at):
            return False
        identity_valid = all(
            _valid_sha256(value)
            for value in (item.dataset_hash, item.config_hash, item.code_hash)
        )
        if not identity_valid:
            return False
        if (
            item.input_materialization_hash is not None
            and not _valid_sha256(item.input_materialization_hash)
        ):
            return False
        if any(
            value is not None and not _valid_sha256(value)
            for value in (
                item.output_hash,
                item.report_hash,
                item.cost_suite_hash,
            )
        ):
            return False
        if requirement_id in STRATEGY_ADMISSION_REQUIREMENTS:
            expected_experiment_id = (
                "normal-h1-quality-diversification"
                if requirement_id == "normal_strategy_admission"
                else "daytrade-h9-rvol-reward-risk"
            )
            return (
                item.authenticated
                and item.canonical_verified
                and _valid_sha256(item.cost_model_hash)
                and _valid_sha256(item.input_materialization_hash)
                and _valid_sha256(item.output_hash)
                and _valid_sha256(item.report_hash)
                and _valid_sha256(item.cost_suite_hash)
                and _valid_sha256(item.permit_record_hash)
                and _valid_sha256(item.execution_receipt_hash)
                and _valid_sha256(item.execution_result_hash)
                and _valid_sha256(item.execution_artifact_hash)
                and item.canonical_verifier_version
                == CANONICAL_VERIFIER_VERSION
                and _valid_sha256(item.canonical_verification_hash)
                and _valid_sha256(item.formal_record_hash)
                and isinstance(item.run_id, str)
                and bool(item.run_id.strip())
                and item.experiment_id == expected_experiment_id
            )
        return True


def load_evidence_file(path: Path) -> Dict[str, RequirementEvidence]:
    """Load the latest evidence for each requirement."""

    evidence: Dict[str, RequirementEvidence] = {}
    for item in load_evidence_records(path):
        evidence[item.requirement_id] = item
    return evidence


def load_evidence_records(path: Path) -> tuple[RequirementEvidence, ...]:
    """Load every evidence row without collapsing distinct runs or costs."""

    evidence: list[RequirementEvidence] = []
    formal_previous_hash = FORMAL_EVIDENCE_GENESIS_HASH
    with Path(path).open("r", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            item = json.loads(line)
            authenticated, formal_previous_hash = authenticate_formal_payload(
                item,
                previous_hash=formal_previous_hash,
            )
            canonical_verified = bool(
                authenticated and verify_canonical_verification_payload(item)
            )
            requirement_id = item["requirement_id"]
            record = RequirementEvidence(
                requirement_id=requirement_id,
                passed=parse_evidence_passed(item.get("passed")),
                detail=item.get("detail", ""),
                run_type=item.get("run_type"),
                observed_at=item.get("observed_at"),
                dataset_hash=item.get("dataset_hash"),
                config_hash=item.get("config_hash"),
                cost_model_hash=item.get("cost_model_hash"),
                code_hash=item.get("code_hash"),
                policy_version=item.get("policy_version"),
                input_materialization_hash=item.get(
                    "input_materialization_hash"
                ),
                output_hash=item.get("output_hash"),
                report_hash=item.get("report_hash"),
                cost_suite_hash=item.get("cost_suite_hash"),
                run_id=item.get("run_id"),
                experiment_id=item.get("experiment_id"),
                permit_record_hash=item.get("permit_record_hash"),
                execution_receipt_hash=item.get("execution_receipt_hash"),
                execution_result_hash=item.get("execution_result_hash"),
                execution_artifact_hash=item.get("execution_artifact_hash"),
                canonical_verifier_version=item.get(
                    "canonical_verifier_version"
                ),
                canonical_verification_hash=item.get(
                    "canonical_verification_hash"
                ),
                formal_record_hash=item.get("formal_record_hash"),
            )
            if authenticated:
                object.__setattr__(record, "_authenticated", True)
            if canonical_verified:
                object.__setattr__(record, "_canonical_verified", True)
            evidence.append(record)
    return tuple(evidence)


def _is_paper_or_live_requirement(requirement_id: str) -> bool:
    normalized = requirement_id.lower()
    return "paper" in normalized or "live" in normalized


def parse_evidence_passed(value) -> bool:
    return value if type(value) is bool else False


def _valid_observed_at(value: Optional[str]) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_sha256(value: Optional[str]) -> bool:
    if not value or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)
