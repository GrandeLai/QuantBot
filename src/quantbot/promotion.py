"""Paper reconciliation gate before any small-capital live promotion."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .acceptance_policy import ACCEPTANCE_POLICY_VERSION
from .audit import RequirementEvidence
from .trading import RunManifest


def _digest(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class PaperReconciliationRecord:
    strategy_family: str
    session_date: date
    run_type: str
    dataset_hash: str
    config_hash: str
    code_hash: str
    orders_reconciled: bool
    ledger_reconciled: bool
    broker_reconciled: bool
    intent_contract_verified: bool

    def __post_init__(self):
        if self.strategy_family not in {"normal", "daytrade"}:
            raise ValueError("strategy_family must be normal or daytrade")
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise TypeError("session_date must be a date")
        if not isinstance(self.run_type, str) or not self.run_type:
            raise ValueError("run_type must be a non-empty string")
        for name in ("dataset_hash", "config_hash", "code_hash"):
            _digest(getattr(self, name), name)
        for name in (
            "orders_reconciled",
            "ledger_reconciled",
            "broker_reconciled",
            "intent_contract_verified",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be a boolean")

    @property
    def fully_reconciled(self) -> bool:
        return (
            self.orders_reconciled
            and self.ledger_reconciled
            and self.broker_reconciled
            and self.intent_contract_verified
        )


@dataclass(frozen=True, slots=True)
class PaperPromotionDecision:
    eligible_for_small_live: bool
    unique_sessions: int
    required_sessions: int
    reasons: tuple[str, ...]
    dataset_hash: str | None = None
    config_hash: str | None = None
    code_hash: str | None = None


def paper_promotion_gate_from_runtime(config) -> "PaperPromotionGate":
    """Build the promotion gate from ``validation.min_paper_sessions``.

    The key was declared, validated and hashed while the gate was always
    constructed with its own default, so configuring it changed nothing.  The
    gate still clamps to the 60-session floor, so this can only tighten.
    """

    return PaperPromotionGate(config.paper_promotion_min_sessions())


class PaperPromotionGate:
    def __init__(self, min_unique_sessions: int = 60):
        if isinstance(min_unique_sessions, bool) or not isinstance(min_unique_sessions, int) or min_unique_sessions < 1:
            raise ValueError("min_unique_sessions must be a positive integer")
        self.min_unique_sessions = max(60, min_unique_sessions)

    def evaluate(
        self,
        records,
        *,
        strategy_family: str,
        strategy_admitted: bool,
    ) -> PaperPromotionDecision:
        if strategy_family not in {"normal", "daytrade"}:
            raise ValueError("strategy_family must be normal or daytrade")
        if not isinstance(strategy_admitted, bool):
            raise ValueError("strategy_admitted must be a boolean")
        values = tuple(records)
        if any(not isinstance(item, PaperReconciliationRecord) for item in values):
            raise TypeError("records must contain PaperReconciliationRecord values")
        reasons = []
        unique_sessions = len({item.session_date for item in values})
        if unique_sessions < self.min_unique_sessions:
            reasons.append("duration")
        if not strategy_admitted:
            reasons.append("strategy_admission")
        if any(item.strategy_family != strategy_family for item in values):
            reasons.append("strategy_family")
        if any(item.run_type != "paper_trading" for item in values):
            reasons.append("paper_run_type")
        if any(not item.fully_reconciled for item in values):
            reasons.append("reconciliation")
        artifact_ids = {
            (item.dataset_hash, item.config_hash, item.code_hash)
            for item in values
        }
        if len(artifact_ids) != 1:
            reasons.append("artifact_drift")
        artifact = next(iter(artifact_ids)) if len(artifact_ids) == 1 else (None, None, None)
        return PaperPromotionDecision(
            eligible_for_small_live=not reasons,
            unique_sessions=unique_sessions,
            required_sessions=self.min_unique_sessions,
            reasons=tuple(reasons),
            dataset_hash=artifact[0],
            config_hash=artifact[1],
            code_hash=artifact[2],
        )


def build_paper_promotion_evidence(
    decision: PaperPromotionDecision,
    manifest: RunManifest,
) -> RequirementEvidence:
    if not isinstance(decision, PaperPromotionDecision):
        raise TypeError("decision must be a PaperPromotionDecision")
    if not isinstance(manifest, RunManifest):
        raise TypeError("manifest must be a RunManifest")
    if manifest.run_type != "paper_trading":
        raise ValueError("paper promotion evidence requires a paper_trading manifest")
    if (
        decision.dataset_hash,
        decision.config_hash,
        decision.code_hash,
    ) != (
        manifest.dataset_hash,
        manifest.config_hash,
        manifest.code_hash,
    ):
        raise ValueError("paper manifest must match reconciled artifact identity")
    detail = (
        f"unique_sessions={decision.unique_sessions};reconciled=true"
        if decision.eligible_for_small_live
        else "failed:" + ",".join(decision.reasons)
    )
    return RequirementEvidence(
        requirement_id="paper_reconciliation_60_sessions",
        passed=decision.eligible_for_small_live,
        detail=detail,
        run_type=manifest.run_type,
        observed_at=manifest.observed_at.isoformat(),
        dataset_hash=manifest.dataset_hash,
        config_hash=manifest.config_hash,
        code_hash=manifest.code_hash,
        policy_version=ACCEPTANCE_POLICY_VERSION,
    )


__all__ = (
    "PaperPromotionDecision",
    "PaperPromotionGate",
    "PaperReconciliationRecord",
    "build_paper_promotion_evidence",
)
