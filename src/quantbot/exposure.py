"""Tamper-evident data-exposure ledger for validation-window access."""

from __future__ import annotations

import fcntl
import hmac
import json
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from .validation_protocol import (
    DEFAULT_EXPOSURE_HMAC_KEY_ENV,
    EXCLUSIVE_ACCESS_MODES,
    EXPOSURE_LEDGER_VERSION,
    LEGACY_EXPOSED_SEALED_END,
    LEGACY_EXPOSED_SEALED_START,
    LEGACY_EXPOSURE_LEDGER_VERSION,
    DataAccessPermit,
    ValidationWindowManifest,
    _aware,
    _canonical_json,
    _digest,
    _environment_hmac_key,
    _fixed_holdout_run_id,
    _protocol_for,
    _record_digest,
    _record_signature,
    _trusted_utc_now,
)


DEFAULT_EXPOSURE_LEDGER_PATH_ENV = "STOCKBOT_EXPOSURE_LEDGER_PATH"
GENESIS_HASH = "0" * 64
LEDGER_FILENAME = "exposure-ledger.jsonl"
_LEGACY_RECORD_FIELDS = frozenset(
    {
        "ledger_version",
        "run_id",
        "strategy_family",
        "mode",
        "request_start",
        "request_end",
        "warmup_start",
        "warmup_end",
        "actual_start",
        "actual_end",
        "dataset_hash",
        "config_hash",
        "cost_model_hash",
        "code_hash",
        "manifest_hash",
        "claimed_at",
        "outcome",
        "denial_reason",
        "prev_hash",
        "record_hash",
        "signature",
    }
)
_RECORD_FIELDS = _LEGACY_RECORD_FIELDS | {
    "read_interval",
    "protected_holdout_interval",
}
_PROCESS_LOCKS: dict[Path, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class ExposureLedgerIntegrityError(ValueError):
    """Raised when an existing ledger no longer verifies."""


class ExposureDeniedError(ValueError):
    """Raised after a denied access attempt has been durably recorded."""

    def __init__(self, reason: str, record: "ExposureRecord"):
        super().__init__(reason)
        self.reason = reason
        self.record = record


@dataclass(frozen=True, slots=True)
class ExposureRecord:
    ledger_version: int
    run_id: str
    manifest: ValidationWindowManifest
    claimed_at: datetime
    outcome: str
    denial_reason: str | None
    prev_hash: str
    record_hash: str
    signature: str

    def __post_init__(self):
        if self.ledger_version not in {
            LEGACY_EXPOSURE_LEDGER_VERSION,
            EXPOSURE_LEDGER_VERSION,
        }:
            raise ValueError("unsupported exposure ledger version")
        _fixed_holdout_run_id(self.run_id)
        if not isinstance(self.manifest, ValidationWindowManifest):
            raise TypeError("manifest must be a ValidationWindowManifest")
        _aware(self.claimed_at, "claimed_at")
        if self.outcome not in {"granted", "denied"}:
            raise ValueError("outcome must be granted or denied")
        if self.outcome == "granted" and self.denial_reason is not None:
            raise ValueError("a granted exposure must not have a denial_reason")
        if self.outcome == "denied" and (
            not isinstance(self.denial_reason, str) or not self.denial_reason
        ):
            raise ValueError("a denied exposure must have a denial_reason")
        for name in ("prev_hash", "record_hash", "signature"):
            _digest(getattr(self, name), name)
        if self.record_hash != _record_digest(self.unsigned_payload()):
            raise ValueError("record_hash does not match the exposure record")

    @property
    def strategy_family(self) -> str:
        return self.manifest.strategy_family

    @property
    def mode(self) -> str:
        return self.manifest.mode

    @property
    def actual_start(self) -> date:
        return self.manifest.actual_start

    @property
    def actual_end(self) -> date:
        return self.manifest.actual_end

    @property
    def protected_start(self) -> date:
        # Version-one ledgers hashed and compared the complete actual/read
        # interval. Preserve that conservative interpretation when migrating
        # an existing chain; only version-two records have an explicit
        # protected holdout interval.
        if self.ledger_version == LEGACY_EXPOSURE_LEDGER_VERSION:
            return self.manifest.actual_start
        return self.manifest.protected_holdout_interval.start

    @property
    def protected_end(self) -> date:
        if self.ledger_version == LEGACY_EXPOSURE_LEDGER_VERSION:
            return self.manifest.actual_end
        return self.manifest.protected_holdout_interval.end

    @property
    def manifest_hash(self) -> str:
        return self.manifest.manifest_hash_for_ledger_version(self.ledger_version)

    def unsigned_payload(self) -> dict[str, object]:
        return {
            "ledger_version": self.ledger_version,
            "run_id": self.run_id,
            **self.manifest.payload_for_ledger_version(self.ledger_version),
            "manifest_hash": self.manifest_hash,
            "claimed_at": self.claimed_at.isoformat(),
            "outcome": self.outcome,
            "denial_reason": self.denial_reason,
            "prev_hash": self.prev_hash,
        }

    def as_payload(self) -> dict[str, object]:
        return {
            **self.unsigned_payload(),
            "record_hash": self.record_hash,
            "signature": self.signature,
        }

    def is_authentic(self, key: bytes) -> bool:
        return hmac.compare_digest(
            self.signature, _record_signature(self.record_hash, key)
        )

    def to_permit(self) -> DataAccessPermit:
        if self.outcome != "granted":
            raise ValueError("a denied exposure record cannot become a permit")
        if _is_legacy_exposed_sealed(self.manifest):
            raise PermissionError(
                "legacy exposed sealed records are read-only audit evidence"
            )
        manifest = self.manifest
        return DataAccessPermit(
            run_id=self.run_id,
            strategy_family=manifest.strategy_family,
            mode=manifest.mode,
            request_start=manifest.request_start,
            request_end=manifest.request_end,
            warmup_start=manifest.warmup_start,
            warmup_end=manifest.warmup_end,
            actual_start=manifest.actual_start,
            actual_end=manifest.actual_end,
            dataset_hash=manifest.dataset_hash,
            config_hash=manifest.config_hash,
            cost_model_hash=manifest.cost_model_hash,
            code_hash=manifest.code_hash,
            claimed_at=self.claimed_at,
            manifest_hash=self.manifest_hash,
            prev_hash=self.prev_hash,
            record_hash=self.record_hash,
            signature=self.signature,
            ledger_version=self.ledger_version,
        )

    @classmethod
    def from_payload(cls, payload: object) -> "ExposureRecord":
        if not isinstance(payload, dict):
            raise ExposureLedgerIntegrityError("exposure record fields are invalid")
        ledger_version = payload.get("ledger_version")
        expected_fields = (
            _LEGACY_RECORD_FIELDS
            if ledger_version == LEGACY_EXPOSURE_LEDGER_VERSION
            else _RECORD_FIELDS
        )
        if set(payload) != expected_fields:
            raise ExposureLedgerIntegrityError("exposure record fields are invalid")
        if ledger_version not in {
            LEGACY_EXPOSURE_LEDGER_VERSION,
            EXPOSURE_LEDGER_VERSION,
        }:
            raise ExposureLedgerIntegrityError("unsupported exposure ledger version")
        try:
            manifest = ValidationWindowManifest(
                strategy_family=payload["strategy_family"],
                mode=payload["mode"],
                request_start=date.fromisoformat(payload["request_start"]),
                request_end=date.fromisoformat(payload["request_end"]),
                warmup_start=_optional_date_from_payload(payload["warmup_start"]),
                warmup_end=_optional_date_from_payload(payload["warmup_end"]),
                dataset_hash=payload["dataset_hash"],
                config_hash=payload["config_hash"],
                cost_model_hash=payload["cost_model_hash"],
                code_hash=payload["code_hash"],
                _allow_legacy_exposed_sealed=(
                    ledger_version == LEGACY_EXPOSURE_LEDGER_VERSION
                    and payload["mode"] == "sealed"
                    and payload["request_start"]
                    == LEGACY_EXPOSED_SEALED_START.isoformat()
                    and payload["request_end"]
                    == LEGACY_EXPOSED_SEALED_END.isoformat()
                ),
            )
            if payload["actual_start"] != manifest.actual_start.isoformat():
                raise ValueError("actual_start does not match the manifest")
            if payload["actual_end"] != manifest.actual_end.isoformat():
                raise ValueError("actual_end does not match the manifest")
            if ledger_version >= 2:
                if payload["read_interval"] != manifest.read_interval.as_payload():
                    raise ValueError("read_interval does not match the manifest")
                if (
                    payload["protected_holdout_interval"]
                    != manifest.protected_holdout_interval.as_payload()
                ):
                    raise ValueError(
                        "protected_holdout_interval does not match the manifest"
                    )
            if payload["manifest_hash"] != manifest.manifest_hash_for_ledger_version(
                ledger_version
            ):
                raise ValueError("manifest_hash does not match the manifest")
            return cls(
                ledger_version=ledger_version,
                run_id=payload["run_id"],
                manifest=manifest,
                claimed_at=datetime.fromisoformat(payload["claimed_at"]),
                outcome=payload["outcome"],
                denial_reason=payload["denial_reason"],
                prev_hash=payload["prev_hash"],
                record_hash=payload["record_hash"],
                signature=payload["signature"],
            )
        except (TypeError, ValueError) as error:
            raise ExposureLedgerIntegrityError(
                "exposure record payload is invalid"
            ) from error


class ExposureLedger:
    """Append-only, HMAC-authenticated ledger shared by all validation runs."""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        key_env: str = DEFAULT_EXPOSURE_HMAC_KEY_ENV,
        path_env: str = DEFAULT_EXPOSURE_LEDGER_PATH_ENV,
        _clock: Callable[[], datetime] | None = None,
    ):
        configured = os.environ.get(path_env) if path_env else None
        if configured:
            configured_source = Path(configured).expanduser()
            if not configured_source.is_absolute():
                raise ValueError(f"{path_env} must be an absolute path")
            configured_path = canonical_exposure_ledger_path(configured_source)
            if (
                path is not None
                and canonical_exposure_ledger_path(path) != configured_path
            ):
                raise ValueError(
                    f"exposure ledger path must match the canonical {path_env}"
                )
            selected = configured_path
        elif path is not None:
            selected = canonical_exposure_ledger_path(path)
        else:
            raise ValueError(
                f"exposure ledger path is required or must be set in {path_env}"
            )
        self.path = selected.resolve()
        self.key_env = key_env
        self._key = _environment_hmac_key(key_env)
        if _clock is not None and not callable(_clock):
            raise TypeError("_clock must be callable")
        self._clock = _clock

    @property
    def records(self) -> tuple[ExposureRecord, ...]:
        with _process_lock(self.path):
            if not self.path.exists():
                return ()
            with self.path.open("r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    return self._read_locked(handle)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def claim(
        self,
        manifest: ValidationWindowManifest,
        *,
        run_id: str,
        claimed_at: datetime | None = None,
    ) -> DataAccessPermit:
        if not isinstance(manifest, ValidationWindowManifest):
            raise TypeError("manifest must be a ValidationWindowManifest")
        if _is_legacy_exposed_sealed(manifest):
            raise ValueError(
                "legacy exposed sealed windows cannot create a new claim"
            )
        identifier = _fixed_holdout_run_id(run_id)
        observed = _trusted_utc_now(self._clock)
        asserted = None if claimed_at is None else _aware(claimed_at, "claimed_at")
        future_claim = asserted is not None and asserted > observed
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _process_lock(self.path):
            with self.path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    existing = self._read_locked(handle)
                    idempotent = next(
                        (
                            record
                            for record in existing
                            if record.run_id == identifier
                            and record.manifest == manifest
                        ),
                        None,
                    )
                    if idempotent is not None:
                        if idempotent.outcome == "granted":
                            return idempotent.to_permit()
                        raise ExposureDeniedError(
                            idempotent.denial_reason,
                            idempotent,
                        )
                    denial_reason = "caller-provided claimed_at is in the future"
                    if not future_claim:
                        denial_reason = self._denial_reason(
                            manifest,
                            observed,
                            existing,
                            run_id=identifier,
                        )
                    record = self._build_record(
                        identifier,
                        manifest,
                        observed,
                        existing[-1].record_hash if existing else GENESIS_HASH,
                        denial_reason,
                    )
                    handle.seek(0, os.SEEK_END)
                    handle.write(_canonical_json(record.as_payload()) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        if denial_reason is not None:
            raise ExposureDeniedError(denial_reason, record)
        return record.to_permit()

    def verify(self) -> tuple[ExposureRecord, ...]:
        """Verify and return the complete chain, failing closed on any mutation."""

        return self.records

    def verify_permit(self, permit: DataAccessPermit) -> ExposureRecord:
        """Require a permit to be an authentic granted record in this chain."""

        if not isinstance(permit, DataAccessPermit):
            raise TypeError("permit must be a DataAccessPermit")
        if not permit.is_authentic(key_env=self.key_env):
            raise PermissionError("data access permit signature is invalid")
        for record in self.records:
            if record.record_hash == permit.record_hash:
                if record.outcome != "granted" or record.to_permit() != permit:
                    raise PermissionError(
                        "data access permit does not match its ledger record"
                    )
                return record
        raise PermissionError("data access permit is absent from the exposure ledger")

    def _read_locked(self, handle) -> tuple[ExposureRecord, ...]:
        return _read_records_locked(handle, self._key)

    def _denial_reason(self, manifest, claimed_at, existing, *, run_id):
        protocol = _protocol_for(manifest.strategy_family)
        if (
            manifest.mode == "sealed"
            and claimed_at.astimezone(timezone.utc).date() <= protocol.sealed_end
        ):
            return "sealed holdout is not complete"
        if any(record.run_id == run_id for record in existing):
            return "run_id is already bound to a different exposure record"
        protected = manifest.protected_holdout_interval
        for record in existing:
            if (
                record.outcome == "granted"
                and record.strategy_family == manifest.strategy_family
                and (
                    manifest.mode in EXCLUSIVE_ACCESS_MODES
                    or record.mode in EXCLUSIVE_ACCESS_MODES
                )
                and _overlaps(
                    record.protected_start,
                    record.protected_end,
                    protected.start,
                    protected.end,
                )
            ):
                return (
                    "exclusive validation interval already exposed by "
                    f"{record.run_id}"
                )
        return None

    def _build_record(
        self, run_id, manifest, claimed_at, prev_hash, denial_reason
    ) -> ExposureRecord:
        outcome = "granted" if denial_reason is None else "denied"
        unsigned = {
            "ledger_version": EXPOSURE_LEDGER_VERSION,
            "run_id": run_id,
            **manifest.as_payload(),
            "manifest_hash": manifest.manifest_hash,
            "claimed_at": claimed_at.isoformat(),
            "outcome": outcome,
            "denial_reason": denial_reason,
            "prev_hash": prev_hash,
        }
        record_hash = _record_digest(unsigned)
        return ExposureRecord(
            ledger_version=EXPOSURE_LEDGER_VERSION,
            run_id=run_id,
            manifest=manifest,
            claimed_at=claimed_at,
            outcome=outcome,
            denial_reason=denial_reason,
            prev_hash=prev_hash,
            record_hash=record_hash,
            signature=_record_signature(record_hash, self._key),
        )


def canonical_exposure_ledger_path(value: Path | str) -> Path:
    """Return the absolute canonical JSONL path for a ledger file or directory."""

    path = Path(value).expanduser()
    if path.name.endswith(".jsonl"):
        return path.resolve()
    return (path / LEDGER_FILENAME).resolve()


def _process_lock(path: Path) -> threading.Lock:
    identity = path.resolve()
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(identity, threading.Lock())


def _optional_date_from_payload(value: object) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional date must be an ISO date string")
    return date.fromisoformat(value)


def _overlaps(first_start, first_end, second_start, second_end) -> bool:
    return first_start <= second_end and second_start <= first_end


def _is_legacy_exposed_sealed(manifest: ValidationWindowManifest) -> bool:
    return bool(
        manifest.mode == "sealed"
        and manifest.request_start == LEGACY_EXPOSED_SEALED_START
        and manifest.request_end == LEGACY_EXPOSED_SEALED_END
    )


def verify_exposure_ledger_file(
    path: Path | str,
    *,
    hmac_key: str | bytes,
) -> tuple[ExposureRecord, ...]:
    """Verify an existing ledger without creating it or changing canonical state."""

    key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
    if not isinstance(key, bytes) or not key:
        raise ValueError("hmac_key must be non-empty")
    selected = canonical_exposure_ledger_path(path)
    if not selected.exists():
        return ()
    with _process_lock(selected):
        with selected.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                return _read_records_locked(handle, key)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_records_locked(handle, key: bytes) -> tuple[ExposureRecord, ...]:
    records = []
    expected_prev = GENESIS_HASH
    for line_number, line in enumerate(handle, start=1):
        if not line.strip():
            raise ExposureLedgerIntegrityError(
                f"blank exposure ledger record at line {line_number}"
            )
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise ExposureLedgerIntegrityError(
                f"invalid exposure ledger JSON at line {line_number}"
            ) from error
        record = ExposureRecord.from_payload(payload)
        if record.prev_hash != expected_prev:
            raise ExposureLedgerIntegrityError(
                f"broken exposure hash chain at line {line_number}"
            )
        if not record.is_authentic(key):
            raise ExposureLedgerIntegrityError(
                f"invalid exposure signature at line {line_number}"
            )
        records.append(record)
        expected_prev = record.record_hash
    return tuple(records)


__all__ = (
    "DEFAULT_EXPOSURE_HMAC_KEY_ENV",
    "DEFAULT_EXPOSURE_LEDGER_PATH_ENV",
    "DataAccessPermit",
    "ExposureDeniedError",
    "ExposureLedger",
    "ExposureLedgerIntegrityError",
    "ExposureRecord",
    "ValidationWindowManifest",
    "canonical_exposure_ledger_path",
    "verify_exposure_ledger_file",
)
