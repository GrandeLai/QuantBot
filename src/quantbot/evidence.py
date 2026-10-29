"""Evidence recording helpers for completion audits."""

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .evidence_auth import (
    STRATEGY_ADMISSION_REQUIREMENTS,
    append_json_line_idempotently,
    append_json_line_locked,
)


UNAVAILABLE_PROVENANCE = "unavailable"
UNSPECIFIED_RUN_TYPE = "unspecified"


def hash_payload(payload: Any) -> str:
    """Return a stable SHA-256 for a non-secret, JSON-compatible payload."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_hash_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_tree_hash(root: Optional[Path] = None) -> str:
    """Hash source file paths and bytes without relying on Git metadata."""

    source_root = Path(root) if root is not None else Path(__file__).resolve().parent
    digest = hashlib.sha256()
    excluded_parts = {".git", "__pycache__", ".pytest_cache", ".mypy_cache"}
    files = [
        path
        for path in source_root.rglob("*")
        if path.is_file()
        and not excluded_parts.intersection(path.relative_to(source_root).parts)
        and not path.name.endswith((".pyc", ".pyo"))
        and not any(part.endswith(".egg-info") for part in path.relative_to(source_root).parts)
    ]
    for path in sorted(files, key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _json_hash_default(value: Any):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError("unsupported value for stable hash: %s" % type(value).__name__)


class EvidenceRecorder:
    def __init__(self, path: Path):
        self.path = Path(path)

    def record(
        self,
        requirement_id: str,
        passed: bool,
        detail: str,
        *,
        run_type: str = UNSPECIFIED_RUN_TYPE,
        observed_at: Optional[str] = None,
        dataset_hash: Optional[str] = None,
        config_hash: Optional[str] = None,
        cost_model_hash: Optional[str] = None,
        code_hash: Optional[str] = None,
        policy_version: Optional[str] = None,
        input_materialization_hash: Optional[str] = None,
        output_hash: Optional[str] = None,
        report_hash: Optional[str] = None,
        cost_suite_hash: Optional[str] = None,
        run_id: Optional[str] = None,
        experiment_id: Optional[str] = None,
        idempotency_fields: tuple[str, ...] | None = None,
    ) -> bool:
        if requirement_id in STRATEGY_ADMISSION_REQUIREMENTS:
            raise ValueError(
                "formal strategy admission evidence must be materialized from "
                "an authenticated completed execution receipt"
            )
        if input_materialization_hash is not None:
            _require_sha256(
                input_materialization_hash,
                "input_materialization_hash",
            )
            if dataset_hash is None:
                dataset_hash = input_materialization_hash
        for value, name in (
            (output_hash, "output_hash"),
            (report_hash, "report_hash"),
            (cost_suite_hash, "cost_suite_hash"),
        ):
            if value is not None:
                _require_sha256(value, name)
        for value, name in ((run_id, "run_id"), (experiment_id, "experiment_id")):
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"{name} must be a non-empty string")
        observed_at = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "requirement_id": requirement_id,
            "passed": passed,
            "detail": detail,
            "run_type": run_type,
            "observed_at": observed_at,
            "dataset_hash": dataset_hash or UNAVAILABLE_PROVENANCE,
            "config_hash": config_hash or UNAVAILABLE_PROVENANCE,
            "code_hash": code_hash or source_tree_hash(),
        }
        if cost_model_hash is not None:
            payload["cost_model_hash"] = cost_model_hash
        if policy_version is not None:
            payload["policy_version"] = policy_version
        for name, value in (
            ("input_materialization_hash", input_materialization_hash),
            ("output_hash", output_hash),
            ("report_hash", report_hash),
            ("cost_suite_hash", cost_suite_hash),
            ("run_id", None if run_id is None else run_id.strip()),
            (
                "experiment_id",
                None if experiment_id is None else experiment_id.strip(),
            ),
        ):
            if value is not None:
                payload[name] = value
        if idempotency_fields is not None:
            return append_json_line_idempotently(
                self.path,
                payload,
                identity_fields=idempotency_fields,
            )
        append_json_line_locked(self.path, payload)
        return True


def _require_sha256(value: str, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value
