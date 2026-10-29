"""In-memory portfolio ledger with explicit order and PnL invariants."""

import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

from .domain import (
    FillEvent,
    MarkSnapshot,
    OrderIntent,
    OrderReconciliation,
    OrderSnapshot,
    OrderState,
    PortfolioSnapshot,
    PositionSnapshot,
)


def _quantity_tolerance(*values: float) -> float:
    magnitude = max((abs(value) for value in values), default=0.0)
    return math.ulp(magnitude) * 8.0


def _quantity_close(left: float, right: float) -> bool:
    return abs(left - right) <= _quantity_tolerance(left, right)


def _quantity_zero(value: float, *scale: float) -> bool:
    return abs(value) <= _quantity_tolerance(value, *scale)


def _fill_journal_key(fill: FillEvent) -> Tuple[datetime, str]:
    return fill.filled_at, fill.fill_id


@dataclass
class _OrderRecord:
    intent: OrderIntent
    state: OrderState = OrderState.CREATED
    filled_quantity: float = 0.0
    fill_quantities: list[float] = field(default_factory=list)
    reconciliation_observed_at: Optional[datetime] = None
    reconciliation_revision: Optional[int] = None
    reconciliation_cursor: Optional[OrderReconciliation] = None
    terminal_required: bool = False


@dataclass
class _Position:
    quantity: float
    average_cost: float
    market_price: float


class PortfolioLedger:
    """Authoritative cash, position, order, fill, and PnL state."""

    _TRANSITIONS = {
        OrderState.CREATED: {
            OrderState.ACCEPTED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        },
        OrderState.ACCEPTED: {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.RECONCILIATION_REQUIRED,
        },
        OrderState.PARTIALLY_FILLED: {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.RECONCILIATION_REQUIRED,
        },
        OrderState.FILLED: {OrderState.RECONCILIATION_REQUIRED},
        OrderState.CANCELED: {OrderState.RECONCILIATION_REQUIRED},
        OrderState.REJECTED: {OrderState.RECONCILIATION_REQUIRED},
        OrderState.RECONCILIATION_REQUIRED: set(),
    }

    def __init__(
        self,
        initial_cash: float,
        observed_at: Optional[datetime] = None,
        allow_short: bool = False,
    ) -> None:
        if type(allow_short) is not bool:
            raise ValueError("allow_short must be a bool")
        self._initial_cash = self._non_negative(initial_cash, "initial_cash")
        self._cash = self._initial_cash
        self._fees = 0.0
        self._realized_pnl = 0.0
        self._realized_by_symbol: Dict[str, float] = {}
        self._positions: Dict[str, _Position] = {}
        self._orders: Dict[str, _OrderRecord] = {}
        self._fills: Dict[str, FillEvent] = {}
        self._effective_fill_quantities: Dict[str, float] = {}
        self._marks: Dict[str, MarkSnapshot] = {}
        self._allow_short = allow_short
        self._observed_at = observed_at or datetime.now(timezone.utc)
        self._require_aware(self._observed_at, "observed_at")

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

    @classmethod
    def _non_negative(cls, value: float, field_name: str) -> float:
        result = cls._finite(value, field_name)
        if result < 0.0:
            raise ValueError("%s must be non-negative" % field_name)
        return result

    @staticmethod
    def _require_aware(value: datetime, field_name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("%s must be timezone-aware" % field_name)

    @property
    def initial_cash(self) -> float:
        return self._initial_cash

    @property
    def observed_at(self) -> datetime:
        return self._observed_at

    @property
    def cash(self) -> float:
        return self._cash

    @property
    def fees(self) -> float:
        return self._fees

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    @property
    def unrealized_pnl(self) -> float:
        return sum(
            position.quantity * (position.market_price - position.average_cost)
            for position in self._positions.values()
        )

    @property
    def equity(self) -> float:
        return self._cash + sum(
            position.quantity * position.market_price
            for position in self._positions.values()
        )

    @property
    def orders(self) -> Mapping[str, OrderState]:
        return MappingProxyType(
            {order_id: record.state for order_id, record in self._orders.items()}
        )

    @property
    def requires_reconciliation(self) -> bool:
        return any(
            record.state == OrderState.RECONCILIATION_REQUIRED
            for record in self._orders.values()
        )

    @property
    def reconciliation_orders(self) -> Mapping[str, OrderSnapshot]:
        return MappingProxyType(
            {
                client_order_id: self._order_snapshot(record)
                for client_order_id, record in self._orders.items()
                if record.state == OrderState.RECONCILIATION_REQUIRED
            }
        )

    @property
    def fills(self) -> Tuple[FillEvent, ...]:
        return tuple(self._fills.values())

    def record_order(self, intent: OrderIntent) -> OrderState:
        preview_state = self.preview_record_order(intent)
        if intent.client_order_id in self._orders:
            return preview_state
        self._orders[intent.client_order_id] = _OrderRecord(intent=intent)
        if intent.created_at > self._observed_at:
            self._observed_at = intent.created_at
        return OrderState.CREATED

    def preview_record_order(self, intent: OrderIntent) -> OrderState:
        """Validate an order recording operation without changing the ledger."""
        if not isinstance(intent, OrderIntent):
            raise ValueError("intent must be an OrderIntent")
        existing = self._orders.get(intent.client_order_id)
        if existing is not None:
            if existing.intent != intent:
                raise ValueError("client_order_id already refers to another intent")
            return existing.state
        if intent.created_at < self._observed_at:
            raise ValueError("new order cannot precede current ledger time")
        return OrderState.CREATED

    def has_order(self, client_order_id: str) -> bool:
        return client_order_id in self._orders

    def order_state(self, client_order_id: str) -> OrderState:
        return self._order(client_order_id).state

    def remaining_order_quantity(self, client_order_id: str) -> float:
        """Return the exact unfilled quantity without changing ledger state."""
        record = self._order(client_order_id)
        return record.intent.quantity - record.filled_quantity

    def transition_order(
        self, client_order_id: str, new_state: OrderState
    ) -> OrderState:
        if not isinstance(new_state, OrderState):
            raise ValueError("new_state must be an OrderState")
        record = self._order(client_order_id)
        if record.state == new_state:
            return record.state
        if new_state not in self._TRANSITIONS[record.state]:
            raise ValueError(
                "illegal order transition: %s -> %s"
                % (record.state.value, new_state.value)
            )
        if new_state == OrderState.PARTIALLY_FILLED and not (
            record.filled_quantity > 0.0
            and self.remaining_order_quantity(client_order_id) > 0.0
        ):
            raise ValueError("partial state requires a partial fill")
        if (
            new_state == OrderState.FILLED
            and self.remaining_order_quantity(client_order_id) != 0.0
        ):
            raise ValueError("filled state requires the full quantity")
        record.state = new_state
        return new_state

    def accept_order(self, client_order_id: str) -> OrderState:
        return self.transition_order(client_order_id, OrderState.ACCEPTED)

    def cancel_order(self, client_order_id: str) -> OrderState:
        return self.transition_order(client_order_id, OrderState.CANCELED)

    def preview_cancel_order(self, client_order_id: str) -> OrderState:
        """Validate order cancellation without changing the ledger."""
        record = self._order(client_order_id)
        if record.state == OrderState.CANCELED:
            return OrderState.CANCELED
        if OrderState.CANCELED not in self._TRANSITIONS[record.state]:
            raise ValueError(
                "illegal order transition: %s -> %s"
                % (record.state.value, OrderState.CANCELED.value)
            )
        return OrderState.CANCELED

    def reject_order(self, client_order_id: str) -> OrderState:
        return self.transition_order(client_order_id, OrderState.REJECTED)

    def require_reconciliation(self, client_order_id: str) -> OrderState:
        record = self._order(client_order_id)
        if record.state == OrderState.RECONCILIATION_REQUIRED:
            return record.state
        record.state = OrderState.RECONCILIATION_REQUIRED
        return record.state

    def reconcile_order(self, result: OrderReconciliation) -> OrderState:
        self.observe_reconciliation(result)
        record = self._order(result.client_order_id)
        if record.terminal_required and result.state in {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
        }:
            raise ValueError("terminal reconciliation result required")

        broker_fills = {fill.fill_id: fill for fill in result.fills}
        merged_fills = dict(self._fills)
        for broker_fill in result.fills:
            merged_fills[broker_fill.fill_id] = broker_fill
        ordered_fills = tuple(
            sorted(
                merged_fills.values(),
                key=_fill_journal_key,
            )
        )
        effective_fill_quantities = self._merged_effective_fill_quantities(
            record,
            result,
        )

        candidate = self._replay_candidate(
            ordered_fills,
            effective_fill_quantities,
        )

        candidate_record = candidate._order(result.client_order_id)
        if not _quantity_close(
            candidate_record.filled_quantity, result.filled_quantity
        ):
            raise ValueError("broker filled quantity does not match imported fills")
        candidate._validate_reconciliation_state(candidate_record, result.state)
        candidate_record.state = result.state
        candidate_record.terminal_required = False
        candidate._observed_at = max(
            self._observed_at,
            candidate._observed_at,
            result.observed_at or candidate._observed_at,
        )
        candidate.assert_invariants()
        candidate.snapshot()
        self._commit_candidate(candidate)
        return result.state

    def _merged_effective_fill_quantities(
        self,
        record: _OrderRecord,
        result: OrderReconciliation,
    ) -> Dict[str, float]:
        effective = dict(self._effective_fill_quantities)
        new_broker_fills = []
        for broker_fill in result.fills:
            if broker_fill.fill_id not in self._fills:
                new_broker_fills.append(broker_fill)
                effective[broker_fill.fill_id] = broker_fill.quantity

        if not new_broker_fills:
            return effective
        final_raw_fill = max(
            new_broker_fills,
            key=_fill_journal_key,
        )
        if not _quantity_close(result.filled_quantity, record.intent.quantity):
            return effective
        ordered_result_fills = sorted(
            result.fills,
            key=_fill_journal_key,
        )
        effective_before_final = math.fsum(
            effective[fill.fill_id]
            for fill in ordered_result_fills
            if fill.fill_id != final_raw_fill.fill_id
        )
        exact_remaining = record.intent.quantity - effective_before_final
        if (
            exact_remaining > 0.0
            and final_raw_fill.quantity >= exact_remaining
            and _quantity_close(
                effective_before_final + final_raw_fill.quantity,
                record.intent.quantity,
            )
        ):
            effective[final_raw_fill.fill_id] = exact_remaining
        return effective

    def observe_reconciliation(
        self,
        result: OrderReconciliation,
        terminal_required: bool = False,
    ) -> OrderState:
        """Advance a broker-query cursor without importing financial effects."""
        if type(terminal_required) is not bool:
            raise ValueError("terminal_required must be a bool")
        record = self._validate_reconciliation_query(result)
        self._validate_reconciliation_cursor(record, result)
        record.reconciliation_cursor = result
        record.reconciliation_observed_at = result.observed_at
        record.reconciliation_revision = result.revision
        if terminal_required:
            record.terminal_required = True
        if result.observed_at is not None:
            self._observed_at = max(self._observed_at, result.observed_at)
        self.snapshot()
        return record.state

    def _validate_reconciliation_query(
        self, result: OrderReconciliation
    ) -> _OrderRecord:
        if not isinstance(result, OrderReconciliation):
            raise ValueError("result must be an OrderReconciliation")
        record = self._order(result.client_order_id)
        if record.state != OrderState.RECONCILIATION_REQUIRED:
            raise ValueError("order does not require reconciliation")
        broker_fills = {fill.fill_id: fill for fill in result.fills}
        local_fills = tuple(
            fill
            for fill in self._fills.values()
            if fill.client_order_id == result.client_order_id
        )
        for local_fill in local_fills:
            broker_fill = broker_fills.get(local_fill.fill_id)
            if broker_fill is None or broker_fill != local_fill:
                raise ValueError("broker fills must contain every exact local fill")
        for broker_fill in result.fills:
            if (
                broker_fill.symbol != record.intent.symbol
                or broker_fill.side != record.intent.side
            ):
                raise ValueError("broker fill does not match order intent")
            if broker_fill.filled_at < record.intent.created_at:
                raise ValueError("broker fill cannot precede order intent")
            existing = self._fills.get(broker_fill.fill_id)
            if existing is not None and existing != broker_fill:
                raise ValueError("fill_id already refers to another fill")
        if result.filled_quantity > record.intent.quantity and not _quantity_close(
            result.filled_quantity,
            record.intent.quantity,
        ):
            raise ValueError("broker filled quantity exceeds order quantity")
        broker_reports_full_quantity = _quantity_close(
            result.filled_quantity,
            record.intent.quantity,
        )
        if result.state == OrderState.FILLED and not broker_reports_full_quantity:
            raise ValueError("filled broker state requires the full order quantity")
        if result.state in {
            OrderState.CANCELED,
            OrderState.PARTIALLY_FILLED,
        } and broker_reports_full_quantity:
            raise ValueError("broker state requires an unfilled remainder")
        return record

    @staticmethod
    def _validate_reconciliation_cursor(
        record: _OrderRecord,
        result: OrderReconciliation,
    ) -> None:
        cursor = record.reconciliation_cursor
        if cursor is None:
            return
        if cursor.revision is not None:
            if result.revision is None or result.revision < cursor.revision:
                raise ValueError("reconciliation revision is stale")
            if result.revision == cursor.revision:
                if result != cursor:
                    raise ValueError("reconciliation revision conflicts with cursor")
                return
        if cursor.observed_at is not None and (
            result.observed_at is None
            or result.observed_at < cursor.observed_at
        ):
            raise ValueError("reconciliation observation is stale")
        if (
            result.filled_quantity < cursor.filled_quantity
            and not _quantity_close(
                result.filled_quantity,
                cursor.filled_quantity,
            )
        ):
            raise ValueError("reconciliation broker economics regressed")
        result_fills = {fill.fill_id: fill for fill in result.fills}
        for confirmed_fill in cursor.fills:
            if result_fills.get(confirmed_fill.fill_id) != confirmed_fill:
                raise ValueError(
                    "reconciliation must retain every confirmed broker fill"
                )
        terminal_states = {
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.FILLED,
        }
        active_states = {
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
        }
        if cursor.state in terminal_states and result.state in active_states:
            raise ValueError("terminal reconciliation cannot return to active state")

    def _replay_candidate(
        self,
        ordered_fills: Tuple[FillEvent, ...],
        effective_fill_quantities: Mapping[str, float],
    ) -> "PortfolioLedger":
        """Rebuild all economic state from the immutable raw fill journal."""
        candidate = copy.deepcopy(self)
        original_states = {
            order_id: record.state for order_id, record in candidate._orders.items()
        }
        candidate._cash = candidate._initial_cash
        candidate._fees = 0.0
        candidate._realized_pnl = 0.0
        candidate._realized_by_symbol = {}
        candidate._positions = {}
        candidate._fills = {}
        candidate._effective_fill_quantities = {}
        for record in candidate._orders.values():
            record.filled_quantity = 0.0
            record.fill_quantities = []
            record.state = OrderState.ACCEPTED

        for raw_fill in ordered_fills:
            record = candidate._order(raw_fill.client_order_id)
            if (
                raw_fill.symbol != record.intent.symbol
                or raw_fill.side != record.intent.side
            ):
                raise ValueError("fill does not match its order")
            if raw_fill.filled_at < record.intent.created_at:
                raise ValueError("fill cannot precede its order")
            try:
                effective_quantity = effective_fill_quantities[raw_fill.fill_id]
            except KeyError:
                raise ValueError("raw fill is missing its effective quantity")
            candidate._apply_new_fill(raw_fill, effective_quantity)

        for order_id, state in original_states.items():
            candidate._orders[order_id].state = state
        return candidate

    @staticmethod
    def _validate_reconciliation_state(
        record: _OrderRecord, state: OrderState
    ) -> None:
        remaining = record.intent.quantity - record.filled_quantity
        if state in {OrderState.ACCEPTED, OrderState.REJECTED}:
            if record.filled_quantity != 0.0:
                raise ValueError("unfilled broker state cannot have fills")
        elif state == OrderState.CANCELED:
            if remaining <= 0.0:
                raise ValueError("canceled broker state requires a remainder")
        elif state == OrderState.PARTIALLY_FILLED:
            if record.filled_quantity <= 0.0 or remaining <= 0.0:
                raise ValueError("partial broker state requires fill and remainder")
        elif state == OrderState.FILLED:
            if remaining != 0.0:
                raise ValueError("filled broker state requires no remainder")
        else:
            raise ValueError("unsupported broker reconciliation state")

    def _commit_candidate(self, candidate: "PortfolioLedger") -> None:
        self._cash = candidate._cash
        self._fees = candidate._fees
        self._realized_pnl = candidate._realized_pnl
        self._realized_by_symbol = candidate._realized_by_symbol
        self._positions = candidate._positions
        self._orders = candidate._orders
        self._fills = candidate._fills
        self._effective_fill_quantities = candidate._effective_fill_quantities
        self._marks = candidate._marks
        self._observed_at = candidate._observed_at

    def _order(self, client_order_id: str) -> _OrderRecord:
        try:
            return self._orders[client_order_id]
        except KeyError:
            raise ValueError("unknown client_order_id: %s" % client_order_id)

    def apply_fill(self, fill: FillEvent) -> bool:
        is_new = self.preview_fill(fill)
        if not is_new:
            return False
        return self._apply_new_fill(fill, fill.quantity)

    def _apply_new_fill(
        self,
        fill: FillEvent,
        effective_quantity: float,
    ) -> bool:
        effective_quantity = self._finite(
            effective_quantity,
            "effective_quantity",
        )
        if effective_quantity <= 0.0 or effective_quantity > fill.quantity:
            raise ValueError("effective quantity must be in (0, raw quantity]")
        record = self._order(fill.client_order_id)
        # Stable cumulative quantity also works with Python 3.12+ compensated
        # sum semantics; repeated += can make a valid last fragment exceed remaining.
        new_filled_quantity = math.fsum((*record.fill_quantities, effective_quantity))
        remaining = record.intent.quantity - record.filled_quantity
        if effective_quantity > remaining:
            raise ValueError("fill exceeds remaining order quantity")
        remaining_after_fill = record.intent.quantity - new_filled_quantity

        signed_quantity = (
            effective_quantity if fill.side == "buy" else -effective_quantity
        )
        position = self._positions.get(fill.symbol)
        old_quantity = position.quantity if position is not None else 0.0
        old_average_cost = position.average_cost if position is not None else 0.0
        new_quantity = old_quantity + signed_quantity
        if not self._allow_short and new_quantity < 0.0:
            raise ValueError("naked short selling is disabled")

        cash_after = self._cash - signed_quantity * fill.price - fill.fee
        if cash_after < 0.0:
            raise ValueError("insufficient cash for fill")

        realized_delta = self._realized_delta(
            old_quantity, old_average_cost, signed_quantity, fill.price
        )
        new_average_cost = self._new_average_cost(
            old_quantity,
            old_average_cost,
            signed_quantity,
            fill.price,
            new_quantity,
        )

        self._cash = cash_after
        self._fees += fill.fee
        self._realized_pnl += realized_delta
        self._realized_by_symbol[fill.symbol] = (
            self._realized_by_symbol.get(fill.symbol, 0.0) + realized_delta
        )
        if new_quantity == 0.0:
            self._positions.pop(fill.symbol, None)
        else:
            mark = self._marks.get(fill.symbol)
            market_price = fill.price
            if mark is not None and mark.timestamp >= fill.filled_at:
                market_price = mark.price
            self._positions[fill.symbol] = _Position(
                quantity=new_quantity,
                average_cost=new_average_cost,
                market_price=market_price,
            )
        self._fills[fill.fill_id] = fill
        self._effective_fill_quantities[fill.fill_id] = effective_quantity
        record.filled_quantity = new_filled_quantity
        record.fill_quantities.append(effective_quantity)
        record.state = (
            OrderState.FILLED
            if remaining_after_fill == 0.0
            else OrderState.PARTIALLY_FILLED
        )
        if fill.filled_at > self._observed_at:
            self._observed_at = fill.filled_at
        self.assert_invariants()
        return True

    def preview_fill(self, fill: FillEvent) -> bool:
        """Validate fill identity and order compatibility without mutation.

        The return value is false only for an exact replay already committed to
        the ledger. Risk and account-capacity checks remain in
        :meth:`fillable_quantity` because earlier fills in the same market event
        can legitimately change them.
        """
        if not isinstance(fill, FillEvent):
            raise ValueError("fill must be a FillEvent")
        existing = self._fills.get(fill.fill_id)
        if existing is not None:
            if existing != fill:
                raise ValueError("fill_id already refers to another fill")
            return False
        record = self._order(fill.client_order_id)
        if fill.symbol != record.intent.symbol or fill.side != record.intent.side:
            raise ValueError("fill does not match its order")
        if fill.filled_at < record.intent.created_at:
            raise ValueError("fill cannot precede its order")
        fill_key = _fill_journal_key(fill)
        latest_journal_key = max(
            (
                _fill_journal_key(known)
                for known in self._fills.values()
            ),
            default=None,
        )
        if (
            fill.filled_at < self._observed_at
            or (
                latest_journal_key is not None
                and fill_key < latest_journal_key
            )
        ):
            record.state = OrderState.RECONCILIATION_REQUIRED
            raise ValueError("new fill cannot precede current ledger time or journal")
        if record.state not in (OrderState.ACCEPTED, OrderState.PARTIALLY_FILLED):
            raise ValueError("order is not fillable in state %s" % record.state.value)
        return True

    def fillable_quantity(self, fill: FillEvent) -> float:
        """Return the risk-permitted quantity without changing ledger state."""
        if not self.preview_fill(fill):
            return fill.quantity
        record = self._order(fill.client_order_id)
        remaining = record.intent.quantity - record.filled_quantity
        order_quantity = min(fill.quantity, remaining)
        if fill.side == "buy":
            cash_after_fee = self._cash - fill.fee
            if cash_after_fee <= 0.0:
                return 0.0
            return min(order_quantity, cash_after_fee / fill.price)
        if not self._allow_short:
            position = self._positions.get(fill.symbol)
            available = max(0.0, position.quantity if position is not None else 0.0)
            return min(order_quantity, available)
        return order_quantity

    @staticmethod
    def _realized_delta(
        old_quantity: float,
        old_average_cost: float,
        signed_fill_quantity: float,
        fill_price: float,
    ) -> float:
        if old_quantity == 0.0 or old_quantity * signed_fill_quantity >= 0.0:
            return 0.0
        closing_quantity = min(abs(old_quantity), abs(signed_fill_quantity))
        direction = 1.0 if old_quantity > 0.0 else -1.0
        return closing_quantity * (fill_price - old_average_cost) * direction

    @staticmethod
    def _new_average_cost(
        old_quantity: float,
        old_average_cost: float,
        signed_fill_quantity: float,
        fill_price: float,
        new_quantity: float,
    ) -> float:
        if new_quantity == 0.0:
            return 0.0
        if old_quantity == 0.0 or old_quantity * signed_fill_quantity > 0.0:
            return (
                abs(old_quantity) * old_average_cost
                + abs(signed_fill_quantity) * fill_price
            ) / abs(new_quantity)
        if old_quantity * new_quantity > 0.0:
            return old_average_cost
        return fill_price

    def mark_to_market(
        self,
        prices: Mapping[str, float],
        observed_at: Optional[datetime] = None,
    ) -> PortfolioSnapshot:
        mark_time = observed_at or self._observed_at
        self._require_aware(mark_time, "observed_at")
        if mark_time < self._observed_at:
            raise ValueError("mark cannot precede current ledger time")
        try:
            price_items = prices.items()
        except AttributeError:
            raise ValueError("prices must be a mapping")
        updates = {}
        for symbol, raw_price in price_items:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("price symbol must be a non-empty string")
            price = self._finite(raw_price, "price[%s]" % symbol)
            if price <= 0.0:
                raise ValueError("market prices must be positive")
            mark = MarkSnapshot(symbol, price, mark_time)
            existing = self._marks.get(symbol)
            if existing is not None:
                if mark.timestamp < existing.timestamp:
                    raise ValueError("mark cannot precede symbol mark time")
            updates[symbol] = mark
        for symbol, mark in updates.items():
            self._marks[symbol] = mark
            position = self._positions.get(symbol)
            if position is not None:
                position.market_price = mark.price
        if mark_time > self._observed_at:
            self._observed_at = mark_time
        self.assert_invariants()
        return self.snapshot()

    def snapshot(self) -> PortfolioSnapshot:
        positions = {
            symbol: PositionSnapshot(
                symbol=symbol,
                quantity=position.quantity,
                average_cost=position.average_cost,
                market_price=position.market_price,
                realized_pnl=self._realized_by_symbol.get(symbol, 0.0),
            )
            for symbol, position in self._positions.items()
        }
        order_details = {
            order_id: self._order_snapshot(record)
            for order_id, record in self._orders.items()
        }
        return PortfolioSnapshot(
            observed_at=self._observed_at,
            cash=self._cash,
            positions=positions,
            orders={
                order_id: record.state for order_id, record in self._orders.items()
            },
            fills=tuple(self._fills.values()),
            fees=self._fees,
            realized_pnl=self._realized_pnl,
            open_orders={
                order_id: order_details[order_id]
                for order_id, record in self._orders.items()
                if record.state
                in {
                    OrderState.CREATED,
                    OrderState.ACCEPTED,
                    OrderState.PARTIALLY_FILLED,
                }
            },
            order_details=order_details,
            effective_fill_quantities=self._effective_fill_quantities,
            marks=self._marks,
        )

    @staticmethod
    def _order_snapshot(record: _OrderRecord) -> OrderSnapshot:
        return OrderSnapshot(
            intent=record.intent,
            state=record.state,
            filled_quantity=record.filled_quantity,
            remaining_quantity=(record.intent.quantity - record.filled_quantity),
            reconciliation_observed_at=record.reconciliation_observed_at,
            reconciliation_revision=record.reconciliation_revision,
            terminal_required=record.terminal_required,
            reconciliation_cursor=record.reconciliation_cursor,
        )

    def assert_invariants(self) -> None:
        cash_tolerance = math.ulp(max(abs(self._cash), abs(self._initial_cash))) * 8.0
        if not math.isfinite(self._cash) or self._cash < -cash_tolerance:
            raise AssertionError("cash must stay finite and non-negative")
        if not math.isfinite(self._fees) or self._fees < 0.0:
            raise AssertionError("fees must stay finite and non-negative")
        if set(self._effective_fill_quantities) != set(self._fills):
            raise AssertionError("every raw fill requires an effective quantity")
        for fill_id, effective_quantity in self._effective_fill_quantities.items():
            raw_quantity = self._fills[fill_id].quantity
            if (
                not math.isfinite(effective_quantity)
                or effective_quantity <= 0.0
                or effective_quantity > raw_quantity
            ):
                raise AssertionError("invalid effective fill quantity")
        for symbol, mark in self._marks.items():
            if mark.symbol != symbol or mark.timestamp > self._observed_at:
                raise AssertionError("invalid market mark")
        for symbol, position in self._positions.items():
            if position.quantity == 0.0:
                raise AssertionError("zero quantity position retained: %s" % symbol)
            if not self._allow_short and position.quantity < 0.0:
                raise AssertionError("short position retained while disabled")
            if position.average_cost <= 0.0 or position.market_price <= 0.0:
                raise AssertionError("position prices must be positive")
        for record in self._orders.values():
            if record.filled_quantity < 0.0:
                raise AssertionError("filled quantity cannot be negative")
            if record.filled_quantity > record.intent.quantity:
                raise AssertionError("order is overfilled")
            remaining = record.intent.quantity - record.filled_quantity
            if record.state == OrderState.FILLED and remaining != 0.0:
                raise AssertionError("filled state has remaining quantity")
            if record.state == OrderState.PARTIALLY_FILLED and not (
                record.filled_quantity > 0.0
                and remaining > 0.0
            ):
                raise AssertionError("partial state lacks a partial fill")
        accounting_pnl = self._realized_pnl + self.unrealized_pnl - self._fees
        equity_pnl = self.equity - self._initial_cash
        if not math.isclose(
            accounting_pnl, equity_pnl, rel_tol=1e-9, abs_tol=1e-8
        ):
            raise AssertionError(
                "PnL conservation failed: accounting=%r equity=%r"
                % (accounting_pnl, equity_pnl)
            )
