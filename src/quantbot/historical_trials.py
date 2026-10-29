"""Trust contract for imported DayTrade historical trial executions.

The aggregate H1-H8 artifact is useful for transporting aligned return
histories, but one aggregate signature cannot prove that each declared trial
had its own data-access permit and execution evidence.  This module validates
an independently signed proof for every imported trial and binds that proof to
the exact return series used by DSR/PBO.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime
from hashlib import sha256
from math import isfinite
from typing import Mapping, Sequence

from .acceptance_policy import ACCEPTANCE_POLICY_VERSION
from .evidence import hash_payload
from .validation_protocol import ValidationWindowManifest


HISTORICAL_TRIAL_ARTIFACT_VERSION = 2
HISTORICAL_TRIAL_PROOF_VERSION = 1
HISTORICAL_TRIAL_RUN_TYPE = "historical_optimization"

_PROOF_FIELDS = frozenset(
    {
        "proof_version",
        "issuer",
        "trial_id",
        "experiment_id",
        "strategy_family",
        "run_id",
        "run_type",
        "policy_version",
        "permit_mode",
        "permit_request_start",
        "permit_request_end",
        "permit_warmup_start",
        "permit_warmup_end",
        "permit_actual_start",
        "permit_actual_end",
        "permit_claimed_at",
        "permit_manifest_hash",
        "permit_record_hash",
        "permit_dataset_hash",
        "permit_config_hash",
        "permit_cost_model_hash",
        "permit_code_hash",
        "execution_observed_at",
        "execution_evidence_hash",
        "return_history_hash",
        "content_hash",
        "signature",
    }
)


def historical_trial_return_history_hash(
    observation_dates: Sequence[str],
    daily_returns: Sequence[object],
) -> str:
    """Hash one trial's normalized, date-aligned OOS return history."""

    if any(not isinstance(value, str) for value in observation_dates):
        raise ValueError("historical trial observation dates must be ISO strings")
    try:
        parsed_dates = tuple(date.fromisoformat(value) for value in observation_dates)
    except ValueError as error:
        raise ValueError("historical trial observation dates must be ISO dates") from error
    if parsed_dates != tuple(sorted(set(parsed_dates))):
        raise ValueError("historical trial observation dates must be unique and sorted")
    normalized = []
    for value in daily_returns:
        if isinstance(value, bool):
            raise ValueError("historical trial returns must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError("historical trial returns must be numeric") from error
        if not isfinite(number) or number <= -1.0:
            raise ValueError(
                "historical trial returns must be finite and greater than -1"
            )
        normalized.append(number)
    if len(normalized) != len(parsed_dates):
        raise ValueError("historical trial returns must align with observation dates")
    return hash_payload(
        {
            "observation_dates": list(observation_dates),
            "daily_returns": normalized,
        }
    )


def validate_historical_trial_execution_proofs(
    execution_evidence: object,
    *,
    completed_trial_ids: Sequence[str],
    experiment_by_trial: Mapping[str, str] | None,
    observation_dates: Sequence[str],
    trial_daily_returns: Mapping[str, object],
    historical_dataset_hash: str,
    trusted_keys: Mapping[str, bytes],
) -> dict[str, dict[str, object]]:
    """Validate every independently signed imported-trial proof.

    The independent proof signer attests to the exact permit identity and the
    immutable execution-evidence digest.  The aggregate artifact has its own
    separate signature, so neither layer can silently substitute the other.
    """

    expected_ids = set(completed_trial_ids)
    if (
        not isinstance(execution_evidence, dict)
        or set(execution_evidence) != expected_ids
    ):
        raise ValueError(
            "historical trial execution evidence must exactly cover completed trials"
        )
    if set(trial_daily_returns) != expected_ids:
        raise ValueError(
            "historical trial returns must exactly cover completed trials"
        )

    verified: dict[str, dict[str, object]] = {}
    run_ids: set[str] = set()
    permit_record_hashes: set[str] = set()
    execution_hashes: set[str] = set()
    proof_hashes: set[str] = set()
    for trial_id in sorted(expected_ids):
        raw = execution_evidence[trial_id]
        if not isinstance(raw, dict) or set(raw) != _PROOF_FIELDS:
            raise ValueError(
                f"historical trial {trial_id} proof fields are invalid"
            )
        proof = dict(raw)
        signature = _digest(
            proof.pop("signature"),
            f"historical trial {trial_id} signature",
        )
        content_hash = _digest(
            proof.pop("content_hash"),
            f"historical trial {trial_id} content_hash",
        )
        if proof.get("proof_version") != HISTORICAL_TRIAL_PROOF_VERSION:
            raise ValueError(
                f"historical trial {trial_id} proof_version is unsupported"
            )
        if proof.get("trial_id") != trial_id:
            raise ValueError("historical trial execution evidence trial_id mismatch")
        expected_experiment = (
            None if experiment_by_trial is None else experiment_by_trial.get(trial_id)
        )
        experiment_id = proof.get("experiment_id")
        if not isinstance(experiment_id, str) or not experiment_id:
            raise ValueError("historical trial proof requires experiment_id")
        if expected_experiment is not None and experiment_id != expected_experiment:
            raise ValueError("historical trial execution evidence experiment_id mismatch")
        if proof.get("strategy_family") != "daytrade":
            raise ValueError("historical trial strategy_family must be daytrade")
        if proof.get("run_type") != HISTORICAL_TRIAL_RUN_TYPE:
            raise ValueError(
                "historical trial run_type must be historical_optimization"
            )
        if proof.get("policy_version") != ACCEPTANCE_POLICY_VERSION:
            raise ValueError("historical trial policy_version is not current")
        run_id = proof.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("historical trial execution evidence requires run_id")

        manifest = _validation_manifest_from_proof(proof, trial_id)
        if proof.get("permit_manifest_hash") != manifest.manifest_hash:
            raise ValueError("historical trial permit_manifest_hash mismatch")
        if proof.get("permit_actual_start") != manifest.actual_start.isoformat():
            raise ValueError("historical trial permit_actual_start mismatch")
        if proof.get("permit_actual_end") != manifest.actual_end.isoformat():
            raise ValueError("historical trial permit_actual_end mismatch")
        if manifest.dataset_hash != historical_dataset_hash:
            raise ValueError(
                "historical trial permit_dataset_hash does not match the "
                "auditable historical evaluation identity"
            )
        observed_dates = tuple(date.fromisoformat(value) for value in observation_dates)
        if observed_dates and (
            min(observed_dates) < manifest.request_start
            or max(observed_dates) > manifest.request_end
        ):
            raise ValueError(
                "historical trial return dates are outside the permit request interval"
            )

        permit_claimed_at = _aware_datetime(
            proof.get("permit_claimed_at"),
            "historical trial permit_claimed_at",
        )
        execution_observed_at = _aware_datetime(
            proof.get("execution_observed_at"),
            "historical trial execution_observed_at",
        )
        if execution_observed_at < permit_claimed_at:
            raise ValueError(
                "historical trial execution evidence predates its permit claim"
            )
        for name in (
            "permit_record_hash",
            "permit_dataset_hash",
            "permit_config_hash",
            "permit_cost_model_hash",
            "permit_code_hash",
            "execution_evidence_hash",
            "return_history_hash",
        ):
            _digest(proof.get(name), f"historical trial {trial_id} {name}")
        expected_return_hash = historical_trial_return_history_hash(
            observation_dates,
            trial_daily_returns[trial_id],
        )
        if proof["return_history_hash"] != expected_return_hash:
            raise ValueError("historical trial return_history_hash mismatch")
        if content_hash != hash_payload(proof):
            raise ValueError("historical trial proof content_hash mismatch")
        issuer = proof.get("issuer")
        if not isinstance(issuer, str) or not issuer:
            raise ValueError("historical trial proof requires issuer")
        key = trusted_keys.get(issuer)
        if key is None:
            raise ValueError("trusted historical trial proof issuer key is missing")
        expected_signature = hmac.new(
            key,
            content_hash.encode("ascii"),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, signature):
            raise ValueError("historical trial proof signature is invalid")

        permit_record_hash = str(proof["permit_record_hash"])
        execution_hash = str(proof["execution_evidence_hash"])
        if run_id in run_ids:
            raise ValueError("historical trial run_id must be unique")
        if permit_record_hash in permit_record_hashes:
            raise ValueError("historical trial permit_record_hash must be unique")
        if execution_hash in execution_hashes:
            raise ValueError("historical trial execution_evidence_hash must be unique")
        if content_hash in proof_hashes:
            raise ValueError("historical trial proof content_hash must be unique")
        run_ids.add(run_id)
        permit_record_hashes.add(permit_record_hash)
        execution_hashes.add(execution_hash)
        proof_hashes.add(content_hash)
        verified[trial_id] = {
            **proof,
            "content_hash": content_hash,
            "signature": signature,
        }
    return verified


def _validation_manifest_from_proof(
    proof: Mapping[str, object],
    trial_id: str,
) -> ValidationWindowManifest:
    try:
        request_start = date.fromisoformat(str(proof["permit_request_start"]))
        request_end = date.fromisoformat(str(proof["permit_request_end"]))
        raw_warmup_start = proof["permit_warmup_start"]
        raw_warmup_end = proof["permit_warmup_end"]
        warmup_start = (
            None
            if raw_warmup_start is None
            else date.fromisoformat(str(raw_warmup_start))
        )
        warmup_end = (
            None
            if raw_warmup_end is None
            else date.fromisoformat(str(raw_warmup_end))
        )
        return ValidationWindowManifest(
            strategy_family="daytrade",
            mode=str(proof["permit_mode"]),
            request_start=request_start,
            request_end=request_end,
            warmup_start=warmup_start,
            warmup_end=warmup_end,
            dataset_hash=_digest(
                proof.get("permit_dataset_hash"),
                f"historical trial {trial_id} permit_dataset_hash",
            ),
            config_hash=_digest(
                proof.get("permit_config_hash"),
                f"historical trial {trial_id} permit_config_hash",
            ),
            cost_model_hash=_digest(
                proof.get("permit_cost_model_hash"),
                f"historical trial {trial_id} permit_cost_model_hash",
            ),
            code_hash=_digest(
                proof.get("permit_code_hash"),
                f"historical trial {trial_id} permit_code_hash",
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(
            f"historical trial {trial_id} permit identity is invalid"
        ) from error


def _aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value
