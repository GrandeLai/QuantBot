"""Day-trading CLI parsing, data loading, and payload helpers."""

import json
import os
import hmac
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from ..audit import RequirementEvidence
from ..daytrade import (
    DayTradeBacktester,
    DayTradeConfig,
    DayTradeMinuteCache,
    daytrade_config_to_payload,
    minute_bar_from_row,
    minute_bar_to_row,
)
from ..daytrade_strategy import (
    DEFAULT_DAYTRADE_SECTOR_PROXIES,
    DailyUniverseSnapshot,
    IntradayFeatureBuildConfig,
    IntradayFeatureRequirements,
    intraday_feature_config_to_payload,
)
from ..data.pit_calendar import PointInTimeTradingCalendar, TradingSession
from ..evidence import hash_payload, source_tree_hash
from ..exposure import (
    DEFAULT_EXPOSURE_LEDGER_PATH_ENV,
    ExposureLedger,
    canonical_exposure_ledger_path,
)
from ..historical_trials import (
    HISTORICAL_TRIAL_ARTIFACT_VERSION,
    validate_historical_trial_execution_proofs,
)
from ..validation_protocol import (
    DataAccessPermit,
    ValidationWindowManifest,
    assert_validation_clock_released,
    assert_validation_data_released,
)
from .support import (
    alpaca_bars_url,
    parse_date,
    parse_symbols,
    resolve_alpaca_feed,
    resolve_runtime_config,
)


DAYTRADE_TOP50_SELECTION_POLICY = MappingProxyType(
    {
        "as_of": "prior_close",
        "history_sessions": 252,
        "minimum_price": 5.0,
        "adv_window_sessions": 20,
        "minimum_adv_dollars": 5_000_000.0,
        "limit": 50,
        "tie_break": "stable_security_id",
    }
)


@dataclass(frozen=True)
class DayTradeUniverseManifest:
    """Verified CLI representation of a daily PIT universe manifest."""

    path: Path
    dataset_hash: str
    manifest_hash: str
    snapshots: tuple[DailyUniverseSnapshot, ...]
    suspended_sessions: frozenset[tuple[date, str]]
    selection_policy: Mapping[str, object] | None = None
    minute_data_hash: str | None = None
    calendar_source: str | None = None
    calendar_hash: str | None = None
    calendar_sessions: tuple[date, ...] = ()
    trading_calendar: PointInTimeTradingCalendar | None = None
    construction_evidence: Mapping[str, object] | None = None
    historical_trial_artifact: Mapping[str, object] | None = None

    def schedule(self, start: date, end: date) -> tuple[DailyUniverseSnapshot, ...]:
        return tuple(
            snapshot
            for snapshot in self.snapshots
            if start <= snapshot.trading_day <= end
        )


@dataclass(frozen=True)
class DayTradeDataAccess:
    """Symbols, daily identities, and the permit used by one CLI invocation."""

    symbols: tuple[str, ...]
    universe_schedule: tuple[DailyUniverseSnapshot, ...] | None = None
    permit: DataAccessPermit | None = None
    universe_manifest: DayTradeUniverseManifest | None = None
    frozen_cache_required: bool = False
    declared_minute_data_hash: str | None = None
    historical_trial_artifact: Mapping[str, object] | None = None

    def session_dates(self, start: date, end: date) -> tuple[date, ...] | None:
        if self.universe_manifest is None:
            return None
        return tuple(
            snapshot.trading_day
            for snapshot in self.universe_manifest.schedule(start, end)
        )

    def active_sessions(
        self,
        start: date,
        end: date,
    ) -> Mapping[str, tuple[date, ...]]:
        if self.universe_manifest is None:
            return MappingProxyType({})
        suspended = self.universe_manifest.suspended_sessions
        values: dict[str, list[date]] = {}
        for snapshot in self.universe_manifest.schedule(start, end):
            for ticker in snapshot.active_tickers:
                values.setdefault(ticker, [])
                if (snapshot.trading_day, ticker) not in suspended:
                    values[ticker].append(snapshot.trading_day)
        return MappingProxyType(
            {symbol: tuple(sorted(days)) for symbol, days in values.items()}
        )


def daytrade_config_from_args(args) -> DayTradeConfig:
    return DayTradeConfig(
        account_equity=args.account_equity,
        capital_fraction=args.capital_fraction,
        per_trade_risk_fraction=args.per_trade_risk_fraction,
        daily_loss_limit_fraction=args.daily_loss_limit_fraction,
        max_trades_per_day=args.max_trades_per_day,
        max_concurrent_positions=args.max_concurrent_positions,
        max_losing_trades_per_day=args.max_losing_trades_per_day,
        max_consecutive_losing_trades_per_day=args.max_consecutive_losing_trades_per_day,
        volume_multiplier=args.volume_multiplier,
        min_relative_volume=args.min_relative_volume,
        cost_bps=args.cost_bps,
        max_volume_participation=args.max_volume_participation,
        symbol_filter=args.symbol_filter,
        max_active_symbols=args.max_active_symbols,
        min_opening_range_pct=args.min_opening_range_pct,
        min_opening_dollar_volume=args.min_opening_dollar_volume,
        require_positive_opening_momentum=args.require_positive_opening_momentum,
        entry_end=args.entry_end,
        breakeven_after_r=args.breakeven_after_r,
        trailing_stop_after_r=args.trailing_stop_after_r,
        max_holding_minutes=args.max_holding_minutes,
        vwap_exit_bars=args.vwap_exit_bars,
        min_directional_spy_return=args.min_directional_spy_return,
        min_directional_qqq_return=args.min_directional_qqq_return,
        min_directional_sector_strength=args.min_directional_sector_strength,
        dynamic_risk_sizing=args.dynamic_risk_sizing,
        max_daytrade_drawdown=args.max_drawdown,
        min_soak_sessions=args.min_sessions,
    )


def prepare_daytrade_data_access(
    args,
    config: DayTradeConfig,
    *,
    config_identity: object | None = None,
    cost_identity: object | None = None,
    warmup_start: date | None = None,
) -> DayTradeDataAccess:
    """Claim dynamic PIT data before any cache or market-data read.

    Static ``--symbols`` remains a development-smoke path and deliberately
    does not create formal validation evidence.
    """

    _require_sealed_h9_command(args)
    start = parse_date(args.start)
    end = parse_date(args.end)
    assert_validation_clock_released(
        strategy_family="daytrade",
        start=warmup_start or start,
        end=end,
    )
    manifest_path = str(getattr(args, "universe_manifest", "") or "").strip()
    if not manifest_path:
        return DayTradeDataAccess(symbols=parse_daytrade_symbols(args.symbols, config))
    access_mode = str(getattr(args, "access_mode", "") or "").strip()

    requested_warmup = getattr(args, "warmup_start", "")
    if requested_warmup:
        pinned_warmup = parse_date(requested_warmup)
        if warmup_start is not None and pinned_warmup != warmup_start:
            raise ValueError(
                "warmup_start must equal the feature loader's actual warmup start"
            )
        warmup_start = pinned_warmup
    actual_start = warmup_start or start
    universe = load_daytrade_universe_manifest(
        Path(manifest_path),
        request_start=start,
        request_end=end,
        actual_start=actual_start,
        allow_historical_trial_artifact=access_mode != "sealed",
    )
    schedule = universe.schedule(start, end)
    if not schedule:
        raise ValueError("universe manifest has no snapshots in the requested interval")
    symbols = tuple(
        sorted(
            {
                ticker
                for snapshot in universe.schedule(actual_start, end)
                for ticker in snapshot.active_tickers
            }
        )
    )
    if not symbols:
        raise ValueError("universe manifest has no active tickers in the read interval")

    ledger_path = str(getattr(args, "exposure_ledger", "") or "").strip()
    run_id = str(getattr(args, "run_id", "") or "").strip()
    missing = [
        name
        for name, value in (
            ("--exposure-ledger", ledger_path),
            ("--run-id", run_id),
            ("--access-mode", access_mode),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "dynamic universe access requires " + ", ".join(missing)
        )
    frozen_cache_required = _requires_frozen_dynamic_data(
        config_identity,
        access_mode,
    )
    if frozen_cache_required:
        _require_frozen_dynamic_identity(args, universe, actual_start=actual_start)
    evaluation_dataset_hash = _dynamic_evaluation_dataset_hash(universe)
    _require_canonical_ledger(ledger_path)
    _require_external_dynamic_cache(args)

    computed_config_hash = hash_payload(
        config_identity
        if config_identity is not None
        else daytrade_config_to_payload(config)
    )
    computed_cost_hash = hash_payload(
        cost_identity
        if cost_identity is not None
        else {
            "cost_bps": config.cost_bps,
            "max_volume_participation": config.max_volume_participation,
        }
    )
    config_hash = _pinned_hash(args, "config_hash", computed_config_hash)
    cost_model_hash = _pinned_hash(args, "cost_model_hash", computed_cost_hash)
    code_hash = _pinned_hash(args, "code_hash", source_tree_hash())
    requested_dataset_hash = str(getattr(args, "dataset_hash", "") or "").strip()
    if requested_dataset_hash and requested_dataset_hash != evaluation_dataset_hash:
        raise ValueError("dataset_hash does not match the universe manifest")

    validation_manifest = ValidationWindowManifest(
        strategy_family="daytrade",
        mode=access_mode,
        request_start=start,
        request_end=end,
        warmup_start=warmup_start,
        warmup_end=(start - timedelta(days=1)) if warmup_start is not None else None,
        dataset_hash=evaluation_dataset_hash,
        config_hash=config_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
    )
    permit = ExposureLedger(Path(ledger_path)).claim(
        validation_manifest,
        run_id=run_id,
    )
    assert_validation_data_released(
        strategy_family="daytrade",
        start=actual_start,
        end=end,
        permit=permit,
    )
    permit.assert_allows(
        dataset_hash=evaluation_dataset_hash,
        start=actual_start,
        end=end,
        strategy_family="daytrade",
    )
    return DayTradeDataAccess(
        symbols=symbols,
        universe_schedule=schedule,
        permit=permit,
        universe_manifest=universe,
        frozen_cache_required=frozen_cache_required,
        declared_minute_data_hash=universe.minute_data_hash,
        historical_trial_artifact=(
            {
                **dict(universe.historical_trial_artifact),
                "permit_dataset_hash": evaluation_dataset_hash,
            }
            if universe.historical_trial_artifact is not None
            else None
        ),
    )


def _require_sealed_h9_command(args) -> None:
    """Reserve the one-shot DayTrade sealed interval for frozen H3-vs-H9."""

    if str(getattr(args, "access_mode", "") or "").strip() != "sealed":
        return
    if (
        getattr(args, "command", "") != "daytrade-optimize-run"
        or getattr(args, "experiment_set", "") != "h9-rvol-reward-risk"
    ):
        raise PermissionError(
            "sealed DayTrade access is reserved for the single frozen "
            "H3-vs-H9 optimization run"
        )


def claim_daytrade_static_data_access(
    args,
    config: DayTradeConfig,
    *,
    dataset_hash: str,
    config_identity: object,
    cost_identity: object,
    warmup_start: date | None = None,
    minute_data_hash: str | None = None,
) -> DayTradeDataAccess:
    """Claim a canonical fixed/sealed comparison before loading static data."""

    start = parse_date(args.start)
    end = parse_date(args.end)
    assert_validation_clock_released(
        strategy_family="daytrade",
        start=warmup_start or start,
        end=end,
    )
    ledger_path = str(getattr(args, "exposure_ledger", "") or "").strip()
    run_id = str(getattr(args, "run_id", "") or "").strip()
    access_mode = str(getattr(args, "access_mode", "") or "").strip()
    if not ledger_path or not run_id or not access_mode:
        raise ValueError(
            "holdout access requires --exposure-ledger, --run-id, and --access-mode"
        )
    if access_mode not in {"fixed_holdout", "sealed"}:
        raise ValueError("holdout comparison access_mode must be fixed_holdout or sealed")
    frozen_cache_required = access_mode == "sealed"
    if frozen_cache_required:
        if not str(getattr(args, "cache_dir", "") or "").strip():
            raise ValueError("sealed daytrade access requires a frozen cache")
        minute_data_hash = _sha256_digest(
            minute_data_hash,
            "minute_data_hash",
        )
    _require_canonical_ledger(ledger_path)
    dataset_hash = _sha256_digest(dataset_hash, "dataset_hash")
    source_dataset_hash = dataset_hash
    if minute_data_hash is not None:
        dataset_hash = hash_payload(
            {
                "source_dataset_hash": source_dataset_hash,
                "minute_data_hash": minute_data_hash,
            }
        )
    requested_dataset_hash = str(getattr(args, "dataset_hash", "") or "").strip()
    if requested_dataset_hash and requested_dataset_hash != dataset_hash:
        raise ValueError("dataset_hash does not match the holdout manifest")
    config_hash = _pinned_hash(
        args,
        "config_hash",
        hash_payload(config_identity),
    )
    cost_model_hash = _pinned_hash(
        args,
        "cost_model_hash",
        hash_payload(cost_identity),
    )
    code_hash = _pinned_hash(args, "code_hash", source_tree_hash())
    manifest = ValidationWindowManifest(
        strategy_family="daytrade",
        mode=access_mode,
        request_start=start,
        request_end=end,
        warmup_start=warmup_start,
        warmup_end=(start - timedelta(days=1)) if warmup_start is not None else None,
        dataset_hash=dataset_hash,
        config_hash=config_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
    )
    permit = ExposureLedger(Path(ledger_path)).claim(
        manifest,
        run_id=run_id,
    )
    assert_validation_data_released(
        strategy_family="daytrade",
        start=warmup_start or start,
        end=end,
        permit=permit,
    )
    return DayTradeDataAccess(
        symbols=parse_daytrade_symbols(args.symbols, config),
        permit=permit,
        frozen_cache_required=frozen_cache_required,
        declared_minute_data_hash=minute_data_hash,
    )


def _requires_frozen_dynamic_data(config_identity, access_mode: str) -> bool:
    identity = config_identity if isinstance(config_identity, Mapping) else {}
    experiment_set = identity.get("experiment_set")
    candidate_name = str(identity.get("candidate_name", ""))
    preregistered_candidate = candidate_name.startswith(
        (
            "daytrade-h3-pre-entry-confirmation:",
            "daytrade-h9-rvol-reward-risk:",
        )
    )
    return access_mode == "sealed" or preregistered_candidate or experiment_set in {
        "h3-rvol",
        "h9-rvol-reward-risk",
    }


def _dynamic_evaluation_dataset_hash(universe: DayTradeUniverseManifest) -> str:
    return hash_payload(
        {
            "source_dataset_hash": universe.dataset_hash,
            "universe_manifest_hash": universe.manifest_hash,
            "minute_data_hash": universe.minute_data_hash or "unfrozen",
            "calendar_hash": universe.calendar_hash or "unverified",
            "construction_evidence_hash": (
                universe.construction_evidence.get("content_hash")
                if universe.construction_evidence is not None
                else "unverified"
            ),
            "historical_trial_artifact_hash": (
                universe.historical_trial_artifact.get("content_hash")
                if universe.historical_trial_artifact is not None
                else "missing"
            ),
        }
    )


def _require_frozen_dynamic_identity(
    args,
    universe: DayTradeUniverseManifest,
    *,
    actual_start: date,
) -> None:
    if not str(getattr(args, "cache_dir", "") or "").strip():
        raise ValueError("H3/H9 or sealed dynamic access requires a frozen cache")
    if dict(universe.selection_policy or {}) != dict(DAYTRADE_TOP50_SELECTION_POLICY):
        raise ValueError("universe manifest does not bind the exact Top50 selection policy")
    if universe.minute_data_hash is None:
        raise ValueError("universe manifest requires minute_data_hash")
    if universe.calendar_hash is None or not universe.calendar_sessions:
        raise ValueError("formal dynamic universe requires independent trading_calendar evidence")
    if universe.construction_evidence is None:
        raise ValueError("formal dynamic universe requires PIT construction evidence")
    _require_complete_calendar_coverage(args, universe, actual_start=actual_start)


def _require_complete_calendar_coverage(
    args,
    universe: DayTradeUniverseManifest,
    *,
    actual_start: date,
) -> None:
    start = actual_start
    end = parse_date(args.end)
    if universe.trading_calendar is None:
        raise ValueError("formal dynamic universe requires a verified trading calendar")
    universe.trading_calendar.require_complete_coverage(start, end)
    sessions = tuple(day for day in universe.calendar_sessions if start <= day <= end)
    snapshot_days = tuple(
        snapshot.trading_day
        for snapshot in universe.snapshots
        if start <= snapshot.trading_day <= end
    )
    if sessions != snapshot_days:
        raise ValueError("universe snapshots must cover every independent calendar session")
    if not sessions or (sessions[0] - start).days > 4 or (end - sessions[-1]).days > 4:
        raise ValueError("independent trading calendar does not span the requested interval")
    if any((right - left).days > 4 for left, right in zip(sessions, sessions[1:])):
        raise ValueError("independent trading calendar contains an implausible session gap")


def _require_canonical_ledger(ledger_path: str) -> None:
    canonical = os.environ.get(DEFAULT_EXPOSURE_LEDGER_PATH_ENV, "").strip()
    if not canonical:
        raise ValueError(
            f"{DEFAULT_EXPOSURE_LEDGER_PATH_ENV} must pin the canonical exposure ledger"
        )
    if canonical_exposure_ledger_path(canonical) != canonical_exposure_ledger_path(
        ledger_path
    ):
        raise ValueError("data access must use the canonical exposure ledger")


def _require_external_dynamic_cache(args) -> None:
    cache_dir = str(getattr(args, "cache_dir", "") or "").strip()
    if not cache_dir:
        return
    raw_root = os.environ.get("STOCKBOT_DATA_ROOT", "").strip()
    if not raw_root:
        raise ValueError(
            "STOCKBOT_DATA_ROOT is required for dynamic daytrade cache data"
        )
    data_root = Path(raw_root).expanduser().resolve()
    repository_root = Path(__file__).resolve().parents[3]
    try:
        data_root.relative_to(repository_root)
    except ValueError:
        pass
    else:
        raise ValueError("STOCKBOT_DATA_ROOT must be outside the repository")
    cache_path = Path(cache_dir).expanduser().resolve()
    try:
        cache_path.relative_to(data_root)
    except ValueError as error:
        raise ValueError(
            "dynamic daytrade cache_dir must be inside STOCKBOT_DATA_ROOT"
        ) from error


def load_daytrade_universe_manifest(
    path: Path,
    *,
    request_start: date,
    request_end: date,
    actual_start: date | None = None,
    allow_historical_trial_artifact: bool = True,
) -> DayTradeUniverseManifest:
    """Load a vendor-neutral JSON daily universe and fail closed on gaps."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("universe manifest must be a JSON object")
    dataset_hash = _sha256_digest(payload.get("dataset_hash"), "dataset_hash")
    raw_snapshots = payload.get("snapshots")
    if not isinstance(raw_snapshots, list) or not raw_snapshots:
        raise ValueError("universe manifest snapshots must be a non-empty array")
    snapshots = tuple(_daily_universe_snapshot(item) for item in raw_snapshots)
    days = tuple(snapshot.trading_day for snapshot in snapshots)
    if len(set(days)) != len(days):
        raise ValueError("universe manifest contains duplicate trading days")
    if days != tuple(sorted(days)):
        raise ValueError("universe manifest snapshots must be sorted by trading_day")
    for snapshot in snapshots:
        if not snapshot.active_tickers:
            raise ValueError("daily universe snapshot must not be empty")
        if len(snapshot.active_tickers) > 50:
            raise ValueError("dynamic daytrade universe supports at most 50 daily members")

    first_read = actual_start or request_start
    relevant = tuple(day for day in days if first_read <= day <= request_end)
    if not relevant:
        raise ValueError("universe manifest does not cover the actual read interval")
    requested = tuple(day for day in days if request_start <= day <= request_end)
    if not requested:
        raise ValueError("universe manifest does not cover the requested trade interval")

    declared_sessions = payload.get("expected_sessions")
    if declared_sessions is not None:
        declared = tuple(
            sorted(
                {
                    date.fromisoformat(value)
                    for value in _json_string_array(
                        declared_sessions,
                        "expected_sessions",
                    )
                    if first_read <= date.fromisoformat(value) <= request_end
                }
            )
        )
        if tuple(relevant) != declared:
            raise ValueError(
                "universe manifest is missing a declared daily snapshot"
            )

    suspended = _universe_suspensions(payload.get("suspensions", []), snapshots)
    selection_policy = payload.get("selection_policy")
    if selection_policy is not None and not isinstance(selection_policy, dict):
        raise ValueError("universe manifest selection_policy must be a JSON object")
    minute_data_hash = payload.get("minute_data_hash")
    if minute_data_hash is not None:
        minute_data_hash = _sha256_digest(minute_data_hash, "minute_data_hash")
    (
        calendar_source,
        calendar_hash,
        calendar_sessions,
        trading_calendar,
    ) = _trading_calendar_evidence(payload.get("trading_calendar"))
    construction_evidence = _universe_construction_evidence(
        payload.get("construction_evidence"),
        snapshots,
        selection_policy=selection_policy,
        source_dataset_hash=dataset_hash,
        calendar_hash=calendar_hash,
    )
    raw_historical_trial_artifact = payload.get("historical_trial_artifact")
    if raw_historical_trial_artifact is not None and not allow_historical_trial_artifact:
        raise ValueError(
            "sealed H9 universe must not contain a same-period historical_trial_artifact"
        )
    historical_trial_artifact = _historical_trial_artifact(
        raw_historical_trial_artifact,
        source_dataset_hash=dataset_hash,
        minute_data_hash=minute_data_hash,
        calendar_hash=calendar_hash,
        construction_evidence=construction_evidence,
    )
    return DayTradeUniverseManifest(
        path=path,
        dataset_hash=dataset_hash,
        manifest_hash=hash_payload(payload),
        snapshots=snapshots,
        suspended_sessions=frozenset(suspended),
        selection_policy=(
            MappingProxyType(dict(selection_policy))
            if selection_policy is not None
            else None
        ),
        minute_data_hash=minute_data_hash,
        calendar_source=calendar_source,
        calendar_hash=calendar_hash,
        calendar_sessions=calendar_sessions,
        trading_calendar=trading_calendar,
        construction_evidence=construction_evidence,
        historical_trial_artifact=historical_trial_artifact,
    )


def _trading_calendar_evidence(payload):
    if payload is None:
        return None, None, (), None
    if not isinstance(payload, dict):
        raise ValueError("trading_calendar must be a JSON object")
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list):
        raise ValueError("trading_calendar sessions must be an array")
    try:
        sessions = tuple(
            TradingSession(
                session_date=date.fromisoformat(item["session_date"]),
                close_at=datetime.fromisoformat(
                    item["close_at"].replace("Z", "+00:00")
                ),
            )
            for item in raw_sessions
        )
        calendar = PointInTimeTradingCalendar(
            exchange=payload["exchange"],
            effective_date_timezone=payload["effective_date_timezone"],
            issuer=payload["issuer"],
            issuer_version=payload["issuer_version"],
            generated_at=datetime.fromisoformat(
                payload["generated_at"].replace("Z", "+00:00")
            ),
            coverage_start=date.fromisoformat(payload["coverage_start"]),
            coverage_end=date.fromisoformat(payload["coverage_end"]),
            missing_intervals=tuple(payload.get("missing_intervals", ())),
            sessions=sessions,
            content_hash=payload["content_hash"],
            signature=payload["signature"],
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("trading_calendar is not a valid signed PIT calendar") from error
    calendar.require_trusted(
        _trusted_hmac_keys("STOCKBOT_TRUSTED_CALENDAR_KEYS_JSON")
    )
    if calendar.missing_intervals:
        raise ValueError("signed trading_calendar contains missing coverage intervals")
    return (
        f"{calendar.issuer}:{calendar.issuer_version}",
        calendar.content_hash,
        tuple(item.session_date for item in calendar.sessions),
        calendar,
    )


def _universe_construction_evidence(
    payload,
    snapshots,
    *,
    selection_policy,
    source_dataset_hash,
    calendar_hash,
):
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("construction_evidence must be a JSON object")
    proof = dict(payload)
    signature = _sha256_digest(
        proof.pop("signature", None),
        "construction_evidence.signature",
    )
    content_hash = _sha256_digest(
        proof.pop("content_hash", None),
        "construction_evidence.content_hash",
    )
    if proof.get("proof_type") != "stockbot_pit_bundle_core_v1":
        raise ValueError("construction_evidence must come from the PIT bundle core")
    expected_identity = {
        "source_dataset_hash": source_dataset_hash,
        "calendar_hash": calendar_hash,
        "selection_policy_hash": hash_payload(selection_policy),
    }
    mismatches = [
        name
        for name, value in expected_identity.items()
        if proof.get(name) != value
    ]
    if mismatches:
        raise ValueError(
            "construction_evidence identity mismatch: " + ",".join(mismatches)
        )
    _sha256_digest(proof.get("input_rows_hash"), "construction_evidence.input_rows_hash")
    ranked_hash = _sha256_digest(
        proof.get("ranked_snapshots_hash"),
        "construction_evidence.ranked_snapshots_hash",
    )
    if ranked_hash != _ranked_snapshots_hash(snapshots):
        raise ValueError("construction_evidence ranked snapshots do not match the manifest")
    if content_hash != hash_payload(proof):
        raise ValueError("construction_evidence content_hash mismatch")
    issuer = proof.get("issuer")
    key = _trusted_hmac_keys("STOCKBOT_TRUSTED_UNIVERSE_KEYS_JSON").get(issuer)
    if key is None:
        raise ValueError("trusted PIT universe proof issuer key is missing")
    expected_signature = hmac.new(
        key,
        content_hash.encode("ascii"),
        sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise ValueError("PIT universe construction proof signature is invalid")
    return MappingProxyType(
        {**proof, "content_hash": content_hash, "signature": signature}
    )


def _trusted_hmac_keys(environment_name: str) -> dict[str, bytes]:
    raw = os.environ.get(environment_name, "").strip()
    if not raw:
        raise ValueError(f"{environment_name} must configure trusted issuer keys")
    try:
        payload = json.loads(raw)
        keys = {
            str(issuer): bytes.fromhex(value)
            for issuer, value in payload.items()
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"{environment_name} must be a JSON object of hex keys") from error
    if not keys or any(len(key) < 16 for key in keys.values()):
        raise ValueError(f"{environment_name} keys must contain at least 16 bytes")
    return keys


def _ranked_snapshots_hash(snapshots) -> str:
    return hash_payload(
        [
            {
                "trading_day": snapshot.trading_day.isoformat(),
                "ticker_by_security_id": dict(snapshot.ticker_by_security_id),
            }
            for snapshot in snapshots
        ]
    )


def _historical_trial_artifact(
    payload,
    *,
    source_dataset_hash,
    minute_data_hash,
    calendar_hash,
    construction_evidence,
):
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("historical_trial_artifact must be a JSON object")
    artifact = dict(payload)
    signature = _sha256_digest(
        artifact.pop("signature", None),
        "historical_trial_artifact.signature",
    )
    content_hash = _sha256_digest(
        artifact.pop("content_hash", None),
        "historical_trial_artifact.content_hash",
    )
    expected_identity = {
        "source_dataset_hash": source_dataset_hash,
        "minute_data_hash": minute_data_hash,
        "calendar_hash": calendar_hash,
        "construction_evidence_hash": (
            construction_evidence.get("content_hash")
            if construction_evidence is not None
            else None
        ),
    }
    historical_evaluation_dataset_hash = hash_payload(expected_identity)
    expected_identity["historical_evaluation_dataset_hash"] = (
        historical_evaluation_dataset_hash
    )
    mismatches = [
        name
        for name, value in expected_identity.items()
        if artifact.get(name) != value
    ]
    if mismatches:
        raise ValueError(
            "historical_trial_artifact dataset identity mismatch: "
            + ",".join(mismatches)
        )
    if artifact.get("artifact_version") != HISTORICAL_TRIAL_ARTIFACT_VERSION:
        raise ValueError(
            "historical_trial_artifact artifact_version must be "
            f"{HISTORICAL_TRIAL_ARTIFACT_VERSION}"
        )
    if content_hash != hash_payload(artifact):
        raise ValueError("historical_trial_artifact content_hash mismatch")
    issuer = artifact.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("historical trial artifact requires issuer")
    trusted_keys = _trusted_hmac_keys("STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON")
    key = trusted_keys.get(issuer)
    if key is None:
        raise ValueError("trusted historical experiment issuer key is missing")
    expected_signature = hmac.new(
        key,
        content_hash.encode("ascii"),
        sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        raise ValueError("historical trial artifact signature is invalid")
    completed = artifact.get("completed_trial_ids")
    execution_evidence = artifact.get("trial_execution_evidence")
    if (
        not isinstance(completed, list)
        or any(not isinstance(trial_id, str) or not trial_id for trial_id in completed)
        or len(completed) != len(set(completed))
        or not isinstance(execution_evidence, dict)
        or set(execution_evidence) != set(completed)
    ):
        raise ValueError(
            "historical_trial_artifact requires execution evidence for every trial"
        )
    observation_dates = artifact.get("observation_dates")
    trial_daily_returns = artifact.get("trial_daily_returns")
    if not isinstance(observation_dates, list) or not isinstance(
        trial_daily_returns, dict
    ):
        raise ValueError(
            "historical_trial_artifact requires observation_dates and trial_daily_returns"
        )
    validate_historical_trial_execution_proofs(
        execution_evidence,
        completed_trial_ids=completed,
        experiment_by_trial=None,
        observation_dates=observation_dates,
        trial_daily_returns=trial_daily_returns,
        historical_dataset_hash=historical_evaluation_dataset_hash,
        trusted_keys=trusted_keys,
    )
    return MappingProxyType(
        {
            **artifact,
            "content_hash": content_hash,
            "signature": signature,
        }
    )


def _daily_universe_snapshot(payload: object) -> DailyUniverseSnapshot:
    if not isinstance(payload, dict):
        raise ValueError("daily universe snapshot must be a JSON object")
    raw_day = payload.get("trading_day", payload.get("day", payload.get("date")))
    if not isinstance(raw_day, str):
        raise ValueError("daily universe snapshot requires trading_day")
    mapping = payload.get("ticker_by_security_id")
    if mapping is None:
        members = payload.get("members")
        if not isinstance(members, list):
            raise ValueError(
                "daily universe snapshot requires ticker_by_security_id or members"
            )
        mapping = {}
        for member in members:
            if not isinstance(member, dict):
                raise ValueError("daily universe member must be a JSON object")
            security_id = member.get("security_id")
            ticker = member.get("ticker")
            if security_id in mapping:
                raise ValueError("duplicate security_id in daily universe members")
            mapping[security_id] = ticker
    return DailyUniverseSnapshot(date.fromisoformat(raw_day), mapping)


def _universe_suspensions(payload, snapshots) -> set[tuple[date, str]]:
    if not isinstance(payload, list):
        raise ValueError("universe manifest suspensions must be an array")
    by_day = {snapshot.trading_day: snapshot for snapshot in snapshots}
    output: set[tuple[date, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("universe suspension must be a JSON object")
        raw_day = item.get("trading_day", item.get("day"))
        security_id = item.get("security_id")
        if not isinstance(raw_day, str) or not isinstance(security_id, str):
            raise ValueError("universe suspension requires trading_day and security_id")
        trading_day = date.fromisoformat(raw_day)
        snapshot = by_day.get(trading_day)
        if snapshot is None or security_id not in snapshot.ticker_by_security_id:
            raise ValueError("universe suspension must reference an active stable security_id")
        output.add((trading_day, snapshot.ticker_for(security_id)))
    return output


def _json_string_array(payload, name: str) -> tuple[str, ...]:
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ValueError(f"universe manifest {name} must be a string array")
    return tuple(payload)


def _sha256_digest(value, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _pinned_hash(args, attribute: str, computed: str) -> str:
    requested = str(getattr(args, attribute, "") or "").strip()
    if requested and requested != computed:
        raise ValueError(f"{attribute} does not match the actual CLI identity")
    return computed


def run_daytrade_from_args(
    args,
    environ,
    injected_loader,
    injected_session_loader=None,
    data_access: DayTradeDataAccess | None = None,
):
    config = daytrade_config_from_args(args)
    access = data_access or prepare_daytrade_data_access(args, config)
    bars, source_name = load_daytrade_bars_from_args(
        args,
        environ,
        injected_loader,
        config,
        injected_session_loader,
        data_access=access,
    )
    run_kwargs = {}
    if access.universe_schedule is not None:
        run_kwargs["universe_schedule"] = access.universe_schedule
    return (
        DayTradeBacktester(config).run(bars, **run_kwargs),
        source_name,
        minute_bars_hash(bars),
    )


def load_daytrade_bars_from_args(
    args,
    environ,
    injected_loader,
    config: DayTradeConfig,
    injected_session_loader=None,
    data_access: DayTradeDataAccess | None = None,
):
    access = data_access or prepare_daytrade_data_access(args, config)
    symbols = access.symbols
    start = parse_date(args.start)
    end = parse_date(args.end)
    return load_daytrade_bars_for_symbols_from_args(
        args,
        environ,
        injected_loader,
        symbols,
        start,
        end,
        injected_session_loader,
        data_access=access,
    )


def load_daytrade_bars_for_symbols_from_args(
    args,
    environ,
    injected_loader,
    symbols,
    start: date,
    end: date,
    injected_session_loader=None,
    data_access: DayTradeDataAccess | None = None,
):
    normalized_symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols))
    if not normalized_symbols or any(not symbol for symbol in normalized_symbols):
        raise ValueError("daytrade data symbols must not be empty")
    if start > end:
        raise ValueError("daytrade data start must not be after end")
    assert_validation_data_released(
        strategy_family="daytrade",
        start=start,
        end=end,
        permit=(data_access.permit if data_access is not None else None),
    )
    if getattr(args, "universe_manifest", "") and data_access is None:
        raise PermissionError(
            "dynamic universe data requires a claimed DataAccessPermit"
        )
    if data_access is not None and data_access.permit is not None:
        data_access.permit.assert_allows(
            dataset_hash=data_access.permit.dataset_hash,
            start=start,
            end=end,
            strategy_family="daytrade",
        )
    dynamic_sessions = (
        data_access.session_dates(start, end)
        if data_access is not None
        else None
    )
    active_sessions = (
        data_access.active_sessions(start, end)
        if data_access is not None
        else MappingProxyType({})
    )
    cache_dir = getattr(args, "cache_dir", "")
    expected_sessions = None
    missing = []
    if cache_dir:
        cache_source_name = (
            "injected"
            if injected_loader
            else daytrade_cache_source_name(args, environ)
        )
        expected_sessions = dynamic_sessions or resolve_daytrade_expected_sessions(
            args,
            environ,
            injected_session_loader,
            cache_source_name,
            start,
            end,
        )
        if dynamic_sessions is not None:
            _require_schedule_matches_independent_sessions(
                args,
                environ,
                injected_session_loader,
                cache_source_name,
                start,
                end,
                dynamic_sessions,
            )
        cache = DayTradeMinuteCache(Path(cache_dir), cache_source_name)
        if expected_sessions is not None:
            missing = _missing_cache_partitions(
                cache,
                normalized_symbols,
                start,
                end,
                expected_sessions,
                active_sessions,
            )
            if not missing:
                bars = cache.read(normalized_symbols, start, end)
                verified = _verify_declared_minute_data(
                    _filter_and_validate_dynamic_bars(
                        bars,
                        normalized_symbols,
                        start,
                        end,
                        active_sessions,
                    ),
                    data_access,
                )
                return verified, "cache:%s" % cache_source_name

        if data_access is not None and data_access.frozen_cache_required:
            if expected_sessions is None:
                raise ValueError(
                    "frozen daytrade cache requires declared expected sessions"
                )
            missing_detail = ",".join("%s:%s" % item for item in missing)
            raise ValueError(
                "frozen daytrade cache is incomplete; API fallback forbidden: "
                + missing_detail
            )

    loader, source_name = resolve_daytrade_minute_loader(args, environ, injected_loader)
    if cache_dir and expected_sessions is not None:
        if source_name != cache_source_name:
            raise ValueError("daytrade cache source changed during load")
        missing_by_month = {}
        for symbol, month in missing:
            missing_by_month.setdefault(month, set()).add(symbol)
        for month_start, month_end in _calendar_month_windows(start, end):
            month = "%04d-%02d" % (month_start.year, month_start.month)
            missing_symbols = missing_by_month.get(month)
            if not missing_symbols:
                continue
            requested_symbols = tuple(
                symbol for symbol in normalized_symbols if symbol in missing_symbols
            )
            rows = loader(
                symbols=requested_symbols,
                start=month_start,
                end=month_end,
            )
            requested_symbol_set = set(requested_symbols)
            bars = [
                bar
                for bar in (minute_bar_from_row(row) for row in rows)
                if bar.symbol.upper() in requested_symbol_set
                and month_start <= _market_day(bar.timestamp) <= month_end
                and _bar_is_expected_for_dynamic_universe(bar, active_sessions)
            ]
            month_sessions = [
                value
                for value in expected_sessions
                if month_start <= value <= month_end
            ]
            _write_cache_partitions(
                cache,
                bars,
                requested_symbols,
                month_start,
                month_end,
                month_sessions,
                active_sessions,
            )
            remaining = _missing_cache_partitions(
                cache,
                requested_symbols,
                month_start,
                month_end,
                month_sessions,
                active_sessions,
            )
            if remaining:
                raise ValueError(
                    "incomplete daytrade cache month: %s"
                    % ",".join("%s:%s" % item for item in remaining)
                )
        bars = cache.read(normalized_symbols, start, end)
        verified = _verify_declared_minute_data(
            _filter_and_validate_dynamic_bars(
                bars,
                normalized_symbols,
                start,
                end,
                active_sessions,
            ),
            data_access,
        )
        return verified, "cache:%s" % source_name

    rows = loader(symbols=normalized_symbols, start=start, end=end)
    bars = _filter_and_validate_dynamic_bars(
        [minute_bar_from_row(row) for row in rows],
        normalized_symbols,
        start,
        end,
        active_sessions,
    )
    if cache_dir:
        manifest_sessions = list(dynamic_sessions or sorted({_market_day(bar.timestamp) for bar in bars}))
        cache = DayTradeMinuteCache(Path(cache_dir), source_name)
        _write_cache_partitions(
            cache,
            bars,
            normalized_symbols,
            start,
            end,
            manifest_sessions,
            active_sessions,
        )
        return (
            _filter_and_validate_dynamic_bars(
                cache.read(normalized_symbols, start, end),
                normalized_symbols,
                start,
                end,
                active_sessions,
            ),
            "cache:%s" % source_name,
        )
    return _verify_declared_minute_data(bars, data_access), source_name


def _verify_declared_minute_data(bars, data_access):
    if data_access is None or data_access.declared_minute_data_hash is None:
        return bars
    actual = minute_bars_hash(bars)
    if actual != data_access.declared_minute_data_hash:
        raise ValueError("minute_data_hash does not match the actual frozen bars")
    return bars


def _calendar_month_windows(start: date, end: date):
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        if cursor.month == 12:
            next_month = date(cursor.year + 1, 1, 1)
        else:
            next_month = date(cursor.year, cursor.month + 1, 1)
        yield max(start, cursor), min(end, next_month - timedelta(days=1))
        cursor = next_month


def _market_day(timestamp: datetime) -> date:
    return timestamp.astimezone(ZoneInfo("US/Eastern")).date()


def _bar_is_expected_for_dynamic_universe(bar, active_sessions) -> bool:
    sessions = active_sessions.get(bar.symbol.upper())
    return sessions is None or _market_day(bar.timestamp) in sessions


def _filter_and_validate_dynamic_bars(
    bars,
    symbols,
    start: date,
    end: date,
    active_sessions,
):
    if not active_sessions:
        return list(bars)
    requested = set(symbols)
    filtered = [
        bar
        for bar in bars
        if bar.symbol.upper() in requested
        and start <= _market_day(bar.timestamp) <= end
        and _bar_is_expected_for_dynamic_universe(bar, active_sessions)
    ]
    observed = {
        (bar.symbol.upper(), _market_day(bar.timestamp))
        for bar in filtered
    }
    missing = sorted(
        (symbol, trading_day)
        for symbol in symbols
        for trading_day in active_sessions.get(symbol, ())
        if (symbol, trading_day) not in observed
    )
    if missing:
        raise ValueError(
            "incomplete dynamic universe minute data: "
            + ",".join(
                f"{symbol}:{trading_day.isoformat()}"
                for symbol, trading_day in missing
            )
        )
    return filtered


def _symbol_expected_sessions(
    symbol: str,
    expected_sessions,
    active_sessions,
):
    if symbol in active_sessions:
        return tuple(active_sessions[symbol])
    return tuple(expected_sessions)


def _missing_cache_partitions(
    cache,
    symbols,
    start,
    end,
    expected_sessions,
    active_sessions,
):
    if not active_sessions:
        return cache.missing_months(
            symbols,
            start,
            end,
            expected_sessions=expected_sessions,
        )
    missing = []
    for symbol in symbols:
        symbol_sessions = _symbol_expected_sessions(
            symbol,
            expected_sessions,
            active_sessions,
        )
        for month_start, month_end in _calendar_month_windows(start, end):
            month_sessions = tuple(
                item
                for item in symbol_sessions
                if month_start <= item <= month_end
            )
            if not month_sessions:
                continue
            missing.extend(
                cache.missing_months(
                    (symbol,),
                    month_start,
                    month_end,
                    expected_sessions=month_sessions,
                )
            )
    return sorted(set(missing))


def _write_cache_partitions(
    cache,
    bars,
    symbols,
    start,
    end,
    expected_sessions,
    active_sessions,
):
    if not active_sessions:
        return cache.write_all(
            bars,
            request_start=start,
            request_end=end,
            expected_sessions=expected_sessions,
        )
    written = []
    for symbol in symbols:
        symbol_sessions = _symbol_expected_sessions(
            symbol,
            expected_sessions,
            active_sessions,
        )
        if not symbol_sessions:
            continue
        symbol_bars = [bar for bar in bars if bar.symbol.upper() == symbol]
        written.extend(
            cache.write_all(
                symbol_bars,
                request_start=start,
                request_end=end,
                expected_sessions=symbol_sessions,
            )
        )
    return written


def _require_schedule_matches_independent_sessions(
    args,
    environ,
    injected_session_loader,
    source_name,
    start,
    end,
    schedule_sessions,
):
    has_independent_calendar = (
        getattr(args, "expected_sessions_json", None) is not None
        or injected_session_loader is not None
        or str(source_name).lower().startswith("alpaca")
    )
    if not has_independent_calendar:
        return
    expected = resolve_daytrade_expected_sessions(
        args,
        environ,
        injected_session_loader,
        source_name,
        start,
        end,
    )
    if expected is not None and tuple(expected) != tuple(schedule_sessions):
        raise ValueError(
            "universe manifest trading days do not match the independent calendar"
        )


def load_daytrade_feature_inputs_from_args(
    args,
    environ,
    injected_loader,
    config: DayTradeConfig,
    injected_session_loader=None,
    data_access: DayTradeDataAccess | None = None,
    feature_requirements: IntradayFeatureRequirements | None = None,
):
    start = parse_date(args.start)
    end = parse_date(args.end)
    warmup_days = getattr(args, "feature_warmup_days", 60)
    if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or warmup_days <= 0:
        raise ValueError("feature_warmup_days must be a positive integer")
    warmup_start = start - timedelta(days=warmup_days)
    access = data_access or prepare_daytrade_data_access(
        args,
        config,
        warmup_start=warmup_start,
    )
    trade_symbols = access.symbols
    feature_config = daytrade_feature_config_from_args(
        args,
        trade_symbols,
        requirements=feature_requirements,
    )
    feature_bars, source_name = load_daytrade_bars_for_symbols_from_args(
        args,
        environ,
        injected_loader,
        feature_config.required_symbols,
        warmup_start,
        end,
        injected_session_loader,
        data_access=access,
    )
    trade_symbol_set = (
        {
            ticker
            for snapshot in access.universe_schedule
            for ticker in snapshot.active_tickers
        }
        if access.universe_schedule is not None
        else set(trade_symbols)
    )
    eastern = ZoneInfo("US/Eastern")
    trade_bars = [
        bar
        for bar in feature_bars
        if bar.symbol.upper() in trade_symbol_set
        and start <= bar.timestamp.astimezone(eastern).date() <= end
    ]
    return (
        trade_bars,
        feature_bars,
        source_name,
        feature_config,
        warmup_start,
    )


def daytrade_feature_config_from_args(
    args,
    symbols,
    *,
    requirements: IntradayFeatureRequirements | None = None,
) -> IntradayFeatureBuildConfig:
    proxies = dict(DEFAULT_DAYTRADE_SECTOR_PROXIES)
    raw_mapping = getattr(args, "sector_proxies_json", "")
    if raw_mapping:
        payload = json.loads(raw_mapping)
        if not isinstance(payload, dict):
            raise ValueError("sector_proxies_json must be a JSON object")
        proxies.update(payload)
    entry_end = _parse_daytrade_clock(args.entry_end, "entry_end")
    requested_observation_end = getattr(args, "feature_observation_end", "")
    observation_end = (
        _parse_daytrade_clock(
            requested_observation_end,
            "feature_observation_end",
        )
        if requested_observation_end
        else max(time(11, 0), entry_end)
    )
    if observation_end < entry_end:
        raise ValueError("feature_observation_end must not be before entry_end")
    return IntradayFeatureBuildConfig(
        symbols=tuple(symbols),
        sector_proxy_by_symbol=proxies,
        observation_start=time(9, 45),
        observation_end=observation_end,
        requirements=requirements or IntradayFeatureRequirements.all(),
    )


def _parse_daytrade_clock(value, name: str) -> time:
    try:
        hour, minute = str(value).split(":", 1)
        return time(int(hour), int(minute))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must use HH:MM") from error


def resolve_daytrade_expected_sessions(
    args,
    environ,
    injected_loader,
    source_name: str,
    start: date,
    end: date,
):
    raw_override = getattr(args, "expected_sessions_json", None)
    if raw_override is not None:
        return normalize_daytrade_sessions(json.loads(raw_override), start, end)
    if injected_loader is not None:
        return normalize_daytrade_sessions(
            injected_loader(start=start, end=end),
            start,
            end,
        )
    if not str(source_name).lower().startswith("alpaca"):
        return None

    from ..integrations.alpaca import AlpacaTradingClient

    env = environ or os.environ
    api_key = env.get("ALPACA_API_KEY")
    secret_key = env.get("ALPACA_SECRET_KEY")
    if not api_key or not secret_key:
        return None
    client = AlpacaTradingClient(
        api_key=api_key,
        secret_key=secret_key,
        base_url=env.get("ALPACA_ENDPOINT", "https://paper-api.alpaca.markets"),
    )
    return normalize_daytrade_sessions(
        client.fetch_calendar(start=start, end=end),
        start,
        end,
    )


def normalize_daytrade_sessions(values, start: date, end: date):
    if not isinstance(values, (list, tuple)):
        raise ValueError("expected sessions must be a JSON array or date sequence")
    sessions = set()
    for value in values:
        if isinstance(value, datetime):
            session = value.date()
        elif isinstance(value, date):
            session = value
        elif isinstance(value, str):
            session = date.fromisoformat(value)
        else:
            raise ValueError("expected session must be an ISO date")
        if start <= session <= end:
            sessions.add(session)
    return sorted(sessions)


def daytrade_cache_source_name(args, environ) -> str:
    env = environ or os.environ
    data_source = args.data_source
    if data_source == "auto":
        if env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY"):
            data_source = "alpaca"
        elif env.get("POLYGON_API_KEY"):
            data_source = "polygon"
    if data_source == "alpaca":
        feed = resolve_alpaca_feed(
            args, env, config=resolve_runtime_config(args, env), minute=True
        )
        return "alpaca_minute_bars:%s" % feed if feed else "alpaca_minute_bars"
    if data_source == "polygon":
        return "polygon_minute_bars"
    return "auto"


def resolve_daytrade_minute_loader(args, environ, injected_loader):
    if injected_loader:
        return injected_loader, "injected"

    env = environ or os.environ
    data_source = args.data_source
    if data_source == "auto":
        if env.get("ALPACA_API_KEY") and env.get("ALPACA_SECRET_KEY"):
            data_source = "alpaca"
        elif env.get("POLYGON_API_KEY"):
            data_source = "polygon"
        else:
            raise ValueError(
                "ALPACA_API_KEY/ALPACA_SECRET_KEY or POLYGON_API_KEY is "
                "required for daytrade minute data"
            )

    if data_source == "alpaca":
        from ..integrations.alpaca import AlpacaDataClient

        api_key = env.get("ALPACA_API_KEY")
        secret_key = env.get("ALPACA_SECRET_KEY")
        if not api_key or not secret_key:
            raise ValueError(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY are required for "
                "Alpaca minute data"
            )
        base_url = alpaca_bars_url(
            args.alpaca_data_url
            or env.get("ALPACA_DATA_ENDPOINT")
            or "https://data.alpaca.markets"
        )
        feed = resolve_alpaca_feed(
            args, env, config=resolve_runtime_config(args, env), minute=True
        )
        client = AlpacaDataClient(
            api_key=api_key,
            secret_key=secret_key,
            base_url=base_url,
            feed=feed,
        )
        suffix = ":%s" % feed if feed else ""
        return client.fetch_minute_bars, "alpaca_minute_bars%s" % suffix

    from ..integrations.polygon import PolygonClient

    api_key = env.get("POLYGON_API_KEY")
    if not api_key:
        raise ValueError("POLYGON_API_KEY is required for Polygon minute data")
    client = PolygonClient(
        api_key=api_key,
        base_url=args.polygon_url or env.get("POLYGON_ENDPOINT", "https://api.polygon.io"),
    )

    def load_from_polygon(symbols, start, end):
        rows = []
        for symbol in symbols:
            rows.extend(client.fetch_minute_bars(symbol, start, end))
        return rows

    return load_from_polygon, "polygon_minute_bars"


def daytrade_result_payload(
    result,
    source_name: str,
    evidence: RequirementEvidence,
    logs: dict,
) -> dict:
    return {
        "strategy": result.strategy,
        "source": source_name,
        "passed": evidence.passed,
        "requirement_id": evidence.requirement_id,
        "sessions": len(result.daily_results),
        "trade_count": result.trade_count,
        "initial_capital": result.initial_capital,
        "ending_equity": result.ending_equity,
        "cumulative_return": result.cumulative_return,
        "annual_return": result.annual_return,
        "sharpe": result.sharpe,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "average_holding_minutes": result.average_holding_minutes,
        "logs": logs,
    }


def daytrade_data_access_payload(access: DayTradeDataAccess) -> dict:
    sealed_data_access = bool(
        access.permit is not None and access.permit.mode == "sealed"
    )
    payload = {
        "dynamic_universe": access.universe_schedule is not None,
        "universe_snapshot_count": len(access.universe_schedule or ()),
        "universe_symbols": list(access.symbols),
        "sealed_data_access": sealed_data_access,
        "frozen_cache_required": access.frozen_cache_required,
        "formal_admission_eligible": False,
    }
    if access.universe_manifest is not None:
        payload.update(
            {
                "universe_manifest_hash": access.universe_manifest.manifest_hash,
                "source_dataset_hash": access.universe_manifest.dataset_hash,
                "dataset_hash": (
                    access.permit.dataset_hash
                    if access.permit is not None
                    else _dynamic_evaluation_dataset_hash(access.universe_manifest)
                ),
                "selection_policy": dict(
                    access.universe_manifest.selection_policy or {}
                ),
                "declared_minute_data_hash": (
                    access.universe_manifest.minute_data_hash
                ),
                "trading_calendar_source": access.universe_manifest.calendar_source,
                "trading_calendar_hash": access.universe_manifest.calendar_hash,
                "construction_evidence_hash": (
                    access.universe_manifest.construction_evidence.get("content_hash")
                    if access.universe_manifest.construction_evidence is not None
                    else None
                ),
                "historical_trial_artifact_hash": (
                    access.universe_manifest.historical_trial_artifact.get("content_hash")
                    if access.universe_manifest.historical_trial_artifact is not None
                    else None
                ),
            }
        )
    elif access.declared_minute_data_hash is not None:
        payload["declared_minute_data_hash"] = access.declared_minute_data_hash
    if access.permit is not None:
        payload["data_access_permit"] = access.permit.as_payload()
    return payload


def daytrade_optimization_payload(
    result,
    source_name: str,
    evidence: RequirementEvidence,
    feature_metadata: dict | None = None,
) -> dict:
    statistical_validation = dict(result.statistical_validation)
    # Raw fills/equity are retained on the in-memory optimization result so a
    # sealed handler can build the canonical formal artifact.  They are not a
    # presentation metric and must not bloat development result files.
    statistical_validation.pop("canonical_execution_inputs", None)
    payload = {
        "source": source_name,
        "passed": evidence.passed,
        "requirement_id": evidence.requirement_id,
        "best_config_name": result.best_config_name,
        "best_config": daytrade_config_to_payload(result.best_config),
        "fragile": result.fragile,
        "baseline": _daytrade_summary_payload(result.baseline),
        "validation": _daytrade_summary_payload(result.validation),
        "rankings": result.rankings,
        "windows": result.windows,
        "declared_trial_count": result.declared_trial_count,
        "completed_trial_count": result.completed_trial_count,
        "executed_trial_ids": list(result.executed_trial_ids),
        "feature_coverage_complete": result.feature_coverage_complete,
        "statistical_validation": statistical_validation,
        "statistical_validation_hash": hash_payload(statistical_validation),
    }
    if feature_metadata:
        payload.update(feature_metadata)
    return payload


def daytrade_feature_metadata(
    feature_config: IntradayFeatureBuildConfig,
    feature_bars,
    feature_snapshots,
    warmup_start: date,
) -> dict:
    snapshot_payload = [
        {
            "symbol": item.symbol,
            "observed_at": item.observed_at,
            "atr": item.atr,
            "relative_volume": item.relative_volume,
            "gap_fraction": item.gap_fraction,
            "premarket_volume": item.premarket_volume,
            "spy_return": item.spy_return,
            "qqq_return": item.qqq_return,
            "sector_relative_strength": item.sector_relative_strength,
            "max_abs_correlation": item.max_abs_correlation,
        }
        for item in feature_snapshots
    ]
    return {
        "feature_config": intraday_feature_config_to_payload(feature_config),
        "feature_warmup_start": warmup_start.isoformat(),
        "feature_bar_count": len(feature_bars),
        "feature_data_hash": minute_bars_hash(feature_bars),
        "feature_snapshot_count": len(feature_snapshots),
        "feature_snapshot_hash": hash_payload(snapshot_payload),
    }


def _daytrade_summary_payload(result) -> dict:
    return {
        "sessions": len(result.daily_results),
        "trade_count": result.trade_count,
        "cumulative_return": result.cumulative_return,
        "annual_return": result.annual_return,
        "sharpe": result.sharpe,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
    }


def write_json_file(path: str, payload: dict) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def parse_daytrade_symbols(raw_symbols: str, config: DayTradeConfig):
    symbols = parse_symbols(raw_symbols)
    duplicates = sorted({symbol for symbol in symbols if symbols.count(symbol) > 1})
    if duplicates:
        raise ValueError("daytrade symbols must be unique: %s" % ",".join(duplicates))
    if len(symbols) > config.max_symbols:
        raise ValueError(
            "daytrade_intraday supports at most %d stock symbols; got %d"
            % (config.max_symbols, len(symbols))
        )
    return symbols


def daytrade_runs_hash(daily_results, trades) -> str:
    return hash_payload(
        {
            "daily_results": [
                {
                    "day": item.day,
                    "trades": item.trades,
                    "pnl": item.pnl,
                    "ending_equity": item.ending_equity,
                    "errors": item.errors,
                }
                for item in daily_results
            ],
            "trades": [
                {
                    "symbol": trade.symbol,
                    "entry_timestamp": trade.entry_timestamp,
                    "exit_timestamp": trade.exit_timestamp,
                    "entry_price": trade.entry_price,
                    "exit_price": trade.exit_price,
                    "quantity": trade.quantity,
                    "pnl": trade.pnl,
                }
                for trade in trades
            ],
        }
    )


def minute_bars_hash(bars) -> str:
    return hash_payload([minute_bar_to_row(bar) for bar in bars])
