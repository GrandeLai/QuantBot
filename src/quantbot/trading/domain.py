"""Immutable domain values shared by trading runtimes."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import math
from types import MappingProxyType
from typing import Literal, Mapping, Optional, Sequence, Tuple

from ..data.pit_domain import SecurityMasterSnapshot


def _non_empty(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("%s must be a non-empty string" % field_name)
    return value


def _aware(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("%s must be timezone-aware" % field_name)
    return value


def _finite(value: float, field_name: str) -> float:
    if isinstance(value, bool):
        raise ValueError("%s must be a finite number" % field_name)
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("%s must be a finite number" % field_name)
    if not math.isfinite(result):
        raise ValueError("%s must be finite" % field_name)
    return result


def _positive(value: float, field_name: str) -> float:
    result = _finite(value, field_name)
    if result <= 0.0:
        raise ValueError("%s must be positive" % field_name)
    return result


def _non_negative(value: float, field_name: str) -> float:
    result = _finite(value, field_name)
    if result < 0.0:
        raise ValueError("%s must be non-negative" % field_name)
    return result


def _quantity_tolerance(*values: float) -> float:
    magnitude = max((abs(value) for value in values), default=0.0)
    return math.ulp(magnitude) * 8.0


def _quantity_close(left: float, right: float) -> bool:
    return abs(left - right) <= _quantity_tolerance(left, right)


def _side(value: str) -> str:
    if value not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    return value


@dataclass(frozen=True)
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        _non_empty(self.symbol, "symbol")
        _aware(self.timestamp, "timestamp")
        for field_name in ("open", "high", "low", "close"):
            object.__setattr__(
                self,
                field_name,
                _positive(getattr(self, field_name), field_name),
            )
        object.__setattr__(self, "volume", _non_negative(self.volume, "volume"))
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be at least open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be at most open, high, and close")


@dataclass(frozen=True)
class Quote:
    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    def __post_init__(self) -> None:
        _non_empty(self.symbol, "symbol")
        _aware(self.timestamp, "timestamp")
        object.__setattr__(self, "bid", _positive(self.bid, "bid"))
        object.__setattr__(self, "ask", _positive(self.ask, "ask"))
        object.__setattr__(
            self, "bid_size", _non_negative(self.bid_size, "bid_size")
        )
        object.__setattr__(
            self, "ask_size", _non_negative(self.ask_size, "ask_size")
        )
        if self.bid > self.ask:
            raise ValueError("bid must not exceed ask")

    @property
    def mid_price(self) -> float:
        return (self.bid + self.ask) / 2.0


class OrderState(str, Enum):
    CREATED = "created"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class MarginStatus(str, Enum):
    CASH = "cash"
    MARGIN = "margin"
    PORTFOLIO_MARGIN = "portfolio_margin"
    SIMULATED = "simulated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    """Broker-observed account facts used by every pre-trade risk check."""

    broker: str
    observed_at: datetime
    equity: float
    buying_power: float
    trading_blocked: bool
    shorting_enabled: bool
    margin_status: MarginStatus
    day_trade_buying_power: Optional[float] = None
    restrictions: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _non_empty(self.broker, "broker")
        _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "equity", _non_negative(self.equity, "equity"))
        object.__setattr__(
            self,
            "buying_power",
            _non_negative(self.buying_power, "buying_power"),
        )
        if self.day_trade_buying_power is not None:
            object.__setattr__(
                self,
                "day_trade_buying_power",
                _non_negative(
                    self.day_trade_buying_power,
                    "day_trade_buying_power",
                ),
            )
        for field_name in ("trading_blocked", "shorting_enabled"):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError("%s must be a boolean" % field_name)
        if not isinstance(self.margin_status, MarginStatus):
            raise ValueError("margin_status must be a MarginStatus")
        if isinstance(self.restrictions, str):
            raise ValueError("restrictions must be a sequence of strings")
        try:
            restrictions = tuple(self.restrictions)
        except TypeError:
            raise ValueError("restrictions must be a sequence of strings")
        for restriction in restrictions:
            _non_empty(restriction, "restriction")
        if len(set(restrictions)) != len(restrictions):
            raise ValueError("restrictions must be unique")
        object.__setattr__(self, "restrictions", restrictions)


@dataclass(frozen=True)
class OrderIntent:
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    created_at: datetime

    def __post_init__(self) -> None:
        _non_empty(self.client_order_id, "client_order_id")
        _non_empty(self.symbol, "symbol")
        _side(self.side)
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        _aware(self.created_at, "created_at")


@dataclass(frozen=True)
class FillEvent:
    fill_id: str
    client_order_id: str
    symbol: str
    side: Literal["buy", "sell"]
    quantity: float
    price: float
    fee: float
    filled_at: datetime

    def __post_init__(self) -> None:
        _non_empty(self.fill_id, "fill_id")
        _non_empty(self.client_order_id, "client_order_id")
        _non_empty(self.symbol, "symbol")
        _side(self.side)
        object.__setattr__(self, "quantity", _positive(self.quantity, "quantity"))
        object.__setattr__(self, "price", _positive(self.price, "price"))
        object.__setattr__(self, "fee", _non_negative(self.fee, "fee"))
        _aware(self.filled_at, "filled_at")


@dataclass(frozen=True)
class OrderReconciliation:
    client_order_id: str
    state: OrderState
    filled_quantity: float
    fills: Tuple[FillEvent, ...] = field(default_factory=tuple)
    observed_at: Optional[datetime] = None
    revision: Optional[int] = None

    def __post_init__(self) -> None:
        _non_empty(self.client_order_id, "client_order_id")
        if not isinstance(self.state, OrderState) or self.state not in {
            OrderState.ACCEPTED,
            OrderState.REJECTED,
            OrderState.CANCELED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        }:
            raise ValueError("reconciliation state must be a broker terminal or working state")
        object.__setattr__(
            self,
            "filled_quantity",
            _non_negative(self.filled_quantity, "filled_quantity"),
        )
        if self.observed_at is not None:
            _aware(self.observed_at, "observed_at")
        if self.revision is not None and (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("revision must be a non-negative integer")
        try:
            fills = tuple(self.fills)
        except TypeError:
            raise ValueError("fills must be a sequence of FillEvent values")
        if any(not isinstance(fill, FillEvent) for fill in fills):
            raise ValueError("fills must contain FillEvent values")
        if len({fill.fill_id for fill in fills}) != len(fills):
            raise ValueError("reconciliation fills must have unique fill ids")
        previous_time = None
        expected_symbol = fills[0].symbol if fills else None
        expected_side = fills[0].side if fills else None
        for fill in fills:
            if fill.client_order_id != self.client_order_id:
                raise ValueError("reconciliation fill must match client_order_id")
            if fill.symbol != expected_symbol or fill.side != expected_side:
                raise ValueError("reconciliation fills must share symbol and side")
            if previous_time is not None and fill.filled_at < previous_time:
                raise ValueError("reconciliation fills must be time ordered")
            if self.observed_at is not None and fill.filled_at > self.observed_at:
                raise ValueError("reconciliation fills cannot follow observed_at")
            previous_time = fill.filled_at
        total_filled = sum(fill.quantity for fill in fills)
        if not _quantity_close(total_filled, self.filled_quantity):
            raise ValueError("filled_quantity must match reconciliation fills")
        if self.state in {OrderState.ACCEPTED, OrderState.REJECTED}:
            if self.filled_quantity != 0.0:
                raise ValueError("unfilled reconciliation state cannot have fills")
        elif self.state in {
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        } and self.filled_quantity <= 0.0:
            raise ValueError("filled reconciliation state requires fills")
        object.__setattr__(self, "fills", fills)


def _freeze_float_mapping(
    values: Mapping[str, float], field_name: str
) -> Mapping[str, float]:
    try:
        items = values.items()
    except AttributeError:
        raise ValueError("%s must be a mapping" % field_name)
    frozen = {}
    for key, value in items:
        symbol = _non_empty(key, "%s key" % field_name)
        frozen[symbol] = _finite(value, "%s[%s]" % (field_name, symbol))
    return MappingProxyType(frozen)


@dataclass(frozen=True)
class SignalFrame:
    observed_at: datetime
    signals: Mapping[str, float]

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        object.__setattr__(
            self, "signals", _freeze_float_mapping(self.signals, "signals")
        )


@dataclass(frozen=True)
class TargetPortfolio:
    observed_at: datetime
    weights: Mapping[str, float]

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        object.__setattr__(
            self, "weights", _freeze_float_mapping(self.weights, "weights")
        )


@dataclass(frozen=True)
class PointInTimeSnapshot:
    observed_at: datetime
    bars: Mapping[str, Bar] = field(default_factory=dict)
    quotes: Mapping[str, Quote] = field(default_factory=dict)
    signals: Optional[SignalFrame] = None
    security_master: Optional[SecurityMasterSnapshot] = None

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        bars = self._validated_events(self.bars, Bar, "bars")
        quotes = self._validated_events(self.quotes, Quote, "quotes")
        object.__setattr__(self, "bars", MappingProxyType(bars))
        object.__setattr__(self, "quotes", MappingProxyType(quotes))
        if self.signals is not None:
            if not isinstance(self.signals, SignalFrame):
                raise ValueError("signals must be a SignalFrame")
            if self.signals.observed_at > self.observed_at:
                raise ValueError("signals cannot contain future information")
        if self.security_master is not None:
            if not isinstance(self.security_master, SecurityMasterSnapshot):
                raise ValueError("security_master must be a SecurityMasterSnapshot")
            if self.security_master.as_of > self.observed_at:
                raise ValueError("security_master cannot contain future information")

    def _validated_events(self, values, expected_type, field_name):
        try:
            items = values.items()
        except AttributeError:
            raise ValueError("%s must be a mapping" % field_name)
        result = {}
        for symbol, event in items:
            _non_empty(symbol, "%s key" % field_name)
            if not isinstance(event, expected_type):
                raise ValueError("%s values have the wrong type" % field_name)
            if event.symbol != symbol:
                raise ValueError("%s key must match event symbol" % field_name)
            if event.timestamp > self.observed_at:
                raise ValueError("%s cannot contain future information" % field_name)
            result[symbol] = event
        return result

    def latest_price(self, symbol: str) -> float:
        quote = self.quotes.get(symbol)
        bar = self.bars.get(symbol)
        if quote is not None and (
            bar is None or quote.timestamp >= bar.timestamp
        ):
            return quote.mid_price
        if bar is not None:
            return bar.close
        raise KeyError(symbol)


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: float
    average_cost: float
    market_price: float
    realized_pnl: float = 0.0

    def __post_init__(self) -> None:
        _non_empty(self.symbol, "symbol")
        quantity = _finite(self.quantity, "quantity")
        if quantity == 0.0:
            raise ValueError("quantity must be non-zero")
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(
            self, "average_cost", _positive(self.average_cost, "average_cost")
        )
        object.__setattr__(
            self, "market_price", _positive(self.market_price, "market_price")
        )
        object.__setattr__(
            self, "realized_pnl", _finite(self.realized_pnl, "realized_pnl")
        )

    @property
    def market_value(self) -> float:
        return self.quantity * self.market_price

    @property
    def unrealized_pnl(self) -> float:
        return self.quantity * (self.market_price - self.average_cost)


@dataclass(frozen=True)
class MarkSnapshot:
    symbol: str
    price: float
    timestamp: datetime

    def __post_init__(self) -> None:
        _non_empty(self.symbol, "symbol")
        object.__setattr__(self, "price", _positive(self.price, "price"))
        _aware(self.timestamp, "timestamp")


@dataclass(frozen=True)
class OrderSnapshot:
    intent: OrderIntent
    state: OrderState
    filled_quantity: float
    remaining_quantity: float
    reconciliation_observed_at: Optional[datetime] = None
    reconciliation_revision: Optional[int] = None
    terminal_required: bool = False
    reconciliation_cursor: Optional[OrderReconciliation] = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, OrderIntent):
            raise ValueError("intent must be an OrderIntent")
        if not isinstance(self.state, OrderState):
            raise ValueError("state must be an OrderState")
        filled = _non_negative(self.filled_quantity, "filled_quantity")
        remaining = _non_negative(self.remaining_quantity, "remaining_quantity")
        object.__setattr__(self, "filled_quantity", filled)
        object.__setattr__(self, "remaining_quantity", remaining)
        if self.reconciliation_observed_at is not None:
            _aware(
                self.reconciliation_observed_at,
                "reconciliation_observed_at",
            )
        if self.reconciliation_revision is not None and (
            isinstance(self.reconciliation_revision, bool)
            or not isinstance(self.reconciliation_revision, int)
            or self.reconciliation_revision < 0
        ):
            raise ValueError(
                "reconciliation_revision must be a non-negative integer"
            )
        if type(self.terminal_required) is not bool:
            raise ValueError("terminal_required must be a bool")
        if self.terminal_required and self.state != OrderState.RECONCILIATION_REQUIRED:
            raise ValueError("terminal_required requires reconciliation state")
        if self.reconciliation_cursor is not None:
            if not isinstance(self.reconciliation_cursor, OrderReconciliation):
                raise ValueError(
                    "reconciliation_cursor must be an OrderReconciliation"
                )
            if (
                self.reconciliation_cursor.client_order_id
                != self.intent.client_order_id
            ):
                raise ValueError("reconciliation_cursor must match its order")
            if (
                self.reconciliation_observed_at
                != self.reconciliation_cursor.observed_at
                or self.reconciliation_revision
                != self.reconciliation_cursor.revision
            ):
                raise ValueError("reconciliation cursor metadata must match")
        if not _quantity_close(filled + remaining, self.intent.quantity):
            raise ValueError("filled and remaining quantities must equal order quantity")
        has_fill = filled != 0.0
        has_remaining = remaining != 0.0
        if self.state in {OrderState.CREATED, OrderState.ACCEPTED} and has_fill:
            raise ValueError("unfilled order states cannot have filled quantity")
        if self.state == OrderState.PARTIALLY_FILLED and not (
            has_fill and has_remaining
        ):
            raise ValueError("partially filled state requires fill and remainder")
        if self.state == OrderState.FILLED and (
            has_remaining or filled != self.intent.quantity
        ):
            raise ValueError("filled state requires the full order quantity")
        if self.state == OrderState.CANCELED and not has_remaining:
            raise ValueError("canceled state requires an unfilled remainder")
        if self.state == OrderState.REJECTED and has_fill:
            raise ValueError("rejected state cannot have filled quantity")


@dataclass(frozen=True)
class PortfolioSnapshot:
    observed_at: datetime
    cash: float
    positions: Mapping[str, PositionSnapshot] = field(default_factory=dict)
    orders: Mapping[str, OrderState] = field(default_factory=dict)
    fills: Sequence[FillEvent] = field(default_factory=tuple)
    fees: float = 0.0
    realized_pnl: float = 0.0
    open_orders: Mapping[str, OrderSnapshot] = field(default_factory=dict)
    order_details: Mapping[str, OrderSnapshot] = field(default_factory=dict)
    effective_fill_quantities: Optional[Mapping[str, float]] = None
    marks: Mapping[str, MarkSnapshot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "cash", _non_negative(self.cash, "cash"))
        object.__setattr__(self, "fees", _non_negative(self.fees, "fees"))
        object.__setattr__(
            self, "realized_pnl", _finite(self.realized_pnl, "realized_pnl")
        )

        try:
            position_items = self.positions.items()
        except AttributeError:
            raise ValueError("positions must be a mapping")
        positions = {}
        for symbol, position in position_items:
            _non_empty(symbol, "positions key")
            if not isinstance(position, PositionSnapshot) or position.symbol != symbol:
                raise ValueError("position key must match a PositionSnapshot symbol")
            positions[symbol] = position
        object.__setattr__(self, "positions", MappingProxyType(positions))

        try:
            mark_items = self.marks.items()
        except AttributeError:
            raise ValueError("marks must be a mapping")
        marks = {}
        for symbol, mark in mark_items:
            _non_empty(symbol, "marks key")
            if not isinstance(mark, MarkSnapshot) or mark.symbol != symbol:
                raise ValueError("mark key must match a MarkSnapshot symbol")
            if mark.timestamp > self.observed_at:
                raise ValueError("marks cannot contain future information")
            marks[symbol] = mark
        object.__setattr__(self, "marks", MappingProxyType(marks))

        try:
            order_items = self.orders.items()
        except AttributeError:
            raise ValueError("orders must be a mapping")
        orders = {}
        for client_order_id, state in order_items:
            _non_empty(client_order_id, "orders key")
            if not isinstance(state, OrderState):
                raise ValueError("orders values must be OrderState values")
            orders[client_order_id] = state
        object.__setattr__(self, "orders", MappingProxyType(orders))

        try:
            open_order_items = self.open_orders.items()
        except AttributeError:
            raise ValueError("open_orders must be a mapping")
        open_orders = {}
        active_states = {
            OrderState.CREATED,
            OrderState.ACCEPTED,
            OrderState.PARTIALLY_FILLED,
        }
        for client_order_id, order in open_order_items:
            _non_empty(client_order_id, "open_orders key")
            if (
                not isinstance(order, OrderSnapshot)
                or order.intent.client_order_id != client_order_id
                or order.state not in active_states
            ):
                raise ValueError("open_orders must contain active matching orders")
            if client_order_id not in orders or orders[client_order_id] != order.state:
                raise ValueError("open_orders state must match orders")
            if order.intent.created_at > self.observed_at:
                raise ValueError("open_orders cannot contain future orders")
            open_orders[client_order_id] = order
        if {
            client_order_id
            for client_order_id, state in orders.items()
            if state in active_states
        } != set(open_orders):
            raise ValueError("every active order must have one open_orders entry")
        object.__setattr__(self, "open_orders", MappingProxyType(open_orders))

        try:
            detail_items = self.order_details.items()
        except AttributeError:
            raise ValueError("order_details must be a mapping")
        order_details = {}
        for client_order_id, order in detail_items:
            _non_empty(client_order_id, "order_details key")
            if (
                not isinstance(order, OrderSnapshot)
                or order.intent.client_order_id != client_order_id
                or orders.get(client_order_id) != order.state
                or order.intent.created_at > self.observed_at
            ):
                raise ValueError("order_details must contain matching known orders")
            order_details[client_order_id] = order
        for client_order_id, state in orders.items():
            if (
                state == OrderState.RECONCILIATION_REQUIRED
                and client_order_id not in order_details
            ):
                raise ValueError("reconciliation orders require order_details")
        if set(order_details) != set(orders):
            raise ValueError("orders and order_details must have identical ids")
        for client_order_id, order in open_orders.items():
            if order_details[client_order_id] != order:
                raise ValueError("open_orders must be the working order_details subset")
        object.__setattr__(
            self, "order_details", MappingProxyType(order_details)
        )

        fills = tuple(self.fills)
        if any(not isinstance(fill, FillEvent) for fill in fills):
            raise ValueError("fills must contain FillEvent values")
        if len({fill.fill_id for fill in fills}) != len(fills):
            raise ValueError("fills must have unique fill_id values")
        raw_fills = {fill.fill_id: fill for fill in fills}
        if self.effective_fill_quantities is None:
            effective_fill_quantities = {
                fill.fill_id: fill.quantity for fill in fills
            }
        else:
            try:
                effective_items = self.effective_fill_quantities.items()
            except AttributeError:
                raise ValueError("effective_fill_quantities must be a mapping")
            effective_fill_quantities = {}
            for fill_id, quantity in effective_items:
                _non_empty(fill_id, "effective_fill_quantities key")
                effective = _positive(
                    quantity,
                    "effective_fill_quantities[%s]" % fill_id,
                )
                raw_fill = raw_fills.get(fill_id)
                if raw_fill is None:
                    raise ValueError(
                        "effective fill quantities must refer to raw fills"
                    )
                if effective > raw_fill.quantity:
                    raise ValueError(
                        "effective fill quantity cannot exceed raw quantity"
                    )
                effective_fill_quantities[fill_id] = effective
            if set(effective_fill_quantities) != set(raw_fills):
                raise ValueError("every raw fill requires an effective quantity")
        object.__setattr__(
            self,
            "effective_fill_quantities",
            MappingProxyType(effective_fill_quantities),
        )
        fill_quantities = {}
        for fill in fills:
            if fill.filled_at > self.observed_at:
                raise ValueError("fills cannot contain future information")
            if fill.client_order_id not in orders:
                raise ValueError("fills must refer to orders in the snapshot")
            detail = order_details[fill.client_order_id]
            if (
                fill.symbol != detail.intent.symbol
                or fill.side != detail.intent.side
            ):
                raise ValueError("fill does not match its order detail")
            if fill.filled_at < detail.intent.created_at:
                raise ValueError("fill cannot precede its order")
            fill_quantities[fill.client_order_id] = (
                fill_quantities.get(fill.client_order_id, 0.0)
                + effective_fill_quantities[fill.fill_id]
            )
        for client_order_id, order in order_details.items():
            if not _quantity_close(
                fill_quantities.get(client_order_id, 0.0),
                order.filled_quantity,
            ):
                raise ValueError("order detail filled quantity must match fills")
        object.__setattr__(self, "fills", fills)

    @property
    def unrealized_pnl(self) -> float:
        return sum(position.unrealized_pnl for position in self.positions.values())

    @property
    def equity(self) -> float:
        return self.cash + sum(
            position.market_value for position in self.positions.values()
        )

    @property
    def requires_reconciliation(self) -> bool:
        return any(
            state == OrderState.RECONCILIATION_REQUIRED
            for state in self.orders.values()
        )

    @property
    def reconciliation_orders(self) -> Mapping[str, OrderSnapshot]:
        return MappingProxyType(
            {
                client_order_id: detail
                for client_order_id, detail in self.order_details.items()
                if detail.state == OrderState.RECONCILIATION_REQUIRED
            }
        )


@dataclass(frozen=True)
class RunManifest:
    run_type: str
    observed_at: datetime
    dataset_hash: str
    config_hash: str
    code_hash: str
    policy_version: Optional[str] = None
    cost_model_hash: Optional[str] = None
    input_materialization_hash: Optional[str] = None
    output_hash: Optional[str] = None
    report_hash: Optional[str] = None
    cost_suite_hash: Optional[str] = None
    run_id: Optional[str] = None
    experiment_id: Optional[str] = None

    def __post_init__(self) -> None:
        _non_empty(self.run_type, "run_type")
        _aware(self.observed_at, "observed_at")
        _non_empty(self.dataset_hash, "dataset_hash")
        _non_empty(self.config_hash, "config_hash")
        _non_empty(self.code_hash, "code_hash")
        if self.policy_version is not None:
            _non_empty(self.policy_version, "policy_version")
        if self.cost_model_hash is not None:
            _non_empty(self.cost_model_hash, "cost_model_hash")
        for name in (
            "input_materialization_hash",
            "output_hash",
            "report_hash",
            "cost_suite_hash",
            "run_id",
            "experiment_id",
        ):
            value = getattr(self, name)
            if value is not None:
                _non_empty(value, name)


@dataclass(frozen=True)
class BacktestReport:
    manifest: RunManifest
    started_at: datetime
    ended_at: datetime
    initial_equity: float
    final_equity: float
    fees: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    order_count: int = 0
    fill_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, RunManifest):
            raise ValueError("manifest must be a RunManifest")
        _aware(self.started_at, "started_at")
        _aware(self.ended_at, "ended_at")
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must not be before started_at")
        if self.manifest.observed_at < self.ended_at:
            raise ValueError("manifest observed_at must not precede ended_at")
        object.__setattr__(
            self, "initial_equity", _positive(self.initial_equity, "initial_equity")
        )
        object.__setattr__(
            self, "final_equity", _non_negative(self.final_equity, "final_equity")
        )
        object.__setattr__(self, "fees", _non_negative(self.fees, "fees"))
        object.__setattr__(
            self, "realized_pnl", _finite(self.realized_pnl, "realized_pnl")
        )
        object.__setattr__(
            self, "unrealized_pnl", _finite(self.unrealized_pnl, "unrealized_pnl")
        )
        equity_change = self.final_equity - self.initial_equity
        accounting_pnl = self.realized_pnl + self.unrealized_pnl - self.fees
        scale = max(abs(equity_change), abs(accounting_pnl), 1.0)
        if not math.isclose(
            equity_change,
            accounting_pnl,
            rel_tol=1e-12,
            abs_tol=math.ulp(scale) * 16.0,
        ):
            raise ValueError("backtest report PnL does not conserve equity")
        for field_name in ("order_count", "fill_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("%s must be a non-negative integer" % field_name)

    @property
    def total_return(self) -> float:
        return self.final_equity / self.initial_equity - 1.0
