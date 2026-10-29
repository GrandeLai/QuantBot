"""Dependency-inversion contracts for trading runtimes."""

from datetime import datetime
from typing import Iterable, Protocol, Sequence, Union, runtime_checkable

from .domain import (
    Bar,
    BrokerAccountSnapshot,
    FillEvent,
    OrderIntent,
    OrderReconciliation,
    OrderState,
    PointInTimeSnapshot,
    PortfolioSnapshot,
    Quote,
    TargetPortfolio,
)


MarketEvent = Union[Bar, Quote, PointInTimeSnapshot]


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        ...


@runtime_checkable
class MarketDataPort(Protocol):
    def events(self) -> Iterable[MarketEvent]:
        ...


@runtime_checkable
class AccountStatePort(Protocol):
    def account_snapshot(self) -> BrokerAccountSnapshot:
        ...


@runtime_checkable
class ExecutionPort(Protocol):
    def preview_submit_order(self, intent: OrderIntent) -> OrderState:
        ...

    def submit_order(self, intent: OrderIntent) -> OrderState:
        ...

    def on_event(self, event: MarketEvent) -> Sequence[FillEvent]:
        ...

    def acknowledge_fill(self, fill: FillEvent) -> OrderState:
        ...

    def cancel_order(self, client_order_id: str) -> OrderState:
        ...

    def preview_cancel_order(self, client_order_id: str) -> OrderState:
        ...

    def query_order(self, client_order_id: str) -> OrderReconciliation:
        ...

    def resize_fill(self, fill: FillEvent, quantity: float) -> FillEvent:
        ...


@runtime_checkable
class DailyStrategy(Protocol):
    def generate_targets(
        self, snapshot: PointInTimeSnapshot
    ) -> TargetPortfolio:
        ...


@runtime_checkable
class IntradayStrategy(Protocol):
    def on_event(
        self, state: PortfolioSnapshot, event: MarketEvent
    ) -> Sequence[OrderIntent]:
        ...


@runtime_checkable
class OrderManagingStrategy(IntradayStrategy, Protocol):
    def cancel_order_ids(
        self, state: PortfolioSnapshot, event: MarketEvent
    ) -> Sequence[str]:
        ...
