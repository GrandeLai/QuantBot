"""Minimal in-memory OMS event ledger for unified trading."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from threading import Lock

from quantbot.stocks.broker.types import (
    TradingOrder,
    TradingOrderEvent,
    TradingOrderEventType,
    TradingOrderStatus,
)


def _status_to_event_type(status: TradingOrderStatus) -> TradingOrderEventType:
    mapping = {
        TradingOrderStatus.SUBMITTED: TradingOrderEventType.SUBMITTED,
        TradingOrderStatus.PENDING_SUBMIT: TradingOrderEventType.SUBMITTED,
        TradingOrderStatus.PARTIAL_FILLED: TradingOrderEventType.PARTIAL_FILLED,
        TradingOrderStatus.FILLED: TradingOrderEventType.FILLED,
        TradingOrderStatus.CANCELED: TradingOrderEventType.CANCELED,
        TradingOrderStatus.REJECTED: TradingOrderEventType.REJECTED,
    }
    return mapping.get(status, TradingOrderEventType.UPDATED)


class TradingOrderEventStore:
    """In-memory order event ledger."""

    def __init__(self) -> None:
        self._events: dict[str, list[TradingOrderEvent]] = defaultdict(list)
        self._seq = 1
        self._lock = Lock()

    def list_events(self, order_id: str) -> list[TradingOrderEvent]:
        """Return all events for an order."""
        return list(self._events.get(order_id, ()))

    def reset(self) -> None:
        """Reset in-memory state for tests."""
        with self._lock:
            self._events.clear()
            self._seq = 1

    def record_order_snapshot(
        self,
        order: TradingOrder,
        *,
        event_type: TradingOrderEventType | None = None,
        message: str | None = None,
    ) -> list[TradingOrderEvent]:
        """Record order state transition unless it duplicates the latest event."""
        with self._lock:
            event = TradingOrderEvent(
                event_id=f"evt-{self._seq:06d}",
                order_id=order.order_id,
                event_type=event_type or _status_to_event_type(order.status),
                status=order.status,
                message=message or order.message or "",
                occurred_at=order.updated_at or order.submitted_at or self._now(),
            )
            self._seq += 1
            current = self._events[order.order_id]
            if current and self._is_duplicate(current[-1], event):
                return current
            current.append(event)
            return list(current)

    def _is_duplicate(self, previous: TradingOrderEvent, current: TradingOrderEvent) -> bool:
        return (
            previous.event_type == current.event_type
            and previous.status == current.status
        )

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()


_STORE = TradingOrderEventStore()


def get_order_event_store() -> TradingOrderEventStore:
    """Return the singleton OMS event store."""
    return _STORE
