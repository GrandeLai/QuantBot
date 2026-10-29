"""Tamper-evident execution receipts for one-shot strategy experiments."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping

from .exposure import (
    DEFAULT_EXPOSURE_LEDGER_PATH_ENV,
    canonical_exposure_ledger_path,
)
from .validation_protocol import DEFAULT_EXPOSURE_HMAC_KEY_ENV


EXECUTION_LEDGER_VERSION = 1
EXECUTION_LEDGER_FILENAME = "experiment-execution-ledger.jsonl"
GENESIS_HASH = "0" * 64
_LOCKS: dict[Path, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _key() -> bytes:
    raw = os.environ.get(DEFAULT_EXPOSURE_HMAC_KEY_ENV, "")
    if not raw:
        raise ValueError(
            f"{DEFAULT_EXPOSURE_HMAC_KEY_ENV} is required for execution receipts"
        )
    return raw.encode("utf-8")


def _signature(record_hash: str, key: bytes) -> str:
    return hmac.new(key, record_hash.encode("ascii"), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ExperimentExecutionIdentity:
    strategy_family: str
    experiment_id: str
    access_mode: str
    request_start: date
    request_end: date
    dataset_hash: str
    config_hash: str
    cost_model_hash: str
    code_hash: str

    def __post_init__(self) -> None:
        for name in ("strategy_family", "experiment_id", "access_mode"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.request_start, date) or not isinstance(
            self.request_end, date
        ):
            raise TypeError("execution request bounds must be dates")
        if self.request_end < self.request_start:
            raise ValueError("execution request end must not precede start")
        for name in ("dataset_hash", "config_hash", "cost_model_hash", "code_hash"):
            _digest(getattr(self, name), name)

    @property
    def stage_hash(self) -> str:
        return _hash(
            {
                "strategy_family": self.strategy_family,
                "experiment_id": self.experiment_id,
                "access_mode": self.access_mode,
            }
        )

    @property
    def identity_hash(self) -> str:
        return _hash(self.as_payload())

    def as_payload(self) -> dict[str, object]:
        return {
            "strategy_family": self.strategy_family,
            "experiment_id": self.experiment_id,
            "access_mode": self.access_mode,
            "request_start": self.request_start.isoformat(),
            "request_end": self.request_end.isoformat(),
            "dataset_hash": self.dataset_hash,
            "config_hash": self.config_hash,
            "cost_model_hash": self.cost_model_hash,
            "code_hash": self.code_hash,
        }


@dataclass(frozen=True, slots=True)
class ExperimentExecutionState:
    status: str
    run_id: str
    identity_hash: str
    result_path: Path
    result_payload: Mapping[str, object] | None = None
    exit_code: int | None = None
    receipt_hash: str | None = None
    result_hash: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


class ExperimentExecutionDeniedError(ValueError):
    """Raised when a stage was already bound to another immutable identity."""


class ExperimentExecutionIntegrityError(ValueError):
    """Raised when a receipt chain or completed result was modified."""


class ExperimentExecutionLedger:
    """Persist one immutable run identity per experiment and access stage."""

    def __init__(
        self,
        exposure_ledger_path: Path | str | None = None,
    ) -> None:
        self.path = canonical_experiment_execution_ledger_path(
            exposure_ledger_path
        )
        self._key = _key()

    @classmethod
    def for_testing(cls, path: Path | str) -> "ExperimentExecutionLedger":
        """Build an isolated unit-test ledger at an explicit absolute path.

        Production callers must use ``__init__`` so the execution ledger is
        always colocated with, and derived from, the canonical exposure
        ledger.  Keeping this bypass explicitly test-only prevents a caller
        from selecting a fresh execution ledger to repeat a protected stage.
        """

        selected = Path(path).expanduser()
        if not selected.is_absolute():
            raise ValueError("test execution ledger path must be absolute")
        instance = cls.__new__(cls)
        instance.path = selected.resolve()
        instance._key = _key()
        return instance

    def begin(
        self,
        identity: ExperimentExecutionIdentity,
        *,
        run_id: str,
        result_path: Path | str,
    ) -> ExperimentExecutionState:
        identifier = _run_id(run_id)
        output = Path(result_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _process_lock(self.path):
            with self.path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    records = self._read_locked(handle)
                    stage_records = tuple(
                        item
                        for item in records
                        if item["stage_hash"] == identity.stage_hash
                    )
                    if stage_records:
                        return self._resume_existing(
                            stage_records,
                            identity,
                            identifier,
                            output,
                        )
                    if output.exists():
                        raise ExperimentExecutionDeniedError(
                            "refusing to overwrite an untracked experiment result"
                        )
                    self._append_locked(
                        handle,
                        records,
                        event="started",
                        identity=identity,
                        run_id=identifier,
                        result_path=output,
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return ExperimentExecutionState(
            "started", identifier, identity.identity_hash, output
        )

    def complete(
        self,
        identity: ExperimentExecutionIdentity,
        *,
        run_id: str,
        result_path: Path | str,
        payload: Mapping[str, object],
        exit_code: int,
    ) -> dict[str, object]:
        identifier = _run_id(run_id)
        if exit_code not in (0, 1):
            raise ValueError("experiment exit_code must be 0 or 1")
        output = Path(result_path).expanduser().resolve()
        completed_payload = {
            **dict(payload),
            "execution_identity_hash": identity.identity_hash,
            "execution_run_id": identifier,
            "execution_complete": True,
            "execution_exit_code": exit_code,
        }
        result_hash = _hash(completed_payload)
        with _process_lock(self.path):
            with self.path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    records = self._read_locked(handle)
                    stage_records = tuple(
                        item
                        for item in records
                        if item["stage_hash"] == identity.stage_hash
                    )
                    self._require_matching_started(
                        stage_records, identity, identifier, output
                    )
                    completed = next(
                        (
                            item
                            for item in reversed(stage_records)
                            if item["event"] == "completed"
                        ),
                        None,
                    )
                    if completed is not None:
                        if (
                            completed["result_hash"] != result_hash
                            or completed["exit_code"] != exit_code
                        ):
                            raise ExperimentExecutionIntegrityError(
                                "completed execution result does not match its receipt"
                            )
                        persisted = _load_completed_result(
                            output,
                            identity_hash=identity.identity_hash,
                            run_id=identifier,
                            required=True,
                        )
                        if _hash(persisted) != completed["result_hash"]:
                            raise ExperimentExecutionIntegrityError(
                                "completed experiment result hash is invalid"
                            )
                        return completed_payload
                    _atomic_write_json(output, completed_payload)
                    self._append_locked(
                        handle,
                        records,
                        event="completed",
                        identity=identity,
                        run_id=identifier,
                        result_path=output,
                        result_hash=result_hash,
                        exit_code=exit_code,
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return completed_payload

    def verify(self) -> tuple[dict[str, object], ...]:
        if not self.path.exists():
            return ()
        with _process_lock(self.path):
            with self.path.open("r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    return self._read_locked(handle)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def verify_completed(
        self,
        identity: ExperimentExecutionIdentity,
        *,
        run_id: str,
        result_path: Path | str,
    ) -> ExperimentExecutionState:
        """Return one authenticated completed result and its signed receipt.

        Formal evidence must be derived from this method instead of trusting a
        caller-provided result payload.  The receipt chain, immutable stage
        identity, result path, and persisted result hash are checked while the
        execution ledger is held under a shared lock.
        """

        if not isinstance(identity, ExperimentExecutionIdentity):
            raise TypeError("identity must be an ExperimentExecutionIdentity")
        identifier = _run_id(run_id)
        output = Path(result_path).expanduser().resolve()
        if not self.path.exists():
            raise ExperimentExecutionIntegrityError(
                "experiment execution ledger is missing"
            )
        with _process_lock(self.path):
            with self.path.open("r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    records = self._read_locked(handle)
                    stage_records = tuple(
                        item
                        for item in records
                        if item["stage_hash"] == identity.stage_hash
                    )
                    self._require_matching_started(
                        stage_records,
                        identity,
                        identifier,
                        output,
                    )
                    completed = next(
                        (
                            item
                            for item in reversed(stage_records)
                            if item["event"] == "completed"
                        ),
                        None,
                    )
                    if completed is None:
                        raise ExperimentExecutionIntegrityError(
                            "experiment execution is not completed"
                        )
                    payload = _load_completed_result(
                        output,
                        identity_hash=identity.identity_hash,
                        run_id=identifier,
                        required=True,
                    )
                    if _hash(payload) != completed["result_hash"]:
                        raise ExperimentExecutionIntegrityError(
                            "completed experiment result hash is invalid"
                        )
                    return ExperimentExecutionState(
                        "completed",
                        identifier,
                        identity.identity_hash,
                        output,
                        payload,
                        int(completed["exit_code"]),
                        completed["record_hash"],
                        completed["result_hash"],
                    )
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _resume_existing(
        self,
        stage_records,
        identity,
        run_id,
        result_path,
    ) -> ExperimentExecutionState:
        self._require_matching_started(
            stage_records, identity, run_id, result_path
        )
        completed = next(
            (
                item
                for item in reversed(stage_records)
                if item["event"] == "completed"
            ),
            None,
        )
        if completed is None:
            # A result file is not an authenticated completion receipt.  It
            # may be a crash remnant or an attacker-created payload, so only
            # complete() is allowed to append the completed event.
            return ExperimentExecutionState(
                "started", run_id, identity.identity_hash, result_path
            )
        payload = _load_completed_result(
            result_path,
            identity_hash=identity.identity_hash,
            run_id=run_id,
            required=True,
        )
        if _hash(payload) != completed["result_hash"]:
            raise ExperimentExecutionIntegrityError(
                "completed experiment result hash is invalid"
            )
        return ExperimentExecutionState(
            "completed",
            run_id,
            identity.identity_hash,
            result_path,
            payload,
            int(completed["exit_code"]),
            completed["record_hash"],
            completed["result_hash"],
        )

    @staticmethod
    def _require_matching_started(
        stage_records,
        identity,
        run_id,
        result_path,
    ) -> None:
        started = next(
            (item for item in stage_records if item["event"] == "started"),
            None,
        )
        if started is None:
            raise ExperimentExecutionIntegrityError(
                "execution stage has no started receipt"
            )
        if started["identity_hash"] != identity.identity_hash:
            raise ExperimentExecutionDeniedError(
                "experiment stage is already bound to another immutable identity"
            )
        if started["run_id"] != run_id:
            raise ExperimentExecutionDeniedError(
                "experiment stage cannot be repeated with a different run_id"
            )
        if Path(started["result_path"]).resolve() != result_path:
            raise ExperimentExecutionDeniedError(
                "experiment recovery must use the originally bound result path"
            )

    def _read_locked(self, handle) -> tuple[dict[str, object], ...]:
        records = []
        previous = GENESIS_HASH
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise ExperimentExecutionIntegrityError(
                    f"blank execution receipt at line {line_number}"
                )
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ExperimentExecutionIntegrityError(
                    f"invalid execution receipt JSON at line {line_number}"
                ) from error
            if not isinstance(item, dict):
                raise ExperimentExecutionIntegrityError(
                    f"execution receipt at line {line_number} is not an object"
                )
            signature = item.pop("signature", None)
            record_hash = item.pop("record_hash", None)
            if item.get("version") != EXECUTION_LEDGER_VERSION:
                raise ExperimentExecutionIntegrityError(
                    "unsupported execution receipt version"
                )
            if item.get("prev_hash") != previous:
                raise ExperimentExecutionIntegrityError(
                    f"broken execution receipt chain at line {line_number}"
                )
            expected = _hash(item)
            if record_hash != expected or not hmac.compare_digest(
                str(signature), _signature(expected, self._key)
            ):
                raise ExperimentExecutionIntegrityError(
                    f"invalid execution receipt signature at line {line_number}"
                )
            try:
                _validate_unsigned_record(item)
            except (TypeError, ValueError) as error:
                raise ExperimentExecutionIntegrityError(
                    f"invalid execution receipt fields at line {line_number}"
                ) from error
            item["record_hash"] = record_hash
            item["signature"] = signature
            records.append(item)
            previous = record_hash
        return tuple(records)

    def _append_locked(
        self,
        handle,
        records,
        *,
        event,
        identity,
        run_id,
        result_path,
        result_hash=None,
        exit_code=None,
    ):
        unsigned = {
            "version": EXECUTION_LEDGER_VERSION,
            "event": event,
            "stage_hash": identity.stage_hash,
            "identity_hash": identity.identity_hash,
            "identity": identity.as_payload(),
            "run_id": run_id,
            "result_path": str(result_path),
            "result_hash": result_hash,
            "exit_code": exit_code,
            "prev_hash": records[-1]["record_hash"] if records else GENESIS_HASH,
        }
        record_hash = _hash(unsigned)
        record = {
            **unsigned,
            "record_hash": record_hash,
            "signature": _signature(record_hash, self._key),
        }
        handle.seek(0, os.SEEK_END)
        handle.write(_canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return record


def canonical_experiment_execution_ledger_path(
    exposure_ledger_path: Path | str | None,
) -> Path:
    """Derive the one canonical execution-ledger path from Exposure Ledger."""

    value = exposure_ledger_path
    if value is None:
        raw = os.environ.get(DEFAULT_EXPOSURE_LEDGER_PATH_ENV, "")
        if not raw:
            raise ValueError(
                "canonical exposure ledger path is required for execution receipts"
            )
        value = raw
    exposure = Path(value).expanduser()
    if not exposure.is_absolute():
        raise ValueError("canonical exposure ledger path must be absolute")
    return (
        canonical_exposure_ledger_path(exposure).parent
        / EXECUTION_LEDGER_FILENAME
    ).resolve()


def _validate_unsigned_record(item: dict[str, object]) -> None:
    expected_fields = {
        "version",
        "event",
        "stage_hash",
        "identity_hash",
        "identity",
        "run_id",
        "result_path",
        "result_hash",
        "exit_code",
        "prev_hash",
    }
    if set(item) != expected_fields:
        raise ValueError("execution receipt fields are invalid")
    identity_payload = item["identity"]
    if not isinstance(identity_payload, dict):
        raise TypeError("execution identity must be an object")
    if set(identity_payload) != {
        "strategy_family",
        "experiment_id",
        "access_mode",
        "request_start",
        "request_end",
        "dataset_hash",
        "config_hash",
        "cost_model_hash",
        "code_hash",
    }:
        raise ValueError("execution identity fields are invalid")
    identity = ExperimentExecutionIdentity(
        strategy_family=identity_payload["strategy_family"],
        experiment_id=identity_payload["experiment_id"],
        access_mode=identity_payload["access_mode"],
        request_start=date.fromisoformat(identity_payload["request_start"]),
        request_end=date.fromisoformat(identity_payload["request_end"]),
        dataset_hash=identity_payload["dataset_hash"],
        config_hash=identity_payload["config_hash"],
        cost_model_hash=identity_payload["cost_model_hash"],
        code_hash=identity_payload["code_hash"],
    )
    if item["identity_hash"] != identity.identity_hash:
        raise ValueError("execution identity_hash does not match identity")
    if item["stage_hash"] != identity.stage_hash:
        raise ValueError("execution stage_hash does not match identity")
    _run_id(item["run_id"])
    result_path = Path(item["result_path"])
    if not result_path.is_absolute():
        raise ValueError("execution result_path must be absolute")
    _digest(item["prev_hash"], "prev_hash")
    if item["event"] == "started":
        if item["result_hash"] is not None or item["exit_code"] is not None:
            raise ValueError("started execution cannot contain a result")
    elif item["event"] == "completed":
        _digest(item["result_hash"], "result_hash")
        if (
            isinstance(item["exit_code"], bool)
            or not isinstance(item["exit_code"], int)
            or item["exit_code"] not in (0, 1)
        ):
            raise ValueError("completed execution exit_code is invalid")
    else:
        raise ValueError("execution event is invalid")


def _run_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("execution run_id must be a non-empty string")
    return value.strip()


def _process_lock(path: Path) -> threading.Lock:
    identity = path.resolve()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(identity, threading.Lock())


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_completed_result(
    path: Path,
    *,
    identity_hash: str,
    run_id: str,
    required: bool,
) -> dict[str, object] | None:
    if not path.exists():
        if required:
            raise ExperimentExecutionIntegrityError(
                "completed experiment result is missing"
            )
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExperimentExecutionIntegrityError(
            "completed experiment result is unreadable"
        ) from error
    if not isinstance(payload, dict):
        raise ExperimentExecutionIntegrityError(
            "completed experiment result must be a JSON object"
        )
    if not payload.get("execution_complete"):
        if required:
            raise ExperimentExecutionIntegrityError(
                "completed experiment result lacks its completion marker"
            )
        return None
    if (
        payload.get("execution_identity_hash") != identity_hash
        or payload.get("execution_run_id") != run_id
        or payload.get("execution_exit_code") not in (0, 1)
    ):
        raise ExperimentExecutionIntegrityError(
            "completed experiment result identity is invalid"
        )
    return payload


__all__ = (
    "ExperimentExecutionDeniedError",
    "ExperimentExecutionIdentity",
    "ExperimentExecutionIntegrityError",
    "ExperimentExecutionLedger",
    "ExperimentExecutionState",
    "canonical_experiment_execution_ledger_path",
)
