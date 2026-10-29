"""Fixed research windows, purged splits, and one-shot sealed holdouts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import InitVar, asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


VALIDATION_ACCESS_MODES = frozenset(
    {"research", "adaptive_development", "fixed_holdout", "sealed"}
)
EXCLUSIVE_ACCESS_MODES = frozenset({"fixed_holdout", "sealed"})
DEFAULT_EXPOSURE_HMAC_KEY_ENV = "STOCKBOT_EXPOSURE_HMAC_KEY"
LEGACY_EXPOSURE_LEDGER_VERSION = 1
EXPOSURE_LEDGER_VERSION = 2
LEGACY_EXPOSED_SEALED_START = date(2025, 1, 1)
LEGACY_EXPOSED_SEALED_END = date(2026, 6, 30)
FORMAL_SEALED_START = date(2026, 8, 1)
FORMAL_SEALED_END = date(2028, 7, 31)
NORMAL_FORMAL_SEALED_WARMUP_START = date(2023, 8, 1)
NORMAL_FORMAL_SEALED_WARMUP_END = FORMAL_SEALED_START - timedelta(days=1)
DAYTRADE_FORMAL_SEALED_WARMUP_DAYS = 60


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _trusted_utc_now(clock: Callable[[], datetime] | None = None) -> datetime:
    """Read and normalize the internal clock; ``clock`` is a test-only hook."""

    source = _utc_now if clock is None else clock
    if not callable(source):
        raise TypeError("clock must be callable")
    return _aware(source(), "trusted clock").astimezone(timezone.utc)


def _day(value, name):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{name} must be a date")
    return value


def _aware(value, name):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _digest(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _optional_day(value, name):
    if value is None:
        return None
    return _day(value, name)


def _canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _record_digest(payload) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _record_signature(record_hash: str, key: bytes) -> str:
    return hmac.new(key, record_hash.encode("ascii"), hashlib.sha256).hexdigest()


def _environment_hmac_key(name: str = DEFAULT_EXPOSURE_HMAC_KEY_ENV) -> bytes:
    if not isinstance(name, str) or not name:
        raise ValueError("HMAC key environment variable name must be non-empty")
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"{name} must provide the exposure-ledger HMAC key")
    return value.encode("utf-8")


@dataclass(frozen=True, slots=True)
class ValidationProtocol:
    strategy_family: str
    research_start: date
    research_end: date
    walk_forward_start: date
    walk_forward_end: date
    sealed_start: date
    sealed_end: date

    def __post_init__(self):
        if self.strategy_family not in {"normal", "daytrade"}:
            raise ValueError("strategy_family must be normal or daytrade")
        for name in (
            "research_start",
            "research_end",
            "walk_forward_start",
            "walk_forward_end",
            "sealed_start",
            "sealed_end",
        ):
            _day(getattr(self, name), name)
        if not (
            self.research_start <= self.research_end
            < self.walk_forward_start <= self.walk_forward_end
            < self.sealed_start <= self.sealed_end
        ):
            raise ValueError("validation protocol phases must be ordered and disjoint")

    def phase(self, value: date) -> str:
        day = _day(value, "value")
        if self.research_start <= day <= self.research_end:
            return "research"
        if self.walk_forward_start <= day <= self.walk_forward_end:
            return "walk_forward"
        if self.sealed_start <= day <= self.sealed_end:
            return "sealed"
        return "outside"


NORMAL_VALIDATION_PROTOCOL = ValidationProtocol(
    "normal",
    date(2010, 1, 1),
    date(2020, 12, 31),
    date(2021, 1, 1),
    date(2024, 12, 31),
    FORMAL_SEALED_START,
    FORMAL_SEALED_END,
)
DAYTRADE_VALIDATION_PROTOCOL = ValidationProtocol(
    "daytrade",
    date(2019, 1, 1),
    date(2022, 12, 31),
    date(2023, 1, 1),
    date(2024, 12, 31),
    FORMAL_SEALED_START,
    FORMAL_SEALED_END,
)


@dataclass(frozen=True, slots=True)
class ValidationInterval:
    """Inclusive date interval used by the exposure protocol."""

    start: date
    end: date

    def __post_init__(self) -> None:
        start = _day(self.start, "interval.start")
        end = _day(self.end, "interval.end")
        if end < start:
            raise ValueError("interval.end must not precede interval.start")

    def as_payload(self) -> dict[str, str]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat()}


def formal_sealed_warmup_interval(
    strategy_family: str, request_start: date
) -> ValidationInterval:
    """Return the complete, frozen warmup required by formal admission.

    Normal factors require the preregistered three-year PIT history.  The
    intraday experiment requires the 60 calendar days immediately preceding
    its request interval.  Keeping both rules in the shared validation
    protocol prevents the CLI and admission layer from drifting apart.
    """

    protocol = _protocol_for(strategy_family)
    start = _day(request_start, "request_start")
    if start != protocol.sealed_start:
        raise ValueError("formal sealed warmup requires the formal request_start")
    if strategy_family == "normal":
        return ValidationInterval(
            NORMAL_FORMAL_SEALED_WARMUP_START,
            NORMAL_FORMAL_SEALED_WARMUP_END,
        )
    return ValidationInterval(
        start - timedelta(days=DAYTRADE_FORMAL_SEALED_WARMUP_DAYS),
        start - timedelta(days=1),
    )


@dataclass(frozen=True, slots=True)
class ValidationWindowManifest:
    """Immutable identity of both readable and protected validation intervals."""

    strategy_family: str
    mode: str
    request_start: date
    request_end: date
    dataset_hash: str
    config_hash: str
    cost_model_hash: str
    code_hash: str
    warmup_start: date | None = None
    warmup_end: date | None = None
    _allow_legacy_exposed_sealed: InitVar[bool] = False

    def __post_init__(self, _allow_legacy_exposed_sealed: bool):
        protocol = _protocol_for(self.strategy_family)
        if self.mode not in VALIDATION_ACCESS_MODES:
            raise ValueError(
                "mode must be research, adaptive_development, fixed_holdout, or sealed"
            )
        start = _day(self.request_start, "request_start")
        end = _day(self.request_end, "request_end")
        if end < start:
            raise ValueError("request_end must not precede request_start")
        warmup_start = _optional_day(self.warmup_start, "warmup_start")
        warmup_end = _optional_day(self.warmup_end, "warmup_end")
        if (warmup_start is None) != (warmup_end is None):
            raise ValueError("warmup_start and warmup_end must be provided together")
        if warmup_start is not None and warmup_end is not None:
            if warmup_end < warmup_start:
                raise ValueError("warmup_end must not precede warmup_start")
            if warmup_end >= start:
                raise ValueError("warmup_end must precede request_start")
        for name in ("dataset_hash", "config_hash", "cost_model_hash", "code_hash"):
            _digest(getattr(self, name), name)
        if self.mode == "research" and not (
            protocol.research_start <= start <= end <= protocol.research_end
        ):
            raise ValueError("research access must remain inside the research window")
        if self.mode == "adaptive_development" and not (
            protocol.walk_forward_start <= start <= end <= protocol.walk_forward_end
        ):
            raise ValueError(
                "adaptive_development access must remain inside the walk-forward window"
            )
        if self.mode == "fixed_holdout" and end > protocol.walk_forward_end:
            raise ValueError("fixed_holdout access must precede the formal sealed window")
        if self.mode == "sealed" and (
            start != protocol.sealed_start or end != protocol.sealed_end
        ):
            if (
                start == LEGACY_EXPOSED_SEALED_START
                and end == LEGACY_EXPOSED_SEALED_END
            ):
                if _allow_legacy_exposed_sealed is True:
                    return
                raise ValueError(
                    "legacy 2025-01-01..2026-06-30 window is exposed and cannot "
                    "be used for formal sealed admission"
                )
            raise ValueError(
                "sealed access must use the active formal 2026-08-01..2028-07-31 window"
            )

    @property
    def actual_start(self) -> date:
        """Compatibility alias for ``read_interval.start``."""

        return self.read_interval.start

    @property
    def actual_end(self) -> date:
        """Compatibility alias for ``read_interval.end``."""

        return self.read_interval.end

    @property
    def read_interval(self) -> ValidationInterval:
        """Complete interval a permit may read, including feature warmup."""

        boundaries = [self.request_start, self.request_end]
        if self.warmup_start is not None:
            boundaries.append(self.warmup_start)
        if self.warmup_end is not None:
            boundaries.append(self.warmup_end)
        return ValidationInterval(min(boundaries), max(boundaries))

    @property
    def protected_holdout_interval(self) -> ValidationInterval:
        """Experiment interval used for exclusivity and anti-replay checks."""

        return ValidationInterval(self.request_start, self.request_end)

    def as_payload(self) -> dict[str, object]:
        return self.payload_for_ledger_version(EXPOSURE_LEDGER_VERSION)

    def payload_for_ledger_version(self, ledger_version: int) -> dict[str, object]:
        if ledger_version not in {
            LEGACY_EXPOSURE_LEDGER_VERSION,
            EXPOSURE_LEDGER_VERSION,
        }:
            raise ValueError("unsupported exposure ledger version")
        payload: dict[str, object] = {
            "strategy_family": self.strategy_family,
            "mode": self.mode,
            "request_start": self.request_start.isoformat(),
            "request_end": self.request_end.isoformat(),
            "warmup_start": (
                None if self.warmup_start is None else self.warmup_start.isoformat()
            ),
            "warmup_end": (
                None if self.warmup_end is None else self.warmup_end.isoformat()
            ),
            "actual_start": self.actual_start.isoformat(),
            "actual_end": self.actual_end.isoformat(),
            "dataset_hash": self.dataset_hash,
            "config_hash": self.config_hash,
            "cost_model_hash": self.cost_model_hash,
            "code_hash": self.code_hash,
        }
        if ledger_version >= 2:
            payload["read_interval"] = self.read_interval.as_payload()
            payload[
                "protected_holdout_interval"
            ] = self.protected_holdout_interval.as_payload()
        return payload

    def manifest_hash_for_ledger_version(self, ledger_version: int) -> str:
        return _record_digest(self.payload_for_ledger_version(ledger_version))

    @property
    def manifest_hash(self) -> str:
        return self.manifest_hash_for_ledger_version(EXPOSURE_LEDGER_VERSION)


@dataclass(frozen=True, slots=True)
class DataAccessPermit:
    """Signed proof that an exposure-ledger claim was granted."""

    run_id: str
    strategy_family: str
    mode: str
    request_start: date
    request_end: date
    dataset_hash: str
    config_hash: str
    cost_model_hash: str
    code_hash: str
    claimed_at: datetime
    manifest_hash: str
    prev_hash: str
    record_hash: str
    signature: str
    actual_start: date
    actual_end: date
    warmup_start: date | None = None
    warmup_end: date | None = None
    ledger_version: int = EXPOSURE_LEDGER_VERSION

    def __post_init__(self):
        _fixed_holdout_run_id(self.run_id)
        manifest = ValidationWindowManifest(
            strategy_family=self.strategy_family,
            mode=self.mode,
            request_start=self.request_start,
            request_end=self.request_end,
            dataset_hash=self.dataset_hash,
            config_hash=self.config_hash,
            cost_model_hash=self.cost_model_hash,
            code_hash=self.code_hash,
            warmup_start=self.warmup_start,
            warmup_end=self.warmup_end,
        )
        if self.ledger_version not in {
            LEGACY_EXPOSURE_LEDGER_VERSION,
            EXPOSURE_LEDGER_VERSION,
        }:
            raise ValueError("unsupported exposure ledger version")
        _aware(self.claimed_at, "claimed_at")
        if self.actual_start != manifest.actual_start:
            raise ValueError("actual_start must include the complete warmup interval")
        if self.actual_end != manifest.actual_end:
            raise ValueError("actual_end must include the complete warmup interval")
        if self.manifest_hash != manifest.manifest_hash_for_ledger_version(
            self.ledger_version
        ):
            raise ValueError("manifest_hash does not match the validation manifest")
        for name in ("manifest_hash", "prev_hash", "record_hash", "signature"):
            _digest(getattr(self, name), name)
        if self.record_hash != _record_digest(self._unsigned_record_payload()):
            raise ValueError("record_hash does not match the permit payload")

    @property
    def validation_manifest(self) -> ValidationWindowManifest:
        return ValidationWindowManifest(
            strategy_family=self.strategy_family,
            mode=self.mode,
            request_start=self.request_start,
            request_end=self.request_end,
            dataset_hash=self.dataset_hash,
            config_hash=self.config_hash,
            cost_model_hash=self.cost_model_hash,
            code_hash=self.code_hash,
            warmup_start=self.warmup_start,
            warmup_end=self.warmup_end,
        )

    @property
    def read_interval(self) -> ValidationInterval:
        return self.validation_manifest.read_interval

    @property
    def protected_holdout_interval(self) -> ValidationInterval:
        return self.validation_manifest.protected_holdout_interval

    def _unsigned_record_payload(self) -> dict[str, object]:
        return {
            "ledger_version": self.ledger_version,
            "run_id": self.run_id,
            **self.validation_manifest.payload_for_ledger_version(
                self.ledger_version
            ),
            "manifest_hash": self.manifest_hash,
            "claimed_at": self.claimed_at.isoformat(),
            "outcome": "granted",
            "denial_reason": None,
            "prev_hash": self.prev_hash,
        }

    def as_payload(self) -> dict[str, object]:
        return {
            **self._unsigned_record_payload(),
            "record_hash": self.record_hash,
            "signature": self.signature,
        }

    def is_authentic(
        self, *, key_env: str = DEFAULT_EXPOSURE_HMAC_KEY_ENV
    ) -> bool:
        try:
            key = _environment_hmac_key(key_env)
        except ValueError:
            return False
        return hmac.compare_digest(
            self.signature, _record_signature(self.record_hash, key)
        )

    def assert_allows(
        self,
        *,
        dataset_hash: str,
        start: date,
        end: date,
        strategy_family: str,
        key_env: str = DEFAULT_EXPOSURE_HMAC_KEY_ENV,
        _clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Fail closed unless a read is inside this authentic permit."""

        self._assert_identity_allows(
            dataset_hash=dataset_hash,
            start=start,
            end=end,
            strategy_family=strategy_family,
            key_env=key_env,
        )
        try:
            from .exposure import ExposureLedger

            ExposureLedger(key_env=key_env, _clock=_clock).verify_permit(self)
        except (OSError, PermissionError, ValueError) as error:
            raise PermissionError(
                "data access permit is not in the canonical exposure ledger"
            ) from error
        assert_validation_data_released(
            strategy_family=strategy_family,
            start=start,
            end=end,
            permit=self,
            key_env=key_env,
            _clock=_clock,
        )

    def _assert_identity_allows(
        self,
        *,
        dataset_hash: str,
        start: date,
        end: date,
        strategy_family: str,
        key_env: str,
    ) -> None:
        """Validate signed read identity without ledger or release recursion."""

        read_start = _day(start, "start")
        read_end = _day(end, "end")
        if read_end < read_start:
            raise ValueError("end must not precede start")
        _digest(dataset_hash, "dataset_hash")
        if not self.is_authentic(key_env=key_env):
            raise PermissionError("data access permit signature is invalid")
        if dataset_hash != self.dataset_hash:
            raise PermissionError("data access permit dataset_hash does not match")
        if not isinstance(strategy_family, str) or not strategy_family:
            raise PermissionError("data access permit strategy_family is required")
        if strategy_family != self.strategy_family:
            raise PermissionError("data access permit strategy_family does not match")
        if read_start < self.actual_start or read_end > self.actual_end:
            raise PermissionError("requested read is outside the permitted interval")

    def admits(
        self,
        *,
        strategy_family: str,
        start: date,
        end: date,
        dataset_hash: str,
        config_hash: str,
        cost_model_hash: str,
        code_hash: str,
        key_env: str = DEFAULT_EXPOSURE_HMAC_KEY_ENV,
        _clock: Callable[[], datetime] | None = None,
    ) -> bool:
        """Return whether this authentic permit proves the exact sealed identity."""

        try:
            protocol = _protocol_for(strategy_family)
            required_warmup = formal_sealed_warmup_interval(
                strategy_family, protocol.sealed_start
            )
            if not (
                self.mode == "sealed"
                and self.strategy_family == strategy_family
                and self.request_start == _day(start, "start") == protocol.sealed_start
                and self.request_end == _day(end, "end") == protocol.sealed_end
                and self.warmup_start == required_warmup.start
                and self.warmup_end == required_warmup.end
                and self.dataset_hash == dataset_hash
                and self.config_hash == config_hash
                and self.cost_model_hash == cost_model_hash
                and self.code_hash == code_hash
            ):
                return False
            self.assert_allows(
                dataset_hash=dataset_hash,
                start=start,
                end=end,
                strategy_family=strategy_family,
                key_env=key_env,
                _clock=_clock,
            )
        except (OSError, PermissionError, TypeError, ValueError):
            return False
        return True


def assert_validation_clock_released(
    *,
    strategy_family: str,
    start: date,
    end: date,
    _clock: Callable[[], datetime] | None = None,
) -> None:
    """Fail closed until an intersecting formal sealed interval has elapsed.

    This clock-only check is intentionally separate from permit validation so
    callers can perform the required order of operations after release:
    validate the clock, claim the canonical permit, then validate that permit
    before reading any protected data.
    """

    protocol = _protocol_for(strategy_family)
    read_start = _day(start, "start")
    read_end = _day(end, "end")
    if read_end < read_start:
        raise ValueError("end must not precede start")
    if read_end < protocol.sealed_start or read_start > protocol.sealed_end:
        return
    observed = _trusted_utc_now(_clock)
    if observed.date() <= protocol.sealed_end:
        raise PermissionError("formal sealed validation data is not released")


def assert_validation_data_released(
    *,
    strategy_family: str,
    start: date,
    end: date,
    permit: DataAccessPermit | None = None,
    key_env: str = DEFAULT_EXPOSURE_HMAC_KEY_ENV,
    _clock: Callable[[], datetime] | None = None,
) -> None:
    """Require both release time and a canonical permit for sealed reads.

    Non-sealed reads are unaffected.  ``_clock`` exists only for deterministic
    tests; production callers must rely on this module's internal UTC clock.
    A future external release-attestation mechanism may add another trust root,
    but no such proof is synthesized or accepted here.
    """

    read_start = _day(start, "start")
    read_end = _day(end, "end")
    assert_validation_clock_released(
        strategy_family=strategy_family,
        start=read_start,
        end=read_end,
        _clock=_clock,
    )
    protocol = _protocol_for(strategy_family)
    if read_end < protocol.sealed_start or read_start > protocol.sealed_end:
        return
    if permit is None:
        raise PermissionError(
            "released sealed validation data requires a canonical sealed permit"
        )
    if not isinstance(permit, DataAccessPermit):
        raise PermissionError("sealed validation data requires a DataAccessPermit")
    if (
        permit.mode != "sealed"
        or permit.strategy_family != strategy_family
        or permit.request_start != protocol.sealed_start
        or permit.request_end != protocol.sealed_end
    ):
        raise PermissionError(
            "sealed validation data requires the exact formal sealed permit"
        )
    permit._assert_identity_allows(
        dataset_hash=permit.dataset_hash,
        start=read_start,
        end=read_end,
        strategy_family=strategy_family,
        key_env=key_env,
    )
    try:
        from .exposure import ExposureLedger

        ExposureLedger(key_env=key_env, _clock=_clock).verify_permit(permit)
    except (OSError, PermissionError, ValueError) as error:
        raise PermissionError(
            "sealed validation data requires a canonical sealed permit"
        ) from error


@dataclass(frozen=True, slots=True)
class PurgedWalkForwardSplit:
    train_dates: tuple[date, ...]
    purged_dates: tuple[date, ...]
    validation_dates: tuple[date, ...]
    embargoed_dates: tuple[date, ...]


def purged_walk_forward_split(
    dates,
    *,
    validation_start: date,
    validation_end: date,
    purge_days: int,
    embargo_days: int,
) -> PurgedWalkForwardSplit:
    start = _day(validation_start, "validation_start")
    end = _day(validation_end, "validation_end")
    if end < start:
        raise ValueError("validation_end must not precede validation_start")
    for value, name in ((purge_days, "purge_days"), (embargo_days, "embargo_days")):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    ordered = tuple(sorted({_day(value, "dates item") for value in dates}))
    validation = tuple(value for value in ordered if start <= value <= end)
    if not validation:
        raise ValueError("validation window contains no observations")
    purge_start = start - timedelta(days=purge_days)
    embargo_end = end + timedelta(days=embargo_days)
    return PurgedWalkForwardSplit(
        train_dates=tuple(value for value in ordered if value < purge_start),
        purged_dates=tuple(value for value in ordered if purge_start <= value < start),
        validation_dates=validation,
        embargoed_dates=tuple(value for value in ordered if end < value <= embargo_end),
    )


@dataclass(frozen=True, slots=True)
class HoldoutAccessClaim:
    strategy_family: str
    dataset_hash: str
    config_hash: str
    code_hash: str
    claimed_at: str
    sealed_start: str
    sealed_end: str


@dataclass(frozen=True, slots=True)
class FixedHoldoutAccessClaim:
    run_id: str
    strategy_family: str
    holdout_start: str
    holdout_end: str
    manifest_hash: str
    config_hash: str
    code_hash: str
    claimed_at: str


class FixedHoldoutGate:
    """Atomically consume one preregistered, non-formal holdout run."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def claim(
        self,
        *,
        run_id: str,
        strategy_family: str,
        holdout_start: date,
        holdout_end: date,
        manifest_hash: str,
        config_hash: str,
        code_hash: str,
        claimed_at: datetime,
    ) -> FixedHoldoutAccessClaim:
        identifier = _fixed_holdout_run_id(run_id)
        _protocol_for(strategy_family)
        start = _day(holdout_start, "holdout_start")
        end = _day(holdout_end, "holdout_end")
        if end < start:
            raise ValueError("holdout_end must not precede holdout_start")
        claim = FixedHoldoutAccessClaim(
            run_id=identifier,
            strategy_family=strategy_family,
            holdout_start=start.isoformat(),
            holdout_end=end.isoformat(),
            manifest_hash=_digest(manifest_hash, "manifest_hash"),
            config_hash=_digest(config_hash, "config_hash"),
            code_hash=_digest(code_hash, "code_hash"),
            claimed_at=_aware(claimed_at, "claimed_at").isoformat(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{identifier}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(asdict(claim), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise ValueError(f"fixed holdout already claimed for {identifier}") from error
        return claim


class SealedHoldoutGate:
    def __init__(self, root: Path):
        self.root = Path(root)

    def claim(
        self,
        *,
        strategy_family: str,
        dataset_hash: str,
        config_hash: str,
        code_hash: str,
        claimed_at: datetime,
    ) -> HoldoutAccessClaim:
        protocol = _protocol_for(strategy_family)
        observed = _aware(claimed_at, "claimed_at")
        if observed.astimezone(timezone.utc).date() <= protocol.sealed_end:
            raise ValueError("sealed holdout is not complete")
        claim = HoldoutAccessClaim(
            strategy_family=protocol.strategy_family,
            dataset_hash=_digest(dataset_hash, "dataset_hash"),
            config_hash=_digest(config_hash, "config_hash"),
            code_hash=_digest(code_hash, "code_hash"),
            claimed_at=observed.isoformat(),
            sealed_start=protocol.sealed_start.isoformat(),
            sealed_end=protocol.sealed_end.isoformat(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{protocol.strategy_family}.json"
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(asdict(claim), handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError as error:
            raise ValueError(
                f"sealed holdout already claimed for {protocol.strategy_family}"
            ) from error
        return claim


def _protocol_for(strategy_family):
    if strategy_family == "normal":
        return NORMAL_VALIDATION_PROTOCOL
    if strategy_family == "daytrade":
        return DAYTRADE_VALIDATION_PROTOCOL
    raise ValueError("strategy_family must be normal or daytrade")


def _fixed_holdout_run_id(value):
    if (
        not isinstance(value, str)
        or not value
        or any(not (character.isalnum() or character in "-_") for character in value)
    ):
        raise ValueError("run_id must use only letters, numbers, hyphens, and underscores")
    return value


__all__ = (
    "DEFAULT_EXPOSURE_HMAC_KEY_ENV",
    "DAYTRADE_FORMAL_SEALED_WARMUP_DAYS",
    "DAYTRADE_VALIDATION_PROTOCOL",
    "DataAccessPermit",
    "EXCLUSIVE_ACCESS_MODES",
    "EXPOSURE_LEDGER_VERSION",
    "FORMAL_SEALED_END",
    "FORMAL_SEALED_START",
    "FixedHoldoutAccessClaim",
    "FixedHoldoutGate",
    "HoldoutAccessClaim",
    "LEGACY_EXPOSED_SEALED_END",
    "LEGACY_EXPOSED_SEALED_START",
    "LEGACY_EXPOSURE_LEDGER_VERSION",
    "NORMAL_VALIDATION_PROTOCOL",
    "NORMAL_FORMAL_SEALED_WARMUP_END",
    "NORMAL_FORMAL_SEALED_WARMUP_START",
    "PurgedWalkForwardSplit",
    "SealedHoldoutGate",
    "VALIDATION_ACCESS_MODES",
    "ValidationProtocol",
    "ValidationInterval",
    "ValidationWindowManifest",
    "assert_validation_data_released",
    "assert_validation_clock_released",
    "formal_sealed_warmup_interval",
    "purged_walk_forward_split",
)
