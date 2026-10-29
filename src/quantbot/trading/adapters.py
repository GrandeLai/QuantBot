"""Adapters between strategy-level targets and execution-level orders."""

from hashlib import sha256
import math
from datetime import datetime
from typing import Mapping, Optional, Tuple

from .domain import (
    BrokerAccountSnapshot,
    MarginStatus,
    OrderIntent,
    PointInTimeSnapshot,
    PortfolioSnapshot,
    TargetPortfolio,
)
from .ports import DailyStrategy, MarketEvent


class StaticAccountStatePort:
    """Deterministic account-state source for simulations and tests."""

    def __init__(self, snapshot: BrokerAccountSnapshot) -> None:
        if not isinstance(snapshot, BrokerAccountSnapshot):
            raise ValueError("snapshot must be a BrokerAccountSnapshot")
        self._snapshot = snapshot

    def account_snapshot(self) -> BrokerAccountSnapshot:
        return self._snapshot


def broker_account_snapshot_from_payload(
    payload,
    broker: str,
    observed_at: datetime,
) -> BrokerAccountSnapshot:
    """Maps a broker schema without defaulting missing safety facts to safe."""

    normalized_broker = str(broker).strip().lower()
    if normalized_broker == "alpaca":
        equity = _required_field(payload, ("equity",))
        buying_power = _required_field(payload, ("buying_power",))
        trading_blocked = _required_strict_bool(payload, "trading_blocked")
        account_blocked = _required_strict_bool(payload, "account_blocked")
        trade_suspended = _required_strict_bool(
            payload, "trade_suspended_by_user"
        )
        shorting_enabled = _required_strict_bool(payload, "shorting_enabled")
        status = _required_field(payload, ("status",))
        margin_status = _alpaca_margin_status(payload)
        allowed_statuses = {"ACTIVE"}
    elif normalized_broker == "ibkr":
        equity = _required_field(
            payload, ("equity", "net_liquidation", "NetLiquidation")
        )
        buying_power = _required_field(
            payload, ("buying_power", "buyingPower", "BuyingPower")
        )
        trading_blocked = _required_strict_bool(payload, "trading_blocked")
        account_blocked = False
        trade_suspended = False
        shorting_enabled = _required_strict_bool(payload, "shorting_enabled")
        status = _required_field(payload, ("status", "account_status"))
        margin_status = _required_margin_status(payload)
        allowed_statuses = {"ACTIVE", "OPEN", "ENABLED"}
    else:
        raise ValueError("unsupported broker account schema: %s" % broker)
    day_trade_buying_power = _optional_field(
        payload,
        (
            "day_trade_buying_power",
            "daytrading_buying_power",
            "dayTradeBuyingPower",
        ),
    )
    restrictions = list(_restrictions(payload))
    normalized_status = str(status).strip().upper()
    if not normalized_status:
        raise ValueError("broker account status must be non-empty")
    if normalized_status not in allowed_statuses:
        restrictions.append("account_status:%s" % str(status).strip().lower())
    if trade_suspended:
        restrictions.append("trade_suspended_by_user")
    restrictions = tuple(dict.fromkeys(restrictions))
    return BrokerAccountSnapshot(
        broker=normalized_broker,
        observed_at=observed_at,
        equity=equity,
        buying_power=buying_power,
        day_trade_buying_power=day_trade_buying_power,
        trading_blocked=trading_blocked or account_blocked,
        shorting_enabled=shorting_enabled,
        margin_status=margin_status,
        restrictions=restrictions,
    )


def _field(payload, name):
    if isinstance(payload, Mapping):
        return payload.get(name)
    return getattr(payload, name, None)


def _has_field(payload, name):
    if isinstance(payload, Mapping):
        return name in payload
    return hasattr(payload, name)


def _optional_field(payload, names):
    for name in names:
        value = _field(payload, name)
        if value is not None:
            return value
    return None


def _required_field(payload, names):
    value = _optional_field(payload, names)
    if value is None:
        raise ValueError("broker account is missing %s" % names[0])
    return value


def _required_strict_bool(payload, name):
    if not _has_field(payload, name):
        raise ValueError("broker account is missing %s" % name)
    value = _field(payload, name)
    if not isinstance(value, bool):
        raise ValueError("%s must be a boolean" % name)
    return value


def _required_margin_status(payload):
    raw = _required_field(
        payload, ("margin_status", "account_type", "AccountType")
    )
    if isinstance(raw, MarginStatus):
        return MarginStatus.UNKNOWN if raw == MarginStatus.SIMULATED else raw
    normalized = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "cash": MarginStatus.CASH,
        "margin": MarginStatus.MARGIN,
        "reg_t_margin": MarginStatus.MARGIN,
        "portfolio_margin": MarginStatus.PORTFOLIO_MARGIN,
        "portfolio": MarginStatus.PORTFOLIO_MARGIN,
        "unknown": MarginStatus.UNKNOWN,
    }
    return aliases.get(normalized, MarginStatus.UNKNOWN)


def _alpaca_margin_status(payload):
    if _has_field(payload, "margin_status"):
        return _required_margin_status(payload)
    multiplier = _required_field(payload, ("multiplier",))
    if isinstance(multiplier, bool):
        raise ValueError("multiplier must be a positive number")
    try:
        value = float(multiplier)
    except (TypeError, ValueError):
        raise ValueError("multiplier must be a positive number")
    if not math.isfinite(value) or value <= 0.0:
        raise ValueError("multiplier must be a positive number")
    if value == 1.0:
        return MarginStatus.CASH
    return MarginStatus.MARGIN


def _restrictions(payload):
    raw = _optional_field(payload, ("restrictions",))
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, Mapping):
        raise ValueError("restrictions must be a sequence")
    try:
        return tuple(raw)
    except TypeError:
        raise ValueError("restrictions must be a sequence")


class TargetPortfolioAdapter:
    """Turns target weights into stable, price-aware quantity deltas."""

    def __init__(
        self,
        strategy: DailyStrategy,
        quantity_epsilon: float = 1e-9,
    ) -> None:
        try:
            epsilon = float(quantity_epsilon)
        except (TypeError, ValueError):
            raise ValueError("quantity_epsilon must be finite and non-negative")
        if not math.isfinite(epsilon) or epsilon < 0.0:
            raise ValueError("quantity_epsilon must be finite and non-negative")
        self.strategy = strategy
        self.quantity_epsilon = epsilon
        self._cached_snapshot: Optional[PointInTimeSnapshot] = None
        self._cached_targets: Optional[TargetPortfolio] = None

    def on_event(
        self, state: PortfolioSnapshot, event: MarketEvent
    ) -> Tuple[OrderIntent, ...]:
        if not isinstance(event, PointInTimeSnapshot):
            raise ValueError("daily target strategies require PointInTimeSnapshot events")
        return self.generate_intents(state, event)

    def generate_intents(
        self,
        state: PortfolioSnapshot,
        snapshot: PointInTimeSnapshot,
    ) -> Tuple[OrderIntent, ...]:
        if not isinstance(state, PortfolioSnapshot):
            raise ValueError("state must be a PortfolioSnapshot")
        if state.observed_at > snapshot.observed_at:
            raise ValueError("portfolio state cannot use future information")
        targets = self._targets_for(snapshot)
        intents = []
        symbols = sorted(
            set(targets.weights)
            .union(state.positions)
            .union(order.intent.symbol for order in state.open_orders.values())
        )
        for symbol in symbols:
            target_weight = targets.weights.get(symbol, 0.0)
            target_quantity = self._target_quantity(
                state, snapshot, symbol, target_weight
            )
            current_position = state.positions.get(symbol)
            current_quantity = (
                current_position.quantity if current_position is not None else 0.0
            )
            working_quantity = sum(
                order.remaining_quantity
                * (1.0 if order.intent.side == "buy" else -1.0)
                for order in state.open_orders.values()
                if order.intent.symbol == symbol
            )
            delta = target_quantity - current_quantity - working_quantity
            if abs(delta) <= self.quantity_epsilon:
                continue
            side = "buy" if delta > 0.0 else "sell"
            quantity = abs(delta)
            identity = "%s|%s|%.17g" % (
                snapshot.observed_at.isoformat(),
                symbol,
                target_weight,
            )
            intents.append(
                OrderIntent(
                    client_order_id=(
                        "target-"
                        + sha256(identity.encode("utf-8")).hexdigest()[:24]
                    ),
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    created_at=snapshot.observed_at,
                )
            )
        return tuple(intents)

    def cancel_order_ids(
        self,
        state: PortfolioSnapshot,
        event: MarketEvent,
    ) -> Tuple[str, ...]:
        if not isinstance(event, PointInTimeSnapshot):
            raise ValueError("daily target strategies require PointInTimeSnapshot events")
        if not isinstance(state, PortfolioSnapshot):
            raise ValueError("state must be a PortfolioSnapshot")
        if state.observed_at > event.observed_at:
            raise ValueError("portfolio state cannot use future information")
        targets = self._targets_for(event)
        cancel_ids = []
        symbols = sorted(
            set(targets.weights)
            .union(state.positions)
            .union(order.intent.symbol for order in state.open_orders.values())
        )
        for symbol in symbols:
            working = [
                order
                for order in state.open_orders.values()
                if order.intent.symbol == symbol
            ]
            if not working:
                continue
            target_quantity = self._target_quantity(
                state,
                event,
                symbol,
                targets.weights.get(symbol, 0.0),
            )
            position = state.positions.get(symbol)
            current_quantity = position.quantity if position is not None else 0.0
            gap = target_quantity - current_quantity
            working_quantity = sum(
                order.remaining_quantity
                * (1.0 if order.intent.side == "buy" else -1.0)
                for order in working
            )
            expected_side = "buy" if gap > 0.0 else "sell"
            covered = (
                abs(gap) > self.quantity_epsilon
                and abs(gap - working_quantity) <= self.quantity_epsilon
                and all(order.intent.side == expected_side for order in working)
            )
            if not covered:
                cancel_ids.extend(order.intent.client_order_id for order in working)
        return tuple(cancel_ids)

    @staticmethod
    def _target_quantity(
        state: PortfolioSnapshot,
        snapshot: PointInTimeSnapshot,
        symbol: str,
        target_weight: float,
    ) -> float:
        if target_weight == 0.0:
            return 0.0
        return state.equity * target_weight / snapshot.latest_price(symbol)

    def _targets_for(self, snapshot: PointInTimeSnapshot) -> TargetPortfolio:
        if self._cached_snapshot is snapshot and self._cached_targets is not None:
            return self._cached_targets
        targets = self.strategy.generate_targets(snapshot)
        if not isinstance(targets, TargetPortfolio):
            raise ValueError("daily strategy must return TargetPortfolio")
        if targets.observed_at > snapshot.observed_at:
            raise ValueError("target portfolio cannot use future information")
        self._cached_snapshot = snapshot
        self._cached_targets = targets
        return targets
