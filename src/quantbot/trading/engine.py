"""Small event-driven trading engine and deterministic execution ports."""

from datetime import datetime
from hashlib import sha256
import math
from typing import Dict, Sequence, Set, Tuple

from .domain import (
    Bar,
    FillEvent,
    OrderIntent,
    OrderReconciliation,
    OrderState,
    PointInTimeSnapshot,
    Quote,
)
from .ledger import PortfolioLedger
from .ports import ExecutionPort, IntradayStrategy, MarketEvent


def _quantity_tolerance(*values: float) -> float:
    magnitude = max((abs(value) for value in values), default=0.0)
    return math.ulp(magnitude) * 8.0


class BacktestExecutionPort:
    """Queues orders and fills them at the next matching bar's open."""

    def __init__(
        self,
        cost_bps: float = 0.0,
        max_volume_participation: float = 1.0,
    ) -> None:
        self.cost_bps = self._finite(cost_bps, "cost_bps")
        self.max_volume_participation = self._finite(
            max_volume_participation, "max_volume_participation"
        )
        if self.cost_bps < 0.0 or self.cost_bps >= 10_000.0:
            raise ValueError("cost_bps must be in [0, 10000)")
        if not 0.0 < self.max_volume_participation <= 1.0:
            raise ValueError("max_volume_participation must be in (0, 1]")
        self._known: Dict[str, OrderIntent] = {}
        self._states: Dict[str, OrderState] = {}
        self._revisions: Dict[str, int] = {}
        self._observed_ats: Dict[str, datetime] = {}
        self._pending: Dict[str, OrderIntent] = {}
        self._remaining: Dict[str, float] = {}
        self._staged: Dict[str, FillEvent] = {}
        self._staged_by_order: Dict[str, str] = {}
        self._acknowledged: Dict[str, FillEvent] = {}
        self._event_allocated: Dict[Tuple[str, datetime], float] = {}
        self._next_fill_sequence = 0

    @staticmethod
    def _finite(value: float, field_name: str) -> float:
        if isinstance(value, bool):
            raise ValueError("%s must be finite" % field_name)
        try:
            result = float(value)
        except (TypeError, ValueError):
            raise ValueError("%s must be finite" % field_name)
        if not math.isfinite(result):
            raise ValueError("%s must be finite" % field_name)
        return result

    @property
    def pending_order_ids(self) -> Tuple[str, ...]:
        return tuple(self._pending)

    def preview_submit_order(self, intent: OrderIntent) -> OrderState:
        if not isinstance(intent, OrderIntent):
            raise ValueError("intent must be an OrderIntent")
        existing = self._known.get(intent.client_order_id)
        if existing is not None and existing != intent:
            raise ValueError("client_order_id already refers to another intent")
        if existing is not None:
            return self._states[intent.client_order_id]
        return OrderState.ACCEPTED

    def submit_order(self, intent: OrderIntent) -> OrderState:
        state = self.preview_submit_order(intent)
        existing = self._known.get(intent.client_order_id)
        if existing is not None:
            return state
        self._known[intent.client_order_id] = intent
        self._states[intent.client_order_id] = OrderState.ACCEPTED
        self._revisions[intent.client_order_id] = 1
        self._observed_ats[intent.client_order_id] = intent.created_at
        self._pending[intent.client_order_id] = intent
        self._remaining[intent.client_order_id] = intent.quantity
        return OrderState.ACCEPTED

    def on_event(self, event: MarketEvent) -> Sequence[FillEvent]:
        if isinstance(event, Bar):
            bars = (event,)
        elif isinstance(event, PointInTimeSnapshot):
            bars = tuple(
                sorted(
                    event.bars.values(),
                    key=lambda market_bar: (
                        market_bar.timestamp,
                        market_bar.symbol,
                    ),
                )
            )
        else:
            return ()
        fills = []
        returned_fill_ids = set()
        for market_bar in bars:
            event_key = (market_bar.symbol, market_bar.timestamp)
            available_volume = max(
                0.0,
                market_bar.volume * self.max_volume_participation
                - self._event_allocated.get(event_key, 0.0),
            )
            for client_order_id, intent in self._pending.items():
                if (
                    intent.symbol != market_bar.symbol
                    or market_bar.timestamp <= intent.created_at
                ):
                    continue
                staged_fill_id = self._staged_by_order.get(client_order_id)
                if staged_fill_id is not None:
                    staged = self._staged[staged_fill_id]
                    if staged.fill_id not in returned_fill_ids:
                        fills.append(staged)
                        returned_fill_ids.add(staged.fill_id)
                    continue
                if available_volume <= 0.0:
                    continue
                remaining = self._remaining[client_order_id]
                fill_quantity = min(remaining, available_volume)
                if fill_quantity <= 0.0:
                    continue
                filled_before = intent.quantity - remaining
                identity = "%s|%s|%s" % (
                    client_order_id,
                    market_bar.timestamp.isoformat(),
                    filled_before,
                )
                multiplier = 1.0 + self.cost_bps / 10_000.0
                if intent.side == "sell":
                    multiplier = 1.0 - self.cost_bps / 10_000.0
                candidate = FillEvent(
                    fill_id=(
                        "bt-%020d-%s"
                        % (
                            self._next_fill_sequence,
                            sha256(identity.encode("utf-8")).hexdigest()[:16],
                        )
                    ),
                    client_order_id=client_order_id,
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=fill_quantity,
                    price=market_bar.open * multiplier,
                    fee=0.0,
                    filled_at=market_bar.timestamp,
                )
                self._next_fill_sequence += 1
                self._staged[candidate.fill_id] = candidate
                self._staged_by_order[client_order_id] = candidate.fill_id
                self._event_allocated[event_key] = (
                    self._event_allocated.get(event_key, 0.0) + fill_quantity
                )
                fills.append(candidate)
                returned_fill_ids.add(candidate.fill_id)
                available_volume -= fill_quantity
        return tuple(fills)

    def acknowledge_fill(self, fill: FillEvent) -> OrderState:
        acknowledged = self._acknowledged.get(fill.fill_id)
        if acknowledged is not None:
            if acknowledged != fill:
                raise ValueError("fill_id already acknowledges another fill")
            return self._states[fill.client_order_id]

        staged = self._staged.get(fill.fill_id)
        if staged is None:
            raise ValueError("unknown staged fill_id: %s" % fill.fill_id)
        if staged != fill:
            raise ValueError("fill does not match staged candidate")

        client_order_id = fill.client_order_id
        remaining = self._remaining[client_order_id]
        if fill.quantity > remaining:
            raise ValueError("acknowledged fill exceeds remaining quantity")
        remaining_after_fill = remaining - fill.quantity
        remaining = remaining_after_fill
        self._acknowledged[fill.fill_id] = fill
        del self._staged[fill.fill_id]
        del self._staged_by_order[client_order_id]
        if remaining == 0.0:
            self._states[client_order_id] = OrderState.FILLED
            del self._pending[client_order_id]
            del self._remaining[client_order_id]
        else:
            self._states[client_order_id] = OrderState.PARTIALLY_FILLED
            self._remaining[client_order_id] = remaining
        self._revisions[client_order_id] += 1
        self._observed_ats[client_order_id] = max(
            self._observed_ats[client_order_id],
            fill.filled_at,
        )
        return self._states[client_order_id]

    def cancel_order(self, client_order_id: str) -> OrderState:
        self.preview_cancel_order(client_order_id)
        state = self._states[client_order_id]
        if state in {OrderState.CANCELED, OrderState.FILLED, OrderState.REJECTED}:
            return state
        staged_fill_id = self._staged_by_order.pop(client_order_id, None)
        if staged_fill_id is not None:
            staged = self._staged.pop(staged_fill_id)
            event_key = (staged.symbol, staged.filled_at)
            allocated = self._event_allocated[event_key] - staged.quantity
            if allocated <= 0.0:
                del self._event_allocated[event_key]
            else:
                self._event_allocated[event_key] = allocated
        self._pending.pop(client_order_id, None)
        self._states[client_order_id] = OrderState.CANCELED
        self._revisions[client_order_id] += 1
        return OrderState.CANCELED

    def preview_cancel_order(self, client_order_id: str) -> OrderState:
        if client_order_id not in self._known:
            raise ValueError("unknown client_order_id: %s" % client_order_id)
        state = self._states[client_order_id]
        if state in {OrderState.FILLED, OrderState.REJECTED}:
            return state
        return OrderState.CANCELED

    def query_order(self, client_order_id: str) -> OrderReconciliation:
        if client_order_id not in self._known:
            raise ValueError("unknown client_order_id: %s" % client_order_id)
        state = self._states[client_order_id]
        fills = tuple(
            fill
            for fill in self._acknowledged.values()
            if fill.client_order_id == client_order_id
        )
        filled_quantity = sum(fill.quantity for fill in fills)
        return OrderReconciliation(
            client_order_id,
            state,
            filled_quantity,
            fills,
            observed_at=self._observed_ats[client_order_id],
            revision=self._revisions[client_order_id],
        )

    def resize_fill(self, fill: FillEvent, quantity: float) -> FillEvent:
        staged = self._staged.get(fill.fill_id)
        if staged is None or staged != fill:
            raise ValueError("fill does not match staged candidate")
        resized_quantity = self._finite(quantity, "quantity")
        if resized_quantity <= 0.0 or resized_quantity > fill.quantity:
            raise ValueError("resized quantity must be in (0, staged quantity]")
        if resized_quantity == fill.quantity:
            return fill
        resized = FillEvent(
            fill_id=fill.fill_id,
            client_order_id=fill.client_order_id,
            symbol=fill.symbol,
            side=fill.side,
            quantity=resized_quantity,
            price=fill.price,
            fee=fill.fee,
            filled_at=fill.filled_at,
        )
        self._staged[fill.fill_id] = resized
        event_key = (fill.symbol, fill.filled_at)
        self._event_allocated[event_key] -= fill.quantity - resized_quantity
        return resized


class RecordingExecutionPort:
    """Paper/live contract adapter that records intent without placing orders."""

    def __init__(self) -> None:
        self._recorded: Dict[str, OrderIntent] = {}
        self._states: Dict[str, OrderState] = {}
        self._revisions: Dict[str, int] = {}
        self._observed_ats: Dict[str, datetime] = {}

    @property
    def recorded_intents(self) -> Tuple[OrderIntent, ...]:
        return tuple(self._recorded.values())

    def preview_submit_order(self, intent: OrderIntent) -> OrderState:
        if not isinstance(intent, OrderIntent):
            raise ValueError("intent must be an OrderIntent")
        existing = self._recorded.get(intent.client_order_id)
        if existing is not None and existing != intent:
            raise ValueError("client_order_id already refers to another intent")
        if existing is not None:
            return self._states[intent.client_order_id]
        return OrderState.ACCEPTED

    def submit_order(self, intent: OrderIntent) -> OrderState:
        state = self.preview_submit_order(intent)
        existing = self._recorded.get(intent.client_order_id)
        if existing is not None:
            return state
        self._recorded[intent.client_order_id] = intent
        self._states[intent.client_order_id] = OrderState.ACCEPTED
        self._revisions[intent.client_order_id] = 1
        self._observed_ats[intent.client_order_id] = intent.created_at
        return OrderState.ACCEPTED

    def on_event(self, event: MarketEvent) -> Sequence[FillEvent]:
        del event
        return ()

    def acknowledge_fill(self, fill: FillEvent) -> OrderState:
        raise ValueError(
            "recording execution port did not issue fill_id: %s" % fill.fill_id
        )

    def cancel_order(self, client_order_id: str) -> OrderState:
        self.preview_cancel_order(client_order_id)
        state = self._states[client_order_id]
        if state in {OrderState.FILLED, OrderState.REJECTED}:
            return state
        if state == OrderState.CANCELED:
            return state
        self._states[client_order_id] = OrderState.CANCELED
        self._revisions[client_order_id] += 1
        return OrderState.CANCELED

    def preview_cancel_order(self, client_order_id: str) -> OrderState:
        if client_order_id not in self._recorded:
            raise ValueError("unknown client_order_id: %s" % client_order_id)
        state = self._states[client_order_id]
        if state in {OrderState.FILLED, OrderState.REJECTED}:
            return state
        return OrderState.CANCELED

    def query_order(self, client_order_id: str) -> OrderReconciliation:
        if client_order_id not in self._recorded:
            raise ValueError("unknown client_order_id: %s" % client_order_id)
        return OrderReconciliation(
            client_order_id,
            self._states[client_order_id],
            0.0,
            observed_at=self._observed_ats[client_order_id],
            revision=self._revisions[client_order_id],
        )

    def resize_fill(self, fill: FillEvent, quantity: float) -> FillEvent:
        del quantity
        raise ValueError(
            "recording execution port did not issue fill_id: %s" % fill.fill_id
        )


class EventDrivenTradingEngine:
    """Runs one intraday strategy against any conforming execution port."""

    def __init__(
        self,
        ledger: PortfolioLedger,
        strategy: IntradayStrategy,
        execution_port: ExecutionPort,
        max_fill_rounds: int = 1_024,
    ) -> None:
        if isinstance(max_fill_rounds, bool) or not isinstance(max_fill_rounds, int):
            raise ValueError("max_fill_rounds must be a positive integer")
        if max_fill_rounds <= 0:
            raise ValueError("max_fill_rounds must be a positive integer")
        self.ledger = ledger
        self.strategy = strategy
        self.execution_port = execution_port
        self.max_fill_rounds = max_fill_rounds

    def process_event(self, event: MarketEvent) -> Tuple[OrderIntent, ...]:
        if self.ledger.requires_reconciliation:
            raise ValueError("ledger has orders requiring reconciliation")
        event_time = self._event_time(event)
        if event_time < self.ledger.observed_at:
            raise ValueError("market event cannot precede current ledger time")
        self._process_fill_rounds(event, event_time)

        if isinstance(event, Bar):
            self.ledger.mark_to_market(
                {event.symbol: event.close}, observed_at=event.timestamp
            )
        elif isinstance(event, Quote):
            self.ledger.mark_to_market(
                {event.symbol: event.mid_price}, observed_at=event.timestamp
            )
        elif isinstance(event, PointInTimeSnapshot):
            symbols = set(event.bars).union(event.quotes)
            prices = {symbol: event.latest_price(symbol) for symbol in symbols}
            self.ledger.mark_to_market(prices, observed_at=event.observed_at)

        state = self.ledger.snapshot()
        cancel_order_ids = getattr(self.strategy, "cancel_order_ids", None)
        if callable(cancel_order_ids):
            requested_cancellations = tuple(cancel_order_ids(state, event))
            self._prevalidate_cancellations(requested_cancellations)
            for client_order_id in requested_cancellations:
                self._cancel_working_order(
                    client_order_id,
                    "execution port did not cancel working order",
                )
            state = self.ledger.snapshot()

        intents = tuple(self.strategy.on_event(state, event))
        self._prevalidate_intents(intents, event_time)
        for intent in intents:
            recorded_state = self.ledger.record_order(intent)
            if recorded_state != OrderState.CREATED:
                continue
            try:
                state = self.execution_port.submit_order(intent)
            except Exception:
                self._reconcile_failed_submission(intent.client_order_id)
                raise
            if state == OrderState.ACCEPTED:
                self.ledger.accept_order(intent.client_order_id)
            elif state == OrderState.REJECTED:
                self.ledger.reject_order(intent.client_order_id)
            else:
                self._reconcile_failed_submission(intent.client_order_id)
                raise ValueError("execution port must accept or reject submitted orders")
        return intents

    def _reconcile_failed_submission(self, client_order_id: str) -> None:
        """Cancel best-effort and reject contradictory broker observations."""
        self.ledger.require_reconciliation(client_order_id)
        cancel_state = None
        try:
            cancel_state = self.execution_port.cancel_order(client_order_id)
        except Exception:
            pass
        result = self._query_reconciliation(client_order_id)
        self._raise_on_cancel_query_conflict(cancel_state, result)
        self.ledger.reconcile_order(result)

    def _raise_on_cancel_query_conflict(
        self,
        cancel_state: object,
        result: OrderReconciliation,
    ) -> None:
        if cancel_state in {
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.FILLED,
        } and result.state in {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
        }:
            self.ledger.observe_reconciliation(
                result,
                terminal_required=True,
            )
            raise ValueError("cancel and query outcomes conflict")

    def reconcile_order(self, client_order_id: str) -> OrderState:
        if not isinstance(client_order_id, str) or not client_order_id:
            raise ValueError("client_order_id must be a non-empty string")
        return self.ledger.reconcile_order(
            self._query_reconciliation(client_order_id)
        )

    def _query_reconciliation(
        self, client_order_id: str
    ) -> OrderReconciliation:
        result = self.execution_port.query_order(client_order_id)
        if not isinstance(result, OrderReconciliation):
            raise ValueError("execution port query must return OrderReconciliation")
        if result.client_order_id != client_order_id:
            raise ValueError("execution port query returned another order")
        if result.observed_at is None or result.revision is None:
            raise ValueError(
                "execution port query must provide observed_at and revision"
            )
        return result

    def _acknowledge_fill(self, fill: FillEvent) -> OrderState:
        expected_state = self.ledger.order_state(fill.client_order_id)
        try:
            port_state = self.execution_port.acknowledge_fill(fill)
        except Exception as error:
            self.ledger.require_reconciliation(fill.client_order_id)
            raise ValueError("fill acknowledgement remains unresolved") from error
        if not isinstance(port_state, OrderState) or port_state != expected_state:
            self.ledger.require_reconciliation(fill.client_order_id)
            raise ValueError("fill acknowledgement disagrees with ledger state")
        return port_state

    def _cancel_working_order(
        self, client_order_id: str, error_message: str
    ) -> OrderState:
        self.ledger.require_reconciliation(client_order_id)
        cancel_state = None
        try:
            cancel_state = self.execution_port.cancel_order(client_order_id)
        except Exception:
            pass
        try:
            result = self._query_reconciliation(client_order_id)
            self._raise_on_cancel_query_conflict(cancel_state, result)
            if result.state not in {
                OrderState.CANCELED,
                OrderState.REJECTED,
                OrderState.FILLED,
            }:
                raise ValueError("cancel did not reach a broker terminal state")
            return self.ledger.reconcile_order(result)
        except Exception as error:
            raise ValueError(error_message) from error

    def _prevalidate_cancellations(
        self, requested_cancellations: Tuple[str, ...]
    ) -> None:
        seen: Set[str] = set()
        for client_order_id in requested_cancellations:
            if not isinstance(client_order_id, str) or not client_order_id:
                raise ValueError("cancel_order_ids must contain non-empty strings")
            if client_order_id in seen:
                raise ValueError("cancel_order_ids must be unique")
            seen.add(client_order_id)
            port_state = self.execution_port.preview_cancel_order(client_order_id)
            if port_state != OrderState.CANCELED:
                raise ValueError("execution port cannot cancel working order")
            if self.ledger.preview_cancel_order(client_order_id) != OrderState.CANCELED:
                raise ValueError("ledger cannot cancel working order")

    def _prevalidate_intents(
        self, intents: Tuple[OrderIntent, ...], event_time: datetime
    ) -> None:
        seen: Set[str] = set()
        for intent in intents:
            if not isinstance(intent, OrderIntent):
                raise ValueError("strategy must return OrderIntent values")
            if intent.client_order_id in seen:
                raise ValueError("strategy order ids must be unique per batch")
            seen.add(intent.client_order_id)
            if (
                not self.ledger.has_order(intent.client_order_id)
                and intent.created_at != event_time
            ):
                raise ValueError("new order created_at must equal current event time")
            recorded_state = self.ledger.preview_record_order(intent)
            port_state = self.execution_port.preview_submit_order(intent)
            if not isinstance(port_state, OrderState):
                raise ValueError("execution port preview must return OrderState")
            if recorded_state == OrderState.CREATED:
                if port_state not in {OrderState.ACCEPTED, OrderState.REJECTED}:
                    raise ValueError(
                        "execution port preview must accept or reject new orders"
                    )
            elif port_state != recorded_state:
                raise ValueError("execution port and ledger order states disagree")

    def _process_fill_rounds(
        self, event: MarketEvent, event_time: datetime
    ) -> None:
        processed_batches = 0
        pending_ack_convergence: Dict[str, str] = {}
        while True:
            try:
                candidates = tuple(self.execution_port.on_event(event))
            except Exception as error:
                for client_order_id in pending_ack_convergence.values():
                    self.ledger.require_reconciliation(client_order_id)
                if pending_ack_convergence:
                    raise ValueError(
                        "fill acknowledgement convergence could not be confirmed"
                    ) from error
                raise
            if candidates and all(
                isinstance(candidate, FillEvent) for candidate in candidates
            ):
                candidates = tuple(
                    sorted(
                        candidates,
                        key=lambda candidate: (
                            candidate.filled_at,
                            candidate.fill_id,
                        ),
                    )
                )
            if not candidates:
                return
            returned_fill_ids = {
                candidate.fill_id
                for candidate in candidates
                if isinstance(candidate, FillEvent)
            }
            unconverged_acknowledgements = {
                fill_id: client_order_id
                for fill_id, client_order_id in pending_ack_convergence.items()
                if fill_id in returned_fill_ids
            }
            if processed_batches >= self.max_fill_rounds:
                for client_order_id in unconverged_acknowledgements.values():
                    self.ledger.require_reconciliation(client_order_id)
                if unconverged_acknowledgements:
                    raise ValueError(
                        "fill acknowledgement did not converge within max_fill_rounds"
                    )
                raise ValueError("execution port exceeded max_fill_rounds")
            self._validate_candidate_batch(candidates, event_time)
            processed_batches += 1
            needs_another_round, acknowledged_fills = self._process_fill_batch(
                candidates
            )
            if not needs_another_round:
                return
            pending_ack_convergence = acknowledged_fills

    def _process_fill_batch(
        self, candidates: Tuple[FillEvent, ...]
    ) -> Tuple[bool, Dict[str, str]]:
        needs_another_round = False
        acknowledged_fills: Dict[str, str] = {}
        for candidate in candidates:
            if not self.ledger.preview_fill(candidate):
                self.ledger.apply_fill(candidate)
                self._acknowledge_fill(candidate)
                needs_another_round = True
                acknowledged_fills[candidate.fill_id] = candidate.client_order_id
                continue
            remaining_quantity = self.ledger.remaining_order_quantity(
                candidate.client_order_id
            )
            fillable_quantity = self.ledger.fillable_quantity(candidate)
            if fillable_quantity <= 0.0:
                self._cancel_working_order(
                    candidate.client_order_id,
                    "execution port did not cancel risk-blocked order",
                )
                needs_another_round = True
                continue
            fill = candidate
            quantity_limited = fillable_quantity < candidate.quantity
            risk_limited = fillable_quantity < min(
                candidate.quantity, remaining_quantity
            )
            if quantity_limited:
                fill = self.execution_port.resize_fill(
                    candidate, fillable_quantity
                )
                needs_another_round = True
            self.ledger.apply_fill(fill)
            self._acknowledge_fill(fill)
            needs_another_round = True
            acknowledged_fills[fill.fill_id] = fill.client_order_id
            if risk_limited:
                self._cancel_working_order(
                    fill.client_order_id,
                    "execution port did not cancel risk remainder",
                )
                needs_another_round = True
        return needs_another_round, acknowledged_fills

    def _validate_candidate_batch(
        self, candidates: Tuple[FillEvent, ...], event_time: datetime
    ) -> Set[str]:
        fill_ids: Set[str] = set()
        candidate_order_ids: Set[str] = set()
        batch_observed_at = self.ledger.observed_at
        for candidate in candidates:
            if not isinstance(candidate, FillEvent):
                raise ValueError("execution port must return FillEvent values")
            if candidate.fill_id in fill_ids:
                raise ValueError("execution port fill_id values must be unique")
            fill_ids.add(candidate.fill_id)
            if candidate.client_order_id in candidate_order_ids:
                raise ValueError(
                    "execution port must return at most one candidate per order"
                )
            candidate_order_ids.add(candidate.client_order_id)
            if candidate.filled_at > event_time:
                raise ValueError("fill cannot use future market information")
            is_new = self.ledger.preview_fill(candidate)
            if is_new:
                if candidate.filled_at < batch_observed_at:
                    raise ValueError("fill batch times must be non-decreasing")
                batch_observed_at = candidate.filled_at
        return fill_ids

    @staticmethod
    def _event_time(event: MarketEvent) -> datetime:
        if isinstance(event, (Bar, Quote)):
            return event.timestamp
        if isinstance(event, PointInTimeSnapshot):
            return event.observed_at
        raise ValueError("unsupported market event")
