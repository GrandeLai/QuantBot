"""Read-only readiness checks for active and audit-only experiment families."""

from __future__ import annotations

import ast
import importlib.util
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

from .data import PointInTimeBundleManifest
from .daytrade_validation_artifact import (
    DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    load_daytrade_adaptive_validation_artifact,
    trusted_daytrade_experiment_keys,
)
from .exposure import (
    ExposureLedgerIntegrityError,
    canonical_exposure_ledger_path,
    verify_exposure_ledger_file,
)
from .normal.adapter_trust import normal_adapter_artifact_identity
from .normal.historical_trials import (
    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    load_normal_adaptive_validation_artifact,
    trusted_normal_trial_keys,
)
from .normal.protocol import NORMAL_EXPERIMENT_WINDOWS
from .validation_protocol import (
    DAYTRADE_VALIDATION_PROTOCOL,
    NORMAL_VALIDATION_PROTOCOL,
    ValidationInterval,
)


DATA_PACKAGES = ("pyarrow", "pandas", "numpy", "duckdb")
NORMAL_ADAPTER_ENV = "STOCKBOT_NORMAL_ADAPTER"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_DAYTRADE_TOP50_POLICY = {
    "as_of": "prior_close",
    "history_sessions": 252,
    "minimum_price": 5.0,
    "adv_window_sessions": 20,
    "minimum_adv_dollars": 5_000_000.0,
    "limit": 50,
    "tie_break": "stable_security_id",
}


@dataclass(frozen=True, slots=True)
class StrategyReadinessReport:
    strategy: str
    access_mode: str
    requested_interval: ValidationInterval
    read_interval: ValidationInterval
    ready_to_claim: bool
    formal_time_eligible: bool
    blocked_reasons: tuple[str, ...]
    deferred_checks: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        return {
            "strategy": self.strategy,
            "access_mode": self.access_mode,
            "requested_interval": self.requested_interval.as_payload(),
            "read_interval": self.read_interval.as_payload(),
            "ready_to_claim": self.ready_to_claim,
            "formal_time_eligible": self.formal_time_eligible,
            "blocked_reasons": list(self.blocked_reasons),
            "deferred_checks": list(self.deferred_checks),
        }


def evaluate_strategy_readiness(
    *,
    strategy: str,
    access_mode: str,
    start: date,
    end: date,
    warmup_start: date | None = None,
    warmup_end: date | None = None,
    bundle_root: str = "",
    adapter_spec: str = "",
    adapter_loader=None,
    universe_manifest: str = "",
    cache_dir: str = "",
    exposure_ledger: str = "",
    environ: Mapping[str, str] | None = None,
    import_checker: Callable[[str], bool] | None = None,
    _clock: Callable[[], datetime] | None = None,
) -> StrategyReadinessReport:
    """Inspect identities and filesystem metadata without claiming or reading bars."""

    if strategy not in {"normal-h1", "normal-h2", "daytrade-h3", "daytrade-h9"}:
        raise ValueError("unsupported readiness strategy")
    if access_mode not in {"research", "adaptive_development", "sealed"}:
        raise ValueError("unsupported readiness access_mode")
    if end < start:
        raise ValueError("readiness end must not precede start")
    source = os.environ if environ is None else environ
    checker = import_checker or _can_import
    observed = (_clock or _utc_now)()
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("readiness clock must be timezone-aware")
    observed_day = observed.astimezone(timezone.utc).date()
    # H2 changes only allocator capital deployment, so it consumes exactly
    # the same normal bundle, adapter and validation windows as H1.
    is_normal = strategy in {"normal-h1", "normal-h2"}
    protocol = (
        NORMAL_VALIDATION_PROTOCOL if is_normal else DAYTRADE_VALIDATION_PROTOCOL
    )
    reasons: list[str] = []
    if strategy == "normal-h2":
        reasons.append("experiment_completed_rejected")
    if strategy == "daytrade-h3" and access_mode != "research":
        reasons.append("h3_research_only")

    if sys.version_info < (3, 11):
        reasons.append("python_3_11_required")
    reasons.extend(
        f"missing_data_package:{name}"
        for name in DATA_PACKAGES
        if not checker(name)
    )
    read_start, read_end = _check_window(
        access_mode,
        start,
        end,
        warmup_start,
        warmup_end,
        protocol,
        observed_day,
        reasons,
    )
    data_root = _check_bundle(
        bundle_root,
        source,
        read_start,
        read_end,
        reasons,
    )
    _check_ledger(exposure_ledger, source, reasons)

    if is_normal:
        _check_normal_adapter(
            adapter_spec,
            adapter_loader,
            access_mode,
            source,
            reasons,
        )
        _check_normal_adaptive_validation_artifact(
            strategy,
            access_mode,
            source,
            reasons,
        )
        deferred_items = [
            "adapter_output_provenance_after_permit",
            "partition_content_hashes_after_permit",
            "external_storage_custody_not_provable_in_process",
        ]
        if strategy == "normal-h1" and access_mode == "sealed":
            deferred_items.append(
                "normal_adaptive_validation_identity_compatibility_before_claim"
            )
        deferred = tuple(deferred_items)
    else:
        _check_daytrade_inputs(
            strategy,
            access_mode,
            observed_day,
            protocol.sealed_end,
            universe_manifest,
            cache_dir,
            data_root,
            source,
            reasons,
        )
        deferred = (
            "minute_data_hash_after_permit",
            "minute_session_coverage_after_permit",
            "external_storage_custody_not_provable_in_process",
        )

    unique_reasons = tuple(sorted(set(reasons)))
    formal_time_eligible = bool(
        access_mode == "sealed" and observed_day > protocol.sealed_end
    )
    return StrategyReadinessReport(
        strategy=strategy,
        access_mode=access_mode,
        requested_interval=ValidationInterval(start, end),
        read_interval=ValidationInterval(read_start, read_end),
        ready_to_claim=not unique_reasons,
        formal_time_eligible=formal_time_eligible,
        blocked_reasons=unique_reasons,
        deferred_checks=deferred,
    )


def _check_window(
    access_mode,
    start,
    end,
    warmup_start,
    warmup_end,
    protocol,
    observed_day,
    reasons,
) -> tuple[date, date]:
    if (warmup_start is None) != (warmup_end is None):
        reasons.append("warmup_interval_incomplete")
    elif warmup_start is not None and warmup_end is not None:
        if warmup_end < warmup_start:
            reasons.append("warmup_interval_invalid")
        if warmup_end >= start:
            reasons.append("warmup_must_precede_request")
    if access_mode == "research" and not (
        protocol.research_start <= start <= end <= protocol.research_end
    ):
        reasons.append("window_outside_research")
    elif access_mode == "adaptive_development" and not (
        protocol.walk_forward_start <= start <= end <= protocol.walk_forward_end
    ):
        reasons.append("window_outside_adaptive_development")
    elif access_mode == "sealed":
        if start != protocol.sealed_start or end != protocol.sealed_end:
            reasons.append("sealed_window_identity_mismatch")
        if observed_day <= protocol.sealed_end:
            reasons.append("sealed_window_not_released")
    expected = {
        "research": (protocol.research_start, protocol.research_end),
        "adaptive_development": (
            protocol.walk_forward_start,
            protocol.walk_forward_end,
        ),
        "sealed": (protocol.sealed_start, protocol.sealed_end),
    }[access_mode]
    if (start, end) != expected:
        reasons.append(f"{protocol.strategy_family}_window_identity_mismatch")
    if protocol.strategy_family == "daytrade":
        expected_warmup = (start - timedelta(days=60), start - timedelta(days=1))
        if warmup_start is None and warmup_end is None:
            reasons.append("daytrade_warmup_required")
        elif (warmup_start, warmup_end) != expected_warmup:
            reasons.append("daytrade_warmup_identity_mismatch")
    else:
        normal_window = NORMAL_EXPERIMENT_WINDOWS[access_mode]
        expected_warmup = (
            normal_window.warmup_start,
            normal_window.warmup_end,
        )
        if warmup_start is None and warmup_end is None:
            reasons.append("normal_warmup_required")
        elif (warmup_start, warmup_end) != expected_warmup:
            reasons.append("normal_warmup_identity_mismatch")
    boundaries = [start, end]
    if warmup_start is not None:
        boundaries.append(warmup_start)
    if warmup_end is not None:
        boundaries.append(warmup_end)
    return min(boundaries), max(boundaries)


def _check_bundle(bundle_root, environ, read_start, read_end, reasons) -> Path | None:
    raw = str(bundle_root or environ.get("STOCKBOT_DATA_ROOT", "") or "").strip()
    if not raw:
        reasons.append("data_root_missing")
        return None
    configured = Path(raw).expanduser()
    if not configured.is_absolute():
        reasons.append("data_root_not_absolute")
        return None
    root = configured.resolve()
    if _is_within(root, _REPOSITORY_ROOT):
        reasons.append("data_root_inside_repository")
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        reasons.append("pit_manifest_missing")
        return root
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = PointInTimeBundleManifest.from_payload(payload)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        reasons.append("pit_manifest_invalid")
        return root
    if read_start < manifest.coverage_start or read_end > manifest.coverage_end:
        reasons.append("pit_manifest_coverage_incomplete")
    for table in manifest.tables.values():
        for partition in table.partitions:
            path = (root / partition.path).resolve()
            if not _is_within(path, root) or not path.is_file():
                reasons.append("pit_partition_missing")
                return root
        if not _partitions_cover(table.partitions, read_start, read_end):
            reasons.append("pit_partition_coverage_incomplete")
    return root


def _check_ledger(exposure_ledger, environ, reasons) -> None:
    canonical = str(environ.get("STOCKBOT_EXPOSURE_LEDGER_PATH", "") or "").strip()
    requested = str(exposure_ledger or canonical).strip()
    canonical_path = None
    requested_path = None
    if not canonical:
        reasons.append("canonical_ledger_missing")
    elif not Path(canonical).expanduser().is_absolute():
        reasons.append("canonical_ledger_not_absolute")
    else:
        canonical_path = canonical_exposure_ledger_path(canonical)
    if requested:
        expanded_requested = Path(requested).expanduser()
        if not expanded_requested.is_absolute():
            reasons.append("exposure_ledger_not_absolute")
        else:
            requested_path = canonical_exposure_ledger_path(expanded_requested)
    if (
        requested_path is not None
        and canonical_path is not None
        and requested_path != canonical_path
    ):
        reasons.append("canonical_ledger_mismatch")
    hmac_key = environ.get("STOCKBOT_EXPOSURE_HMAC_KEY")
    if not hmac_key:
        reasons.append("exposure_hmac_key_missing")
    if canonical_path is not None:
        parent = canonical_path.parent
        if not parent.is_dir():
            reasons.append("canonical_ledger_parent_missing")
        elif not os.access(parent, os.W_OK):
            reasons.append("canonical_ledger_parent_not_writable")
        if hmac_key and canonical_path.exists():
            try:
                verify_exposure_ledger_file(canonical_path, hmac_key=hmac_key)
            except (OSError, TypeError, ValueError, ExposureLedgerIntegrityError):
                reasons.append("canonical_ledger_invalid")


def _check_normal_adapter(spec, loader, access_mode, environ, reasons) -> None:
    raw_spec = str(spec or environ.get(NORMAL_ADAPTER_ENV, "") or "").strip()
    if loader is not None:
        if not callable(loader):
            reasons.append("normal_adapter_not_callable")
        else:
            try:
                normal_adapter_artifact_identity(loader)
            except (OSError, TypeError, ValueError):
                reasons.append("normal_adapter_identity_invalid")
    elif raw_spec:
        module_name, separator, attribute = raw_spec.partition(":")
        if separator != ":" or not module_name or not attribute or "." in attribute:
            reasons.append("normal_adapter_invalid")
        elif not _static_adapter_identity_is_valid(module_name, attribute):
            reasons.append("normal_adapter_static_identity_invalid")
    else:
        reasons.append("normal_adapter_missing")
    if access_mode == "sealed":
        path = str(
            environ.get("STOCKBOT_NORMAL_ADAPTER_ATTESTATION_PATH", "") or ""
        ).strip()
        if not path or not Path(path).expanduser().is_absolute() or not Path(path).is_file():
            reasons.append("normal_adapter_attestation_missing")
        if not environ.get("STOCKBOT_TRUSTED_NORMAL_ADAPTER_KEYS_JSON"):
            reasons.append("trusted_normal_adapter_keys_missing")


def _static_adapter_identity_is_valid(module_name: str, attribute: str) -> bool:
    """Identify a deployment adapter from source without importing its module."""

    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return False
    if spec is None or not spec.origin:
        return False
    path = Path(spec.origin)
    if not path.is_file() or path.suffix != ".py":
        return False
    try:
        source = path.read_bytes()
        tree = ast.parse(source, filename=path.name)
    except (OSError, SyntaxError, ValueError):
        return False
    # Computing the digest is deliberate: readiness proves the source has a
    # stable static identity without executing module-level code.
    if not _sha256(hashlib.sha256(source).hexdigest()):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == attribute:
                return True
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if any(
                (alias.asname or alias.name.rsplit(".", 1)[-1]) == attribute
                for alias in node.names
            ):
                return True
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            if any(
                isinstance(target, ast.Name) and target.id == attribute
                for target in node.targets
            ):
                return True
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == attribute
            and isinstance(node.value, ast.Lambda)
        ):
            return True
    return False


def _check_normal_adaptive_validation_artifact(
    strategy,
    access_mode,
    environ,
    reasons,
) -> None:
    """Verify the signed adaptive survivor required before sealed Normal H1."""

    if strategy != "normal-h1" or access_mode != "sealed":
        return
    raw_path = str(
        environ.get(NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV, "") or ""
    ).strip()
    if not raw_path:
        reasons.append("normal_adaptive_validation_artifact_missing")
        return
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not path.is_file():
        reasons.append("normal_adaptive_validation_artifact_missing")
        return
    try:
        trusted_keys = trusted_normal_trial_keys(environ)
    except (TypeError, ValueError):
        reasons.append("trusted_normal_trial_keys_missing_or_invalid")
        return
    try:
        load_normal_adaptive_validation_artifact(path, trusted_keys=trusted_keys)
    except (OSError, TypeError, ValueError):
        reasons.append("normal_adaptive_validation_artifact_invalid")


def _check_daytrade_inputs(
    strategy,
    access_mode,
    observed_day,
    sealed_end,
    universe_manifest,
    cache_dir,
    data_root,
    environ,
    reasons,
) -> None:
    raw_manifest = str(universe_manifest or "").strip()
    if not raw_manifest or not Path(raw_manifest).expanduser().is_absolute():
        reasons.append("universe_manifest_missing")
        manifest_path = None
    else:
        manifest_path = Path(raw_manifest).expanduser().resolve()
        if not manifest_path.is_file():
            reasons.append("universe_manifest_missing")
    protected = access_mode == "sealed" and observed_day <= sealed_end
    if manifest_path is not None and manifest_path.is_file() and not protected:
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            _check_universe_payload(
                payload,
                strategy,
                access_mode,
                reasons,
            )
        except (OSError, json.JSONDecodeError):
            reasons.append("universe_manifest_invalid")
    raw_cache = str(cache_dir or "").strip()
    if not raw_cache or not Path(raw_cache).expanduser().is_absolute():
        reasons.append("minute_cache_missing")
    else:
        cache_path = Path(raw_cache).expanduser().resolve()
        if not cache_path.is_dir():
            reasons.append("minute_cache_missing")
        elif data_root is None or not _is_within(cache_path, data_root):
            reasons.append("minute_cache_outside_data_root")
    for name, reason in (
        ("STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON", "trusted_calendar_keys_missing"),
        ("STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON", "trusted_universe_keys_missing"),
    ):
        if not environ.get(name):
            reasons.append(reason)
    if strategy == "daytrade-h9":
        _check_daytrade_adaptive_validation_artifact(
            access_mode,
            environ,
            reasons,
        )


def _check_universe_payload(payload, strategy, access_mode, reasons) -> None:
    if not isinstance(payload, dict):
        reasons.append("universe_manifest_invalid")
        return
    if payload.get("selection_policy") != _DAYTRADE_TOP50_POLICY:
        reasons.append("top50_policy_mismatch")
    if not _sha256(payload.get("dataset_hash")):
        reasons.append("universe_dataset_hash_invalid")
    if not _sha256(payload.get("minute_data_hash")):
        reasons.append("minute_data_hash_missing")
    if not isinstance(payload.get("snapshots"), list) or not payload.get("snapshots"):
        reasons.append("universe_snapshots_missing")
    if not isinstance(payload.get("trading_calendar"), dict):
        reasons.append("trading_calendar_evidence_missing")
    if not isinstance(payload.get("construction_evidence"), dict):
        reasons.append("universe_construction_evidence_missing")
    if strategy == "daytrade-h9":
        historical = payload.get("historical_trial_artifact")
        if access_mode == "sealed" and historical is not None:
            reasons.append(
                "sealed_universe_historical_trial_artifact_forbidden"
            )
        elif access_mode != "sealed" and not isinstance(historical, dict):
            reasons.append("historical_trial_artifact_missing")


def _check_daytrade_adaptive_validation_artifact(
    access_mode,
    environ,
    reasons,
) -> None:
    try:
        trusted_keys = trusted_daytrade_experiment_keys(environ)
    except (TypeError, ValueError):
        reasons.append("trusted_daytrade_experiment_keys_missing_or_invalid")
        return
    if access_mode != "sealed":
        return
    raw_path = str(
        environ.get(DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV, "") or ""
    ).strip()
    if not raw_path:
        reasons.append("daytrade_adaptive_validation_artifact_missing")
        return
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or not path.is_file():
        reasons.append("daytrade_adaptive_validation_artifact_missing")
        return
    try:
        load_daytrade_adaptive_validation_artifact(
            path,
            trusted_keys=trusted_keys,
        )
    except (OSError, TypeError, ValueError):
        reasons.append("daytrade_adaptive_validation_artifact_invalid")


def _sha256(value) -> bool:
    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _partitions_cover(partitions, start: date, end: date) -> bool:
    cursor = start
    for partition in partitions:
        if partition.coverage_end < cursor:
            continue
        if partition.coverage_start > cursor:
            return False
        if partition.coverage_end >= end:
            return True
        cursor = partition.coverage_end + timedelta(days=1)
    return False


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _can_import(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = (
    "DATA_PACKAGES",
    "StrategyReadinessReport",
    "evaluate_strategy_readiness",
)
