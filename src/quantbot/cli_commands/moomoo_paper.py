"""CLI entrypoint for the simulation-only Moomoo engineering Paper canary."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from pathlib import Path

from ..integrations.moomoo import MoomooApiError, MoomooSimulatedTradingClient
from ..moomoo_paper import (
    MoomooPaperCanary,
    MoomooPaperCanaryConfig,
    MoomooPaperCanaryHalt,
)
from .context import CLIContext
from .moomoo import _add_close_warning, _client_kwargs


def _emit(context: CLIContext, payload: Mapping[str, object]) -> None:
    context.stdout.write(json.dumps(dict(payload), sort_keys=True, default=str) + "\n")
    context.stdout.flush()


def _symbols(value: object) -> tuple[str, ...]:
    symbols = tuple(
        part.strip().upper()
        for part in str(value).split(",")
        if part.strip()
    )
    if not symbols:
        raise ValueError("--symbols must contain at least one US equity symbol")
    if len(set(symbols)) != len(symbols):
        raise ValueError("--symbols must not contain duplicates")
    return symbols


def _finite_number(value: object, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be numeric") from error
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_number(value: object, *, name: str) -> float:
    result = _finite_number(value, name=name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _non_negative_number(value: object, *, name: str) -> float:
    result = _finite_number(value, name=name)
    if result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _config(args) -> MoomooPaperCanaryConfig:
    capital_fraction = _positive_number(
        args.capital_fraction,
        name="--capital-fraction",
    )
    if capital_fraction > 1:
        raise ValueError("--capital-fraction must not exceed 1")
    return MoomooPaperCanaryConfig(
        symbols=_symbols(args.symbols),
        gross_equity_fraction=capital_fraction,
        liquidation_lead_minutes=_positive_int(
            args.flatten_minutes_before_close,
            name="--flatten-minutes-before-close",
        ),
        poll_interval_seconds=_positive_number(
            args.poll_seconds,
            name="--poll-seconds",
        ),
        max_quote_age_seconds=_positive_number(
            args.max_quote_age_seconds,
            name="--max-quote-age-seconds",
        ),
        marketable_limit_buffer_bps=_non_negative_number(
            args.marketable_limit_buffer_bps,
            name="--marketable-limit-buffer-bps",
        ),
    )


def _event_sink(context: CLIContext):
    def emit_event(event: object) -> None:
        _emit(
            context,
            {
                "type": "event",
                "environment": "SIMULATE",
                "event": event,
            },
        )

    return emit_event


def _requires_reconciliation(result: object) -> bool:
    return isinstance(result, Mapping) and bool(
        result.get("reconciliation_required", False)
    )


def _is_flat(result: object) -> bool:
    return isinstance(result, Mapping) and result.get("flat") is True


def _failure_payload(error: BaseException, *, halted: bool) -> dict[str, object]:
    reconciliation_required = bool(
        getattr(error, "reconciliation_required", False)
    )
    payload: dict[str, object] = {
        "type": "result",
        "ok": False,
        "environment": "SIMULATE",
        "flat": False,
        "halted": halted,
        "reconciliation_required": reconciliation_required,
        "error": str(error),
    }
    for name in ("submission_marker", "order_id", "order"):
        value = getattr(error, name, None)
        if value is not None:
            payload[name] = value
    details = getattr(error, "payload", None)
    if isinstance(details, Mapping):
        payload["details"] = dict(details)
    return payload


def _require_confirmation(args) -> None:
    if not args.confirm_simulated_action:
        raise ValueError(
            "moomoo-paper-canary requires --confirm-simulated-action; "
            "it can place and cancel orders in the shared simulated account"
        )


def _durable_paths(args) -> tuple[Path, Path]:
    state_file = str(args.state_file).strip()
    lock_file = str(args.lock_file).strip()
    if not state_file or not lock_file:
        raise ValueError("--state-file and --lock-file must not be empty")
    return Path(state_file), Path(lock_file)


def _run_canary(
    args,
    context: CLIContext,
    client,
    *,
    state_path: Path,
    lock_path: Path,
    config: MoomooPaperCanaryConfig,
):
    canary = MoomooPaperCanary(
        client,
        state_path=state_path,
        config=config,
        lock_path=lock_path,
        event_sink=_event_sink(context),
    )
    return canary.preflight() if args.preflight_only else canary.run()


def _prepare(args, context: CLIContext):
    _require_confirmation(args)
    config = _config(args)
    state_path, lock_path = _durable_paths(args)
    return config, state_path, lock_path, _client_kwargs(args, context)


def _success_payload(args, result: object) -> tuple[dict[str, object], int]:
    flat = _is_flat(result)
    reconciliation_required = _requires_reconciliation(result)
    halted = isinstance(result, Mapping) and bool(result.get("halted", False))
    preflight_ok = (
        args.preflight_only
        and isinstance(result, Mapping)
        and result.get("ok") is True
    )
    completed = preflight_ok if args.preflight_only else flat
    ok = completed and not halted and not reconciliation_required
    payload = {
        "type": "result",
        "ok": ok,
        "environment": "SIMULATE",
        "preflight_only": bool(args.preflight_only),
        "flat": flat,
        "halted": halted,
        "reconciliation_required": reconciliation_required,
        "result": result,
    }
    return payload, 0 if ok else 2


def handle_moomoo_paper_canary(args, context: CLIContext) -> int:
    """Run the fail-closed engineering canary against Moomoo simulation only."""

    client = None
    try:
        config, state_path, lock_path, client_kwargs = _prepare(args, context)
    except (ModuleNotFoundError, OSError, TypeError, ValueError) as error:
        payload = _failure_payload(error, halted=False)
        payload["reconciliation_required"] = False
        _emit(context, payload)
        return 1

    try:
        client = MoomooSimulatedTradingClient(**client_kwargs)
        with client:
            result = _run_canary(
                args,
                context,
                client,
                state_path=state_path,
                lock_path=lock_path,
                config=config,
            )
    except MoomooPaperCanaryHalt as error:
        payload = _failure_payload(error, halted=True)
        _add_close_warning(payload, client)
        _emit(context, payload)
        return 2
    except MoomooApiError as error:
        reconciliation_required = bool(
            getattr(error, "reconciliation_required", False)
        )
        payload = _failure_payload(error, halted=reconciliation_required)
        _add_close_warning(payload, client)
        _emit(context, payload)
        return 2 if reconciliation_required else 1
    except Exception as error:
        payload = _failure_payload(error, halted=True)
        payload["reconciliation_required"] = True
        _add_close_warning(payload, client)
        _emit(context, payload)
        return 2

    payload, exit_code = _success_payload(args, result)
    _add_close_warning(payload, client)
    _emit(context, payload)
    return exit_code


__all__ = ["handle_moomoo_paper_canary"]
