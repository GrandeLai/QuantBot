"""Fail-closed Moomoo OpenD adapter for US equity simulation accounts."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta
import importlib
import math
from numbers import Integral, Real
import socket
import threading
import time
from typing import Any
from uuid import uuid4


_SECURITY_FIRMS = frozenset({"FUTUINC"})
_SIMULATED_STOCK_ACCOUNT_TYPE = "STOCK_AND_OPTION"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SUBMISSION_HISTORY_DAYS = 90
_MARKET_SNAPSHOT_FIELDS = (
    "code",
    "update_time",
    "last_price",
    "bid_price",
    "ask_price",
    "lot_size",
    "price_spread",
    "suspension",
)
_ORDER_STATUSES = frozenset(
    {
        "UNSUBMITTED",
        "WAITING_SUBMIT",
        "SUBMITTING",
        "SUBMIT_FAILED",
        "TIMEOUT",
        "SUBMITTED",
        "FILLED_PART",
        "FILLED_ALL",
        "CANCELLING_PART",
        "CANCELLING_ALL",
        "CANCELLED_PART",
        "CANCELLED_ALL",
        "FAILED",
        "DISABLED",
        "DELETED",
        "FILL_CANCELLED",
    }
)
_TERMINAL_ORDER_STATUSES = frozenset(
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
_ORDER_SIDES = frozenset({"BUY", "SELL", "SELL_SHORT", "BUY_BACK"})
_API_RATE_LIMITS = {
    "accinfo_query": (10, 30.0, 0.0),
    "position_list_query": (10, 30.0, 0.0),
    "order_list_query": (10, 30.0, 0.0),
    "history_order_list_query": (10, 30.0, 0.0),
    "place_order": (15, 30.0, 0.02),
    "modify_order": (20, 30.0, 0.04),
}


class _SlidingWindowRateLimiter:
    """Serialize one in-process SDK quota with a monotonic sliding window."""

    def __init__(
        self,
        *,
        limit: int,
        window: float,
        minimum_interval: float,
        clock: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> None:
        self._limit = limit
        self._window = window
        self._minimum_interval = minimum_interval
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            while True:
                now = float(self._clock())
                if not math.isfinite(now):
                    raise MoomooApiError(
                        "Moomoo API rate-limit clock returned a non-finite value"
                    )
                cutoff = now - self._window
                while self._calls and self._calls[0] <= cutoff:
                    self._calls.popleft()

                allowed_at = now
                if self._calls:
                    allowed_at = max(
                        allowed_at,
                        self._calls[-1] + self._minimum_interval,
                    )
                if len(self._calls) >= self._limit:
                    allowed_at = max(
                        allowed_at,
                        self._calls[0] + self._window,
                    )
                delay = allowed_at - now
                if delay <= 0:
                    self._calls.append(now)
                    return
                self._sleep(delay)


_PROCESS_RATE_LIMITERS: dict[
    tuple[str, int, str, int, str], _SlidingWindowRateLimiter
] = {}
_PROCESS_RATE_LIMITERS_LOCK = threading.Lock()


class MoomooApiError(RuntimeError):
    """A Moomoo response or local safety invariant could not be trusted."""


class MoomooSubmissionUncertainError(MoomooApiError):
    """A place-order call began but its broker-side outcome is unknown."""

    reconciliation_required = True

    def __init__(
        self,
        *,
        submission_marker: str,
        order: Mapping[str, object],
        cause: BaseException,
    ) -> None:
        self.submission_marker = submission_marker
        self.order = dict(order)
        self.cause = cause
        super().__init__(
            "moomoo_submission_uncertain: reconciliation_required; "
            "submission_marker=%s; cause=%s: %s"
            % (submission_marker, type(cause).__name__, cause)
        )


class MoomooSubmissionReconciliationError(MoomooApiError):
    """A submission marker could not be resolved to one broker order."""

    reconciliation_required = True

    def __init__(self, *, submission_marker: str, cause: BaseException) -> None:
        self.submission_marker = submission_marker
        self.cause = cause
        super().__init__(
            "moomoo_submission_reconciliation_failed: reconciliation_required; "
            "submission_marker=%s; cause=%s: %s"
            % (submission_marker, type(cause).__name__, cause)
        )


class MoomooCancellationUncertainError(MoomooApiError):
    """A cancel call began but the broker-side order state is unknown."""

    reconciliation_required = True

    def __init__(self, *, order_id: str, cause: BaseException) -> None:
        self.order_id = order_id
        self.cause = cause
        super().__init__(
            "moomoo_cancellation_uncertain: reconciliation_required; "
            "order_id=%s; cause=%s: %s"
            % (order_id, type(cause).__name__, cause)
        )


class MoomooOrderReconciliationError(MoomooApiError):
    """An order query failed before the broker state could be reconciled."""

    reconciliation_required = True

    def __init__(self, *, order_id: str, cause: BaseException) -> None:
        self.order_id = order_id
        self.cause = cause
        super().__init__(
            "moomoo_order_reconciliation_failed: reconciliation_required; "
            "order_id=%s; cause=%s: %s"
            % (order_id, type(cause).__name__, cause)
        )


class MoomooSimulatedTradingClient:
    """A deliberately simulation-only wrapper around ``OpenSecTradeContext``.

    The optional SDK, trade-context, and quote-context arguments are
    dependency-injection seams for tests. Normal construction imports
    ``moomoo`` only when the first operation needs a connection.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        acc_id: int | str | None = None,
        security_firm: str = "FUTUINC",
        trd_env: str = "SIMULATE",
        *,
        sdk: object | None = None,
        context: object | None = None,
        quote_context: object | None = None,
        connect_timeout: float = 2.0,
        tcp_connect=None,
        monotonic_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.host = _loopback_host(host)
        self.port = _positive_port(port)
        self.requested_acc_id = _optional_acc_id(acc_id)
        self.security_firm = _enum_name(security_firm, "security_firm")
        if self.security_firm not in _SECURITY_FIRMS:
            raise ValueError("unsupported Moomoo security_firm: %s" % security_firm)
        if _enum_name(trd_env, "trd_env") != "SIMULATE":
            raise ValueError("Moomoo integration only permits TrdEnv.SIMULATE")
        self.connect_timeout = _positive_number(
            connect_timeout, "connect_timeout"
        )
        self._tcp_connect = tcp_connect or socket.create_connection
        if not callable(self._tcp_connect):
            raise ValueError("tcp_connect must be callable")
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._sleep = sleep or time.sleep
        if not callable(self._monotonic_clock):
            raise ValueError("monotonic_clock must be callable")
        if not callable(self._sleep):
            raise ValueError("sleep must be callable")

        self._sdk = sdk
        self._context = context
        self._quote_context_instance = quote_context
        self._selected_acc_id: int | None = None
        self._selected_account: dict[str, object] | None = None
        self._close_error: BaseException | None = None
        self._closed = False

    def __enter__(self) -> "MoomooSimulatedTradingClient":
        self._ensure_open()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()

    @property
    def close_error(self) -> BaseException | None:
        return self._close_error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        context = self._context
        if context is not None:
            self._close_context(context)
        quote_context = self._quote_context_instance
        if quote_context is not None and quote_context is not context:
            self._close_context(quote_context)

    def close_quote_context(self) -> None:
        """Close only the quote socket so later trade writes use one context.

        Moomoo OpenD can route delayed packets across concurrent context
        sockets.  The Paper canary therefore treats quote access as a bounded
        read phase and closes that socket before any non-idempotent trade call.
        A later quote request opens a fresh quote context.
        """

        self._ensure_open()
        context = self._quote_context_instance
        if context is None:
            return
        self._quote_context_instance = None
        if context is not self._context:
            previous_close_error = self._close_error
            self._close_context(context)
            if self._close_error is not previous_close_error:
                raise MoomooApiError(
                    "Moomoo quote context could not be closed before a trade write"
                ) from self._close_error

    def account_id(self) -> str:
        """Return the validated SIMULATE account id without a funds query."""

        return str(self._account_id())

    def snapshot(self) -> dict[str, object]:
        """Return fresh account, position, and today's-order data."""

        context = self._trade_context()
        sdk = self._moomoo_sdk()
        acc_id = self._account_id()
        simulate = self._constant("TrdEnv", "SIMULATE")
        us_market = self._constant("TrdMarket", "US")

        account_rows = self._query_records(
            "accinfo_query",
            context.accinfo_query,
            trd_env=simulate,
            acc_id=acc_id,
            refresh_cache=True,
            currency=self._constant("Currency", "USD"),
        )
        if len(account_rows) != 1:
            raise MoomooApiError(
                "accinfo_query must return exactly one account row; got %d"
                % len(account_rows)
            )
        account = account_rows[0]
        _required_non_negative(account, "total_assets", "accinfo_query")
        _required_non_negative(account, "power", "accinfo_query")
        if "acc_id" in account and _required_acc_id(
            account["acc_id"], "accinfo_query.acc_id"
        ) != acc_id:
            raise MoomooApiError("accinfo_query returned a different acc_id")
        account = dict(account)
        account["acc_id"] = acc_id

        positions = self._query_records(
            "position_list_query",
            context.position_list_query,
            position_market=us_market,
            trd_env=simulate,
            acc_id=acc_id,
            refresh_cache=True,
        )
        for position in positions:
            self._validate_position(position)

        orders = self._query_records(
            "order_list_query",
            context.order_list_query,
            order_market=us_market,
            trd_env=simulate,
            acc_id=acc_id,
            refresh_cache=True,
        )
        orders = [self._validate_order(order) for order in orders]

        # Referencing sdk here makes a missing/invalid SDK fail before returning
        # a seemingly healthy snapshot even when a context was injected.
        if sdk is None:  # pragma: no cover - defensive, _moomoo_sdk rejects this
            raise MoomooApiError("Moomoo SDK is unavailable")
        return {
            "broker": "moomoo",
            "trd_env": "SIMULATE",
            "market": "US",
            "acc_id": acc_id,
            "account": account,
            "positions": positions,
            "orders": orders,
        }

    def orders_snapshot(self) -> list[dict[str, object]]:
        """Return one fresh validated snapshot of all today's US paper orders."""

        rows = self._query_records(
            "order_list_query",
            self._trade_context().order_list_query,
            order_market=self._constant("TrdMarket", "US"),
            trd_env=self._constant("TrdEnv", "SIMULATE"),
            acc_id=self._account_id(),
            refresh_cache=True,
        )
        orders: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in rows:
            order = self._validate_order(raw)
            order_id = str(order["order_id"])
            if order_id in seen:
                raise MoomooApiError(
                    "order_list_query returned duplicate order_id %s" % order_id
                )
            seen.add(order_id)
            orders.append(order)
        return orders

    def history_orders_snapshot(
        self, start: date | str, end: date | str
    ) -> list[dict[str, object]]:
        """Return one validated US paper-order history snapshot, up to 90 days."""

        normalized_start = _iso_day(start)
        normalized_end = _iso_day(end)
        start_day = date.fromisoformat(normalized_start)
        end_day = date.fromisoformat(normalized_end)
        if start_day > end_day:
            raise ValueError("start must be on or before end")
        if (end_day - start_day).days > _SUBMISSION_HISTORY_DAYS - 1:
            raise ValueError("history order date range must not exceed 89 days")

        rows = self._query_records(
            "history_order_list_query",
            self._trade_context().history_order_list_query,
            order_market=self._constant("TrdMarket", "US"),
            start=normalized_start,
            end=normalized_end,
            trd_env=self._constant("TrdEnv", "SIMULATE"),
            acc_id=self._account_id(),
        )
        orders: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in rows:
            order = self._validate_order(raw)
            order_id = str(order["order_id"])
            if order_id in seen:
                raise MoomooApiError(
                    "history_order_list_query returned duplicate order_id %s"
                    % order_id
                )
            seen.add(order_id)
            orders.append(order)
        return orders

    def place_limit_order(
        self,
        code: str,
        side: str,
        quantity: int,
        price: float,
        submission_marker: str | None = None,
        *,
        pre_write_guard: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Submit one US equity RTH limit DAY order without automatic retry."""

        if pre_write_guard is not None and not callable(pre_write_guard):
            raise ValueError("pre_write_guard must be callable")
        normalized_code = _us_code(code)
        normalized_side = _enum_name(side, "side")
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        normalized_quantity = _positive_integer(quantity, "quantity")
        normalized_price = _positive_number(price, "price")
        marker = (
            "STOCKBOT:%s" % uuid4().hex
            if submission_marker is None
            else _place_submission_marker(submission_marker)
        )
        order_summary = {
            "code": normalized_code,
            "side": normalized_side,
            "quantity": normalized_quantity,
            "price": normalized_price,
            "order_type": "NORMAL",
            "time_in_force": "DAY",
            "session": "RTH",
            "trd_env": "SIMULATE",
            "submission_marker": marker,
        }

        context = self._trade_context()
        acc_id = self._account_id()
        self._acquire_api_quota("place_order", acc_id)
        if pre_write_guard is not None:
            pre_write_guard()
        try:
            response = context.place_order(
                price=normalized_price,
                qty=normalized_quantity,
                code=normalized_code,
                trd_side=self._constant("TrdSide", normalized_side),
                order_type=self._constant("OrderType", "NORMAL"),
                trd_env=self._constant("TrdEnv", "SIMULATE"),
                acc_id=acc_id,
                time_in_force=self._constant("TimeInForce", "DAY"),
                session=self._constant("Session", "RTH"),
                remark=marker,
            )
        except Exception as exc:
            raise MoomooSubmissionUncertainError(
                submission_marker=marker,
                order=order_summary,
                cause=exc,
            ) from exc

        try:
            ret, data = self._decode_response("place_order", response)
            ret_ok = self._ret_ok()
        except Exception as exc:
            raise MoomooSubmissionUncertainError(
                submission_marker=marker,
                order=order_summary,
                cause=exc,
            ) from exc
        if ret != ret_ok:
            cause = MoomooApiError(
                "place_order returned non-success: %s"
                % _response_error_message(data)
            )
            raise MoomooSubmissionUncertainError(
                submission_marker=marker,
                order=order_summary,
                cause=cause,
            ) from cause
        try:
            rows = _records(data, "place_order")
            if len(rows) != 1:
                raise MoomooApiError(
                    "place_order must return exactly one order row; got %d"
                    % len(rows)
                )
            order = self._validate_order(rows[0])
            if order["outcome_uncertain"]:
                raise MoomooApiError("place_order returned TIMEOUT order status")
        except Exception as exc:
            raise MoomooSubmissionUncertainError(
                submission_marker=marker,
                order=order_summary,
                cause=exc,
            ) from exc
        order["submission_marker"] = marker
        return order

    def market_snapshot(
        self, codes: Sequence[str]
    ) -> list[dict[str, object]]:
        """Return one trustworthy current snapshot row for every US code."""

        normalized_codes = _unique_us_codes(codes)
        rows = self._query_records(
            "get_market_snapshot",
            self._quote_context().get_market_snapshot,
            code_list=normalized_codes,
        )
        by_code: dict[str, dict[str, object]] = {}
        for raw_row in rows:
            row = self._validate_market_snapshot(raw_row)
            code = row["code"]
            if not isinstance(code, str):  # pragma: no cover - validated above
                raise MoomooApiError("market snapshot returned an invalid code")
            if code in by_code:
                raise MoomooApiError(
                    "get_market_snapshot returned duplicate code %s" % code
                )
            by_code[code] = row
        requested = set(normalized_codes)
        returned = set(by_code)
        if returned != requested:
            missing = sorted(requested - returned)
            unexpected = sorted(returned - requested)
            raise MoomooApiError(
                "get_market_snapshot code mismatch; missing=%s unexpected=%s"
                % (",".join(missing), ",".join(unexpected))
            )
        return [by_code[code] for code in normalized_codes]

    def trading_day(self, day: date | str) -> dict[str, object]:
        """Return one validated US trading-calendar row for ``day``."""

        normalized_day = _iso_day(day)
        rows = self._query_records(
            "request_trading_days",
            self._quote_context().request_trading_days,
            market=self._constant("TradeDateMarket", "US"),
            start=normalized_day,
            end=normalized_day,
        )
        matches = [
            row
            for row in rows
            if row.get("time") == normalized_day
        ]
        if len(matches) != 1 or len(rows) != 1:
            raise MoomooApiError(
                "request_trading_days must return exactly one matching row for "
                "%s; got %d rows and %d matches"
                % (normalized_day, len(rows), len(matches))
            )
        result = dict(matches[0])
        raw_type = _enum_name(
            _required(result, "trade_date_type", "request_trading_days"),
            "request_trading_days.trade_date_type",
        )
        if raw_type == "WHOLE":
            normalized_type = "WHOLE"
        elif raw_type in {"HALF", "MORNING", "AFTERNOON"}:
            # The v10.9 Python SDK names half days by the open session. Expose
            # one stable canary-level classification while retaining the raw
            # value when it carries more detail.
            normalized_type = "HALF"
            if raw_type != "HALF":
                result["trade_session"] = raw_type
        else:
            raise MoomooApiError(
                "request_trading_days returned unsupported trade_date_type: %s"
                % raw_type
            )
        result["time"] = normalized_day
        result["trade_date_type"] = normalized_type
        return result

    def reconcile_submission(
        self, submission_marker: str
    ) -> dict[str, object]:
        """Resolve one generated marker against fresh today/history queries."""

        marker = _submission_marker(submission_marker)
        try:
            today_rows = self._today_order_rows_with_positive_cache_fallback(
                submission_marker=marker
            )
            matches = [
                row for row in today_rows if row.get("remark") == marker
            ]
            if len(matches) > 1:
                raise MoomooApiError(
                    "submission_marker integrity failure: expected one today "
                    "order for %s; got %d" % (marker, len(matches))
                )
            if not matches:
                history_rows = self._recent_history_orders()
                matches = [
                    row for row in history_rows if row.get("remark") == marker
                ]
            if not matches:
                raise MoomooApiError(
                    "no today or recent-history order matched "
                    "submission_marker %s" % marker
                )
            if len(matches) > 1:
                raise MoomooApiError(
                    "submission_marker integrity failure: expected one "
                    "recent-history order for %s; got %d"
                    % (marker, len(matches))
                )
            order = self._validate_order(matches[0])
        except Exception as exc:
            raise MoomooSubmissionReconciliationError(
                submission_marker=marker, cause=exc
            ) from exc
        order["submission_marker"] = marker
        return order

    def query_order(self, order_id: int | str) -> dict[str, object]:
        normalized_order_id = _order_id(order_id)
        try:
            rows = self._today_order_rows_with_positive_cache_fallback(
                order_id=normalized_order_id
            )
            if not rows:
                history_rows = self._recent_history_orders()
                rows = [
                    row
                    for row in history_rows
                    if _order_id(
                        _required(
                            row,
                            "order_id",
                            "history_order_list_query",
                        )
                    )
                    == normalized_order_id
                ]
            if len(rows) != 1:
                raise MoomooApiError(
                    "today/history order queries must return exactly one "
                    "order for %s; got %d"
                    % (normalized_order_id, len(rows))
                )
            order = self._validate_order(rows[0])
            if order["order_id"] != normalized_order_id:
                raise MoomooApiError(
                    "order_list_query returned a different order_id"
                )
            return order
        except Exception as exc:
            raise MoomooOrderReconciliationError(
                order_id=normalized_order_id, cause=exc
            ) from exc

    def _today_order_rows_with_positive_cache_fallback(
        self,
        *,
        order_id: str | None = None,
        submission_marker: str | None = None,
    ) -> list[dict[str, object]]:
        kwargs: dict[str, object] = {
            "order_market": self._constant("TrdMarket", "US"),
            "trd_env": self._constant("TrdEnv", "SIMULATE"),
            "acc_id": self._account_id(),
        }
        if order_id is not None:
            kwargs["order_id"] = order_id
        try:
            return self._query_records(
                "order_list_query",
                self._trade_context().order_list_query,
                **kwargs,
                refresh_cache=True,
            )
        except MoomooApiError as fresh_error:
            try:
                cached = self._query_records(
                    "order_list_query_cached",
                    self._trade_context().order_list_query,
                    **kwargs,
                    refresh_cache=False,
                )
            except MoomooApiError:
                raise fresh_error
            # A cached positive row is useful evidence of existence.  An empty
            # or unrelated cache is never treated as proof that no order was
            # submitted, so preserve the fresh-read failure in that case.
            if not cached:
                raise fresh_error
            if order_id is not None and not any(
                str(row.get("order_id", "")) == order_id for row in cached
            ):
                raise fresh_error
            if submission_marker is not None and not any(
                row.get("remark") == submission_marker for row in cached
            ):
                raise fresh_error
            return cached

    def _recent_history_orders(self) -> list[dict[str, object]]:
        history_end = date.today()
        history_start = history_end - timedelta(
            days=_SUBMISSION_HISTORY_DAYS - 1
        )
        return self.history_orders_snapshot(history_start, history_end)

    def cancel_order(
        self,
        order_id: int | str,
        *,
        pre_write_guard: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        if pre_write_guard is not None and not callable(pre_write_guard):
            raise ValueError("pre_write_guard must be callable")
        normalized_order_id = _order_id(order_id)
        context = self._trade_context()
        acc_id = self._account_id()
        self._acquire_api_quota("modify_order", acc_id)
        if pre_write_guard is not None:
            pre_write_guard()
        try:
            response = context.modify_order(
                modify_order_op=self._constant("ModifyOrderOp", "CANCEL"),
                order_id=normalized_order_id,
                qty=0,
                price=0,
                trd_env=self._constant("TrdEnv", "SIMULATE"),
                acc_id=acc_id,
            )
        except Exception as exc:
            raise MoomooCancellationUncertainError(
                order_id=normalized_order_id, cause=exc
            ) from exc
        try:
            ret, data = self._decode_response("modify_order", response)
            ret_ok = self._ret_ok()
        except Exception as exc:
            raise MoomooCancellationUncertainError(
                order_id=normalized_order_id, cause=exc
            ) from exc
        if ret != ret_ok:
            cause = MoomooApiError(
                "modify_order returned non-success: %s"
                % _response_error_message(data)
            )
            raise MoomooCancellationUncertainError(
                order_id=normalized_order_id, cause=cause
            ) from cause
        try:
            rows = _records(data, "modify_order")
            if len(rows) != 1:
                raise MoomooApiError(
                    "modify_order must return exactly one cancellation row; got %d"
                    % len(rows)
                )
            result = dict(rows[0])
            returned_order_id = _order_id(
                _required(result, "order_id", "modify_order")
            )
            if returned_order_id != normalized_order_id:
                raise MoomooApiError("modify_order returned a different order_id")
        except Exception as exc:
            raise MoomooCancellationUncertainError(
                order_id=normalized_order_id, cause=exc
            ) from exc
        result["order_id"] = returned_order_id
        result["action"] = "cancel_requested"
        result["reconciliation_required"] = True
        return result

    def _moomoo_sdk(self):
        self._ensure_open()
        if self._sdk is None:
            try:
                sdk = importlib.import_module("moomoo")
            except Exception as exc:
                raise MoomooApiError(
                    "moomoo-api could not be loaded; install the StockBot moomoo "
                    "extra and verify its local runtime directories"
                ) from exc
            sys_config = getattr(sdk, "SysConfig", None)
            console_log_setter = getattr(
                sys_config, "enable_console_log", None
            )
            if not callable(console_log_setter):
                raise MoomooApiError(
                    "Moomoo SDK is missing SysConfig.enable_console_log"
                )
            try:
                console_log_setter(False)
            except Exception as exc:
                raise MoomooApiError(
                    "Moomoo SDK console logging could not be disabled"
                ) from exc
            self._sdk = sdk
        return self._sdk

    def _trade_context(self):
        self._ensure_open()
        if self._context is None:
            self._preflight_opend()
            sdk = self._moomoo_sdk()
            factory = getattr(sdk, "OpenSecTradeContext", None)
            if not callable(factory):
                raise MoomooApiError("Moomoo SDK is missing OpenSecTradeContext")
            try:
                context = factory(
                    filter_trdmarket=self._constant("TrdMarket", "US"),
                    host=self.host,
                    port=self.port,
                    security_firm=self._constant(
                        "SecurityFirm", self.security_firm
                    ),
                )
            except Exception as exc:
                raise MoomooApiError(
                    "failed to connect to Moomoo OpenD at %s:%d: %s"
                    % (self.host, self.port, exc)
                ) from exc
            timeout_setter = getattr(
                context, "set_sync_query_connect_timeout", None
            )
            if not callable(timeout_setter):
                self._close_context(context)
                raise MoomooApiError(
                    "Moomoo SDK context is missing "
                    "set_sync_query_connect_timeout"
                )
            try:
                timeout_setter(self.connect_timeout)
            except Exception as exc:
                self._close_context(context)
                raise MoomooApiError(
                    "failed to configure Moomoo SDK sync query timeout: %s" % exc
                ) from exc
            self._context = context
        return self._context

    def _quote_context(self):
        self._ensure_open()
        if self._quote_context_instance is None:
            self._preflight_opend()
            sdk = self._moomoo_sdk()
            factory = getattr(sdk, "OpenQuoteContext", None)
            if not callable(factory):
                raise MoomooApiError("Moomoo SDK is missing OpenQuoteContext")
            try:
                context = factory(
                    host=self.host,
                    port=self.port,
                    security_firm=self._constant(
                        "SecurityFirm", self.security_firm
                    ),
                )
            except Exception as exc:
                raise MoomooApiError(
                    "failed to connect to Moomoo OpenD quote context at %s:%d: %s"
                    % (self.host, self.port, exc)
                ) from exc
            timeout_setter = getattr(
                context, "set_sync_query_connect_timeout", None
            )
            if not callable(timeout_setter):
                self._close_context(context)
                raise MoomooApiError(
                    "Moomoo SDK quote context is missing "
                    "set_sync_query_connect_timeout"
                )
            try:
                timeout_setter(self.connect_timeout)
            except Exception as exc:
                self._close_context(context)
                raise MoomooApiError(
                    "failed to configure Moomoo quote sync query timeout: %s"
                    % exc
                ) from exc
            self._quote_context_instance = context
        return self._quote_context_instance

    def _close_context(self, context) -> None:
        try:
            context.close()
        except Exception as exc:
            if self._close_error is None:
                self._close_error = exc

    def _preflight_opend(self) -> None:
        try:
            connection = self._tcp_connect(
                (self.host, self.port), timeout=self.connect_timeout
            )
        except Exception as exc:
            raise MoomooApiError(
                "Moomoo OpenD TCP preflight failed at %s:%d after %.1fs: %s"
                % (self.host, self.port, self.connect_timeout, exc)
            ) from exc
        close = getattr(connection, "close", None)
        if not callable(close):
            raise MoomooApiError(
                "Moomoo OpenD TCP preflight returned an invalid socket"
            )
        try:
            close()
        except Exception as exc:
            raise MoomooApiError(
                "Moomoo OpenD TCP preflight socket could not be closed: %s" % exc
            ) from exc

    def _account_id(self) -> int:
        if self._selected_acc_id is not None:
            return self._selected_acc_id
        rows = self._query_records(
            "get_acc_list", self._trade_context().get_acc_list
        )
        candidates: list[dict[str, object]] = []
        requested_match: dict[str, object] | None = None
        for row in rows:
            row_acc_id = _required_acc_id(
                _required(row, "acc_id", "get_acc_list"),
                "get_acc_list.acc_id",
            )
            environment = _enum_name(
                _required(row, "trd_env", "get_acc_list"),
                "get_acc_list.trd_env",
            )
            if environment not in {"REAL", "SIMULATE"}:
                raise MoomooApiError(
                    "get_acc_list returned unknown trd_env: %s" % environment
                )
            if self.requested_acc_id == row_acc_id:
                if environment != "SIMULATE":
                    raise MoomooApiError(
                        "requested acc_id %d is not a SIMULATE account"
                        % row_acc_id
                    )
                requested_match = self._validate_simulated_account(
                    row, selected=True
                )
            if self.requested_acc_id is None and environment == "SIMULATE":
                candidate = self._validate_simulated_account(
                    row, selected=False
                )
                if candidate is not None:
                    candidates.append(candidate)

        if self.requested_acc_id is not None:
            if requested_match is None:
                raise MoomooApiError(
                    "requested SIMULATE acc_id %d was not found"
                    % self.requested_acc_id
                )
            selected = requested_match
        else:
            if len(candidates) != 1:
                candidate_ids = sorted(
                    _required_acc_id(item["acc_id"], "get_acc_list.acc_id")
                    for item in candidates
                )
                raise MoomooApiError(
                    "exactly one eligible SIMULATE account is "
                    "required when acc_id is omitted; found %d (%s)"
                    % (len(candidates), ",".join(map(str, candidate_ids)))
                )
            selected = candidates[0]

        self._selected_acc_id = _required_acc_id(
            selected["acc_id"], "get_acc_list.acc_id"
        )
        self._selected_account = dict(selected)
        return self._selected_acc_id

    def _validate_simulated_account(
        self,
        row: Mapping[str, object],
        *,
        selected: bool,
    ) -> dict[str, object] | None:
        account = dict(row)
        status = _enum_name(
            _required(account, "acc_status", "get_acc_list"),
            "get_acc_list.acc_status",
        )
        if status != "ACTIVE":
            if not selected and status == "DISABLED":
                return None
            raise MoomooApiError(
                "SIMULATE account %s is not ACTIVE"
                % account.get("acc_id", "unknown")
            )
        role = _enum_name(
            _required(account, "acc_role", "get_acc_list"),
            "get_acc_list.acc_role",
        )
        if role not in {"NORMAL", "N/A"}:
            if not selected and role in {"MASTER", "IPO"}:
                return None
            raise MoomooApiError(
                "SIMULATE account %s has unsupported acc_role %s"
                % (account.get("acc_id", "unknown"), role)
            )
        sim_type = _enum_name(
            _required(account, "sim_acc_type", "get_acc_list"),
            "get_acc_list.sim_acc_type",
        )
        if sim_type != _SIMULATED_STOCK_ACCOUNT_TYPE:
            if not selected:
                return None
            raise MoomooApiError(
                "SIMULATE account %s is not a STOCK_AND_OPTION account"
                % account.get("acc_id", "unknown")
            )
        markets = _market_names(
            _required(account, "trdmarket_auth", "get_acc_list")
        )
        if "US" not in markets:
            if not selected:
                return None
            raise MoomooApiError(
                "SIMULATE account %s is not authorized for the US market"
                % account.get("acc_id", "unknown")
            )
        account["acc_id"] = _required_acc_id(
            account["acc_id"], "get_acc_list.acc_id"
        )
        account["trd_env"] = "SIMULATE"
        account["acc_status"] = status
        account["acc_role"] = role
        account["sim_acc_type"] = sim_type
        account["trdmarket_auth"] = sorted(markets)
        return account

    def _validate_position(self, position: Mapping[str, object]) -> None:
        code = _us_code(_required(position, "code", "position_list_query"))
        if code != position["code"]:
            position["code"] = code
        _required_non_negative(position, "qty", "position_list_query")
        _required_non_negative(position, "can_sell_qty", "position_list_query")

    def _validate_order(
        self, raw_order: Mapping[str, object]
    ) -> dict[str, object]:
        order = dict(raw_order)
        order["order_id"] = _order_id(
            _required(order, "order_id", "order data")
        )
        order["code"] = _us_code(_required(order, "code", "order data"))
        side = _enum_name(
            _required(order, "trd_side", "order data"),
            "order data.trd_side",
        )
        if side not in _ORDER_SIDES:
            raise MoomooApiError(
                "order data returned unknown trd_side: %s" % side
            )
        order["trd_side"] = side
        status = _enum_name(
            _required(order, "order_status", "order data"),
            "order data.order_status",
        )
        if status not in _ORDER_STATUSES:
            raise MoomooApiError(
                "order data returned unknown order_status: %s" % status
            )
        order["order_status"] = status
        order["is_terminal"] = status in _TERMINAL_ORDER_STATUSES
        order["reconciliation_required"] = not order["is_terminal"]
        order["outcome_uncertain"] = status == "TIMEOUT"
        quantity = _required_non_negative(order, "qty", "order data")
        dealt_quantity = _required_non_negative(
            order, "dealt_qty", "order data"
        )
        _required_non_negative(order, "price", "order data")
        if dealt_quantity > quantity:
            raise MoomooApiError("order data.dealt_qty cannot exceed qty")
        return order

    def _validate_market_snapshot(
        self, raw_row: Mapping[str, object]
    ) -> dict[str, object]:
        row = dict(raw_row)
        for field in _MARKET_SNAPSHOT_FIELDS:
            value = _required(row, field, "get_market_snapshot")
            if isinstance(value, str) and value.strip().upper() == "N/A":
                raise MoomooApiError(
                    "get_market_snapshot.%s must not be N/A" % field
                )
        try:
            row["code"] = _us_code(row["code"])
        except ValueError as exc:
            raise MoomooApiError(str(exc)) from exc
        update_time = _non_empty_text(
            row["update_time"], "get_market_snapshot.update_time"
        )
        try:
            datetime.strptime(update_time, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise MoomooApiError(
                "get_market_snapshot.update_time must use YYYY-MM-DD HH:MM:SS"
            ) from exc
        row["update_time"] = update_time
        for field in ("last_price", "bid_price", "ask_price", "price_spread"):
            try:
                row[field] = _positive_number(
                    row[field], "get_market_snapshot.%s" % field
                )
            except ValueError as exc:
                raise MoomooApiError(str(exc)) from exc
        try:
            row["lot_size"] = _positive_integer(
                row["lot_size"], "get_market_snapshot.lot_size"
            )
        except ValueError as exc:
            raise MoomooApiError(str(exc)) from exc
        if not isinstance(row["suspension"], bool):
            raise MoomooApiError(
                "get_market_snapshot.suspension must be a boolean"
            )
        if row["suspension"]:
            raise MoomooApiError(
                "get_market_snapshot rejected suspended code %s" % row["code"]
            )
        return row

    def _query_records(self, operation: str, function, **kwargs):
        rate_limited_fresh_query = (
            operation == "history_order_list_query"
            or (
                operation
                in {
                    "accinfo_query",
                    "position_list_query",
                    "order_list_query",
                }
                and kwargs.get("refresh_cache") is True
            )
        )
        if rate_limited_fresh_query:
            self._acquire_api_quota(
                operation,
                _required_acc_id(
                    kwargs.get("acc_id"), "%s.acc_id" % operation
                ),
            )
        try:
            response = function(**kwargs)
        except Exception as exc:
            raise MoomooApiError("%s call failed: %s" % (operation, exc)) from exc
        ret, data = self._decode_response(operation, response)
        if ret != self._ret_ok():
            raise MoomooApiError(
                "%s failed: %s"
                % (operation, _response_error_message(data))
            )
        return _records(data, operation)

    def _acquire_api_quota(self, operation: str, acc_id: int) -> None:
        key = (
            self.host,
            self.port,
            self.security_firm,
            acc_id,
            operation,
        )
        with _PROCESS_RATE_LIMITERS_LOCK:
            limiter = _PROCESS_RATE_LIMITERS.get(key)
            if limiter is None:
                limit, window, minimum_interval = _API_RATE_LIMITS[operation]
                limiter = _SlidingWindowRateLimiter(
                    limit=limit,
                    window=window,
                    minimum_interval=minimum_interval,
                    clock=self._monotonic_clock,
                    sleep=self._sleep,
                )
                _PROCESS_RATE_LIMITERS[key] = limiter
        limiter.acquire()

    def _decode_response(self, operation: str, response):
        if (
            not isinstance(response, (tuple, list))
            or len(response) != 2
        ):
            raise MoomooApiError(
                "%s returned a malformed SDK response" % operation
            )
        ret, data = response
        if isinstance(ret, bool) or not isinstance(ret, Integral):
            raise MoomooApiError("%s returned a malformed ret code" % operation)
        return int(ret), data

    def _ret_ok(self) -> int:
        raw = getattr(self._moomoo_sdk(), "RET_OK", None)
        if isinstance(raw, bool) or not isinstance(raw, Integral):
            raise MoomooApiError("Moomoo SDK is missing a valid RET_OK constant")
        return int(raw)

    def _constant(self, namespace: str, name: str):
        owner = getattr(self._moomoo_sdk(), namespace, None)
        if owner is None or not hasattr(owner, name):
            raise MoomooApiError(
                "Moomoo SDK is missing %s.%s" % (namespace, name)
            )
        value = getattr(owner, name)
        if value is None:
            raise MoomooApiError(
                "Moomoo SDK has an invalid %s.%s" % (namespace, name)
            )
        return value

    def _ensure_open(self) -> None:
        if self._closed:
            raise MoomooApiError("Moomoo client is closed")


def _records(data, operation: str) -> list[dict[str, object]]:
    if hasattr(data, "to_dict") and callable(data.to_dict):
        try:
            rows = data.to_dict(orient="records")
        except TypeError as exc:
            raise MoomooApiError(
                "%s returned an unsupported table" % operation
            ) from exc
    elif isinstance(data, Mapping):
        rows = [data]
    elif isinstance(data, Sequence) and not isinstance(
        data, (str, bytes, bytearray)
    ):
        rows = list(data)
    else:
        raise MoomooApiError("%s did not return tabular data" % operation)
    result: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise MoomooApiError(
                "%s row %d is not a mapping" % (operation, index)
            )
        normalized = _plain(dict(row), "%s row %d" % (operation, index))
        if not isinstance(normalized, dict):  # pragma: no cover - guaranteed
            raise MoomooApiError("%s row %d is invalid" % (operation, index))
        result.append(normalized)
    return result


def _plain(value: Any, field_name: str):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            raise MoomooApiError("%s contains a non-finite number" % field_name)
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise MoomooApiError("%s contains a non-string key" % field_name)
            result[key] = _plain(item, "%s.%s" % (field_name, key))
        return result
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [
            _plain(item, "%s[%d]" % (field_name, index))
            for index, item in enumerate(value)
        ]
    item_method = getattr(value, "item", None)
    if callable(item_method):
        try:
            item = item_method()
        except Exception as exc:
            raise MoomooApiError(
                "%s contains an invalid scalar" % field_name
            ) from exc
        if item is not value:
            return _plain(item, field_name)
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, (str, int, float, bool)):
        return _plain(enum_value, field_name)
    raise MoomooApiError(
        "%s contains unsupported value type %s"
        % (field_name, type(value).__name__)
    )


def _required(row: Mapping[str, object], key: str, operation: str):
    if key not in row or row[key] is None:
        raise MoomooApiError(
            "%s is missing required field %s" % (operation, key)
        )
    return row[key]


def _required_non_negative(
    row: Mapping[str, object], key: str, operation: str
) -> float:
    value = _required(row, key, operation)
    try:
        result = _non_negative_number(value, "%s.%s" % (operation, key))
    except ValueError as exc:
        raise MoomooApiError(str(exc)) from exc
    return result


def _non_empty_text(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field_name)
    return value.strip()


def _loopback_host(value) -> str:
    host = _non_empty_text(value, "host").lower()
    if host not in _LOOPBACK_HOSTS:
        raise ValueError(
            "host must be a local Moomoo OpenD endpoint: "
            "127.0.0.1, localhost, or ::1"
        )
    return host


def _enum_name(value, field_name: str) -> str:
    text = _non_empty_text(str(value), field_name)
    return text.rsplit(".", 1)[-1].strip(" '[]\"").upper()


def _market_names(value) -> set[str]:
    if isinstance(value, str):
        items = [item for item in value.strip("[]").split(",") if item.strip()]
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        items = list(value)
    else:
        raise MoomooApiError("get_acc_list.trdmarket_auth must be a sequence")
    if not items:
        raise MoomooApiError("get_acc_list.trdmarket_auth must not be empty")
    return {_enum_name(item, "get_acc_list.trdmarket_auth") for item in items}


def _positive_port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError("port must be an integer between 1 and 65535")
    result = int(value)
    if not 1 <= result <= 65535:
        raise ValueError("port must be an integer between 1 and 65535")
    return result


def _optional_acc_id(value) -> int | None:
    if value is None:
        return None
    return _required_acc_id(value, "acc_id")


def _required_acc_id(value, field_name: str) -> int:
    if isinstance(value, bool):
        raise MoomooApiError("%s must be a positive integer" % field_name)
    if isinstance(value, str):
        text = value.strip()
        if not text.isdecimal():
            raise MoomooApiError("%s must be a positive integer" % field_name)
        result = int(text)
    elif isinstance(value, Integral):
        result = int(value)
    else:
        raise MoomooApiError("%s must be a positive integer" % field_name)
    if result <= 0:
        raise MoomooApiError("%s must be a positive integer" % field_name)
    return result


def _positive_integer(value, field_name: str) -> int:
    try:
        return _required_acc_id(value, field_name)
    except MoomooApiError as exc:
        raise ValueError(str(exc)) from exc


def _non_negative_number(value, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        try:
            result = float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("%s must be a finite non-negative number" % field_name) from exc
    else:
        result = float(value)
    if not math.isfinite(result) or result < 0.0:
        raise ValueError("%s must be a finite non-negative number" % field_name)
    return result


def _positive_number(value, field_name: str) -> float:
    result = _non_negative_number(value, field_name)
    if result <= 0.0:
        raise ValueError("%s must be positive" % field_name)
    return result


def _us_code(value) -> str:
    text = _non_empty_text(str(value), "code").upper()
    if not text.startswith("US.") or len(text) <= 3 or any(
        character.isspace() for character in text
    ):
        raise ValueError("code must be a fully qualified US symbol such as US.AAPL")
    if len(text.encode("utf-8")) > 64:
        raise ValueError("code is too long")
    return text


def _unique_us_codes(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values, Sequence
    ):
        raise ValueError("codes must be a non-empty sequence of US symbols")
    result = [_us_code(value) for value in values]
    if not result:
        raise ValueError("codes must be a non-empty sequence of US symbols")
    if len(set(result)) != len(result):
        raise ValueError("codes must not contain duplicates")
    if len(result) > 400:
        raise ValueError("codes must contain at most 400 symbols")
    return result


def _iso_day(value: date | str) -> str:
    if isinstance(value, datetime):
        raise ValueError("day must be a date or YYYY-MM-DD string")
    if isinstance(value, date):
        return value.isoformat()
    text = _non_empty_text(value, "day")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("day must use YYYY-MM-DD") from exc
    if parsed.isoformat() != text:
        raise ValueError("day must use YYYY-MM-DD")
    return text


def _place_submission_marker(value) -> str:
    marker = _submission_marker(value)
    if marker != value or len(marker.encode("utf-8")) > 64:
        raise ValueError(
            "submission_marker must begin with STOCKBOT: and contain at most "
            "64 UTF-8 bytes"
        )
    return marker


def _submission_marker(value) -> str:
    marker = _non_empty_text(value, "submission_marker")
    if not marker.startswith("STOCKBOT:") or len(marker.encode("utf-8")) > 64:
        raise ValueError(
            "submission_marker must begin with STOCKBOT: and contain at most "
            "64 UTF-8 bytes"
        )
    return marker


def _order_id(value) -> str:
    if isinstance(value, bool):
        raise ValueError("order_id must be a non-empty broker order id")
    if isinstance(value, Integral):
        if value <= 0:
            raise ValueError("order_id must be a non-empty broker order id")
        return str(int(value))
    if not isinstance(value, str):
        raise ValueError("order_id must be a positive integer or decimal string")
    text = _non_empty_text(value, "order_id")
    if not text.isdecimal() or len(text) > 128 or int(text) <= 0:
        raise ValueError("order_id must be a non-empty broker order id")
    return text


def _response_error_message(data) -> str:
    if isinstance(data, str):
        text = data.strip()
        return text[:500] if text else "empty SDK error"
    return "SDK returned %s" % type(data).__name__


__all__ = [
    "MoomooApiError",
    "MoomooCancellationUncertainError",
    "MoomooOrderReconciliationError",
    "MoomooSimulatedTradingClient",
    "MoomooSubmissionReconciliationError",
    "MoomooSubmissionUncertainError",
]
