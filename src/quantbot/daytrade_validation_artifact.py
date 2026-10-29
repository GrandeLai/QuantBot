"""Signed adaptive DayTrade evidence consumed by the one-shot sealed run.

The adaptive period owns experiment selection and global multiplicity
statistics.  The later sealed period must not recreate H1-H8 on sealed data;
it consumes this immutable receipt and evaluates only the frozen H3/H9 pair.
"""

from __future__ import annotations

import hmac
import json
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from .acceptance_policy import ACCEPTANCE_POLICY_VERSION
from .evidence import hash_payload
from .experiment_reporting import ExperimentRunReport
from .validation_protocol import DAYTRADE_VALIDATION_PROTOCOL


DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_VERSION = 1
DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV = (
    "STOCKBOT_DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH"
)
DAYTRADE_EXPERIMENT_KEYS_ENV = "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON"

_FIELDS = frozenset(
    {
        "artifact_version",
        "issuer",
        "strategy_family",
        "policy_version",
        "experiment_id",
        "source_mode",
        "request_start",
        "request_end",
        "candidate_survived",
        "declared_trial_count",
        "completed_trial_count",
        "dsr_confidence",
        "pbo",
        "dataset_hash",
        "input_materialization_hash",
        "config_hash",
        "cost_model_hash",
        "code_hash",
        "cost_suite_hash",
        "output_hash",
        "report_hash",
        "experiment_run_report",
        "content_hash",
        "signature",
    }
)
_SEAL = object()


@dataclass(frozen=True, slots=True)
class DayTradeAdaptiveValidationArtifact:
    """Core-verified adaptive survival decision and frozen DSR/PBO values."""

    issuer: str
    content_hash: str
    dsr_confidence: float
    pbo: float
    declared_trial_count: int
    completed_trial_count: int
    report: ExperimentRunReport
    identity_hashes: Mapping[str, str]
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _SEAL:
            raise PermissionError(
                "DayTrade adaptive validation must be created by signature verification"
            )
        if not isinstance(self.report, ExperimentRunReport):
            raise TypeError("adaptive validation report must be ExperimentRunReport")
        object.__setattr__(
            self,
            "identity_hashes",
            MappingProxyType(dict(self.identity_hashes)),
        )

    def metadata(self) -> dict[str, object]:
        return {
            "artifact_version": DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_VERSION,
            "issuer": self.issuer,
            "content_hash": self.content_hash,
            "source_mode": "adaptive_development",
            "candidate_survived": True,
            "declared_trial_count": self.declared_trial_count,
            "completed_trial_count": self.completed_trial_count,
            "dsr_confidence": self.dsr_confidence,
            "pbo": self.pbo,
            **dict(self.identity_hashes),
        }


def trusted_daytrade_experiment_keys(
    environ: Mapping[str, str],
) -> dict[str, bytes]:
    raw = str(environ.get(DAYTRADE_EXPERIMENT_KEYS_ENV, "") or "").strip()
    if not raw:
        raise ValueError(
            f"{DAYTRADE_EXPERIMENT_KEYS_ENV} must configure trusted issuer keys"
        )
    try:
        payload = json.loads(raw)
        keys = {str(issuer): bytes.fromhex(value) for issuer, value in payload.items()}
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            f"{DAYTRADE_EXPERIMENT_KEYS_ENV} must be a JSON object of hex keys"
        ) from error
    if not keys or any(not issuer.strip() or len(key) < 16 for issuer, key in keys.items()):
        raise ValueError(
            f"{DAYTRADE_EXPERIMENT_KEYS_ENV} keys must contain at least 16 bytes"
        )
    return keys


def load_daytrade_adaptive_validation_artifact(
    path: Path | str,
    *,
    trusted_keys: Mapping[str, bytes],
) -> DayTradeAdaptiveValidationArtifact:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("DayTrade adaptive validation artifact cannot be read") from error
    return verify_daytrade_adaptive_validation_artifact(
        payload,
        trusted_keys=trusted_keys,
    )


def verify_daytrade_adaptive_validation_artifact(
    payload: object,
    *,
    trusted_keys: Mapping[str, bytes],
) -> DayTradeAdaptiveValidationArtifact:
    """Verify signature, canonical report, identities, and frozen thresholds."""

    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("DayTrade adaptive validation artifact fields are invalid")
    artifact = dict(payload)
    signature = _digest(artifact.pop("signature"), "adaptive artifact signature")
    content_hash = _digest(
        artifact.pop("content_hash"), "adaptive artifact content_hash"
    )
    if hash_payload(artifact) != content_hash:
        raise ValueError("DayTrade adaptive validation content_hash mismatch")
    if artifact.get("artifact_version") != DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_VERSION:
        raise ValueError("DayTrade adaptive validation artifact_version is unsupported")
    if artifact.get("strategy_family") != "daytrade":
        raise ValueError("DayTrade adaptive validation strategy_family is invalid")
    if artifact.get("policy_version") != ACCEPTANCE_POLICY_VERSION:
        raise ValueError("DayTrade adaptive validation policy_version is not current")
    if artifact.get("experiment_id") != "daytrade-h9-rvol-reward-risk":
        raise ValueError("DayTrade adaptive validation experiment_id is invalid")
    if artifact.get("source_mode") != "adaptive_development":
        raise ValueError("DayTrade adaptive validation source_mode is invalid")
    protocol = DAYTRADE_VALIDATION_PROTOCOL
    if (
        _day(artifact.get("request_start"), "request_start")
        != protocol.walk_forward_start
        or _day(artifact.get("request_end"), "request_end")
        != protocol.walk_forward_end
    ):
        raise ValueError("DayTrade adaptive validation window must be 2023-2024")
    if artifact.get("candidate_survived") is not True:
        raise ValueError("DayTrade adaptive candidate did not survive")
    if (
        artifact.get("declared_trial_count") != 21
        or artifact.get("completed_trial_count") != 21
    ):
        raise ValueError("DayTrade adaptive validation requires 21 completed trials")

    issuer = artifact.get("issuer")
    if not isinstance(issuer, str) or not issuer.strip():
        raise ValueError("DayTrade adaptive validation issuer is required")
    key = trusted_keys.get(issuer)
    if not isinstance(key, bytes) or len(key) < 16:
        raise ValueError("trusted DayTrade adaptive validation issuer key is missing")
    expected_signature = hmac.new(
        key,
        content_hash.encode("ascii"),
        sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("DayTrade adaptive validation signature is invalid")

    hash_names = (
        "dataset_hash",
        "input_materialization_hash",
        "config_hash",
        "cost_model_hash",
        "code_hash",
        "cost_suite_hash",
        "output_hash",
        "report_hash",
    )
    identity_hashes = {
        name: _digest(artifact.get(name), name) for name in hash_names
    }
    report = ExperimentRunReport.from_payload(artifact.get("experiment_run_report"))
    report_expected = {
        "dataset_hash": report.dataset_hash,
        "input_materialization_hash": report.input_materialization_hash,
        "config_hash": report.config_hash,
        "cost_model_hash": report.cost_model_hash,
        "code_hash": report.code_hash,
        "cost_suite_hash": report.cost_suite.suite_hash,
        "output_hash": report.output_hash,
        "report_hash": report.report_hash,
    }
    mismatches = [
        name for name, value in report_expected.items() if identity_hashes[name] != value
    ]
    if mismatches:
        raise ValueError(
            "DayTrade adaptive report identity mismatch: " + ",".join(mismatches)
        )
    if (
        report.experiment_id != "daytrade-h9-rvol-reward-risk"
        or report.run_type != "historical_optimization"
    ):
        raise ValueError("DayTrade adaptive report type is invalid")
    if (
        report.admission.get("candidate_survived") is not True
        or report.admission.get("passed") is not False
        or report.admission.get("formal_admission_eligible") is not False
    ):
        raise ValueError("DayTrade adaptive report admission is invalid")
    if (
        report.metrics.get("declared_trial_count") != 21
        or report.metrics.get("completed_trial_count") != 21
    ):
        raise ValueError("DayTrade adaptive report requires 21 completed trials")

    statistics = report.metrics.get("statistical_validation")
    fixed_evaluation = report.metrics.get("fixed_candidate_evaluation")
    if not isinstance(statistics, Mapping) or not isinstance(fixed_evaluation, Mapping):
        raise ValueError("DayTrade adaptive report statistics are missing")
    if (
        statistics.get("request_start") != protocol.walk_forward_start.isoformat()
        or statistics.get("request_end") != protocol.walk_forward_end.isoformat()
        or statistics.get("evaluation_scope") != "full_request_interval"
    ):
        raise ValueError("DayTrade adaptive report evaluation window is invalid")
    fixed_statistics = fixed_evaluation.get("statistical_validation")
    if not isinstance(fixed_statistics, Mapping) or fixed_evaluation.get("passed") is not True:
        raise ValueError("DayTrade adaptive fixed candidate did not survive")
    dsr_payload = fixed_statistics.get("deflated_sharpe_ratio")
    pbo_payload = fixed_statistics.get("probability_of_backtest_overfitting")
    if not isinstance(dsr_payload, Mapping) or not isinstance(pbo_payload, Mapping):
        raise ValueError("DayTrade adaptive DSR/PBO evidence is missing")
    dsr_confidence = _probability(artifact.get("dsr_confidence"), "dsr_confidence")
    pbo = _probability(artifact.get("pbo"), "pbo")
    if (
        dsr_payload.get("available") is not True
        or pbo_payload.get("available") is not True
        or _probability(dsr_payload.get("confidence"), "report DSR confidence")
        != dsr_confidence
        or _probability(pbo_payload.get("probability"), "report PBO") != pbo
    ):
        raise ValueError("DayTrade adaptive DSR/PBO do not match the report")
    if dsr_confidence < 0.95 or pbo > 0.05:
        raise ValueError("DayTrade adaptive candidate_survived conflicts with DSR/PBO")

    return DayTradeAdaptiveValidationArtifact(
        issuer=issuer,
        content_hash=content_hash,
        dsr_confidence=dsr_confidence,
        pbo=pbo,
        declared_trial_count=21,
        completed_trial_count=21,
        report=report,
        identity_hashes=identity_hashes,
        _seal=_SEAL,
    )


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _day(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date")
    return parsed


def _probability(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a probability")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise ValueError(f"{name} must be between zero and one")
    return result


__all__ = (
    "DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV",
    "DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_VERSION",
    "DAYTRADE_EXPERIMENT_KEYS_ENV",
    "DayTradeAdaptiveValidationArtifact",
    "load_daytrade_adaptive_validation_artifact",
    "trusted_daytrade_experiment_keys",
    "verify_daytrade_adaptive_validation_artifact",
)
