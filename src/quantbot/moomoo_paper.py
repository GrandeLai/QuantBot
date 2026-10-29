"""Crash-safe engineering paper canary for Moomoo simulation accounts.

This runtime is intentionally independent from StockBot's research strategies.
It proves the broker plumbing with a small, equal-weight, intraday-only basket.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
DEFAULT_MOOMOO_CANARY_SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "AMD",
    "TSLA",
    "AVGO",
    "NFLX",
)
_SCHEMA_VERSION = 1
_FROZEN_CASH_EPSILON = 1e-6
_HEARTBEAT_SECONDS = 300.0
_EXIT_REPRICE_SECONDS = 30.0
_MIN_BROKER_POLL_SECONDS = 5.0
_PLACE_RATE_LIMIT = 15
_PLACE_RATE_WINDOW_SECONDS = 30.1
_PLACE_MIN_INTERVAL_SECONDS = 0.021
_CANCEL_RATE_LIMIT = 20
_CANCEL_RATE_WINDOW_SECONDS = 30.1
_CANCEL_MIN_INTERVAL_SECONDS = 0.041
_TERMINAL_STATUSES = frozenset(
    {
        "SUBMIT_FAILED",
        "FILLED_ALL",
        "CANCELLED_PART",
        "CANCELLED_ALL",
        "FAILED",
        "DISABLED",
        "DELETED",
        "FILL_CANCELLED",
    }
)


class MoomooPaperCanaryHalt(RuntimeError):
    """The canary stopped because continuing could create an unsafe order."""

    def __init__(
        self,
        reason: str,
        *,
        payload: Mapping[str, object] | None = None,
        reconciliation_required: bool = False,
    ) -> None:
        self.reason = str(reason)
        self.payload = dict(payload or {})
        self.reconciliation_required = bool(reconciliation_required)
        super().__init__(self.reason)


class _EntryWindowClosed(RuntimeError):
    """The entry window closed before another intent could be sent."""


class _ExitWindowClosed(RuntimeError):
    """The regular session closed before an exit could be sent."""


class _ExitQuoteStale(RuntimeError):
    """An exit stayed inside RTH but its quote expired before the SDK call."""


class _CancelWindowClosed(RuntimeError):
    """The regular session is not open for a cancellation write."""


class _TransientMarketRead(MoomooPaperCanaryHalt):
    """A quote condition that may become safe on the next fresh read."""


class _TransientBrokerSnapshot(MoomooPaperCanaryHalt):
    """A non-atomic broker snapshot whose components have not converged."""


class _TransientBrokerOrderRegression(RuntimeError):
    """A stale order row regressed cumulative broker evidence."""


@dataclass(frozen=True)
class MoomooPaperCanaryConfig:
    symbols: tuple[str, ...] = DEFAULT_MOOMOO_CANARY_SYMBOLS
    gross_equity_fraction: float = 0.10
    liquidation_lead_minutes: int = 10
    poll_interval_seconds: float = 5.0
    max_quote_age_seconds: float = 5.0
    marketable_limit_buffer_bps: float = 50.0

    def __post_init__(self) -> None:
        symbols = tuple(_us_code(item) for item in self.symbols)
        if not symbols:
            raise ValueError("symbols must not be empty")
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique after US normalization")
        object.__setattr__(self, "symbols", symbols)
        fraction = _positive_finite(
            self.gross_equity_fraction, "gross_equity_fraction"
        )
        if fraction > 1:
            raise ValueError("gross_equity_fraction must be <= 1")
        if (
            isinstance(self.liquidation_lead_minutes, bool)
            or not isinstance(self.liquidation_lead_minutes, int)
            or not 1 <= self.liquidation_lead_minutes < 210
        ):
            raise ValueError("liquidation_lead_minutes must be in [1, 209]")
        exit_batches = math.ceil(len(symbols) / _PLACE_RATE_LIMIT)
        cancel_batches = math.ceil(len(symbols) / _CANCEL_RATE_LIMIT)
        minimum_exit_seconds = (
            max(0, cancel_batches - 1) * _CANCEL_RATE_WINDOW_SECONDS
            + max(0, min(len(symbols), _CANCEL_RATE_LIMIT) - 1)
            * _CANCEL_MIN_INTERVAL_SECONDS
            + max(0, exit_batches - 1) * _PLACE_RATE_WINDOW_SECONDS
            + max(0, min(len(symbols), _PLACE_RATE_LIMIT) - 1)
            * _PLACE_MIN_INTERVAL_SECONDS
            + 5.0
        )
        if self.liquidation_lead_minutes * 60 <= minimum_exit_seconds:
            raise ValueError(
                "liquidation_lead_minutes cannot fit worst-case cancellation "
                "plus one exit attempt per configured symbol"
            )
        poll_interval = _positive_finite(
            self.poll_interval_seconds, "poll_interval_seconds"
        )
        if poll_interval < _MIN_BROKER_POLL_SECONDS:
            raise ValueError(
                "poll_interval_seconds must be >= %.1f to respect broker "
                "query limits" % _MIN_BROKER_POLL_SECONDS
            )
        _positive_finite(self.max_quote_age_seconds, "max_quote_age_seconds")
        buffer = _nonnegative_finite(
            self.marketable_limit_buffer_bps,
            "marketable_limit_buffer_bps",
        )
        if buffer >= 10_000:
            raise ValueError("marketable_limit_buffer_bps must be < 10000")


class MoomooPaperCanary:
    """Run one crash-recoverable, long-only Moomoo engineering canary."""

    def __init__(
        self,
        client: object,
        state_path: str | Path,
        config: MoomooPaperCanaryConfig | None = None,
        *,
        lock_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.client = client
        self.config = config or MoomooPaperCanaryConfig()
        self.state_path = Path(state_path).expanduser().resolve()
        default_lock = self.state_path.with_suffix(".lock")
        self.lock_path = Path(lock_path or default_lock).expanduser().resolve()
        if self.lock_path == self.state_path:
            raise ValueError("lock_path must differ from state_path")
        self._clock = clock or (lambda: datetime.now(EASTERN))
        self._sleep = sleep or __import__("time").sleep
        self._event_sink = event_sink or (lambda _event: None)
        self._state: dict[str, Any] | None = None

    def preflight(self) -> dict[str, object]:
        """Perform all read-only gates; never place or cancel an order."""

        with _SingleInstanceLock(self.lock_path):
            now = self._now()
            snapshot = self._snapshot()
            state = self._load_state(required=False)
            if state is None:
                self._require_clean_start(snapshot)
            else:
                self._validate_state_binding(state, snapshot)
                if state["status"] == "HALT":
                    raise MoomooPaperCanaryHalt(
                        "persisted HALT requires manual investigation",
                        payload=self._public_payload(state),
                        reconciliation_required=bool(
                            state.get("reconciliation_required", False)
                        ),
                    )
                if state["status"] not in {"FLAT", "SKIPPED"}:
                    raise MoomooPaperCanaryHalt(
                        "unfinished canary state requires full-run reconciliation",
                        payload=self._public_payload(state),
                        reconciliation_required=True,
                    )
                self._require_clean_start(snapshot)
            session = self._session(now.date())
            quotes = self._quotes(now)
            self._release_quote_context()
            payload = {
                "mode": "moomoo_engineering_paper_canary",
                "status": "PREFLIGHT_OK",
                "ok": True,
                "preflight_only": True,
                "flat": not _positive_positions(snapshot),
                "halted": False,
                "reconciliation_required": False,
                "acc_id": snapshot["acc_id"],
                "trading_day": session["trading_day"],
                "session_type": session["session_type"],
                "close_at": session["close_at"].isoformat(),
                "liquidation_at": session["liquidation_at"].isoformat(),
                "symbols": list(self.config.symbols),
                "quote_count": len(quotes),
                "state_path": str(self.state_path),
                "lock_path": str(self.lock_path),
            }
            self._emit("preflight_passed", **payload)
            return payload

    def run(self) -> dict[str, object]:
        with _SingleInstanceLock(self.lock_path):
            self._emit("lock_acquired", lock_path=str(self.lock_path))
            return self._run_locked()

    def _run_locked(self) -> dict[str, object]:
        now = self._now()
        state = self._load_state(required=False)
        effective_state_day = (
            _effective_trading_day(state) if state is not None else None
        )
        if (
            state is not None
            and state["status"] == "HALT"
            and _halt_origin_day(state) == now.date().isoformat()
        ):
            raise MoomooPaperCanaryHalt(
                "persisted HALT requires manual investigation",
                payload=self._public_payload(state),
                reconciliation_required=bool(
                    state.get("reconciliation_required", False)
                ),
            )
        completed_previous_session = (
            state is not None
            and state["status"] in {"FLAT", "SKIPPED"}
            and effective_state_day != now.date().isoformat()
        )
        if completed_previous_session:
            self._state = state
            snapshot = self._snapshot()
            self._validate_state_account_binding(state, snapshot.get("acc_id"))
            self._require_clean_start(snapshot)
            self._archive_completed_state(state)
            session = self._session(now.date())
            self._release_quote_context()
            state = self._new_state(snapshot, session, now)
            self._state = state
            self._persist()
            self._emit("run_started", run_id=state["run_id"])
        elif state is None:
            snapshot = self._snapshot()
            session = self._session(now.date())
            self._release_quote_context()
            self._require_clean_start(snapshot)
            state = self._new_state(snapshot, session, now)
            self._state = state
            self._persist()
            self._emit("run_started", run_id=state["run_id"])
        else:
            self._state = state
            self._validate_state_config_binding(state)
            if state["status"] in {"FLAT", "SKIPPED"}:
                snapshot = self._snapshot()
                self._validate_state_binding(state, snapshot)
                self._require_clean_start(snapshot)
                return self._public_payload(state)
            try:
                account_id = self.client.account_id()
            except Exception as exc:
                return self._recovery_required(
                    "pre-write account identity is unavailable",
                    exc,
                )
            self._validate_state_account_binding(state, account_id)
            try:
                self._release_quote_context()
            except Exception as exc:
                return self._recovery_required(
                    "pre-write quote isolation is unavailable",
                    exc,
                )
            recovered = self._recover_orders()
            if state.get("containment_mode") is True:
                cancel_session, deferred, waited = (
                    self._prepare_containment_write_window(None)
                )
                if deferred is not None:
                    return deferred
                assert cancel_session is not None
                if waited:
                    recovered = self._recover_orders()
                unresolved = self._converge_containment_orders(
                    cancel_session,
                    recovered,
                )
                if unresolved is not None:
                    return unresolved
                recovered = None
            if recovered is not None:
                return recovered
            now = self._now()
            try:
                snapshot = self._snapshot()
            except MoomooPaperCanaryHalt:
                raise
            except Exception as exc:
                return self._recovery_required(
                    "fresh broker snapshot is unavailable after reconciliation",
                    exc,
                )
            self._validate_state_binding(state, snapshot)
            try:
                session = self._session(now.date())
                self._release_quote_context()
            except MoomooPaperCanaryHalt:
                raise
            except Exception as exc:
                return self._recovery_required(
                    "trading calendar or quote isolation is unavailable",
                    exc,
                )
            if state["trading_day"] != session["trading_day"]:
                state["containment_mode"] = True
                state["recovery_session"] = {
                    "trading_day": session["trading_day"],
                    "session_type": session["session_type"],
                    "open_at": session["open_at"].isoformat(),
                    "close_at": session["close_at"].isoformat(),
                    "liquidation_at": session["liquidation_at"].isoformat(),
                }
                self._persist()
            self._require_no_foreign_activity(snapshot)
            if state.get("containment_mode") is True:
                state["status"] = "LIQUIDATING"
            else:
                state["status"] = "ACTIVE"
            state["reconciliation_required"] = False
            self._persist()

        if self._state.get("containment_mode") is True:
            session, deferred = self._prepare_containment_session(session)
            if deferred is not None:
                return deferred
            assert session is not None
            return self._liquidate(
                session,
                entries_already_reconciled=True,
            )

        now = self._now()
        if now >= session["liquidation_at"] and not self._state["orders"]:
            self._state["status"] = "SKIPPED"
            self._state["flat"] = True
            self._state["completed_at"] = now.isoformat()
            self._persist()
            return self._public_payload(self._state)
        if now < session["open_at"]:
            self._state["status"] = "WAITING_OPEN"
            self._persist()
            self._wait_until(session["open_at"])
            now = self._now()
            snapshot = self._snapshot()
            self._validate_state_binding(self._state, snapshot)
            self._require_clean_start(snapshot)
            if now.date().isoformat() != session["trading_day"]:
                return self._complete_skipped(now)
            session = self._session(now.date())
            self._release_quote_context()
            if (
                session["trading_day"] != self._state["trading_day"]
                or now >= session["liquidation_at"]
            ):
                return self._complete_skipped(now)

        if not self._state["orders"]:
            quotes = self._quotes(self._now())
            self._release_quote_context()
            intents = self._entry_intents(snapshot, quotes)
            if not intents:
                self._halt("10% equity budget cannot buy one whole lot")
            self._state["status"] = "ENTERING"
            self._persist()
            for intent in intents:
                try:
                    result = self._submit_intent(intent)
                except _EntryWindowClosed:
                    self._state["containment_mode"] = True
                    self._persist()
                    return self._liquidate(session)
                if result is not None:
                    self._state["containment_mode"] = True
                    self._persist()
                    recovered = self._recover_orders()
                    cancel_session, deferred, waited = (
                        self._prepare_containment_write_window(session)
                    )
                    if deferred is not None:
                        return deferred
                    assert cancel_session is not None
                    if waited:
                        recovered = self._recover_orders()
                    unresolved = self._converge_containment_orders(
                        cancel_session,
                        recovered,
                    )
                    if unresolved is not None:
                        return unresolved
                    containment_session, deferred = (
                        self._prepare_containment_session(cancel_session)
                    )
                    if deferred is not None:
                        return deferred
                    assert containment_session is not None
                    return self._liquidate(
                        containment_session,
                        entries_already_reconciled=True,
                    )
            self._state["status"] = "ACTIVE"
            self._persist()
            self._emit("entry_submitted", order_count=len(intents))

        monitor_result = self._monitor_until(session["liquidation_at"])
        if monitor_result is not None:
            self._state["containment_mode"] = True
            self._persist()
            cancel_session, deferred, _waited = (
                self._prepare_containment_write_window(session)
            )
            if deferred is not None:
                return deferred
            assert cancel_session is not None
            unresolved = self._converge_containment_orders(
                cancel_session,
                monitor_result,
            )
            if unresolved is not None:
                return unresolved
            containment_session, deferred = self._prepare_containment_session(
                cancel_session
            )
            if deferred is not None:
                return deferred
            assert containment_session is not None
            return self._liquidate(containment_session)
        return self._liquidate(session)

    def _complete_skipped(self, observed_at: datetime) -> dict[str, object]:
        assert self._state is not None
        if self._state["orders"]:
            self._halt("a run with persisted orders cannot be skipped")
        self._state["status"] = "SKIPPED"
        self._state["flat"] = True
        self._state["reconciliation_required"] = False
        self._state["completed_at"] = observed_at.isoformat()
        self._persist()
        return self._public_payload(self._state)

    def _prepare_containment_write_window(
        self,
        _session_hint: Mapping[str, Any] | None,
    ) -> tuple[
        dict[str, Any] | None,
        dict[str, object] | None,
        bool,
    ]:
        """Obtain a fresh current-session permit before containment cancels."""

        assert self._state is not None
        waited = False
        while True:
            now = self._now()
            current, unavailable = self._retry_containment_session(now.date())
            if unavailable is not None:
                return None, unavailable, waited
            assert current is not None
            now = self._now()
            if now.date().isoformat() != current["trading_day"]:
                continue
            self._state["recovery_session"] = {
                "trading_day": current["trading_day"],
                "session_type": current["session_type"],
                "open_at": current["open_at"].isoformat(),
                "close_at": current["close_at"].isoformat(),
                "liquidation_at": current["liquidation_at"].isoformat(),
            }
            self._persist()
            if now < current["open_at"]:
                waited = True
                self._wait_until(current["open_at"])
                continue
            if now >= current["close_at"]:
                return None, self._recovery_required(
                    "containment cannot write after the regular session close",
                    RuntimeError("wait for the next supported US trading session"),
                ), waited
            return current, None, waited

    def _prepare_containment_session(
        self,
        session: Mapping[str, Any],
    ) -> tuple[dict[str, Any] | None, dict[str, object] | None]:
        """Refresh time/account/calendar after waits before any containment write."""

        assert self._state is not None
        current = dict(session)
        while True:
            now = self._now()
            if now < current["open_at"]:
                self._wait_until(current["open_at"])
                continue
            try:
                snapshot = self._liquidation_read(
                    self._snapshot,
                    current["close_at"],
                    "containment account snapshot",
                )
                self._validate_state_binding(self._state, snapshot)
                self._require_no_foreign_activity(snapshot)
            except MoomooPaperCanaryHalt:
                raise
            except Exception as exc:
                return None, self._recovery_required(
                    "fresh containment snapshot is unavailable", exc
                )
            now = self._now()
            if now.date().isoformat() != current["trading_day"]:
                current, unavailable = self._retry_containment_session(
                    now.date()
                )
                if unavailable is not None:
                    return None, unavailable
                assert current is not None
                self._state["recovery_session"] = {
                    "trading_day": current["trading_day"],
                    "session_type": current["session_type"],
                    "open_at": current["open_at"].isoformat(),
                    "close_at": current["close_at"].isoformat(),
                    "liquidation_at": current["liquidation_at"].isoformat(),
                }
                self._persist()
                continue
            if now >= current["close_at"]:
                return None, self._recovery_required(
                    "containment cannot write after the regular session close",
                    RuntimeError("wait for the next supported US trading session"),
                )
            return current, None

    def _retry_containment_session(
        self,
        day: date,
    ) -> tuple[dict[str, Any] | None, dict[str, object] | None]:
        """Retry calendar/quote-isolation reads without abandoning exposure."""

        assert self._state is not None
        retry_until = datetime.combine(day, time(16, 0), EASTERN)
        attempt = 0
        last_error: BaseException | None = None
        while self._now().date() == day:
            try:
                current = self._session(day)
                self._release_quote_context()
                return current, None
            except Exception as exc:
                attempt += 1
                last_error = exc
                self._state["reconciliation_required"] = True
                self._state["last_error"] = "%s: %s" % (
                    type(exc).__name__,
                    exc,
                )
                self._persist()
                self._emit(
                    "containment_session_retry",
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
                now = self._now()
                if now >= retry_until:
                    break
                self._sleep(
                    min(
                        _MIN_BROKER_POLL_SECONDS,
                        max(0.0, (retry_until - now).total_seconds()),
                    )
                )
        if last_error is None:
            last_error = RuntimeError(
                "containment session date changed before it could be read"
            )
        return None, self._recovery_required(
            "current containment session is unavailable",
            last_error,
        )

    def _monitor_until(self, liquidation_at: datetime) -> dict[str, object] | None:
        """Reconcile working entries and periodically prove OpenD/account health."""

        assert self._state is not None
        next_heartbeat = self._now()
        consecutive_read_failures = 0
        while self._now() < liquidation_at:
            try:
                if not self._bulk_update_acknowledged_orders("ENTRY"):
                    for order in self._state["orders"]:
                        if (
                            order["kind"] == "ENTRY"
                            and order["send_state"] == "ACKNOWLEDGED"
                            and not order.get("is_terminal", False)
                        ):
                            broker = self.client.query_order(
                                order["broker_order_id"]
                            )
                            self._update_broker_order(order, broker)
                now = self._now()
                if now >= next_heartbeat:
                    snapshot = self._snapshot()
                    self._require_no_foreign_activity(snapshot)
                    self._emit(
                        "heartbeat",
                        run_id=self._state["run_id"],
                        position_count=len(_positive_positions(snapshot)),
                        working_order_count=len(_working_orders(snapshot)),
                    )
                    next_heartbeat = now + timedelta(seconds=_HEARTBEAT_SECONDS)
                consecutive_read_failures = 0
            except Exception as exc:
                if isinstance(exc, MoomooPaperCanaryHalt) and not isinstance(
                    exc, (_TransientMarketRead, _TransientBrokerSnapshot)
                ):
                    raise
                consecutive_read_failures += 1
                self._emit(
                    "read_retry",
                    attempt=consecutive_read_failures,
                    error_type=type(exc).__name__,
                )
                if consecutive_read_failures >= 3:
                    return self._recovery_required(
                        "three consecutive read reconciliations failed", exc
                    )
            self._sleep_bounded(liquidation_at)
        return None

    def _recover_orders(self) -> dict[str, object] | None:
        assert self._state is not None
        try:
            bulk_index = self._bulk_recovery_order_index()
        except MoomooPaperCanaryHalt:
            raise
        except Exception as exc:
            unresolved = self._recovery_required(
                "bulk order reconciliation is unavailable", exc
            )
            self._state["containment_mode"] = True
            self._persist()
            return unresolved
        return self._apply_recovery_order_index(bulk_index)

    def _retry_recovery_reads(
        self, close_at: datetime
    ) -> dict[str, object] | None:
        """Converge recovery reads without repeating any broker write."""

        def read_and_apply() -> None:
            bulk_index = self._bulk_recovery_order_index()
            unresolved = self._apply_recovery_order_index(bulk_index)
            if unresolved is not None:
                raise _TransientBrokerSnapshot(
                    "persisted broker orders have not converged"
                )

        self._liquidation_read(
            read_and_apply,
            close_at,
            "persisted-order recovery",
        )
        return None

    def _converge_containment_orders(
        self,
        session: Mapping[str, Any],
        recovered: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Contain known entries, then converge previously unknown writes."""

        while True:
            containment = self._contain_known_entries()
            unknown_cancel = any(
                isinstance(order.get("cancel"), Mapping)
                and order["cancel"].get("state") == "SEND_STARTED"
                for order in self._state["orders"]
            )
            if recovered is None and not unknown_cancel:
                return containment
            recovered = self._retry_recovery_reads(session["close_at"])
            # Recovery can reveal another working ENTRY; loop so that it gets
            # exactly one WAL-backed cancel. If that cancel reply is then lost,
            # the next iteration performs reads only until its state converges.

    def _apply_recovery_order_index(
        self,
        bulk_index: tuple[
            dict[str, Mapping[str, object]],
            dict[str, list[Mapping[str, object]]],
        ]
        | None,
    ) -> dict[str, object] | None:
        """Apply one completed recovery read to durable order state."""

        assert self._state is not None
        containment_evidence = (
            self._state.get("containment_mode") is True
            or self._state.get("status")
            in {"ENTERING", "LIQUIDATING", "RECOVERY_REQUIRED", "HALT"}
            or any(order.get("cancel") is not None for order in self._state["orders"])
            or any(
                order.get("kind") == "ENTRY"
                and order.get("send_state") == "SEND_STARTED"
                for order in self._state["orders"]
            )
        )
        if bulk_index is None:
            bulk_by_id = None
            bulk_by_marker = None
        else:
            bulk_by_id, bulk_by_marker = bulk_index
        unresolved: dict[str, object] | None = None
        for order in self._state["orders"]:
            send_state = order["send_state"]
            if send_state == "ABORTED":
                continue
            if send_state == "INTENT_PERSISTED":
                self._abort_unsent_intent(
                    order,
                    "recovery proved the broker submission never started",
                )
                containment_evidence = True
            elif send_state == "SEND_STARTED":
                try:
                    matches = [] if bulk_by_marker is None else bulk_by_marker.get(
                        order["submission_marker"], []
                    )
                    if len(matches) > 1:
                        self._halt(
                            "submission marker matched multiple broker orders",
                            reconciliation=True,
                        )
                    if len(matches) == 1:
                        broker = matches[0]
                    elif bulk_by_marker is not None:
                        raise RuntimeError(
                            "submission marker is absent from fresh today/history snapshots"
                        )
                    else:
                        broker = self.client.reconcile_submission(
                            order["submission_marker"]
                        )
                except MoomooPaperCanaryHalt:
                    raise
                except Exception as exc:
                    unresolved = self._recovery_required(
                        "submission outcome remains unknown", exc
                    )
                    continue
                self._ack_order(order, broker)
            elif send_state == "ACKNOWLEDGED":
                try:
                    if bulk_by_id is None:
                        broker = self.client.query_order(order["broker_order_id"])
                    else:
                        broker = bulk_by_id.get(str(order["broker_order_id"]))
                        if broker is None:
                            raise RuntimeError(
                                "acknowledged order is absent from fresh "
                                "today/history snapshots"
                            )
                    self._update_broker_order(order, broker)
                except MoomooPaperCanaryHalt:
                    raise
                except Exception as exc:
                    unresolved = self._recovery_required(
                        "order query remains unavailable", exc
                    )
            else:
                self._halt("unknown persisted send_state")
            cancel = order.get("cancel")
            if isinstance(cancel, Mapping) and cancel.get("state") == "SEND_STARTED":
                if not order.get("is_terminal", False):
                    unresolved = self._recovery_required(
                        "cancellation crossed SEND_STARTED and remains non-terminal",
                        RuntimeError("cancel outcome is unknown"),
                    )
                    continue
                cancel["state"] = "CONFIRMED"
                self._persist()
        if unresolved is not None:
            self._state["containment_mode"] = True
            self._persist()
            return unresolved
        if containment_evidence:
            self._state["containment_mode"] = True
            self._persist()
        return None

    def _bulk_recovery_order_index(
        self,
    ) -> tuple[
        dict[str, Mapping[str, object]],
        dict[str, list[Mapping[str, object]]],
    ] | None:
        """Read today plus bounded history once and index locally for recovery."""

        assert self._state is not None
        today_reader = getattr(self.client, "orders_snapshot", None)
        if not callable(today_reader):
            return None
        today_rows = today_reader()
        if isinstance(today_rows, (str, bytes, bytearray)) or not isinstance(
            today_rows, Sequence
        ):
            raise MoomooPaperCanaryHalt(
                "broker orders snapshot must be a sequence"
            )
        sources: list[tuple[str, Sequence[object]]] = [("today", today_rows)]
        eastern_today = self._now().date()
        state_day = date.fromisoformat(str(self._state["trading_day"]))
        if state_day < eastern_today:
            history_reader = getattr(self.client, "history_orders_snapshot", None)
            if not callable(history_reader):
                # Backward-compatible test seam only. The production client
                # always supplies the bounded history reader.
                return None
            history_rows = history_reader(state_day, eastern_today - timedelta(days=1))
            if isinstance(history_rows, (str, bytes, bytearray)) or not isinstance(
                history_rows, Sequence
            ):
                raise MoomooPaperCanaryHalt(
                    "broker history orders snapshot must be a sequence"
                )
            sources.append(("history", history_rows))

        by_id: dict[str, Mapping[str, object]] = {}
        for source, rows in sources:
            seen_in_source: set[str] = set()
            for raw in rows:
                row = _mapping(raw, "broker %s orders snapshot row" % source)
                order_id = str(row.get("order_id", "")).strip()
                if not order_id or order_id in seen_in_source:
                    self._halt(
                        "broker %s orders snapshot has a missing or duplicate "
                        "order_id" % source,
                        reconciliation=True,
                    )
                seen_in_source.add(order_id)
                if order_id in by_id:
                    if not _same_broker_order_contract(by_id[order_id], row):
                        self._halt(
                            "today/history snapshots disagree on an immutable "
                            "broker order contract",
                            reconciliation=True,
                        )
                    # The today query is fresher and intentionally wins for
                    # mutable status/dealt fields. Moomoo may retain terminal
                    # rows there for 24 hours, overlapping bounded history.
                    continue
                by_id[order_id] = row
        by_marker: dict[str, list[Mapping[str, object]]] = {}
        for row in by_id.values():
            marker = str(row.get("remark", "")).strip()
            if marker:
                by_marker.setdefault(marker, []).append(row)
        if any(
            marker.startswith("STOCKBOT:C:") and len(rows) > 1
            for marker, rows in by_marker.items()
        ):
            self._halt(
                "a canary submission marker matched multiple broker orders",
                reconciliation=True,
            )
        return by_id, by_marker

    def _contain_known_entries(self) -> dict[str, object] | None:
        """Send each independently persisted cancel once after any unknown write."""

        assert self._state is not None
        unresolved: dict[str, object] | None = None
        for order in self._state["orders"]:
            if (
                order["kind"] != "ENTRY"
                or order["send_state"] != "ACKNOWLEDGED"
                or order.get("is_terminal", False)
            ):
                continue
            cancel = order.get("cancel")
            if isinstance(cancel, Mapping) and cancel.get("state") in {
                "SEND_STARTED",
                "ACKNOWLEDGED",
                "CONFIRMED",
            }:
                continue
            result = self._cancel_once(order)
            if result is not None:
                unresolved = result
        return unresolved

    def _entry_intents(
        self,
        snapshot: Mapping[str, object],
        quotes: Sequence[Mapping[str, object]],
    ) -> list[dict[str, object]]:
        account = _mapping(snapshot["account"], "snapshot.account")
        equity = _positive_finite(account.get("total_assets"), "total_assets")
        power = _nonnegative_finite(account.get("power"), "power")
        total_budget = min(equity * self.config.gross_equity_fraction, power)
        per_symbol = total_budget / len(quotes)
        remaining_power = power
        intents: list[dict[str, object]] = []
        for quote in quotes:
            ask = _positive_finite(quote.get("ask_price"), "ask_price")
            lot = _positive_int(quote.get("lot_size"), "lot_size")
            price = _marketable_price(
                ask,
                _positive_finite(quote.get("price_spread"), "price_spread"),
                "BUY",
                self.config.marketable_limit_buffer_bps,
            )
            raw_qty = math.floor(per_symbol / price)
            quantity = (raw_qty // lot) * lot
            affordable = math.floor(remaining_power / price)
            quantity = min(quantity, (affordable // lot) * lot)
            if quantity <= 0:
                continue
            remaining_power -= quantity * price
            intents.append(
                self._intent(
                    quote["code"],
                    "BUY",
                    quantity,
                    price,
                    "ENTRY",
                    quoted_at=quote["observed_at"],
                )
            )
        return intents

    def _intent(
        self,
        symbol: object,
        side: str,
        quantity: int,
        price: float,
        kind: str,
        *,
        quoted_at: object,
    ) -> dict[str, object]:
        client_order_id = uuid4().hex
        marker = "STOCKBOT:C:%s" % client_order_id
        if len(marker.encode("utf-8")) > 64:
            raise AssertionError("canary submission marker exceeds 64 bytes")
        intent = {
            "client_order_id": client_order_id,
            "submission_marker": marker,
            "symbol": _us_code(symbol),
            "side": side,
            "quantity": _positive_int(quantity, "quantity"),
            "price": _positive_finite(price, "price"),
            "time_in_force": "DAY",
            "session": "RTH",
            "kind": kind,
            "quoted_at": _aware_datetime(
                quoted_at, "quoted_at"
            ).isoformat(),
            "send_state": "NEW",
            "broker_order_id": None,
            "order_status": None,
            "dealt_qty": 0,
            "cancel": None,
        }
        intent["intent_hash"] = _intent_hash(intent)
        return intent

    def _submit_intent(
        self, intent: dict[str, object]
    ) -> dict[str, object] | None:
        assert self._state is not None
        while True:
            kind = str(intent["kind"])
            try:
                waited = self._wait_for_place_capacity(kind)
            except _ExitWindowClosed as exc:
                self._halt(str(exc), reconciliation=True)
            if waited or not self._intent_quote_is_fresh(intent):
                if kind == "ENTRY":
                    try:
                        self._refresh_intent_quote(intent)
                    except _EntryWindowClosed:
                        raise
                    except Exception as exc:
                        raise _EntryWindowClosed(
                            "entry quote refresh is unavailable"
                        ) from exc
                else:
                    self._liquidation_read(
                        lambda: self._refresh_intent_quote(intent),
                        self._intent_deadline(kind),
                        "rate-limited exit quote refresh",
                    )
            try:
                self._assert_intent_write_window(intent)
            except _ExitQuoteStale:
                self._liquidation_read(
                    lambda: self._refresh_intent_quote(intent),
                    self._intent_deadline(kind),
                    "pre-WAL exit quote refresh",
                )
                continue
            except _ExitWindowClosed as exc:
                self._halt(str(exc), reconciliation=True)
            intent["send_state"] = "INTENT_PERSISTED"
            self._state["orders"].append(intent)
            self._persist()
            self._emit(
                "intent_persisted",
                client_order_id=intent["client_order_id"],
                submission_marker=intent["submission_marker"],
            )
            try:
                return self._send_persisted_intent(intent)
            except _ExitQuoteStale:
                replacement = self._intent(
                    intent["symbol"],
                    intent["side"],
                    int(intent["quantity"]),
                    float(intent["price"]),
                    kind,
                    quoted_at=intent["quoted_at"],
                )
                self._liquidation_read(
                    lambda: self._refresh_intent_quote(replacement),
                    self._intent_deadline(kind),
                    "post-quota exit quote refresh",
                )
                intent = replacement

    def _refresh_intent_quote(self, intent: dict[str, object]) -> None:
        original_notional = Decimal(str(intent["quantity"])) * Decimal(
            str(intent["price"])
        )
        quote = self._quotes(self._now(), (str(intent["symbol"]),))[0]
        self._release_quote_context()
        reference = (
            quote["ask_price"]
            if intent["side"] == "BUY"
            else quote["bid_price"]
        )
        refreshed_price = _marketable_price(
            reference,
            quote["price_spread"],
            str(intent["side"]),
            self.config.marketable_limit_buffer_bps,
        )
        if intent["kind"] == "ENTRY":
            lot_size = _positive_int(quote.get("lot_size"), "lot_size")
            affordable_quantity = int(
                (
                    original_notional / Decimal(str(refreshed_price))
                ).to_integral_value(rounding=ROUND_FLOOR)
            )
            refreshed_quantity = (
                affordable_quantity // lot_size
            ) * lot_size
            if refreshed_quantity <= 0:
                raise _EntryWindowClosed(
                    "refreshed entry quote cannot buy one whole lot"
                )
            intent["quantity"] = refreshed_quantity
        intent["price"] = refreshed_price
        intent["quoted_at"] = quote["observed_at"]
        intent["intent_hash"] = _intent_hash(intent)

    def _intent_quote_is_fresh(self, intent: Mapping[str, object]) -> bool:
        quoted_at = _aware_datetime(intent.get("quoted_at"), "quoted_at")
        age = (self._now() - quoted_at).total_seconds()
        return -2.0 <= age <= self.config.max_quote_age_seconds

    def _assert_intent_write_window(
        self, intent: Mapping[str, object]
    ) -> None:
        assert self._state is not None
        kind = str(intent["kind"])
        deadline = self._intent_deadline(kind)
        effective_session = self._state.get("recovery_session")
        if not isinstance(effective_session, Mapping):
            effective_session = self._state
        now = self._now()
        if (
            now.date().isoformat() != str(effective_session.get("trading_day"))
            or now >= deadline
        ):
            if kind == "ENTRY":
                raise _EntryWindowClosed("entry window is closed")
            raise _ExitWindowClosed(
                "exit write would occur after the regular session close"
            )
        if not self._intent_quote_is_fresh(intent):
            if kind == "ENTRY":
                raise _EntryWindowClosed("entry quote became stale before send")
            raise _ExitQuoteStale("exit quote became stale before send")

    def _intent_deadline(self, kind: str) -> datetime:
        assert self._state is not None
        effective_session = self._state.get("recovery_session")
        if not isinstance(effective_session, Mapping):
            effective_session = self._state
        field = "liquidation_at" if kind == "ENTRY" else "close_at"
        return _aware_datetime(effective_session.get(field), field)

    def _wait_for_place_capacity(self, kind: str) -> bool:
        """Persistently rate-limit place calls and reserve unwind capacity."""

        assert self._state is not None
        effective_session = self._state.get("recovery_session")
        if not isinstance(effective_session, Mapping):
            effective_session = self._state
        field = "liquidation_at" if kind == "ENTRY" else "close_at"
        deadline = _aware_datetime(effective_session.get(field), field)
        trading_day = str(effective_session.get("trading_day"))
        waited = False
        while True:
            now = self._now()
            if now.date().isoformat() != trading_day or now >= deadline:
                if kind == "ENTRY":
                    raise _EntryWindowClosed("entry window is closed")
                raise _ExitWindowClosed(
                    "exit place-rate capacity is unavailable before market close"
                )
            recent: list[datetime] = []
            all_started: list[datetime] = []
            for order in self._state["orders"]:
                raw_started = order.get("send_started_at")
                if raw_started is None:
                    continue
                started = _aware_datetime(raw_started, "send_started_at")
                all_started.append(started)
                age = (now - started).total_seconds()
                if age < -2.0:
                    self._halt("persisted send_started_at is future-dated")
                if age < _PLACE_RATE_WINDOW_SECONDS:
                    recent.append(started)
            total_after = len(recent) + 1
            min_interval_ok = not all_started or (
                now - max(all_started)
            ).total_seconds() >= _PLACE_MIN_INTERVAL_SECONDS
            window_capacity_ok = total_after <= _PLACE_RATE_LIMIT
            reservation_ok = True
            if kind == "ENTRY":
                potential_entries = 1 + sum(
                    1
                    for order in self._state["orders"]
                    if order["kind"] == "ENTRY"
                )
                reserved_unwinds = min(
                    potential_entries,
                    _PLACE_RATE_LIMIT - 1,
                )
                reservation_ok = (
                    total_after + reserved_unwinds <= _PLACE_RATE_LIMIT
                )
            capacity_ok = (
                window_capacity_ok and reservation_ok and min_interval_ok
            )
            if capacity_ok:
                return waited
            release_times: list[datetime] = []
            if not window_capacity_ok or not reservation_ok:
                if not recent:
                    if kind == "ENTRY":
                        raise _EntryWindowClosed(
                            "entry unwind capacity cannot be reserved"
                        )
                    raise _ExitWindowClosed(
                        "place-rate limiter has no trustworthy release timestamp"
                    )
                release_times.append(
                    min(recent)
                    + timedelta(seconds=_PLACE_RATE_WINDOW_SECONDS)
                )
            if not min_interval_ok:
                release_times.append(
                    max(all_started)
                    + timedelta(seconds=_PLACE_MIN_INTERVAL_SECONDS)
                )
            wake_at = max(release_times)
            if wake_at >= deadline:
                if kind == "ENTRY":
                    raise _EntryWindowClosed(
                        "entry rate capacity would cross the liquidation gate"
                    )
                raise _ExitWindowClosed(
                    "exit place-rate capacity is unavailable before market close"
                )
            waited = True
            self._sleep(max(0.0, (wake_at - now).total_seconds()))

    def _send_persisted_intent(
        self, order: dict[str, object]
    ) -> dict[str, object] | None:
        try:
            self._assert_intent_write_window(order)
        except _EntryWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        except _ExitWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            self._halt(str(exc), reconciliation=True)
        except _ExitQuoteStale as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        self._emit("send_started", client_order_id=order["client_order_id"])
        try:
            self._assert_intent_write_window(order)
        except _EntryWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        except _ExitWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            self._halt(str(exc), reconciliation=True)
        except _ExitQuoteStale as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        order["send_state"] = "SEND_STARTED"
        order["send_started_at"] = self._now().isoformat()
        self._persist()
        try:
            self._assert_intent_write_window(order)
        except _EntryWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        except _ExitWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            self._halt(str(exc), reconciliation=True)
        except _ExitQuoteStale as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        try:
            broker = self.client.place_limit_order(
                order["symbol"],
                order["side"],
                order["quantity"],
                order["price"],
                submission_marker=order["submission_marker"],
                pre_write_guard=lambda: self._assert_intent_write_window(
                    order
                ),
            )
        except _EntryWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        except _ExitWindowClosed as exc:
            self._abort_unsent_intent(order, str(exc))
            self._halt(str(exc), reconciliation=True)
        except _ExitQuoteStale as exc:
            self._abort_unsent_intent(order, str(exc))
            raise
        except Exception as exc:
            return self._recovery_required("submission outcome is unknown", exc)
        self._ack_order(order, broker)
        return None

    def _abort_unsent_intent(
        self,
        order: dict[str, object],
        reason: str,
    ) -> None:
        if order.get("send_state") not in {
            "INTENT_PERSISTED",
            "SEND_STARTED",
        }:
            self._halt("only an unsent persisted intent can be aborted")
        order["send_state"] = "ABORTED"
        order["order_status"] = "ABORTED"
        order["is_terminal"] = True
        order.pop("send_started_at", None)
        order["aborted_at"] = self._now().isoformat()
        order["abort_reason"] = reason
        self._persist()
        self._emit(
            "intent_aborted",
            client_order_id=order["client_order_id"],
            reason=reason,
        )

    def _ack_order(
        self, order: dict[str, object], broker: Mapping[str, object]
    ) -> None:
        broker_order_id = str(broker.get("order_id", "")).strip()
        if not broker_order_id:
            self._halt("broker acknowledgement is missing order_id", reconciliation=True)
        if broker.get("submission_marker", order["submission_marker"]) != order["submission_marker"]:
            self._halt("broker acknowledgement marker mismatch", reconciliation=True)
        order["send_state"] = "ACKNOWLEDGED"
        order["broker_order_id"] = broker_order_id
        order["acknowledged_at"] = self._now().isoformat()
        self._update_broker_order(order, broker, persist=False)
        self._persist()
        self._emit(
            "order_acknowledged",
            client_order_id=order["client_order_id"],
            broker_order_id=broker_order_id,
        )

    def _update_broker_order(
        self,
        order: dict[str, object],
        broker: Mapping[str, object],
        *,
        persist: bool = True,
    ) -> None:
        previous = (
            order.get("order_status"),
            order.get("dealt_qty"),
            order.get("is_terminal"),
        )
        if str(broker.get("order_id")) != str(order["broker_order_id"]):
            self._halt("broker order_id changed during reconciliation", reconciliation=True)
        if "code" in broker and _us_code(broker["code"]) != order["symbol"]:
            self._halt("broker order symbol mismatch", reconciliation=True)
        if "trd_side" in broker and str(broker["trd_side"]).upper() != order["side"]:
            self._halt("broker order side mismatch", reconciliation=True)
        if broker.get("outcome_uncertain") is True:
            self._halt("broker order has an uncertain TIMEOUT outcome", reconciliation=True)
        status = str(broker.get("order_status", "")).upper()
        if not status:
            self._halt("broker order status is missing", reconciliation=True)
        dealt_qty = _whole_nonnegative(
            broker.get("dealt_qty", 0), "broker dealt_qty"
        )
        previous_dealt_qty = _whole_nonnegative(
            order.get("dealt_qty", 0), "persisted dealt_qty"
        )
        if dealt_qty < previous_dealt_qty:
            raise _TransientBrokerOrderRegression(
                "broker cumulative dealt quantity regressed"
            )
        if dealt_qty > int(order["quantity"]):
            self._halt("broker dealt quantity exceeds WAL intent", reconciliation=True)
        if "qty" in broker and _whole_nonnegative(
            broker["qty"], "broker qty"
        ) != int(order["quantity"]):
            self._halt("broker quantity differs from WAL intent", reconciliation=True)
        if "price" in broker and not math.isclose(
            _positive_finite(broker["price"], "broker price"),
            float(order["price"]),
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            self._halt("broker price differs from WAL intent", reconciliation=True)
        terminal = _is_terminal(broker)
        if order.get("is_terminal", False) and not terminal:
            raise _TransientBrokerOrderRegression(
                "broker terminal order regressed to a working state"
            )
        order["order_status"] = status
        order["dealt_qty"] = dealt_qty
        order["is_terminal"] = terminal
        current = (
            order.get("order_status"),
            order.get("dealt_qty"),
            order.get("is_terminal"),
        )
        if persist and current != previous:
            self._persist()

    def _liquidate(
        self,
        session: Mapping[str, Any],
        *,
        entries_already_reconciled: bool = False,
    ) -> dict[str, object]:
        assert self._state is not None
        self._state["status"] = "LIQUIDATING"
        self._persist()
        self._emit("liquidation_started", run_id=self._state["run_id"])
        entries_bulk_reconciled = entries_already_reconciled
        if not entries_bulk_reconciled and all(
            item["send_state"] in {"ACKNOWLEDGED", "ABORTED"}
            for item in self._state["orders"]
            if item["kind"] == "ENTRY"
        ):
            entries_bulk_reconciled = self._liquidation_read(
                lambda: self._bulk_update_acknowledged_orders("ENTRY"),
                session["close_at"],
                "entry-order bulk reconciliation",
            )
        for order in [item for item in self._state["orders"] if item["kind"] == "ENTRY"]:
            if order["send_state"] == "ABORTED":
                continue
            if order["send_state"] != "ACKNOWLEDGED":
                recovered = self._recover_orders()
                if recovered is not None:
                    recovered = self._retry_recovery_reads(session["close_at"])
                    if recovered is not None:
                        return recovered
                if order["send_state"] == "ABORTED":
                    continue
                if order["send_state"] != "ACKNOWLEDGED":
                    self._halt(
                        "entry recovery did not reach a terminal local state",
                        reconciliation=True,
                    )
            if order.get("is_terminal", False):
                continue
            if entries_bulk_reconciled:
                result = self._cancel_once(order)
                if result is not None:
                    cancel = order.get("cancel")
                    if not (
                        isinstance(cancel, Mapping)
                        and cancel.get("state") == "SEND_STARTED"
                    ):
                        return result
                    recovered = self._retry_recovery_reads(session["close_at"])
                    if recovered is not None:
                        return recovered
                continue
            broker = self._liquidation_read(
                lambda: self.client.query_order(order["broker_order_id"]),
                session["close_at"],
                "entry-order reconciliation",
            )
            self._update_broker_order(order, broker)
            if not order["is_terminal"]:
                result = self._cancel_once(order)
                if result is not None:
                    cancel = order.get("cancel")
                    if not (
                        isinstance(cancel, Mapping)
                        and cancel.get("state") == "SEND_STARTED"
                    ):
                        return result
                    recovered = self._retry_recovery_reads(session["close_at"])
                    if recovered is not None:
                        return recovered
        entries_are_terminal = all(
            item.get("is_terminal", False)
            for item in self._state["orders"]
            if item["kind"] == "ENTRY"
        )
        if not entries_are_terminal and not self._wait_entries_terminal(
            session["close_at"]
        ):
            self._halt(
                "entry orders did not become terminal before market close",
                reconciliation=True,
            )

        def read_exit_state() -> tuple[dict[str, object], dict[str, int]]:
            current = self._snapshot()
            self._require_no_foreign_activity(current)
            return current, self._owned_positions(current)

        snapshot, positions = self._liquidation_read(
            read_exit_state,
            session["close_at"],
            "account snapshot before exit",
        )
        if positions:
            active_exit_symbols = {
                item["symbol"]
                for item in self._state["orders"]
                if item["kind"] == "EXIT"
                and item["send_state"] == "ACKNOWLEDGED"
                and not item.get("is_terminal", False)
            }
            def read_exit_quotes() -> dict[str, dict[str, object]]:
                rows = self._quotes(self._now(), tuple(positions))
                self._release_quote_context()
                return {row["code"]: row for row in rows}

            quote_by_code = self._liquidation_read(
                read_exit_quotes,
                session["close_at"],
                "exit quotes",
            )
            for symbol, quantity in positions.items():
                if symbol in active_exit_symbols:
                    continue
                quote = quote_by_code[symbol]
                price = _marketable_price(
                    quote["bid_price"],
                    quote["price_spread"],
                    "SELL",
                    self.config.marketable_limit_buffer_bps,
                )
                intent = self._intent(
                    symbol,
                    "SELL",
                    quantity,
                    price,
                    "EXIT",
                    quoted_at=quote["observed_at"],
                )
                result = self._submit_intent(intent)
                if result is not None:
                    latest = self._state["orders"][-1]
                    if latest.get("send_state") != "SEND_STARTED":
                        return result
                    recovered = self._retry_recovery_reads(session["close_at"])
                    if recovered is not None:
                        return recovered
        return self._wait_flat(session["close_at"])

    def _cancel_once(
        self, order: dict[str, object]
    ) -> dict[str, object] | None:
        cancel = order.get("cancel")
        if cancel is None:
            try:
                self._wait_for_cancel_capacity()
            except _CancelWindowClosed as exc:
                return self._recovery_required(str(exc), exc)
            cancel = {
                "client_action_id": uuid4().hex,
                "state": "INTENT_PERSISTED",
            }
            order["cancel"] = cancel
            self._persist()
            self._emit("cancel_intent_persisted", order_id=order["broker_order_id"])
        if cancel["state"] == "INTENT_PERSISTED":
            try:
                self._wait_for_cancel_capacity()
            except _CancelWindowClosed as exc:
                return self._recovery_required(str(exc), exc)
            cancel["state"] = "SEND_STARTED"
            cancel["send_started_at"] = self._now().isoformat()
            self._persist()
            try:
                self._cancel_write_window()
            except _CancelWindowClosed as exc:
                # This process knows the SDK call has not begun. Restore the
                # retryable pre-send state instead of manufacturing an unknown
                # cancel outcome merely because the WAL fsync crossed a gate.
                cancel["state"] = "INTENT_PERSISTED"
                cancel.pop("send_started_at", None)
                self._persist()
                return self._recovery_required(str(exc), exc)
            try:
                self.client.cancel_order(
                    order["broker_order_id"],
                    pre_write_guard=self._cancel_write_window,
                )
            except _CancelWindowClosed as exc:
                cancel["state"] = "INTENT_PERSISTED"
                cancel.pop("send_started_at", None)
                self._persist()
                return self._recovery_required(str(exc), exc)
            except Exception as exc:
                return self._recovery_required("cancellation outcome is unknown", exc)
            cancel["state"] = "ACKNOWLEDGED"
            self._persist()
        return None

    def _wait_for_cancel_capacity(self) -> None:
        assert self._state is not None
        while True:
            now, close_at = self._cancel_write_window()
            started_times = []
            for persisted in self._state["orders"]:
                persisted_cancel = persisted.get("cancel")
                if not isinstance(persisted_cancel, Mapping):
                    continue
                raw_started = persisted_cancel.get("send_started_at")
                if raw_started is not None:
                    started_times.append(
                        _aware_datetime(raw_started, "cancel.send_started_at")
                    )
            recent = [
                started
                for started in started_times
                if (now - started).total_seconds() < _CANCEL_RATE_WINDOW_SECONDS
            ]
            min_interval_ok = not started_times or (
                now - max(started_times)
            ).total_seconds() >= _CANCEL_MIN_INTERVAL_SECONDS
            window_ok = len(recent) < _CANCEL_RATE_LIMIT
            if min_interval_ok and window_ok:
                return
            release_times = []
            if not min_interval_ok:
                release_times.append(
                    max(started_times)
                    + timedelta(seconds=_CANCEL_MIN_INTERVAL_SECONDS)
                )
            if not window_ok:
                release_times.append(
                    min(recent)
                    + timedelta(seconds=_CANCEL_RATE_WINDOW_SECONDS)
                )
            wake_at = max(release_times)
            if wake_at >= close_at:
                raise _CancelWindowClosed(
                    "cancel-rate capacity is unavailable before market close"
                )
            self._sleep(max(0.0, (wake_at - now).total_seconds()))

    def _cancel_write_window(self) -> tuple[datetime, datetime]:
        assert self._state is not None
        effective_session = self._state.get("recovery_session")
        if not isinstance(effective_session, Mapping):
            effective_session = self._state
        trading_day = str(effective_session.get("trading_day"))
        raw_open = effective_session.get("open_at")
        open_at = (
            datetime.combine(date.fromisoformat(trading_day), time(9, 30), EASTERN)
            if raw_open is None
            else _aware_datetime(raw_open, "open_at")
        )
        close_at = _aware_datetime(effective_session.get("close_at"), "close_at")
        now = self._now()
        if (
            now.date().isoformat() != trading_day
            or now < open_at
            or now >= close_at
        ):
            raise _CancelWindowClosed(
                "cancellation is deferred outside the regular session"
            )
        return now, close_at

    def _wait_entries_terminal(self, close_at: datetime) -> bool:
        assert self._state is not None
        while True:
            pending = []
            used_bulk = self._liquidation_read(
                lambda: self._bulk_update_acknowledged_orders("ENTRY"),
                close_at,
                "entry-order terminal reconciliation",
            )
            for order in self._state["orders"]:
                if order["kind"] != "ENTRY":
                    continue
                if order["send_state"] != "ACKNOWLEDGED":
                    if order.get("is_terminal", False):
                        continue
                    self._halt(
                        "non-terminal entry lacks a broker acknowledgement",
                        reconciliation=True,
                    )
                if not used_bulk:
                    broker = self._liquidation_read(
                        lambda: self.client.query_order(
                            order["broker_order_id"]
                        ),
                        close_at,
                        "entry-order terminal query",
                    )
                    self._update_broker_order(order, broker)
                if not order["is_terminal"]:
                    pending.append(order)
            if not pending:
                return True
            if self._now() >= close_at:
                return False
            self._sleep_bounded(close_at)

    def _bulk_update_acknowledged_orders(self, kind: str) -> bool:
        """Refresh all acknowledged orders with one broker request when supported."""

        assert self._state is not None
        state_day = date.fromisoformat(str(self._state["trading_day"]))
        if state_day < self._now().date():
            bulk_index = self._bulk_recovery_order_index()
            if bulk_index is None:
                return False
            by_id = bulk_index[0]
        else:
            reader = getattr(self.client, "orders_snapshot", None)
            if not callable(reader):
                return False
            rows = reader()
            if isinstance(rows, (str, bytes, bytearray)) or not isinstance(
                rows, Sequence
            ):
                raise MoomooPaperCanaryHalt(
                    "broker orders snapshot must be a sequence"
                )
            by_id = {}
            for raw in rows:
                row = _mapping(raw, "broker orders snapshot row")
                order_id = str(row.get("order_id", "")).strip()
                if not order_id or order_id in by_id:
                    self._halt(
                        "broker orders snapshot has a missing or duplicate order_id",
                        reconciliation=True,
                    )
                by_id[order_id] = row
        for order in self._state["orders"]:
            if order["kind"] != kind or order["send_state"] != "ACKNOWLEDGED":
                continue
            broker = by_id.get(str(order["broker_order_id"]))
            if broker is None:
                raise _TransientBrokerSnapshot(
                    "acknowledged order is missing from the fresh broker "
                    "orders snapshot"
                )
            self._update_broker_order(order, broker)
        return True

    def _wait_flat(self, close_at: datetime) -> dict[str, object]:
        assert self._state is not None
        while True:
            def read_flat_state() -> tuple[
                dict[str, object],
                list[Mapping[str, object]],
                list[Mapping[str, object]],
                float,
                dict[str, int] | None,
            ]:
                current = self._snapshot()
                self._require_no_foreign_activity(current)
                snapshot_orders: dict[str, Mapping[str, object]] = {}
                for raw in current["orders"]:
                    broker_order = _mapping(raw, "snapshot order")
                    broker_order_id = str(
                        broker_order.get("order_id", "")
                    ).strip()
                    if not broker_order_id or broker_order_id in snapshot_orders:
                        self._halt(
                            "snapshot orders have a missing or duplicate order_id",
                            reconciliation=True,
                        )
                    snapshot_orders[broker_order_id] = broker_order
                for order in self._state["orders"]:
                    if (
                        order["kind"] != "EXIT"
                        or order["send_state"] != "ACKNOWLEDGED"
                        or order.get("is_terminal", False)
                    ):
                        continue
                    broker = snapshot_orders.get(str(order["broker_order_id"]))
                    if broker is None:
                        raise _TransientBrokerSnapshot(
                            "non-terminal exit order is missing from the fresh "
                            "broker snapshot"
                        )
                    self._update_broker_order(order, broker)
                current_positions = _positive_positions(current)
                current_working = _working_orders(current)
                current_frozen = self._frozen_cash(current)
                owned = None
                if not current_working:
                    owned = self._owned_positions(current)
                return (
                    current,
                    current_positions,
                    current_working,
                    current_frozen,
                    owned,
                )

            snapshot, positions, working, frozen, owned = self._liquidation_read(
                read_flat_state,
                close_at,
                "final account snapshot",
            )
            if not positions and not working and frozen <= _FROZEN_CASH_EPSILON:
                self._state["status"] = "FLAT"
                self._state["flat"] = True
                self._state["reconciliation_required"] = False
                self._state["completed_at"] = self._now().isoformat()
                self._persist()
                payload = self._public_payload(self._state)
                self._emit("flat", **payload)
                return payload
            for order in self._state["orders"]:
                if order["kind"] == "EXIT" and order["send_state"] == "ACKNOWLEDGED":
                    if (
                        not order.get("is_terminal", False)
                        and order.get("cancel") is None
                        and self._now() < close_at
                        and (
                            self._now()
                            - datetime.fromisoformat(str(order["acknowledged_at"]))
                        ).total_seconds()
                        >= _EXIT_REPRICE_SECONDS
                    ):
                        result = self._cancel_once(order)
                        if result is not None:
                            cancel = order.get("cancel")
                            if not (
                                isinstance(cancel, Mapping)
                                and cancel.get("state") == "SEND_STARTED"
                            ):
                                return result
                            recovered = self._retry_recovery_reads(close_at)
                            if recovered is not None:
                                return recovered
            if self._now() >= close_at:
                self._halt(
                    "final broker state is not flat at market close",
                    reconciliation=bool(working or frozen > _FROZEN_CASH_EPSILON),
                )
            # A terminal exit with a remaining owned position may get one new
            # attempt. A working/unknown exit is never duplicated.
            if positions and not working:
                assert owned is not None
                active_exit_symbols = {
                    item["symbol"]
                    for item in self._state["orders"]
                    if item["kind"] == "EXIT" and not item.get("is_terminal", False)
                }
                retry_symbols = tuple(symbol for symbol in owned if symbol not in active_exit_symbols)
                if retry_symbols:
                    def read_retry_quotes() -> dict[str, dict[str, object]]:
                        rows = self._quotes(self._now(), retry_symbols)
                        self._release_quote_context()
                        return {row["code"]: row for row in rows}

                    quote_by_code = self._liquidation_read(
                        read_retry_quotes,
                        close_at,
                        "replacement exit quotes",
                    )
                    for symbol in retry_symbols:
                        quote = quote_by_code[symbol]
                        price = _marketable_price(
                            quote["bid_price"], quote["price_spread"], "SELL",
                            self.config.marketable_limit_buffer_bps,
                        )
                        result = self._submit_intent(
                            self._intent(
                                symbol,
                                "SELL",
                                owned[symbol],
                                price,
                                "EXIT",
                                quoted_at=quote["observed_at"],
                            )
                        )
                        if result is not None:
                            latest = self._state["orders"][-1]
                            if latest.get("send_state") != "SEND_STARTED":
                                return result
                            recovered = self._retry_recovery_reads(close_at)
                            if recovered is not None:
                                return recovered
            self._sleep_bounded(close_at)

    def _owned_positions(
        self, snapshot: Mapping[str, object]
    ) -> dict[str, int]:
        assert self._state is not None
        owned: dict[str, int] = {}
        for order in self._state["orders"]:
            sign = 1 if order["side"] == "BUY" else -1
            owned[order["symbol"]] = owned.get(order["symbol"], 0) + sign * int(
                order.get("dealt_qty", 0)
            )
        broker_positions = {
            _us_code(item["code"]): _whole_nonnegative(item.get("qty", 0), "position qty")
            for item in _positive_positions(snapshot)
        }
        expected = {symbol: qty for symbol, qty in owned.items() if qty > 0}
        if any(qty < 0 for qty in owned.values()):
            self._halt("broker positions do not equal canary-owned filled quantity")
        total_bought: dict[str, int] = {}
        for order in self._state["orders"]:
            if order["side"] == "BUY":
                total_bought[order["symbol"]] = total_bought.get(
                    order["symbol"], 0
                ) + int(order.get("dealt_qty", 0))
        if any(
            symbol not in total_bought or quantity > total_bought[symbol]
            for symbol, quantity in broker_positions.items()
        ):
            self._halt("broker contains a position outside canary-owned fills")
        if broker_positions != expected:
            raise _TransientBrokerSnapshot(
                "broker positions and order fills are not yet atomic"
            )
        for item in _positive_positions(snapshot):
            symbol = _us_code(item["code"])
            sellable = _whole_nonnegative(item.get("can_sell_qty", 0), "can_sell_qty")
            if sellable < expected[symbol]:
                raise _TransientBrokerSnapshot(
                    "canary-owned position is not yet fully sellable"
                )
        return expected

    def _require_clean_start(self, snapshot: Mapping[str, object]) -> None:
        if _positive_positions(snapshot):
            raise MoomooPaperCanaryHalt("startup requires an account with no positions")
        if _working_orders(snapshot):
            raise MoomooPaperCanaryHalt("startup requires no non-terminal orders")
        if self._frozen_cash(snapshot) > _FROZEN_CASH_EPSILON:
            raise MoomooPaperCanaryHalt("startup requires frozen_cash=0")

    def _require_no_foreign_activity(self, snapshot: Mapping[str, object]) -> None:
        assert self._state is not None
        tracked = {
            str(item["broker_order_id"])
            for item in self._state["orders"]
            if item.get("broker_order_id") is not None
        }
        foreign = [
            item
            for item in _working_orders(snapshot)
            if str(item.get("order_id")) not in tracked
        ]
        if foreign:
            self._halt("unowned working broker order detected")

    def _snapshot(self) -> dict[str, object]:
        snapshot = self.client.snapshot()
        if not isinstance(snapshot, Mapping):
            raise MoomooPaperCanaryHalt("broker snapshot must be a mapping")
        if str(snapshot.get("trd_env")) != "SIMULATE":
            raise MoomooPaperCanaryHalt("broker snapshot is not SIMULATE")
        if snapshot.get("broker") != "moomoo":
            raise MoomooPaperCanaryHalt("broker snapshot is not from Moomoo")
        if snapshot.get("market") != "US":
            raise MoomooPaperCanaryHalt("broker snapshot is not for the US market")
        if snapshot.get("acc_id") is None:
            raise MoomooPaperCanaryHalt("broker snapshot is missing acc_id")
        _mapping(snapshot.get("account"), "snapshot.account")
        if not isinstance(snapshot.get("positions"), Sequence):
            raise MoomooPaperCanaryHalt("broker snapshot positions are invalid")
        if not isinstance(snapshot.get("orders"), Sequence):
            raise MoomooPaperCanaryHalt("broker snapshot orders are invalid")
        return dict(snapshot)

    def _session(self, day: date) -> dict[str, object]:
        row = self.client.trading_day(day)
        if not isinstance(row, Mapping) or row.get("time") != day.isoformat():
            raise MoomooPaperCanaryHalt("broker trading calendar returned the wrong day")
        session_type = str(row.get("trade_date_type", ""))
        if session_type not in {"WHOLE", "HALF"}:
            raise MoomooPaperCanaryHalt("day is not a supported US trading session")
        close_time = time(16, 0) if session_type == "WHOLE" else time(13, 0)
        close_at = datetime.combine(day, close_time, EASTERN)
        return {
            "trading_day": day.isoformat(),
            "session_type": session_type,
            "open_at": datetime.combine(day, time(9, 30), EASTERN),
            "close_at": close_at,
            "liquidation_at": close_at
            - timedelta(minutes=self.config.liquidation_lead_minutes),
        }

    def _quotes(
        self,
        now: datetime,
        symbols: Sequence[str] | None = None,
    ) -> list[dict[str, object]]:
        codes = tuple(_us_code(item) for item in (symbols or self.config.symbols))
        rows = self.client.market_snapshot(codes)
        if not isinstance(rows, Sequence) or len(rows) != len(codes):
            raise _TransientMarketRead(
                "market snapshot has incomplete symbol coverage"
            )
        normalized = []
        for expected, raw in zip(codes, rows):
            row = dict(_mapping(raw, "market quote"))
            if _us_code(row.get("code")) != expected:
                raise _TransientMarketRead(
                    "market snapshot order/code mismatch"
                )
            observed = datetime.strptime(
                str(row.get("update_time")), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=EASTERN)
            age = (now - observed).total_seconds()
            if age < -2.0 or age > self.config.max_quote_age_seconds:
                raise _TransientMarketRead(
                    "market quote is stale or future-dated for %s: age=%.3fs"
                    % (expected, age)
                )
            if row.get("suspension") is not False:
                raise _TransientMarketRead("market quote is suspended")
            _positive_finite(row.get("bid_price"), "bid_price")
            _positive_finite(row.get("ask_price"), "ask_price")
            _positive_finite(row.get("price_spread"), "price_spread")
            _positive_int(row.get("lot_size"), "lot_size")
            row["code"] = expected
            row["observed_at"] = observed.isoformat()
            normalized.append(row)
        return normalized

    def _new_state(
        self,
        snapshot: Mapping[str, object],
        session: Mapping[str, object],
        now: datetime,
    ) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "mode": "moomoo_engineering_paper_canary",
            "run_id": uuid4().hex,
            "acc_id": str(snapshot["acc_id"]),
            "trading_day": session["trading_day"],
            "session_type": session["session_type"],
            "open_at": session["open_at"].isoformat(),
            "close_at": session["close_at"].isoformat(),
            "liquidation_at": session["liquidation_at"].isoformat(),
            "created_at": now.isoformat(),
            "status": "READY",
            "flat": False,
            "halted": False,
            "reconciliation_required": False,
            "containment_mode": False,
            "symbols": list(self.config.symbols),
            "config": json.loads(
                json.dumps(asdict(self.config), sort_keys=True)
            ),
            "orders": [],
        }

    def _load_state(self, *, required: bool) -> dict[str, Any] | None:
        if not self.state_path.exists():
            if required:
                raise MoomooPaperCanaryHalt("canary state file is missing")
            return None
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MoomooPaperCanaryHalt("canary state file is unreadable") from exc
        if not isinstance(state, dict) or state.get("schema_version") != _SCHEMA_VERSION:
            raise MoomooPaperCanaryHalt("canary state schema is invalid")
        if not isinstance(state.get("orders"), list):
            raise MoomooPaperCanaryHalt("canary state orders are invalid")
        if state.get("status") not in {
            "READY", "WAITING_OPEN", "ENTERING", "ACTIVE", "LIQUIDATING",
            "RECOVERY_REQUIRED", "FLAT", "SKIPPED", "HALT",
        }:
            raise MoomooPaperCanaryHalt("canary state status is invalid")
        if not isinstance(state.get("run_id"), str) or not _is_hex_id(
            state["run_id"]
        ):
            raise MoomooPaperCanaryHalt("canary state run_id is invalid")
        if not isinstance(state.get("acc_id"), str) or not state["acc_id"].isdigit():
            raise MoomooPaperCanaryHalt("canary state acc_id is invalid")
        try:
            date.fromisoformat(str(state.get("trading_day")))
        except ValueError as exc:
            raise MoomooPaperCanaryHalt("canary trading_day is invalid") from exc
        if not isinstance(state.get("symbols"), list) or not state["symbols"]:
            raise MoomooPaperCanaryHalt("canary state symbols are invalid")
        try:
            state_symbols = tuple(_us_code(item) for item in state["symbols"])
        except ValueError as exc:
            raise MoomooPaperCanaryHalt("canary state symbols are invalid") from exc
        if len(set(state_symbols)) != len(state_symbols):
            raise MoomooPaperCanaryHalt("canary state symbols are duplicated")
        if not isinstance(state.get("config"), dict):
            raise MoomooPaperCanaryHalt("canary state config is invalid")
        markers: set[str] = set()
        client_ids: set[str] = set()
        broker_ids: set[str] = set()
        for order in state["orders"]:
            try:
                self._validate_persisted_order(
                    order,
                    symbols=set(state_symbols),
                    markers=markers,
                    client_ids=client_ids,
                    broker_ids=broker_ids,
                )
            except ValueError as exc:
                raise MoomooPaperCanaryHalt(
                    "persisted order contract is invalid"
                ) from exc
        if state["status"] == "RECOVERY_REQUIRED" and not state.get(
            "reconciliation_required"
        ):
            raise MoomooPaperCanaryHalt(
                "recovery state must require reconciliation"
            )
        if not isinstance(state.get("containment_mode", False), bool):
            raise MoomooPaperCanaryHalt("canary containment_mode is invalid")
        if state["status"] in {"FLAT", "SKIPPED"} and state.get("flat") is not True:
            raise MoomooPaperCanaryHalt("completed canary state must be flat")
        recovery_session = state.get("recovery_session")
        if recovery_session is not None:
            if not isinstance(recovery_session, dict):
                raise MoomooPaperCanaryHalt("canary recovery_session is invalid")
            try:
                date.fromisoformat(str(recovery_session.get("trading_day")))
                if recovery_session.get("session_type") not in {"WHOLE", "HALF"}:
                    raise ValueError("session_type")
                for field in ("open_at", "close_at", "liquidation_at"):
                    value = datetime.fromisoformat(str(recovery_session.get(field)))
                    if value.tzinfo is None:
                        raise ValueError(field)
            except ValueError as exc:
                raise MoomooPaperCanaryHalt(
                    "canary recovery_session is invalid"
                ) from exc
        return state

    def _validate_persisted_order(
        self,
        order: object,
        *,
        symbols: set[str],
        markers: set[str],
        client_ids: set[str],
        broker_ids: set[str],
    ) -> None:
        if not isinstance(order, dict):
            raise MoomooPaperCanaryHalt("persisted order must be an object")
        client_id = order.get("client_order_id")
        if not isinstance(client_id, str) or not _is_hex_id(client_id):
            raise MoomooPaperCanaryHalt("persisted client_order_id is invalid")
        if client_id in client_ids:
            raise MoomooPaperCanaryHalt("persisted client_order_id is duplicated")
        client_ids.add(client_id)
        marker = order.get("submission_marker")
        if marker != f"STOCKBOT:C:{client_id}" or marker in markers:
            raise MoomooPaperCanaryHalt("persisted submission_marker is invalid")
        markers.add(marker)
        try:
            symbol = _us_code(order.get("symbol"))
        except ValueError as exc:
            raise MoomooPaperCanaryHalt("persisted order symbol is invalid") from exc
        if symbol not in symbols:
            raise MoomooPaperCanaryHalt("persisted order symbol is outside the canary universe")
        kind = order.get("kind")
        side = order.get("side")
        if (kind, side) not in {("ENTRY", "BUY"), ("EXIT", "SELL")}:
            raise MoomooPaperCanaryHalt("persisted order kind/side is invalid")
        if order.get("time_in_force") != "DAY" or order.get("session") != "RTH":
            raise MoomooPaperCanaryHalt("persisted order execution terms are invalid")
        quantity = _positive_int(order.get("quantity"), "persisted quantity")
        _positive_finite(order.get("price"), "persisted price")
        if order.get("intent_hash") != _intent_hash(order):
            raise MoomooPaperCanaryHalt("persisted order intent hash is invalid")
        send_state = order.get("send_state")
        if send_state not in {
            "INTENT_PERSISTED",
            "SEND_STARTED",
            "ACKNOWLEDGED",
            "ABORTED",
        }:
            raise MoomooPaperCanaryHalt("persisted order send_state is invalid")
        if "quoted_at" in order:
            _aware_datetime(order.get("quoted_at"), "quoted_at")
        send_started = order.get("send_started_at")
        acknowledged = order.get("acknowledged_at")
        if send_state in {"INTENT_PERSISTED", "ABORTED"}:
            if send_started is not None or acknowledged is not None:
                raise MoomooPaperCanaryHalt(
                    "unsent intent must not contain broker timestamps"
                )
            if send_state == "ABORTED":
                _aware_datetime(order.get("aborted_at"), "aborted_at")
                if (
                    order.get("order_status") != "ABORTED"
                    or order.get("is_terminal") is not True
                ):
                    raise MoomooPaperCanaryHalt(
                        "aborted intent terminal state is invalid"
                    )
        else:
            started_at = _aware_datetime(send_started, "send_started_at")
            if send_state == "ACKNOWLEDGED":
                acknowledged_at = _aware_datetime(
                    acknowledged, "acknowledged_at"
                )
                if acknowledged_at < started_at:
                    raise MoomooPaperCanaryHalt(
                        "acknowledged_at precedes send_started_at"
                    )
            elif acknowledged is not None:
                raise MoomooPaperCanaryHalt(
                    "unacknowledged order must not contain acknowledged_at"
                )
        broker_id = order.get("broker_order_id")
        if send_state == "ACKNOWLEDGED":
            if (
                not isinstance(broker_id, str)
                or not broker_id.isdigit()
                or int(broker_id) <= 0
                or broker_id in broker_ids
            ):
                raise MoomooPaperCanaryHalt("persisted broker_order_id is invalid")
            broker_ids.add(broker_id)
        elif broker_id is not None:
            raise MoomooPaperCanaryHalt(
                "unacknowledged order must not contain broker_order_id"
            )
        dealt = _whole_nonnegative(
            order.get("dealt_qty", 0), "persisted dealt_qty"
        )
        if dealt > quantity:
            raise MoomooPaperCanaryHalt(
                "persisted dealt quantity exceeds intended quantity"
            )
        cancel = order.get("cancel")
        if cancel is not None:
            if send_state != "ACKNOWLEDGED" or not isinstance(cancel, dict):
                raise MoomooPaperCanaryHalt("persisted cancel state is invalid")
            if cancel.get("state") not in {
                "INTENT_PERSISTED",
                "SEND_STARTED",
                "ACKNOWLEDGED",
                "CONFIRMED",
            }:
                raise MoomooPaperCanaryHalt("persisted cancel state is invalid")
            action_id = cancel.get("client_action_id")
            if not isinstance(action_id, str) or not _is_hex_id(action_id):
                raise MoomooPaperCanaryHalt(
                    "persisted cancel client_action_id is invalid"
                )
            cancel_started = cancel.get("send_started_at")
            if cancel.get("state") == "INTENT_PERSISTED":
                if cancel_started is not None:
                    raise MoomooPaperCanaryHalt(
                        "unsent cancel must not contain send_started_at"
                    )
            elif cancel_started is not None:
                _aware_datetime(
                    cancel_started,
                    "cancel.send_started_at",
                )
            elif cancel.get("state") != "CONFIRMED":
                raise MoomooPaperCanaryHalt(
                    "active cancel is missing send_started_at"
                )

    def _validate_state_binding(
        self,
        state: Mapping[str, object],
        snapshot: Mapping[str, object],
    ) -> None:
        self._validate_state_account_binding(state, snapshot.get("acc_id"))
        self._validate_state_config_binding(state)

    def _validate_state_account_binding(
        self,
        state: Mapping[str, object],
        acc_id: object,
    ) -> None:
        if str(state.get("acc_id")) != str(acc_id):
            raise MoomooPaperCanaryHalt("state file is bound to a different acc_id")

    def _validate_state_config_binding(
        self,
        state: Mapping[str, object],
    ) -> None:
        if tuple(state.get("symbols", ())) != self.config.symbols:
            raise MoomooPaperCanaryHalt("state file is bound to a different symbol set")
        expected_config = json.loads(json.dumps(asdict(self.config), sort_keys=True))
        if state.get("config") != expected_config:
            raise MoomooPaperCanaryHalt("state file is bound to a different canary config")

    def _recovery_required(
        self, reason: str, cause: BaseException
    ) -> dict[str, object]:
        assert self._state is not None
        self._state["status"] = "RECOVERY_REQUIRED"
        self._state["reconciliation_required"] = True
        self._state["last_error"] = "%s: %s" % (type(cause).__name__, cause)
        self._persist()
        payload = self._public_payload(self._state)
        payload["reason"] = reason
        self._emit("reconciliation_required", **payload)
        return payload

    def _halt(self, reason: str, *, reconciliation: bool = False) -> None:
        if self._state is not None:
            self._state["status"] = "HALT"
            self._state["flat"] = False
            self._state["halted"] = True
            self._state["reconciliation_required"] = bool(reconciliation)
            self._state["halt_reason"] = reason
            self._state["halted_at"] = self._now().isoformat()
            self._persist()
            payload = self._public_payload(self._state)
        else:
            payload = {
                "mode": "moomoo_engineering_paper_canary",
                "status": "HALT",
                "ok": False,
                "flat": False,
                "halted": True,
                "reconciliation_required": bool(reconciliation),
                "reason": reason,
            }
        self._emit("halted", **payload)
        raise MoomooPaperCanaryHalt(
            reason,
            payload=payload,
            reconciliation_required=reconciliation,
        )

    def _persist(self) -> None:
        assert self._state is not None
        _atomic_write_json(self.state_path, self._state)

    def _archive_completed_state(self, state: Mapping[str, object]) -> None:
        archive_dir = self.state_path.parent / (
            self.state_path.stem + "_sessions"
        )
        archive_path = archive_dir / (
            "%s_%s.json" % (state["trading_day"], state["run_id"])
        )
        if archive_path.exists():
            try:
                existing = json.loads(archive_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise MoomooPaperCanaryHalt(
                    "completed canary archive is unreadable"
                ) from exc
            if existing != dict(state):
                raise MoomooPaperCanaryHalt(
                    "completed canary archive conflicts with durable state"
                )
            return
        _atomic_write_json(archive_path, state)
        self._emit("session_archived", archive_path=str(archive_path))

    def _public_payload(self, state: Mapping[str, object]) -> dict[str, object]:
        recovery_session = state.get("recovery_session")
        effective_session = (
            recovery_session if isinstance(recovery_session, Mapping) else state
        )
        return {
            "mode": "moomoo_engineering_paper_canary",
            "status": state["status"],
            "ok": state["status"] in {"FLAT", "SKIPPED"},
            "flat": bool(state.get("flat", False)),
            "halted": state["status"] == "HALT",
            "reconciliation_required": bool(
                state.get("reconciliation_required", False)
            ),
            "run_id": state.get("run_id"),
            "acc_id": state.get("acc_id"),
            "trading_day": state.get("trading_day"),
            "effective_trading_day": effective_session.get("trading_day"),
            "session_type": effective_session.get("session_type"),
            "close_at": effective_session.get("close_at"),
            "liquidation_at": effective_session.get("liquidation_at"),
            "order_count": len(state.get("orders", ())),
            "state_path": str(self.state_path),
            "lock_path": str(self.lock_path),
        }

    def _emit(self, event: str, **fields: object) -> None:
        payload = {"event": event, "observed_at": self._now().isoformat()}
        payload.update(fields)
        try:
            self._event_sink(payload)
        except Exception:
            # Observability must never split the durable SEND_STARTED boundary
            # from the broker call or strand a position that still needs to be
            # flattened. The state file remains the source of truth.
            return

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise MoomooPaperCanaryHalt("clock must return a timezone-aware datetime")
        return value.astimezone(EASTERN)

    def _wait_until(self, target: datetime) -> None:
        while self._now() < target:
            self._sleep_bounded(target)

    def _sleep_bounded(self, target: datetime) -> None:
        remaining = (target - self._now()).total_seconds()
        if remaining > 0:
            self._sleep(min(self.config.poll_interval_seconds, remaining))

    def _liquidation_read(
        self,
        reader: Callable[[], Any],
        close_at: datetime,
        operation: str,
    ) -> Any:
        """Retry transient liquidation reads until success or market close."""

        assert self._state is not None
        failures = 0
        while True:
            try:
                return reader()
            except Exception as exc:
                if isinstance(exc, MoomooPaperCanaryHalt) and not isinstance(
                    exc, (_TransientMarketRead, _TransientBrokerSnapshot)
                ):
                    raise
                failures += 1
                self._state["reconciliation_required"] = True
                self._state["last_error"] = "%s: %s" % (
                    type(exc).__name__,
                    exc,
                )
                self._persist()
                self._emit(
                    "liquidation_read_retry",
                    operation=operation,
                    attempt=failures,
                    error_type=type(exc).__name__,
                )
                now = self._now()
                if now >= close_at:
                    self._halt(
                        "%s remained unavailable at market close" % operation,
                        reconciliation=True,
                    )
                self._sleep(
                    min(
                        _MIN_BROKER_POLL_SECONDS,
                        max(0.0, (close_at - now).total_seconds()),
                    )
                )

    def _frozen_cash(self, snapshot: Mapping[str, object]) -> float:
        account = _mapping(snapshot["account"], "snapshot.account")
        if "frozen_cash" not in account:
            raise MoomooPaperCanaryHalt("snapshot.account is missing frozen_cash")
        return _nonnegative_finite(account["frozen_cash"], "frozen_cash")

    def _release_quote_context(self) -> None:
        close_quote = getattr(self.client, "close_quote_context", None)
        if callable(close_quote):
            close_quote()


class _SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self) -> "_SingleInstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise MoomooPaperCanaryHalt("another canary process holds the lock") from exc
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % path.name, suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _intent_hash(order: Mapping[str, object]) -> str:
    immutable = {
        "client_order_id": order.get("client_order_id"),
        "submission_marker": order.get("submission_marker"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "quantity": order.get("quantity"),
        "price": order.get("price"),
        "time_in_force": order.get("time_in_force"),
        "session": order.get("session"),
        "kind": order.get("kind"),
    }
    if "quoted_at" in order:
        immutable["quoted_at"] = order.get("quoted_at")
    encoded = json.dumps(
        immutable, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_hex_id(value: str) -> bool:
    return len(value) == 32 and all(
        character in "0123456789abcdef" for character in value
    )


def _effective_trading_day(state: Mapping[str, object]) -> str:
    recovery_session = state.get("recovery_session")
    if isinstance(recovery_session, Mapping):
        return str(recovery_session["trading_day"])
    return str(state["trading_day"])


def _halt_origin_day(state: Mapping[str, object]) -> str:
    halted_at = state.get("halted_at")
    if halted_at is None:
        return str(state["trading_day"])
    return _aware_datetime(halted_at, "halted_at").date().isoformat()


def _aware_datetime(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise MoomooPaperCanaryHalt(
            "%s must be an ISO timestamp" % field
        ) from exc
    if parsed.tzinfo is None:
        raise MoomooPaperCanaryHalt(
            "%s must be timezone-aware" % field
        )
    return parsed.astimezone(EASTERN)


def _same_broker_order_contract(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    try:
        if ("price" in left) != ("price" in right):
            return False
        prices_match = "price" not in left or math.isclose(
            _positive_finite(left.get("price"), "broker price"),
            _positive_finite(right.get("price"), "broker price"),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
        return (
            _us_code(left.get("code")) == _us_code(right.get("code"))
            and str(left.get("trd_side", "")).upper()
            == str(right.get("trd_side", "")).upper()
            and _whole_nonnegative(left.get("qty"), "broker qty")
            == _whole_nonnegative(right.get("qty"), "broker qty")
            and prices_match
            and str(left.get("remark", "")) == str(right.get("remark", ""))
        )
    except (TypeError, ValueError):
        return False


def _working_orders(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    return [
        _mapping(item, "snapshot order")
        for item in snapshot.get("orders", ())
        if not _is_terminal(_mapping(item, "snapshot order"))
    ]


def _positive_positions(snapshot: Mapping[str, object]) -> list[Mapping[str, object]]:
    result = []
    for raw in snapshot.get("positions", ()):
        item = _mapping(raw, "snapshot position")
        quantity = _nonnegative_finite(item.get("qty", 0), "position qty")
        if quantity > 0:
            result.append(item)
    return result


def _is_terminal(order: Mapping[str, object]) -> bool:
    if isinstance(order.get("is_terminal"), bool):
        return bool(order["is_terminal"])
    return str(order.get("order_status", "")).upper() in _TERMINAL_STATUSES


def _marketable_price(
    reference: object,
    tick: object,
    side: str,
    buffer_bps: float,
) -> float:
    reference_decimal = Decimal(str(_positive_finite(reference, "reference price")))
    tick_decimal = Decimal(str(_positive_finite(tick, "price_spread")))
    factor = Decimal("1") + (
        Decimal(str(buffer_bps)) / Decimal("10000")
        if side == "BUY"
        else -Decimal(str(buffer_bps)) / Decimal("10000")
    )
    raw = reference_decimal * factor
    rounding = ROUND_CEILING if side == "BUY" else ROUND_FLOOR
    ticks = (raw / tick_decimal).to_integral_value(rounding=rounding)
    result = ticks * tick_decimal
    if result <= 0:
        raise ValueError("marketable limit price must be positive")
    return float(result)


def _us_code(value: object) -> str:
    text = str(value).strip().upper()
    if text.startswith("US."):
        symbol = text[3:]
    elif "." not in text:
        symbol = text
    else:
        raise ValueError("canary only accepts US equity symbols")
    if not symbol or not all(character.isalnum() or character in "-" for character in symbol):
        raise ValueError("invalid US equity symbol")
    return "US.%s" % symbol


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise MoomooPaperCanaryHalt("%s must be a mapping" % name)
    return value


def _positive_finite(value: object, name: str) -> float:
    result = _nonnegative_finite(value, name)
    if result <= 0:
        raise ValueError("%s must be positive" % name)
    return result


def _nonnegative_finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError("%s must be a finite number" % name)
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("%s must be a finite number" % name) from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("%s must be a non-negative finite number" % name)
    return result


def _positive_int(value: object, name: str) -> int:
    result = _whole_nonnegative(value, name)
    if result <= 0:
        raise ValueError("%s must be positive" % name)
    return result


def _whole_nonnegative(value: object, name: str) -> int:
    number = _nonnegative_finite(value, name)
    if not number.is_integer():
        raise ValueError("%s must be an integer" % name)
    return int(number)


MoomooPaperConfig = MoomooPaperCanaryConfig
