"""Event-accurate backtesting for point-in-time normal trading.

Signals are observed at a session close and can only affect the following
session's open.  Positions are represented as quantities, rather than reset
weights, so overnight gaps, corporate actions, transaction costs and natural
weight drift all flow through one cash-conserving ledger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from math import fsum, isfinite
from types import MappingProxyType
from typing import Mapping, Sequence

from ..analytics import PerformanceSummary, performance_summary
from ..trading import PointInTimeSnapshot, TargetPortfolio
from .domain import NormalDrawdownPolicy
from .portfolio import NormalPortfolioAllocator


def _positive_price_mapping(values: Mapping[str, float], name: str):
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized = {}
    for security_id, raw in values.items():
        if not isinstance(security_id, str) or not security_id.strip():
            raise ValueError(f"{name} keys must be non-empty security IDs")
        value = float(raw)
        if not isfinite(value) or value <= 0:
            raise ValueError(f"{name}[{security_id}] must be finite and positive")
        normalized[security_id.strip()] = value
    return MappingProxyType(dict(sorted(normalized.items())))


def _non_negative_mapping(values: Mapping[str, float], name: str):
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized = {}
    for security_id, raw in values.items():
        if not isinstance(security_id, str) or not security_id.strip():
            raise ValueError(f"{name} keys must be non-empty security IDs")
        value = float(raw)
        if not isfinite(value) or value < 0:
            raise ValueError(f"{name}[{security_id}] must be finite and non-negative")
        normalized[security_id.strip()] = value
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(frozen=True, slots=True)
class NormalBacktestSession:
    """One complete daily session keyed by stable security ID."""

    trading_date: date
    open_prices: Mapping[str, float]
    close_prices: Mapping[str, float]
    benchmark_return: float = 0.0
    signal_snapshot: PointInTimeSnapshot | None = None
    split_ratios: Mapping[str, float] = field(default_factory=dict)
    cash_distributions: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.trading_date, date):
            raise TypeError("trading_date must be a date")
        object.__setattr__(
            self, "open_prices", _positive_price_mapping(self.open_prices, "open_prices")
        )
        object.__setattr__(
            self, "close_prices", _positive_price_mapping(self.close_prices, "close_prices")
        )
        object.__setattr__(
            self, "split_ratios", _positive_price_mapping(self.split_ratios, "split_ratios")
        )
        object.__setattr__(
            self,
            "cash_distributions",
            _non_negative_mapping(self.cash_distributions, "cash_distributions"),
        )
        benchmark_return = float(self.benchmark_return)
        if not isfinite(benchmark_return) or benchmark_return < -1:
            raise ValueError("benchmark_return must be finite and at least -1")
        object.__setattr__(self, "benchmark_return", benchmark_return)
        if self.signal_snapshot is not None:
            if not isinstance(self.signal_snapshot, PointInTimeSnapshot):
                raise TypeError("signal_snapshot must be a PointInTimeSnapshot")
            master = self.signal_snapshot.security_master
            effective_date = master.effective_date if master is not None else self.signal_snapshot.observed_at.date()
            if effective_date != self.trading_date:
                raise ValueError("signal_snapshot must belong to trading_date")


@dataclass(frozen=True, slots=True)
class NormalBacktestPoint:
    trading_date: date
    portfolio_return: float
    benchmark_return: float
    equity: float
    fees: float
    turnover: float
    pre_trade_equity: float
    traded_notional: float
    weights: Mapping[str, float]
    security_pnl: Mapping[str, float] = field(default_factory=dict)
    drawdown: float = 0.0
    exposure_scale: float = 1.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "weights", MappingProxyType(dict(sorted(self.weights.items()))))
        object.__setattr__(
            self,
            "security_pnl",
            MappingProxyType(dict(sorted(self.security_pnl.items()))),
        )


@dataclass(frozen=True, slots=True)
class NormalBacktestResult:
    points: tuple[NormalBacktestPoint, ...]
    initial_equity: float
    final_equity: float
    fees: float
    annual_turnover: float
    rebalance_count: int
    summary: PerformanceSummary
    pnl_by_security_id: Mapping[str, float]
    drawdown_scaled_rebalances: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "pnl_by_security_id",
            MappingProxyType(dict(sorted(self.pnl_by_security_id.items()))),
        )

    @property
    def daily_returns(self) -> tuple[float, ...]:
        return tuple(point.portfolio_return for point in self.points)

    @property
    def benchmark_returns(self) -> tuple[float, ...]:
        return tuple(point.benchmark_return for point in self.points)


IMPLEMENTED_EXECUTION_PRICE = "next_open"


def require_implemented_execution_price(execution_price: str) -> None:
    """Refuse an execution price this backtester does not implement.

    Signals are observed at a close and filled at the following open; nothing
    here can fill at VWAP.  The key was validated at load and then read by
    nothing, so a configuration could describe an execution model the code
    does not have.
    """

    if str(execution_price) != IMPLEMENTED_EXECUTION_PRICE:
        raise ValueError(
            "normal.execution_price=%r is not implemented; this backtester "
            "fills at the next open" % (execution_price,)
        )


class PointInTimeNormalBacktester:
    """Run a provider and allocator against a cash-conserving daily ledger."""

    def __init__(
        self,
        provider,
        allocator: NormalPortfolioAllocator | None = None,
        *,
        initial_equity: float = 100_000.0,
        cost_bps_per_side: float = 5.0,
        slippage_bps_per_side: float = 0.0,
        periods_per_year: int = 252,
        drawdown_policy: NormalDrawdownPolicy | None = None,
    ) -> None:
        if not hasattr(provider, "build_request"):
            raise TypeError("provider must expose build_request(snapshot, current_weights)")
        if allocator is not None and not hasattr(allocator, "allocate"):
            raise TypeError("allocator must expose allocate(request)")
        initial_equity = float(initial_equity)
        cost_bps_per_side = float(cost_bps_per_side)
        slippage_bps_per_side = float(slippage_bps_per_side)
        if not isfinite(initial_equity) or initial_equity <= 0:
            raise ValueError("initial_equity must be finite and positive")
        if not isfinite(cost_bps_per_side) or not 0 <= cost_bps_per_side < 10_000:
            raise ValueError("cost_bps_per_side must be in [0, 10000)")
        if not isfinite(slippage_bps_per_side) or not 0 <= slippage_bps_per_side < 10_000:
            raise ValueError("slippage_bps_per_side must be in [0, 10000)")
        if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int) or periods_per_year <= 0:
            raise ValueError("periods_per_year must be a positive integer")
        self.provider = provider
        self.allocator = allocator or NormalPortfolioAllocator()
        if drawdown_policy is not None and not isinstance(drawdown_policy, NormalDrawdownPolicy):
            raise TypeError("drawdown_policy must be a NormalDrawdownPolicy")
        self.initial_equity = initial_equity
        self.cost_bps_per_side = cost_bps_per_side
        self.slippage_bps_per_side = slippage_bps_per_side
        # Slippage is an execution cost per traded side, so it enters the same
        # rate as commissions rather than being modelled separately.
        self.cost_rate = (cost_bps_per_side + slippage_bps_per_side) / 10_000.0
        self.periods_per_year = periods_per_year
        self.drawdown_policy = drawdown_policy

    def run(self, sessions: Sequence[NormalBacktestSession]) -> NormalBacktestResult:
        sessions = tuple(sessions)
        if not sessions:
            raise ValueError("sessions must not be empty")
        if any(not isinstance(item, NormalBacktestSession) for item in sessions):
            raise TypeError("sessions must contain NormalBacktestSession values")
        dates = tuple(item.trading_date for item in sessions)
        if dates != tuple(sorted(set(dates))):
            raise ValueError("sessions must have unique ascending trading dates")

        cash = self.initial_equity
        quantities: dict[str, float] = {}
        pending: TargetPortfolio | None = None
        previous_close_equity = self.initial_equity
        previous_close_prices: dict[str, float] = {}
        total_fees = 0.0
        turnover_sum = 0.0
        rebalance_count = 0
        points = []
        aggregate_security_pnl: dict[str, float] = {}
        peak_equity = self.initial_equity
        exposure_scale = 1.0
        drawdown_scaled_rebalances = 0
        last_unscaled_target: TargetPortfolio | None = None

        for session in sessions:
            quantities_before_actions = dict(quantities)
            cash, quantities = self._apply_actions(session, cash, quantities)
            open_equity = self._marked_equity(cash, quantities, session.open_prices, "open")
            security_pnl = self._overnight_security_pnl(
                session,
                quantities_before_actions,
                quantities,
                previous_close_prices,
            )
            session_fees = 0.0
            session_turnover = 0.0
            session_traded_notional = 0.0
            if pending is not None:
                (
                    cash,
                    quantities,
                    session_fees,
                    session_turnover,
                    session_traded_notional,
                    fees_by_security,
                ) = self._rebalance(pending, open_equity, quantities, session.open_prices)
                for security_id, fee in fees_by_security.items():
                    security_pnl[security_id] = security_pnl.get(security_id, 0.0) - fee
                total_fees += session_fees
                turnover_sum += session_turnover
                rebalance_count += 1
                pending = None

            close_equity = self._marked_equity(cash, quantities, session.close_prices, "close")
            if close_equity < -1e-8:
                raise ValueError("portfolio equity became negative")
            portfolio_return = close_equity / previous_close_equity - 1.0
            close_weights = self._weights(quantities, session.close_prices, close_equity)
            for security_id, quantity in quantities.items():
                security_pnl[security_id] = security_pnl.get(security_id, 0.0) + quantity * (
                    session.close_prices[security_id] - session.open_prices[security_id]
                )
            equity_change = close_equity - previous_close_equity
            contribution_sum = fsum(security_pnl.values())
            tolerance = max(abs(equity_change), abs(contribution_sum), 1.0) * 1e-10
            if abs(equity_change - contribution_sum) > tolerance:
                raise ValueError("per-security PnL does not conserve portfolio equity")
            for security_id, value in security_pnl.items():
                aggregate_security_pnl[security_id] = (
                    aggregate_security_pnl.get(security_id, 0.0) + value
                )
            # The signal is formed at this close, so the drawdown that governs
            # it is the one observable at this close.
            peak_equity = max(peak_equity, close_equity)
            drawdown = (
                0.0 if peak_equity <= 0 else max(0.0, 1.0 - close_equity / peak_equity)
            )
            if self.drawdown_policy is not None:
                updated_scale = self.drawdown_policy.exposure_scale(
                    drawdown, exposure_scale
                )
                # De-risking runs on the risk clock, not the signal clock.  A
                # monthly-only check arrives after the drawdown has already
                # happened, so a changed scale re-sizes the standing target
                # immediately even on a session with no new signal.
                if (
                    updated_scale != exposure_scale
                    and session.signal_snapshot is None
                    and last_unscaled_target is not None
                ):
                    pending = TargetPortfolio(
                        last_unscaled_target.observed_at,
                        {
                            key: value * updated_scale
                            for key, value in last_unscaled_target.weights.items()
                        },
                    )
                    drawdown_scaled_rebalances += 1
                exposure_scale = updated_scale
            points.append(
                NormalBacktestPoint(
                    trading_date=session.trading_date,
                    portfolio_return=portfolio_return,
                    benchmark_return=session.benchmark_return,
                    equity=close_equity,
                    fees=session_fees,
                    turnover=session_turnover,
                    pre_trade_equity=open_equity,
                    traded_notional=session_traded_notional,
                    weights=close_weights,
                    security_pnl=security_pnl,
                    drawdown=drawdown,
                    exposure_scale=exposure_scale,
                )
            )
            previous_close_equity = close_equity
            previous_close_prices = {
                security_id: session.close_prices[security_id]
                for security_id in quantities
            }

            if session.signal_snapshot is not None:
                request = self.provider.build_request(session.signal_snapshot, close_weights)
                report = self.allocator.allocate(request)
                target = getattr(report, "targets", None)
                if not isinstance(target, TargetPortfolio):
                    raise TypeError("allocator report must expose TargetPortfolio targets")
                self._validated_target(target)
                last_unscaled_target = target
                if exposure_scale < 1.0:
                    target = TargetPortfolio(
                        target.observed_at,
                        {
                            key: value * exposure_scale
                            for key, value in target.weights.items()
                        },
                    )
                    drawdown_scaled_rebalances += 1
                pending = target

        years = len(points) / self.periods_per_year
        annual_turnover = turnover_sum / years if years else 0.0
        returns = tuple(point.portfolio_return for point in points)
        benchmarks = tuple(point.benchmark_return for point in points)
        return NormalBacktestResult(
            points=tuple(points),
            initial_equity=self.initial_equity,
            final_equity=previous_close_equity,
            fees=total_fees,
            annual_turnover=annual_turnover,
            rebalance_count=rebalance_count,
            summary=performance_summary(returns, benchmarks, self.periods_per_year),
            pnl_by_security_id=aggregate_security_pnl,
            drawdown_scaled_rebalances=drawdown_scaled_rebalances,
        )

    @staticmethod
    def _apply_actions(session, cash, quantities):
        adjusted = dict(quantities)
        for security_id, ratio in session.split_ratios.items():
            if security_id in adjusted:
                adjusted[security_id] *= ratio
        for security_id, amount in session.cash_distributions.items():
            cash += adjusted.get(security_id, 0.0) * amount
        return cash, adjusted

    @staticmethod
    def _marked_equity(cash, quantities, prices, label):
        missing = sorted(set(quantities).difference(prices))
        if missing:
            raise ValueError(f"missing {label} price for held securities: {','.join(missing)}")
        return cash + fsum(quantities[key] * prices[key] for key in quantities)

    def _rebalance(self, target, equity, quantities, open_prices):
        weights = dict(target.weights)
        missing = sorted(set(weights).difference(open_prices))
        if missing:
            raise ValueError("missing open price for target securities: " + ",".join(missing))
        current_notional = {
            key: quantities[key] * open_prices[key]
            for key in quantities
        }
        base = self._affordable_target_base(equity, weights, current_notional)
        target_notional = {key: base * value for key, value in weights.items() if value > 0}
        traded_notional = fsum(
            abs(target_notional.get(key, 0.0) - current_notional.get(key, 0.0))
            for key in set(target_notional).union(current_notional)
        )
        fees = traded_notional * self.cost_rate
        fees_by_security = {
            key: abs(target_notional.get(key, 0.0) - current_notional.get(key, 0.0))
            * self.cost_rate
            for key in set(target_notional).union(current_notional)
        }
        cash = equity - fsum(target_notional.values()) - fees
        tolerance = max(equity, 1.0) * 1e-12
        if cash < -tolerance:
            raise ValueError("rebalance cannot conserve non-negative cash")
        cash = max(cash, 0.0)
        next_quantities = {
            key: notional / open_prices[key]
            for key, notional in target_notional.items()
            if notional > 0
        }
        turnover = 0.5 * traded_notional / equity if equity > 0 else 0.0
        return (
            cash,
            next_quantities,
            fees,
            turnover,
            traded_notional,
            fees_by_security,
        )

    @staticmethod
    def _overnight_security_pnl(
        session,
        quantities_before_actions,
        adjusted_quantities,
        previous_close_prices,
    ):
        result = {}
        for security_id, old_quantity in quantities_before_actions.items():
            if security_id not in previous_close_prices:
                raise ValueError(
                    "missing previous close price for held security: " + security_id
                )
            ratio = session.split_ratios.get(security_id, 1.0)
            adjusted_previous_close = previous_close_prices[security_id] / ratio
            quantity = adjusted_quantities[security_id]
            result[security_id] = quantity * (
                session.open_prices[security_id] - adjusted_previous_close
            ) + quantity * session.cash_distributions.get(security_id, 0.0)
        return result

    def _affordable_target_base(self, equity, weights, current_notional):
        gross = fsum(weights.values())

        def remaining_cash(base):
            traded = fsum(
                abs(base * weights.get(key, 0.0) - current_notional.get(key, 0.0))
                for key in set(weights).union(current_notional)
            )
            return equity - base * gross - self.cost_rate * traded

        if remaining_cash(equity) >= 0:
            return equity
        low, high = 0.0, equity
        for _ in range(80):
            midpoint = (low + high) / 2.0
            if remaining_cash(midpoint) >= 0:
                low = midpoint
            else:
                high = midpoint
        return low

    @staticmethod
    def _weights(quantities, prices, equity):
        if equity <= 0:
            return {}
        return {
            key: quantities[key] * prices[key] / equity
            for key in quantities
            if quantities[key] > 0
        }

    @staticmethod
    def _validated_target(target):
        weights = tuple(float(value) for value in target.weights.values())
        if any(not isfinite(value) or value < 0 for value in weights):
            raise ValueError("normal targets must be finite and long-only")
        if fsum(weights) > 1.0 + 1e-12:
            raise ValueError("normal targets cannot exceed 100% gross exposure")


__all__ = (
    "NormalBacktestPoint",
    "NormalBacktestResult",
    "NormalBacktestSession",
    "PointInTimeNormalBacktester",
)
