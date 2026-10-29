"""Broker execution adapter and rebalance logic."""

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import math
from pathlib import Path
from typing import Mapping, Optional, Protocol, Tuple

from .trading import (
    AccountStatePort,
    BrokerAccountRiskGate,
    BrokerAccountSnapshot,
    broker_account_snapshot_from_payload,
)


class BrokerClient(AccountStatePort, Protocol):
    def account_snapshot(self) -> BrokerAccountSnapshot:
        ...

    def submit_order(
        self,
        symbol: str,
        notional: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> None:
        ...

    def close_all_positions(self) -> None:
        ...


@dataclass(frozen=True)
class PlannedRebalanceOrder:
    symbol: str
    notional: float
    side: str
    risk_side: str
    risk_opening_notional: float
    order_type: str
    time_in_force: str

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        for field_name in ("notional", "risk_opening_notional"):
            value = getattr(self, field_name)
            if isinstance(value, bool):
                raise ValueError("%s must be finite" % field_name)
            try:
                value = float(value)
            except (TypeError, ValueError):
                raise ValueError("%s must be finite" % field_name)
            if not math.isfinite(value):
                raise ValueError("%s must be finite" % field_name)
            object.__setattr__(self, field_name, value)
        if self.notional <= 0.0:
            raise ValueError("notional must be positive")
        if self.risk_opening_notional < 0.0:
            raise ValueError("risk_opening_notional must be non-negative")
        if self.risk_opening_notional > self.notional:
            raise ValueError("risk_opening_notional cannot exceed notional")
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be buy or sell")
        if self.risk_side not in ("buy", "sell", "short"):
            raise ValueError("risk_side must be buy, sell, or short")
        for field_name in ("order_type", "time_in_force"):
            if not isinstance(getattr(self, field_name), str) or not getattr(
                self, field_name
            ).strip():
                raise ValueError("%s must be a non-empty string" % field_name)


@dataclass(frozen=True)
class RebalancePlan:
    account_snapshot: BrokerAccountSnapshot
    orders: Tuple[PlannedRebalanceOrder, ...]
    _owner_token: object = field(repr=False, compare=False)
    _execution_token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.account_snapshot, BrokerAccountSnapshot):
            raise ValueError("account_snapshot must be BrokerAccountSnapshot")
        orders = tuple(self.orders)
        if any(not isinstance(order, PlannedRebalanceOrder) for order in orders):
            raise ValueError("orders must contain PlannedRebalanceOrder values")
        object.__setattr__(self, "orders", orders)


class BrokerSubmissionUncertainError(RuntimeError):
    """A submit call failed after execution began and requires reconciliation.

    ``succeeded_orders`` means only that those legacy submit calls returned
    without raising; it is not broker-confirmed order or fill state. Residual
    failover requires the shared ExecutionPort order-state machine and is not
    inferred from this legacy interface.
    """

    def __init__(
        self,
        attempted_orders,
        succeeded_orders,
        original_error: Exception,
    ) -> None:
        self.attempted_orders = tuple(attempted_orders)
        self.succeeded_orders = tuple(succeeded_orders)
        self.attempted_count = len(self.attempted_orders)
        self.succeeded_count = len(self.succeeded_orders)
        self.original_error = original_error
        super().__init__(
            "broker_submission_uncertain: reconciliation_required; "
            "attempted_count=%d; succeeded_count=%d; cause=%s: %s"
            % (
                self.attempted_count,
                self.succeeded_count,
                type(original_error).__name__,
                original_error,
            )
        )

    def __repr__(self) -> str:
        return "%s(%r)" % (type(self).__name__, str(self))


class AccountAwareBrokerAdapter:
    """Adds an explicit account-state port to a legacy submission broker."""

    def __init__(self, legacy_broker, account_state_port: AccountStatePort) -> None:
        if not isinstance(account_state_port, AccountStatePort):
            raise ValueError("account_state_port must implement AccountStatePort")
        self.legacy_broker = legacy_broker
        self.account_state_port = account_state_port

    def account_snapshot(self) -> BrokerAccountSnapshot:
        return self.account_state_port.account_snapshot()

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        return self.account_snapshot()

    def get_portfolio_value(self) -> float:
        return float(self.legacy_broker.get_portfolio_value())

    def submit_order(self, *args, **kwargs):
        return self.legacy_broker.submit_order(*args, **kwargs)

    def close_all_positions(self) -> None:
        self.legacy_broker.close_all_positions()


class AutoExecutor:
    def __init__(self, broker: BrokerClient, min_trade_delta: float = 0.005):
        if not isinstance(broker, AccountStatePort):
            raise ValueError(
                "broker must implement AccountStatePort.account_snapshot; "
                "wrap legacy brokers with AccountAwareBrokerAdapter"
            )
        if isinstance(min_trade_delta, bool):
            raise ValueError("min_trade_delta must be finite and non-negative")
        try:
            delta = float(min_trade_delta)
        except (TypeError, ValueError):
            raise ValueError("min_trade_delta must be finite and non-negative")
        if not math.isfinite(delta) or delta < 0.0:
            raise ValueError("min_trade_delta must be finite and non-negative")
        self.broker = broker
        self.min_trade_delta = delta
        self._owner_token = object()
        self._executed_plan_tokens = set()

    def prepare_rebalance(
        self,
        target_weights: Mapping[str, float],
        current_holdings: Mapping[str, float],
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> RebalancePlan:
        for field_name, value in (
            ("order_type", order_type),
            ("time_in_force", time_in_force),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("%s must be a non-empty string" % field_name)
        targets = self._validated_weights(target_weights, "target_weights")
        current = self._validated_weights(current_holdings, "current_holdings")
        snapshot = self.broker.account_snapshot()
        if not isinstance(snapshot, BrokerAccountSnapshot):
            raise ValueError("broker must return BrokerAccountSnapshot")
        portfolio_value = snapshot.equity
        tickers = sorted(set(targets).union(current))
        planned_orders = []
        for ticker in tickers:
            target_weight = targets.get(ticker, 0.0)
            current_weight = current.get(ticker, 0.0)
            diff = target_weight - current_weight
            if abs(diff) < self.min_trade_delta:
                continue
            notional = round(abs(diff) * portfolio_value, 2)
            if notional == 0.0:
                continue
            side = "buy" if diff > 0 else "sell"
            opening_long_weight = max(0.0, target_weight) - max(
                0.0, current_weight
            )
            opening_short_weight = max(0.0, -target_weight) - max(
                0.0, -current_weight
            )
            if opening_long_weight > 0.0:
                risk_side = "buy"
                risk_notional = self._opening_notional(
                    opening_long_weight, portfolio_value, notional
                )
            elif opening_short_weight > 0.0:
                risk_side = "short"
                risk_notional = self._opening_notional(
                    opening_short_weight, portfolio_value, notional
                )
            else:
                risk_side = "sell"
                risk_notional = 0.0
            planned_orders.append(
                PlannedRebalanceOrder(
                    symbol=ticker,
                    notional=notional,
                    side=side,
                    risk_side=risk_side,
                    risk_opening_notional=risk_notional,
                    order_type=order_type,
                    time_in_force=time_in_force,
                )
            )

        gate = BrokerAccountRiskGate()
        remaining_buying_power = snapshot.buying_power
        for order in planned_orders:
            risk_snapshot = replace(snapshot, buying_power=remaining_buying_power)
            check_notional = order.risk_opening_notional or order.notional
            decision = gate.evaluate(
                risk_snapshot,
                check_notional,
                side=order.risk_side,
            )
            if not decision.allowed:
                raise ValueError(
                    "broker account risk rejected order: %s"
                    % ",".join(decision.reasons)
                )
            remaining_buying_power -= order.risk_opening_notional
        return RebalancePlan(
            account_snapshot=snapshot,
            orders=tuple(planned_orders),
            _owner_token=self._owner_token,
            _execution_token=object(),
        )

    def execute_rebalance(self, plan: RebalancePlan) -> None:
        if not isinstance(plan, RebalancePlan) or plan._owner_token is not self._owner_token:
            raise ValueError("plan was not prepared by this AutoExecutor")
        if plan._execution_token in self._executed_plan_tokens:
            raise ValueError("rebalance plan execution has already started")
        self._executed_plan_tokens.add(plan._execution_token)
        attempted = []
        succeeded = []
        for order in plan.orders:
            attempted.append(order)
            try:
                self.broker.submit_order(
                    symbol=order.symbol,
                    notional=order.notional,
                    side=order.side,
                    order_type=order.order_type,
                    time_in_force=order.time_in_force,
                )
            except Exception as error:
                raise BrokerSubmissionUncertainError(
                    attempted_orders=attempted,
                    succeeded_orders=succeeded,
                    original_error=error,
                ) from error
            succeeded.append(order)

    def rebalance(
        self,
        target_weights: Mapping[str, float],
        current_holdings: Mapping[str, float],
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> None:
        plan = self.prepare_rebalance(
            target_weights,
            current_holdings,
            order_type=order_type,
            time_in_force=time_in_force,
        )
        self.execute_rebalance(plan)

    @staticmethod
    def _validated_weights(values, field_name):
        try:
            items = values.items()
        except AttributeError:
            raise ValueError("%s must be a mapping" % field_name)
        normalized = {}
        for symbol, raw_value in items:
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError("%s symbols must be non-empty strings" % field_name)
            if isinstance(raw_value, bool):
                raise ValueError("%s weights must be finite" % field_name)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError("%s weights must be finite" % field_name)
            if not math.isfinite(value):
                raise ValueError("%s weights must be finite" % field_name)
            normalized[symbol] = value
        return normalized

    @staticmethod
    def _opening_notional(weight, equity, total_notional):
        rounded = round(weight * equity, 2)
        return min(total_notional, max(0.01, rounded))

    def emergency_exit(self) -> None:
        self.broker.close_all_positions()


def daily_signal_execution_plan(style: str = "vwap"):
    if style == "next_open":
        return {"order_type": "market", "time_in_force": "opg"}
    if style == "vwap":
        return {"order_type": "vwap", "time_in_force": "day"}
    raise ValueError("unsupported daily execution style: %s" % style)


SUPPORTED_BROKERS = ("Alpaca", "IBKR")


def broker_adapter_type(name: str):
    """Resolve a configured broker name to its implemented adapter.

    ``execution.primary_broker`` and ``execution.backup_broker`` were validated
    as free strings and then read by nothing, so a configuration could name a
    broker the package cannot drive and nothing would object until live
    submission.  Resolution is explicit and fails closed on an unknown name.
    """

    adapters = {"Alpaca": AlpacaBrokerAdapter, "IBKR": IBKRBrokerAdapter}
    key = str(name).strip()
    if key not in adapters:
        raise ValueError(
            "unsupported broker %r: expected one of %s"
            % (key, ", ".join(SUPPORTED_BROKERS))
        )
    return adapters[key]


def build_broker_failover_executor(execution_config, primary_client, backup_client, **changes):
    """Build a failover executor from the configured broker identities."""

    primary_type = broker_adapter_type(execution_config.primary_broker)
    backup_type = broker_adapter_type(execution_config.backup_broker)
    return BrokerFailoverExecutor(
        primary_type(primary_client),
        backup_type(backup_client),
        **changes,
    )


@dataclass(frozen=True)
class BrokerFailoverResult:
    primary_used: bool
    backup_used: bool
    detail: str


class BrokerFailoverExecutor:
    def __init__(self, primary: BrokerClient, backup: BrokerClient, min_trade_delta: float = 0.005):
        self.primary = AutoExecutor(primary, min_trade_delta=min_trade_delta)
        self.backup = AutoExecutor(backup, min_trade_delta=min_trade_delta)

    def rebalance(
        self,
        target_weights: Mapping[str, float],
        current_holdings: Mapping[str, float],
        order_type: str = "market",
        time_in_force: str = "day",
        backup_current_holdings: Optional[Mapping[str, float]] = None,
    ) -> BrokerFailoverResult:
        try:
            primary_plan = self.primary.prepare_rebalance(
                target_weights=target_weights,
                current_holdings=current_holdings,
                order_type=order_type,
                time_in_force=time_in_force,
            )
        except Exception as error:
            if backup_current_holdings is None:
                raise ValueError(
                    "backup_current_holdings_required: primary preparation "
                    "failed before submission; backup holdings must be "
                    "independently reconciled"
                ) from error
            backup_plan = self.backup.prepare_rebalance(
                target_weights=target_weights,
                current_holdings=backup_current_holdings,
                order_type=order_type,
                time_in_force=time_in_force,
            )
            self.backup.execute_rebalance(backup_plan)
            return BrokerFailoverResult(False, True, str(error))
        self.primary.execute_rebalance(primary_plan)
        return BrokerFailoverResult(True, False, "primary ok")

    def emergency_exit(self) -> BrokerFailoverResult:
        self.primary.emergency_exit()
        return BrokerFailoverResult(True, False, "primary ok")


class AlpacaBrokerAdapter:
    """Adapter for Alpaca-like REST clients.

    Unit tests pass a fake REST object; production code can pass
    alpaca_trade_api.REST or the official SDK equivalent.
    """

    def __init__(self, rest_client, clock=None):
        self.rest_client = rest_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def account_snapshot(self) -> BrokerAccountSnapshot:
        payload = self.rest_client.get_account()
        return broker_account_snapshot_from_payload(
            payload,
            broker="alpaca",
            observed_at=self.clock(),
        )

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        return self.account_snapshot()

    def get_portfolio_value(self) -> float:
        account = self.rest_client.get_account()
        return float(account.portfolio_value)

    def submit_order(
        self,
        symbol: str,
        notional: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> None:
        self.rest_client.submit_order(
            symbol=symbol,
            notional=notional,
            side=side,
            type=order_type,
            time_in_force=time_in_force,
        )

    def close_all_positions(self) -> None:
        self.rest_client.close_all_positions()


class IBKRBrokerAdapter:
    """Adapter for an IBKR client facade.

    The facade is intentionally small so production can wrap ib_insync or the
    official API while tests use a deterministic fake.
    """

    def __init__(self, ibkr_client, clock=None):
        self.ibkr_client = ibkr_client
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def account_snapshot(self) -> BrokerAccountSnapshot:
        if hasattr(self.ibkr_client, "account_snapshot"):
            payload = self.ibkr_client.account_snapshot()
        elif hasattr(self.ibkr_client, "get_account_snapshot"):
            payload = self.ibkr_client.get_account_snapshot()
        elif hasattr(self.ibkr_client, "account_summary"):
            payload = self.ibkr_client.account_summary()
        else:
            raise ValueError("IBKR client must expose broker account facts")
        if isinstance(payload, BrokerAccountSnapshot):
            return payload
        return broker_account_snapshot_from_payload(
            payload,
            broker="ibkr",
            observed_at=self.clock(),
        )

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        return self.account_snapshot()

    def get_portfolio_value(self) -> float:
        return float(self.ibkr_client.account_value())

    def submit_order(
        self,
        symbol: str,
        notional: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> None:
        del order_type, time_in_force
        self.ibkr_client.place_market_order(symbol, notional, side)

    def close_all_positions(self) -> None:
        self.ibkr_client.close_all_positions()


class OrderJournal:
    """Append-only JSONL trade log for audit and tax review."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def record(self, **record) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, sort_keys=True) + "\n")

    def read_all(self):
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]
