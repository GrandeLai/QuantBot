"""Signed H1-H8 artifact fixtures shared by DayTrade trust tests."""

from __future__ import annotations

import hmac
from datetime import date
from hashlib import sha256
from typing import Mapping, Sequence

from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.evidence import hash_payload
from quantbot.historical_trials import (
    HISTORICAL_TRIAL_ARTIFACT_VERSION,
    HISTORICAL_TRIAL_PROOF_VERSION,
    HISTORICAL_TRIAL_RUN_TYPE,
    historical_trial_return_history_hash,
)
from quantbot.validation_protocol import ValidationWindowManifest


def signed_historical_trial_artifact(
    *,
    issuer: str,
    signing_key: bytes,
    historical_identity: Mapping[str, str],
    observation_dates: Sequence[date],
    trial_returns: Mapping[str, Sequence[float]],
    experiment_by_trial: Mapping[str, str],
    permit_mode: str = "adaptive_development",
    evaluation_dataset_hash: str | None = None,
):
    date_strings = [value.isoformat() for value in observation_dates]
    historical_dataset_hash = hash_payload(dict(historical_identity))
    proofs = {
        trial_id: signed_historical_trial_proof(
            issuer=issuer,
            signing_key=signing_key,
            trial_id=trial_id,
            experiment_id=experiment_by_trial[trial_id],
            observation_dates=date_strings,
            daily_returns=trial_returns[trial_id],
            historical_dataset_hash=historical_dataset_hash,
            permit_mode=permit_mode,
        )
        for trial_id in sorted(trial_returns)
    }
    payload = {
        "artifact_version": HISTORICAL_TRIAL_ARTIFACT_VERSION,
        "issuer": issuer,
        **dict(historical_identity),
        "historical_evaluation_dataset_hash": historical_dataset_hash,
        "observation_dates": date_strings,
        "completed_trial_ids": sorted(trial_returns),
        "trial_daily_returns": {
            trial_id: list(trial_returns[trial_id])
            for trial_id in sorted(trial_returns)
        },
        "trial_execution_evidence": proofs,
    }
    return (
        sign_historical_trial_artifact_payload(
            payload,
            signing_key=signing_key,
            evaluation_dataset_hash=evaluation_dataset_hash,
        ),
        payload,
        historical_dataset_hash,
    )


def signed_historical_trial_proof(
    *,
    issuer: str,
    signing_key: bytes,
    trial_id: str,
    experiment_id: str,
    observation_dates: Sequence[str],
    daily_returns: Sequence[float],
    historical_dataset_hash: str,
    permit_mode: str,
):
    request_start = date.fromisoformat(observation_dates[0])
    request_end = date.fromisoformat(observation_dates[-1])
    config_hash = hash_payload({"trial_id": trial_id, "kind": "config"})
    cost_model_hash = hash_payload({"trial_id": trial_id, "kind": "cost"})
    code_hash = hash_payload({"trial_id": trial_id, "kind": "code"})
    manifest = ValidationWindowManifest(
        strategy_family="daytrade",
        mode=permit_mode,
        request_start=request_start,
        request_end=request_end,
        dataset_hash=historical_dataset_hash,
        config_hash=config_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
    )
    proof = {
        "proof_version": HISTORICAL_TRIAL_PROOF_VERSION,
        "issuer": issuer,
        "trial_id": trial_id,
        "experiment_id": experiment_id,
        "strategy_family": "daytrade",
        "run_id": f"historical-{trial_id}",
        "run_type": HISTORICAL_TRIAL_RUN_TYPE,
        "policy_version": ACCEPTANCE_POLICY_VERSION,
        "permit_mode": permit_mode,
        "permit_request_start": request_start.isoformat(),
        "permit_request_end": request_end.isoformat(),
        "permit_warmup_start": None,
        "permit_warmup_end": None,
        "permit_actual_start": request_start.isoformat(),
        "permit_actual_end": request_end.isoformat(),
        "permit_claimed_at": "2025-01-01T00:00:00+00:00",
        "permit_manifest_hash": manifest.manifest_hash,
        "permit_record_hash": hash_payload(
            {"trial_id": trial_id, "kind": "permit-record"}
        ),
        "permit_dataset_hash": historical_dataset_hash,
        "permit_config_hash": config_hash,
        "permit_cost_model_hash": cost_model_hash,
        "permit_code_hash": code_hash,
        "execution_observed_at": "2025-01-01T00:01:00+00:00",
        "execution_evidence_hash": hash_payload(
            {"trial_id": trial_id, "kind": "execution-evidence"}
        ),
        "return_history_hash": historical_trial_return_history_hash(
            observation_dates,
            daily_returns,
        ),
    }
    return sign_historical_trial_proof_payload(
        proof,
        signing_key=signing_key,
    )


def sign_historical_trial_proof_payload(
    payload: Mapping[str, object],
    *,
    signing_key: bytes,
):
    proof = dict(payload)
    proof.pop("content_hash", None)
    proof.pop("signature", None)
    content_hash = hash_payload(proof)
    return {
        **proof,
        "content_hash": content_hash,
        "signature": hmac.new(
            signing_key,
            content_hash.encode("ascii"),
            sha256,
        ).hexdigest(),
    }


def sign_historical_trial_artifact_payload(
    payload: Mapping[str, object],
    *,
    signing_key: bytes,
    evaluation_dataset_hash: str | None = None,
):
    content_hash = hash_payload(dict(payload))
    artifact = {
        **dict(payload),
        "content_hash": content_hash,
        "signature": hmac.new(
            signing_key,
            content_hash.encode("ascii"),
            sha256,
        ).hexdigest(),
    }
    if evaluation_dataset_hash is not None:
        artifact["permit_dataset_hash"] = evaluation_dataset_hash
    return artifact
