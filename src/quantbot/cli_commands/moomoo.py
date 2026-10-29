"""Fail-closed commands for moomoo simulated trading."""

from __future__ import annotations

import json
import os
from ipaddress import ip_address
from typing import Mapping

from ..integrations.moomoo import MoomooApiError, MoomooSimulatedTradingClient
from .context import CLIContext


_WRITE_ACTIONS = frozenset({"place", "cancel"})


def _environment(context: CLIContext) -> Mapping[str, str]:
    return context.environ if context.environ is not None else os.environ


def _optional_acc_id(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    acc_id = str(value).strip()
    if not acc_id:
        raise ValueError(f"{name} must not be empty")
    return acc_id


def _positive_int(value: object, *, name: str) -> int:
    try:
        parsed = int(str(value), 10)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _loopback_host(value: object) -> str:
    host = str(value).strip()
    if not host:
        raise ValueError("MOOMOO_OPEND_HOST must not be empty")
    if host.lower() == "localhost":
        return host
    try:
        is_loopback = ip_address(host).is_loopback
    except ValueError as error:
        raise ValueError("MOOMOO_OPEND_HOST must be a loopback address") from error
    if not is_loopback:
        raise ValueError("MOOMOO_OPEND_HOST must be a loopback address")
    return host


def _client_kwargs(args, context: CLIContext) -> dict[str, object]:
    environ = _environment(context)
    host = _loopback_host(
        args.host or environ.get("MOOMOO_OPEND_HOST", "127.0.0.1")
    )
    port = (
        args.port
        if args.port is not None
        else _positive_int(
            environ.get("MOOMOO_OPEND_PORT", "11111"),
            name="MOOMOO_OPEND_PORT",
        )
    )
    acc_id = _optional_acc_id(
        args.acc_id if args.acc_id is not None else environ.get("MOOMOO_SIM_ACC_ID"),
        name="MOOMOO_SIM_ACC_ID",
    )
    security_firm = str(
        args.security_firm
        or environ.get("MOOMOO_SECURITY_FIRM", "FUTUINC")
    ).strip().upper()
    if security_firm != "FUTUINC":
        raise ValueError("MOOMOO_SECURITY_FIRM must be FUTUINC")
    return {
        "host": host,
        "port": _positive_int(port, name="MOOMOO_OPEND_PORT"),
        "acc_id": acc_id,
        "security_firm": security_firm,
        "trd_env": "SIMULATE",
    }


def _require_action_arguments(args) -> None:
    if args.action in _WRITE_ACTIONS and not args.confirm_simulated_action:
        raise ValueError(
            "write actions require --confirm-simulated-action; only the simulated "
            "account can be changed"
        )
    if args.action == "place" and None in (
        args.code,
        args.side,
        args.quantity,
        args.price,
    ):
        raise ValueError("place requires --code, --side, --quantity, and --price")
    if args.action in {"query", "cancel"} and not args.order_id:
        raise ValueError(f"{args.action} requires --order-id")
    if args.action == "reconcile" and not args.submission_marker:
        raise ValueError("reconcile requires --submission-marker")


def _run_action(args, client: MoomooSimulatedTradingClient):
    if args.action == "snapshot":
        return client.snapshot()
    if args.action == "place":
        return client.place_limit_order(
            code=args.code,
            side=args.side,
            quantity=args.quantity,
            price=args.price,
        )
    if args.action == "query":
        return client.query_order(args.order_id)
    if args.action == "cancel":
        return client.cancel_order(args.order_id)
    return client.reconcile_submission(args.submission_marker)


def _emit(context: CLIContext, payload: dict[str, object]) -> None:
    context.stdout.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _error_details(error: MoomooApiError) -> dict[str, object]:
    details: dict[str, object] = {}
    for name in ("submission_marker", "order_id", "order"):
        value = getattr(error, name, None)
        if value is not None:
            details[name] = value
    return details


def _add_close_warning(payload: dict[str, object], client) -> None:
    close_error = getattr(client, "close_error", None)
    if close_error is not None:
        payload["warnings"] = [
            {
                "code": "moomoo_context_close_failed",
                "detail": str(close_error),
            }
        ]


def _result_requires_reconciliation(result) -> bool:
    return isinstance(result, Mapping) and result.get("reconciliation_required") is True


def handle_moomoo_simulate(args, context: CLIContext) -> int:
    """Run one explicitly simulated moomoo operation and emit JSON."""

    client = None
    try:
        _require_action_arguments(args)
        client = MoomooSimulatedTradingClient(**_client_kwargs(args, context))
        with client:
            result = _run_action(args, client)
    except MoomooApiError as error:
        reconciliation_required = bool(
            getattr(error, "reconciliation_required", False)
        )
        payload = {
            "ok": False,
            "action": args.action,
            "environment": "SIMULATE",
            "error": str(error),
            "reconciliation_required": reconciliation_required,
            **_error_details(error),
        }
        _add_close_warning(payload, client)
        _emit(context, payload)
        return 2 if reconciliation_required else 1
    except (ModuleNotFoundError, ValueError) as error:
        payload = {
            "ok": False,
            "action": args.action,
            "environment": "SIMULATE",
            "error": str(error),
            "reconciliation_required": False,
        }
        _add_close_warning(payload, client)
        _emit(context, payload)
        return 1

    reconciliation_required = _result_requires_reconciliation(result)
    payload = {
        "ok": True,
        "action": args.action,
        "environment": "SIMULATE",
        "reconciliation_required": reconciliation_required,
        "result": result,
    }
    _add_close_warning(payload, client)
    _emit(context, payload)
    return 2 if reconciliation_required else 0


__all__ = ["handle_moomoo_simulate"]
