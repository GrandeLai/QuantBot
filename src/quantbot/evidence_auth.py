"""Authenticated append-only records for formal strategy admission evidence."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .validation_protocol import DEFAULT_EXPOSURE_HMAC_KEY_ENV


FORMAL_EVIDENCE_VERSION = 2
CANONICAL_VERIFIER_VERSION = 1
FORMAL_EVIDENCE_GENESIS_HASH = "0" * 64
STRATEGY_ADMISSION_REQUIREMENTS = frozenset(
    {"normal_strategy_admission", "daytrade_strategy_admission"}
)
_AUTH_FIELDS = frozenset(
    {
        "formal_evidence_version",
        "permit_record_hash",
        "execution_receipt_hash",
        "execution_result_hash",
        "execution_artifact_hash",
        "canonical_verifier_version",
        "canonical_verification_hash",
        "canonical_verification_signature",
        "formal_prev_hash",
        "formal_record_hash",
        "formal_signature",
    }
)
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_VERIFICATION_GRANT_SEAL = object()


class FormalEvidenceIntegrityError(ValueError):
    """Raised when authenticated formal evidence is incomplete or modified."""


class EvidenceAppendConflictError(ValueError):
    """Raised when an idempotency key is already bound to another payload."""


@dataclass(frozen=True, slots=True)
class _CanonicalVerificationGrant:
    """In-process capability minted only after canonical re-evaluation.

    A formal-chain HMAC proves only that a row was appended by a process with
    the ledger key.  This separate, domain-separated grant binds the raw
    execution artifact and every upstream receipt to the exact canonical
    admission payload.  The append primitive refuses plain mappings, so a
    low-level signer cannot accidentally promote a caller-authored summary.
    """

    admission_payload_hash: str
    permit_record_hash: str
    execution_receipt_hash: str
    execution_result_hash: str
    execution_artifact_hash: str
    verification_hash: str
    verification_signature: str
    _seal: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _VERIFICATION_GRANT_SEAL:
            raise PermissionError(
                "canonical verification grants are verifier-owned"
            )


def append_json_line_locked(path: Path | str, payload: Mapping[str, object]) -> None:
    """Append one JSON object durably under process and file locks."""

    selected = Path(path).expanduser().resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock(selected):
        with selected.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(_canonical_json(dict(payload)) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def append_json_line_idempotently(
    path: Path | str,
    payload: Mapping[str, object],
    *,
    identity_fields: tuple[str, ...],
) -> bool:
    """Atomically append one payload unless its exact identity already exists."""

    if not identity_fields or any(
        not isinstance(name, str) or not name for name in identity_fields
    ):
        raise ValueError("identity_fields must contain non-empty field names")
    item = dict(payload)
    missing = [name for name in identity_fields if name not in item]
    if missing:
        raise ValueError(
            "idempotent evidence payload is missing " + ", ".join(missing)
        )
    identity = tuple(item[name] for name in identity_fields)
    selected = Path(path).expanduser().resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    with _process_lock(selected):
        with selected.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        raise EvidenceAppendConflictError(
                            f"blank evidence record at line {line_number}"
                        )
                    try:
                        existing = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise EvidenceAppendConflictError(
                            f"invalid evidence JSON at line {line_number}"
                        ) from error
                    if not isinstance(existing, dict):
                        raise EvidenceAppendConflictError(
                            f"evidence record at line {line_number} is not an object"
                        )
                    if any(name not in existing for name in identity_fields):
                        continue
                    existing_identity = tuple(
                        existing[name] for name in identity_fields
                    )
                    if existing_identity != identity:
                        continue
                    if existing != item:
                        raise EvidenceAppendConflictError(
                            "evidence identity is bound to a conflicting payload"
                        )
                    return False
                handle.seek(0, os.SEEK_END)
                handle.write(_canonical_json(item) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return True
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _mint_canonical_verification_grant(
    payload: Mapping[str, object],
    *,
    permit_record_hash: str,
    execution_receipt_hash: str,
    execution_result_hash: str,
    execution_artifact_hash: str,
) -> _CanonicalVerificationGrant:
    """Bind a canonical admission payload to its verified raw provenance."""

    base = dict(payload)
    if base.get("requirement_id") not in STRATEGY_ADMISSION_REQUIREMENTS:
        raise ValueError("canonical verification requires a formal strategy id")
    if _AUTH_FIELDS.intersection(base):
        raise ValueError("formal authentication fields are recorder-owned")
    for value, name in (
        (permit_record_hash, "permit_record_hash"),
        (execution_receipt_hash, "execution_receipt_hash"),
        (execution_result_hash, "execution_result_hash"),
        (execution_artifact_hash, "execution_artifact_hash"),
    ):
        _digest(value, name)
    admission_payload_hash = _hash(base)
    proof = _canonical_verification_payload(
        admission_payload_hash=admission_payload_hash,
        permit_record_hash=permit_record_hash,
        execution_receipt_hash=execution_receipt_hash,
        execution_result_hash=execution_result_hash,
        execution_artifact_hash=execution_artifact_hash,
    )
    verification_hash = _hash(proof)
    return _CanonicalVerificationGrant(
        admission_payload_hash=admission_payload_hash,
        permit_record_hash=permit_record_hash,
        execution_receipt_hash=execution_receipt_hash,
        execution_result_hash=execution_result_hash,
        execution_artifact_hash=execution_artifact_hash,
        verification_hash=verification_hash,
        verification_signature=_verifier_signature(verification_hash, _key()),
        _seal=_VERIFICATION_GRANT_SEAL,
    )


def _append_verified_strategy_evidence(
    path: Path | str,
    payload: Mapping[str, object],
    *,
    verification_grant: _CanonicalVerificationGrant,
) -> dict[str, object]:
    """Append a verifier-granted record; never accept caller-authored proof."""

    base = dict(payload)
    requirement_id = base.get("requirement_id")
    if requirement_id not in STRATEGY_ADMISSION_REQUIREMENTS:
        raise ValueError("authenticated strategy evidence requires a formal strategy id")
    if _AUTH_FIELDS.intersection(base):
        raise ValueError("formal authentication fields are recorder-owned")
    if (
        not isinstance(verification_grant, _CanonicalVerificationGrant)
        or verification_grant._seal is not _VERIFICATION_GRANT_SEAL
    ):
        raise PermissionError(
            "formal strategy evidence requires canonical verification"
        )
    if _hash(base) != verification_grant.admission_payload_hash:
        raise PermissionError(
            "canonical verification grant does not bind this admission payload"
        )
    for value, name in (
        (verification_grant.permit_record_hash, "permit_record_hash"),
        (verification_grant.execution_receipt_hash, "execution_receipt_hash"),
        (verification_grant.execution_result_hash, "execution_result_hash"),
        (verification_grant.execution_artifact_hash, "execution_artifact_hash"),
        (verification_grant.verification_hash, "canonical_verification_hash"),
        (
            verification_grant.verification_signature,
            "canonical_verification_signature",
        ),
    ):
        _digest(value, name)
    selected = Path(path).expanduser().resolve()
    selected.parent.mkdir(parents=True, exist_ok=True)
    key = _key()
    with _process_lock(selected):
        with selected.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.seek(0)
                previous = FORMAL_EVIDENCE_GENESIS_HASH
                existing_for_receipt = None
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        raise FormalEvidenceIntegrityError(
                            f"blank evidence record at line {line_number}"
                        )
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as error:
                        raise FormalEvidenceIntegrityError(
                            f"invalid evidence JSON at line {line_number}"
                        ) from error
                    authenticated, previous = authenticate_formal_payload(
                        item,
                        previous_hash=previous,
                        key=key,
                    )
                    if (
                        authenticated
                        and item.get("execution_receipt_hash")
                        == verification_grant.execution_receipt_hash
                    ):
                        existing_for_receipt = item
                unsigned = {
                    **base,
                    "formal_evidence_version": FORMAL_EVIDENCE_VERSION,
                    "permit_record_hash": verification_grant.permit_record_hash,
                    "execution_receipt_hash": verification_grant.execution_receipt_hash,
                    "execution_result_hash": verification_grant.execution_result_hash,
                    "execution_artifact_hash": verification_grant.execution_artifact_hash,
                    "canonical_verifier_version": CANONICAL_VERIFIER_VERSION,
                    "canonical_verification_hash": verification_grant.verification_hash,
                    "canonical_verification_signature": (
                        verification_grant.verification_signature
                    ),
                    "formal_prev_hash": previous,
                }
                if existing_for_receipt is not None:
                    comparable = {
                        key: value
                        for key, value in existing_for_receipt.items()
                        if key not in {"formal_record_hash", "formal_signature"}
                    }
                    # The original predecessor remains part of the immutable
                    # record, so compare business identity independently from
                    # the current tail during idempotent recovery.
                    expected = dict(unsigned)
                    expected["formal_prev_hash"] = comparable["formal_prev_hash"]
                    if comparable != expected:
                        raise FormalEvidenceIntegrityError(
                            "execution receipt is bound to conflicting formal evidence"
                        )
                    return dict(existing_for_receipt)
                record_hash = _hash(unsigned)
                record = {
                    **unsigned,
                    "formal_record_hash": record_hash,
                    "formal_signature": _signature(record_hash, key),
                }
                handle.seek(0, os.SEEK_END)
                handle.write(_canonical_json(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                return record
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def authenticate_formal_payload(
    payload: object,
    *,
    previous_hash: str,
    key: bytes | None = None,
) -> tuple[bool, str]:
    """Verify one formal record and advance its authenticated hash chain.

    Legacy unsigned strategy-admission rows remain loadable but are explicitly
    unauthenticated and therefore cannot satisfy ``CompletionAudit``.
    """

    if not isinstance(payload, dict):
        raise FormalEvidenceIntegrityError("evidence record must be an object")
    if payload.get("requirement_id") not in STRATEGY_ADMISSION_REQUIREMENTS:
        return False, previous_hash
    present = _AUTH_FIELDS.intersection(payload)
    if not present:
        return False, previous_hash
    if present != _AUTH_FIELDS:
        raise FormalEvidenceIntegrityError(
            "formal evidence authentication fields are incomplete"
        )
    if payload.get("formal_evidence_version") != FORMAL_EVIDENCE_VERSION:
        raise FormalEvidenceIntegrityError("unsupported formal evidence version")
    for name in (
        "permit_record_hash",
        "execution_receipt_hash",
        "execution_result_hash",
        "execution_artifact_hash",
        "canonical_verification_hash",
        "canonical_verification_signature",
        "formal_prev_hash",
        "formal_record_hash",
        "formal_signature",
    ):
        try:
            _digest(payload.get(name), name)
        except ValueError as error:
            raise FormalEvidenceIntegrityError(str(error)) from error
    if payload["formal_prev_hash"] != previous_hash:
        raise FormalEvidenceIntegrityError("broken formal evidence hash chain")
    unsigned = {
        name: value
        for name, value in payload.items()
        if name not in {"formal_record_hash", "formal_signature"}
    }
    expected = _hash(unsigned)
    if payload["formal_record_hash"] != expected:
        raise FormalEvidenceIntegrityError("formal evidence record hash is invalid")
    selected_key = _key() if key is None else key
    if not hmac.compare_digest(
        payload["formal_signature"],
        _signature(expected, selected_key),
    ):
        raise FormalEvidenceIntegrityError("formal evidence signature is invalid")
    return True, expected


def verify_canonical_verification_payload(payload: object) -> bool:
    """Verify the domain-separated proof without conflating it with chain auth."""

    if not isinstance(payload, dict):
        return False
    if payload.get("canonical_verifier_version") != CANONICAL_VERIFIER_VERSION:
        return False
    try:
        business_payload = {
            name: value for name, value in payload.items() if name not in _AUTH_FIELDS
        }
        proof = _canonical_verification_payload(
            admission_payload_hash=_hash(business_payload),
            permit_record_hash=_digest(
                payload.get("permit_record_hash"), "permit_record_hash"
            ),
            execution_receipt_hash=_digest(
                payload.get("execution_receipt_hash"), "execution_receipt_hash"
            ),
            execution_result_hash=_digest(
                payload.get("execution_result_hash"), "execution_result_hash"
            ),
            execution_artifact_hash=_digest(
                payload.get("execution_artifact_hash"), "execution_artifact_hash"
            ),
        )
        expected_hash = _hash(proof)
        supplied_hash = _digest(
            payload.get("canonical_verification_hash"),
            "canonical_verification_hash",
        )
        supplied_signature = _digest(
            payload.get("canonical_verification_signature"),
            "canonical_verification_signature",
        )
    except ValueError:
        return False
    return hmac.compare_digest(supplied_hash, expected_hash) and hmac.compare_digest(
        supplied_signature,
        _verifier_signature(expected_hash, _key()),
    )


def _canonical_verification_payload(
    *,
    admission_payload_hash: str,
    permit_record_hash: str,
    execution_receipt_hash: str,
    execution_result_hash: str,
    execution_artifact_hash: str,
) -> dict[str, object]:
    return {
        "canonical_verifier_version": CANONICAL_VERIFIER_VERSION,
        "admission_payload_hash": admission_payload_hash,
        "permit_record_hash": permit_record_hash,
        "execution_receipt_hash": execution_receipt_hash,
        "execution_result_hash": execution_result_hash,
        "execution_artifact_hash": execution_artifact_hash,
    }


def _key() -> bytes:
    raw = os.environ.get(DEFAULT_EXPOSURE_HMAC_KEY_ENV, "")
    if not raw:
        raise FormalEvidenceIntegrityError(
            f"{DEFAULT_EXPOSURE_HMAC_KEY_ENV} is required for formal evidence"
        )
    return raw.encode("utf-8")


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _signature(record_hash: str, key: bytes) -> str:
    return hmac.new(key, record_hash.encode("ascii"), hashlib.sha256).hexdigest()


def _verifier_signature(verification_hash: str, key: bytes) -> str:
    message = ("canonical-verifier-v1:" + verification_hash).encode("ascii")
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _process_lock(path: Path) -> threading.Lock:
    identity = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(identity, threading.Lock())


__all__ = (
    "EvidenceAppendConflictError",
    "CANONICAL_VERIFIER_VERSION",
    "FORMAL_EVIDENCE_GENESIS_HASH",
    "FORMAL_EVIDENCE_VERSION",
    "FormalEvidenceIntegrityError",
    "STRATEGY_ADMISSION_REQUIREMENTS",
    "append_json_line_idempotently",
    "append_json_line_locked",
    "authenticate_formal_payload",
    "verify_canonical_verification_payload",
)
