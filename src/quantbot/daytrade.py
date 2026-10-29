"""Intraday day-trading strategy, backtest, journals, and evidence."""

import hashlib
import hmac
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta
from itertools import groupby
from math import isfinite, isinf
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .audit import RequirementEvidence
from .experiment_registry import ExperimentRegistry
from .daytrade_validation_artifact import DayTradeAdaptiveValidationArtifact
from .historical_trials import (
    HISTORICAL_TRIAL_ARTIFACT_VERSION,
    validate_historical_trial_execution_proofs,
)
from .daytrade_strategy import (
    DayTradeHypothesisConfig,
    DayTradeRiskSizingConfig,
    DailyUniverseSnapshot,
    IntradayFeatureBook,
    IntradayFeatureRequirements,
    IntradayFeatureSnapshot,
    PortfolioHeatState,
    evaluate_entry_filters,
    preregistered_daytrade_experiments,
    preregistered_daytrade_followup_experiments,
    preregistered_daytrade_h3_experiments,
    preregistered_daytrade_h6_experiments,
    preregistered_daytrade_h7_experiments,
    preregistered_daytrade_h8_experiments,
    preregistered_daytrade_h9_experiments,
    risk_scaled_stop,
    session_cumulative_vwap,
    size_intraday_position,
    update_vwap_hysteresis,
)
from .statistical_validation import (
    cscv_partition,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)
from .trading import BrokerAccountRiskGate, BrokerAccountSnapshot, MarginStatus


DAYTRADE_STRATEGY_NAME = "daytrade_intraday"
EASTERN = ZoneInfo("US/Eastern")
MAX_DAYTRADE_SYMBOLS = 10
DAYTRADE_MINUTE_CACHE_SCHEMA_VERSION = 2
DEFAULT_DAYTRADE_SYMBOLS = (
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
DayTradeUniverseSchedule = (
    Sequence[DailyUniverseSnapshot]
    | Mapping[date, DailyUniverseSnapshot]
)


@dataclass(frozen=True)
class DayTradeConfig:
    strategy_name: str = DAYTRADE_STRATEGY_NAME
    account_equity: float = 100_000.0
    capital_fraction: float = 0.10
    per_trade_risk_fraction: float = 0.0035
    daily_loss_limit_fraction: float = 0.01
    max_trades_per_day: int = 6
    max_concurrent_positions: int = 3
    max_losing_trades_per_day: int = 0
    max_consecutive_losing_trades_per_day: int = 0
    max_symbols: int = MAX_DAYTRADE_SYMBOLS
    symbol_filter: str = "none"
    max_active_symbols: int = MAX_DAYTRADE_SYMBOLS
    min_opening_range_pct: float = 0.0
    min_opening_dollar_volume: float = 0.0
    require_positive_opening_momentum: bool = False
    open_range_start: str = "09:30"
    open_range_end: str = "09:45"
    entry_start: str = "09:45"
    entry_end: str = "15:50"
    force_exit: str = "15:50"
    stop_loss_pct: float = 0.006
    reward_risk: float = 1.5
    breakeven_after_r: float = 0.0
    trailing_stop_after_r: float = 0.0
    max_holding_minutes: float = 0.0
    vwap_exit_bars: int = 3
    cumulative_vwap: bool = True
    vwap_hysteresis_bps: float = 5.0
    volume_multiplier: float = 1.0
    min_relative_volume: float = 0.0
    stop_basis: str = "fixed_pct"
    atr_stop_multiple: float = 0.75
    opening_range_stop_multiple: float = 0.75
    max_portfolio_heat_fraction: float = 0.02
    correlation_penalty: float = 0.50
    dynamic_risk_sizing: bool = False
    min_directional_spy_return: float = -1.0
    min_directional_qqq_return: float = -1.0
    min_directional_sector_strength: float = -1.0
    cost_bps: float = 0.0
    slippage_bps: float = 0.0
    max_volume_participation: float = 1.0
    max_daytrade_drawdown: float = 0.05
    # ``max_daytrade_drawdown`` was carried into this config and read by
    # nothing.  Enforcement is opt-in so frozen H3/H9 results stay reproducible;
    # enabling it stops new entries once the sleeve is down by that fraction.
    enforce_drawdown_halt: bool = False
    min_soak_sessions: int = 20

    @classmethod
    def from_runtime(cls, daytrade, portfolio, risk, execution=None):
        """Translate canonical TOML sections into execution configuration."""

        return cls(
            slippage_bps=(
                0.0 if execution is None else float(execution.slippage_bps_per_side)
            ),
            account_equity=portfolio.initial_cash,
            capital_fraction=portfolio.daytrade_fraction,
            entry_start=daytrade.entry_start,
            entry_end=daytrade.entry_end,
            cumulative_vwap=daytrade.cumulative_vwap,
            vwap_hysteresis_bps=daytrade.vwap_hysteresis_bps,
            stop_basis=daytrade.stop_basis,
            atr_stop_multiple=daytrade.atr_stop_multiple,
            opening_range_stop_multiple=daytrade.opening_range_stop_multiple,
            cost_bps=daytrade.cost_bps_per_side,
            max_volume_participation=daytrade.max_volume_participation,
            max_portfolio_heat_fraction=daytrade.max_portfolio_heat_fraction,
            correlation_penalty=daytrade.correlation_penalty,
            min_relative_volume=daytrade.min_relative_volume,
            min_directional_spy_return=daytrade.min_directional_spy_return,
            min_directional_qqq_return=daytrade.min_directional_qqq_return,
            min_directional_sector_strength=daytrade.min_directional_sector_strength,
            max_daytrade_drawdown=risk.max_daytrade_drawdown,
            enforce_drawdown_halt=daytrade.enforce_drawdown_halt,
        )

    @property
    def daytrade_capital(self) -> float:
        return float(self.account_equity) * float(self.capital_fraction)

    @property
    def per_trade_risk(self) -> float:
        return self.daytrade_capital * float(self.per_trade_risk_fraction)

    @property
    def daily_loss_limit(self) -> float:
        return self.daytrade_capital * float(self.daily_loss_limit_fraction)


@dataclass(frozen=True)
class MinuteBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None


@dataclass(frozen=True)
class DayTradeSignal:
    strategy: str
    symbol: str
    timestamp: datetime
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    notional: float
    reason: str = "opening_range_breakout_vwap"
    side: str = "long"
    annualized_borrow_rate: float = 0.0
    security_id: str = ""
    entry_event_price: float = 0.0


@dataclass(frozen=True)
class DayTradeTrade:
    strategy: str
    symbol: str
    day: date
    entry_timestamp: datetime
    exit_timestamp: datetime
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    return_pct: float
    exit_reason: str
    notional: float
    holding_minutes: float
    borrow_cost: float = 0.0
    security_id: str = ""
    entry_event_price: float = 0.0
    exit_event_price: float = 0.0


@dataclass(frozen=True)
class DayTradeDailyResult:
    strategy: str
    day: date
    trades: int
    pnl: float
    return_pct: float
    ending_equity: float
    errors: int = 0
    stopped_reason: str = ""


@dataclass(frozen=True)
class DayTradeBacktestResult:
    strategy: str
    trades: List[DayTradeTrade]
    daily_results: List[DayTradeDailyResult]
    initial_capital: float
    ending_equity: float
    cumulative_return: float
    annual_return: float
    sharpe: float
    max_drawdown: float
    win_rate: float
    profit_factor: float
    average_holding_minutes: float
    trade_count: int

    @property
    def pnl_by_security_id(self) -> Dict[str, dict]:
        """Aggregate renamed/reused tickers by stable security identity."""

        return _pnl_by_security_id(self.trades)


@dataclass(frozen=True)
class _OpeningRange:
    high: float
    low: float
    average_volume: float
    first_open: float
    last_close: float
    average_dollar_volume: float


@dataclass(frozen=True)
class _OpenPosition:
    signal: DayTradeSignal
    current_stop: float
    high_water: float
    initial_risk: float


@dataclass(frozen=True)
class _PendingEntry:
    signal: DayTradeSignal
    opening_range: _OpeningRange
    context: Optional[IntradayFeatureSnapshot] = None


@dataclass(frozen=True)
class _EntryCandidate:
    score: float
    signal: DayTradeSignal
    opening_range: _OpeningRange
    context: Optional[IntradayFeatureSnapshot] = None


@dataclass
class _DayState:
    open_positions: Dict[str, _OpenPosition] = field(default_factory=dict)
    pending_entries: Dict[str, _PendingEntry] = field(default_factory=dict)
    traded_symbols: set = field(default_factory=set)
    below_vwap_counts: Dict[str, int] = field(default_factory=dict)
    last_bar_by_symbol: Dict[str, MinuteBar] = field(default_factory=dict)
    trades: List[DayTradeTrade] = field(default_factory=list)
    realized_pnl: float = 0.0
    trades_started: int = 0
    losing_trades: int = 0
    consecutive_losing_trades: int = 0
    stopped_reason: str = ""


@dataclass(frozen=True)
class DayTradeWalkForwardWindow:
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    purge_days: int = 1
    embargo_days: int = 1


@dataclass(frozen=True)
class DayTradeOptimizationResult:
    best_config: DayTradeConfig
    best_config_name: str
    baseline: DayTradeBacktestResult
    validation: DayTradeBacktestResult
    rankings: List[dict]
    windows: List[dict]
    fragile: bool
    declared_trial_count: int = 0
    completed_trial_count: int = 0
    executed_trial_ids: Tuple[str, ...] = ()
    feature_coverage_complete: bool = False
    statistical_validation: dict = field(default_factory=dict)


class DayTradeRiskGate:
    def __init__(self, config: DayTradeConfig = DayTradeConfig()):
        self.config = config
        self.broker_risk_gate = BrokerAccountRiskGate()

    def can_open_new_trade(
        self,
        day: date,
        trades_started_today: int,
        open_positions: int,
        realized_pnl: float,
        broker_account: BrokerAccountSnapshot,
        requested_notional: float,
        side: str = "long",
    ) -> Tuple[bool, List[str]]:
        del day
        reasons: List[str] = []
        if trades_started_today >= self.config.max_trades_per_day:
            reasons.append("max_trades_per_day")
        if open_positions >= self.config.max_concurrent_positions:
            reasons.append("max_concurrent_positions")
        if realized_pnl <= -self.config.daily_loss_limit:
            reasons.append("daily_loss_limit")
        broker_decision = self.broker_risk_gate.evaluate(
            broker_account,
            requested_notional=requested_notional,
            side="short" if side == "short" else "buy",
            day_trade=True,
        )
        reasons.extend(broker_decision.reasons)
        return not reasons, reasons


class OpeningRangeBreakoutStrategy:
    def __init__(self, config: DayTradeConfig = DayTradeConfig()):
        self.config = config

    def opening_ranges(self, bars: Sequence[MinuteBar]) -> Dict[Tuple[date, str], _OpeningRange]:
        grouped: Dict[Tuple[date, str], List[MinuteBar]] = {}
        start = _parse_time(self.config.open_range_start)
        end = _parse_time(self.config.open_range_end)
        for bar in bars:
            local_time = _local_time(bar.timestamp)
            if start <= local_time < end:
                grouped.setdefault((_local_day(bar.timestamp), bar.symbol.upper()), []).append(bar)

        ranges: Dict[Tuple[date, str], _OpeningRange] = {}
        for key, items in grouped.items():
            if not items:
                continue
            ordered = sorted(items, key=lambda item: item.timestamp)
            ranges[key] = _OpeningRange(
                high=max(float(item.high) for item in items),
                low=min(float(item.low) for item in items),
                average_volume=sum(float(item.volume) for item in items) / len(items),
                first_open=float(ordered[0].open),
                last_close=float(ordered[-1].close),
                average_dollar_volume=sum(float(item.volume) * float(item.close) for item in items) / len(items),
            )
        return ranges

    def entry_signal(
        self,
        bar: MinuteBar,
        opening_range: _OpeningRange,
        effective_vwap: Optional[float] = None,
        side: str = "long",
        atr: Optional[float] = None,
    ) -> Optional[DayTradeSignal]:
        if effective_vwap is None:
            effective_vwap = _effective_vwap(bar)
        else:
            effective_vwap = float(effective_vwap)
        is_long = side == "long"
        if side not in {"long", "short"}:
            raise ValueError("side must be long or short")
        if is_long and (
            float(bar.close) <= opening_range.high
            or float(bar.close) <= effective_vwap
        ):
            return None
        if not is_long and (
            float(bar.close) >= opening_range.low
            or float(bar.close) >= effective_vwap
        ):
            return None
        if float(bar.volume) < opening_range.average_volume * self.config.volume_multiplier:
            return None

        entry = float(bar.close)
        stop = _entry_stop(self.config, entry, opening_range, side, atr)
        if (is_long and stop >= entry) or (not is_long and stop <= entry):
            return None
        per_share_risk = abs(entry - stop)
        risk_quantity = self.config.per_trade_risk / per_share_risk
        notional_cap = self.config.daytrade_capital / max(1, self.config.max_concurrent_positions)
        notional_quantity = notional_cap / entry
        quantity = min(risk_quantity, notional_quantity)
        if quantity <= 0:
            return None
        direction = 1.0 if is_long else -1.0
        target = entry + direction * per_share_risk * self.config.reward_risk
        return DayTradeSignal(
            strategy=self.config.strategy_name,
            symbol=bar.symbol.upper(),
            timestamp=bar.timestamp,
            entry_price=entry,
            stop_price=stop,
            target_price=target,
            quantity=quantity,
            notional=quantity * entry,
            side=side,
        )


class DayTradeBacktester:
    def __init__(
        self,
        config: DayTradeConfig = DayTradeConfig(),
        *,
        hypothesis_config: Optional[DayTradeHypothesisConfig] = None,
    ):
        if config.cost_bps < 0:
            raise ValueError("cost_bps must be non-negative")
        if config.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if not 0.0 <= config.max_volume_participation <= 1.0:
            raise ValueError("max_volume_participation must be between 0 and 1")
        if config.stop_basis not in {"fixed_pct", "atr", "opening_range"}:
            raise ValueError("stop_basis must be fixed_pct, atr, or opening_range")
        if hypothesis_config is not None and not isinstance(
            hypothesis_config, DayTradeHypothesisConfig
        ):
            raise TypeError("hypothesis_config must be a DayTradeHypothesisConfig")
        self.config = config
        self.hypothesis_config = hypothesis_config
        self.strategy = OpeningRangeBreakoutStrategy(config)

    def run(
        self,
        bars: Sequence[MinuteBar],
        *,
        feature_snapshots: Sequence[IntradayFeatureSnapshot] = (),
        universe_schedule: Optional[DayTradeUniverseSchedule] = None,
    ) -> DayTradeBacktestResult:
        clean_bars = sorted(
            [
                _normalize_minute_bar(bar)
                for bar in bars
                if bar.close is not None and float(bar.close) > 0
            ],
            key=lambda item: (item.timestamp, item.symbol),
        )
        opening_ranges = self.strategy.opening_ranges(clean_bars)
        cumulative_vwap = (
            session_cumulative_vwap(clean_bars)
            if self.config.cumulative_vwap
            else {}
        )
        feature_book = IntradayFeatureBook(feature_snapshots)
        bars_by_day: Dict[date, List[MinuteBar]] = {}
        for bar in clean_bars:
            bars_by_day.setdefault(_local_day(bar.timestamp), []).append(bar)
        universe_by_day = _normalize_universe_schedule(universe_schedule)
        _require_universe_schedule_coverage(universe_by_day, bars_by_day)

        all_trades: List[DayTradeTrade] = []
        daily_results: List[DayTradeDailyResult] = []
        risk_gate = DayTradeRiskGate(self.config)
        equity = self.config.daytrade_capital
        peak_equity = equity

        for trading_day in sorted(bars_by_day):
            # The halt is decided from equity observable before the session
            # opens, so it can only remove entries that had not happened yet.
            halted = self._drawdown_halted(equity, peak_equity)
            day_trades, daily = self._run_day(
                trading_day=trading_day,
                bars=bars_by_day[trading_day],
                opening_ranges=opening_ranges,
                risk_gate=risk_gate,
                starting_equity=equity,
                drawdown_halted=halted,
                session_vwap_by_event=cumulative_vwap,
                feature_book=feature_book,
                universe_snapshot=(
                    universe_by_day[trading_day]
                    if universe_by_day is not None
                    else None
                ),
            )
            all_trades.extend(day_trades)
            daily_results.append(daily)
            equity = daily.ending_equity
            peak_equity = max(peak_equity, equity)

        return _build_backtest_result(
            strategy=self.config.strategy_name,
            trades=all_trades,
            daily_results=daily_results,
            initial_capital=self.config.daytrade_capital,
        )

    def _drawdown_halted(self, equity: float, peak_equity: float) -> bool:
        if not self.config.enforce_drawdown_halt:
            return False
        limit = float(self.config.max_daytrade_drawdown)
        if limit <= 0 or peak_equity <= 0:
            return False
        return (1.0 - float(equity) / float(peak_equity)) >= limit

    def _run_day(
        self,
        trading_day: date,
        bars: Sequence[MinuteBar],
        opening_ranges: Dict[Tuple[date, str], _OpeningRange],
        risk_gate: DayTradeRiskGate,
        starting_equity: float,
        session_vwap_by_event: Optional[Dict[Tuple[str, datetime], float]] = None,
        feature_book: Optional[IntradayFeatureBook] = None,
        universe_snapshot: Optional[DailyUniverseSnapshot] = None,
        drawdown_halted: bool = False,
    ) -> Tuple[List[DayTradeTrade], DayTradeDailyResult]:
        state = _DayState()
        if drawdown_halted:
            state.stopped_reason = "max_daytrade_drawdown"
        entry_start = _parse_time(self.config.entry_start)
        last_entry_time = min(
            _parse_time(self.config.entry_end),
            _parse_time(self.config.force_exit),
        )
        active_symbols = self._active_symbols_for_day(
            trading_day,
            bars,
            opening_ranges,
            universe_snapshot=universe_snapshot,
        )
        security_id_by_symbol = (
            universe_snapshot.security_id_by_ticker
            if universe_snapshot is not None
            else {}
        )
        session_vwap_by_event = self._session_vwap_input(
            bars,
            session_vwap_by_event,
        )
        feature_book = feature_book or IntradayFeatureBook()
        ordered_bars = sorted(bars, key=lambda item: (item.timestamp, item.symbol))
        for _timestamp, timestamp_items in groupby(
            ordered_bars, key=lambda item: item.timestamp
        ):
            timestamp_bars = list(timestamp_items)
            self._fill_pending_entries(state, timestamp_bars, last_entry_time)
            self._process_exits(state, timestamp_bars, session_vwap_by_event)
            candidates = self._collect_entry_candidates(
                state=state,
                bars=timestamp_bars,
                trading_day=trading_day,
                opening_ranges=opening_ranges,
                active_symbols=active_symbols,
                entry_start=entry_start,
                last_entry_time=last_entry_time,
                session_vwap_by_event=session_vwap_by_event,
                feature_book=feature_book,
                security_id_by_symbol=security_id_by_symbol,
            )
            self._accept_candidates(
                state=state,
                candidates=candidates,
                trading_day=trading_day,
                risk_gate=risk_gate,
                starting_equity=starting_equity,
            )
        self._force_close_positions(state)
        daily = self._build_daily_result(
            state,
            trading_day=trading_day,
            starting_equity=starting_equity,
        )
        return list(state.trades), daily

    def _session_vwap_input(
        self,
        bars: Sequence[MinuteBar],
        supplied: Optional[Dict[Tuple[str, datetime], float]],
    ) -> Dict[Tuple[str, datetime], float]:
        if supplied is not None:
            return supplied
        if not self.config.cumulative_vwap:
            return {}
        return session_cumulative_vwap(bars)

    def _fill_pending_entries(
        self,
        state: _DayState,
        bars: Sequence[MinuteBar],
        last_entry_time: time,
    ) -> None:
        for bar in bars:
            symbol = bar.symbol.upper()
            state.last_bar_by_symbol[symbol] = bar
            pending = state.pending_entries.get(symbol)
            if pending is None or bar.timestamp <= pending.signal.timestamp:
                continue
            state.pending_entries.pop(symbol, None)
            if state.stopped_reason or _local_time(bar.timestamp) >= last_entry_time:
                continue
            position = self._fill_pending_entry(pending, bar)
            if position is None:
                continue
            state.open_positions[symbol] = position
            state.below_vwap_counts[symbol] = 0

    def _process_exits(
        self,
        state: _DayState,
        bars: Sequence[MinuteBar],
        session_vwap_by_event: Dict[Tuple[str, datetime], float],
    ) -> None:
        for bar in bars:
            symbol = bar.symbol.upper()
            position = state.open_positions.get(symbol)
            if position is None:
                continue
            position, reason, exit_event_price, exit_price = self._exit_for_bar(
                position,
                bar,
                state.below_vwap_counts,
                _selected_vwap(bar, session_vwap_by_event),
            )
            if exit_price is None:
                state.open_positions[symbol] = position
                continue
            state.open_positions.pop(symbol, None)
            state.below_vwap_counts.pop(symbol, None)
            self._record_closed_trade(
                state,
                _close_trade(
                    position.signal,
                    bar,
                    exit_price,
                    reason,
                    exit_event_price=exit_event_price,
                ),
            )

    def _record_closed_trade(
        self,
        state: _DayState,
        trade: DayTradeTrade,
    ) -> None:
        state.trades.append(trade)
        state.traded_symbols.add(trade.symbol.upper())
        state.realized_pnl += trade.pnl
        if trade.pnl < 0:
            state.losing_trades += 1
            state.consecutive_losing_trades += 1
        else:
            state.consecutive_losing_trades = 0
        if not state.stopped_reason and state.realized_pnl <= -self.config.daily_loss_limit:
            state.stopped_reason = "daily_loss_limit"
        circuit_reason = self._loss_circuit_breaker_reason(
            state.losing_trades,
            state.consecutive_losing_trades,
        )
        if not state.stopped_reason and circuit_reason:
            state.stopped_reason = circuit_reason

    def _collect_entry_candidates(
        self,
        *,
        state: _DayState,
        bars: Sequence[MinuteBar],
        trading_day: date,
        opening_ranges: Dict[Tuple[date, str], _OpeningRange],
        active_symbols: set,
        entry_start: time,
        last_entry_time: time,
        session_vwap_by_event: Dict[Tuple[str, datetime], float],
        feature_book: IntradayFeatureBook,
        security_id_by_symbol: Mapping[str, str],
    ) -> List[_EntryCandidate]:
        candidates: List[_EntryCandidate] = []
        for bar in bars:
            symbol = bar.symbol.upper()
            if state.stopped_reason or symbol not in active_symbols:
                continue
            if symbol in state.open_positions or symbol in state.pending_entries:
                continue
            if symbol in state.traded_symbols:
                continue
            local_time = _local_time(bar.timestamp)
            if not entry_start <= local_time < last_entry_time:
                continue
            opening_range = opening_ranges.get((trading_day, symbol))
            if opening_range is None:
                continue
            candidate = self._entry_candidate(
                bar,
                opening_range,
                _selected_vwap(bar, session_vwap_by_event),
                feature_book,
                security_id=security_id_by_symbol.get(symbol, ""),
            )
            if candidate is not None:
                candidates.append(candidate)
        return candidates

    def _accept_candidates(
        self,
        *,
        state: _DayState,
        candidates: Sequence[_EntryCandidate],
        trading_day: date,
        risk_gate: DayTradeRiskGate,
        starting_equity: float,
    ) -> None:
        simulated_equity = self._current_simulated_equity(state, starting_equity)
        settled_equity = max(0.0, starting_equity + state.realized_pnl)
        available_power = max(
            0.0,
            min(simulated_equity, settled_equity) - self._committed_notional(state),
        )
        for candidate in sorted(candidates, key=lambda item: (-item.score, item.signal.symbol)):
            accepted = self._candidate_with_current_size(
                candidate,
                state=state,
                starting_equity=starting_equity,
            )
            if accepted is None:
                continue
            broker_account = self._simulated_broker_account(
                accepted.signal.timestamp,
                simulated_equity,
                available_power,
            )
            gate_args = dict(
                day=trading_day,
                trades_started_today=state.trades_started,
                open_positions=len(state.open_positions) + len(state.pending_entries),
                realized_pnl=state.realized_pnl,
                broker_account=broker_account,
                requested_notional=accepted.signal.notional,
            )
            if accepted.signal.side == "short":
                gate_args["side"] = "short"
            allowed, _reasons = risk_gate.can_open_new_trade(**gate_args)
            if not allowed:
                continue
            symbol = accepted.signal.symbol.upper()
            state.pending_entries[symbol] = _PendingEntry(
                signal=accepted.signal,
                opening_range=accepted.opening_range,
                context=accepted.context,
            )
            state.traded_symbols.add(symbol)
            state.trades_started += 1
            available_power = max(0.0, available_power - accepted.signal.notional)

    def _candidate_with_current_size(
        self,
        candidate: _EntryCandidate,
        *,
        state: _DayState,
        starting_equity: float,
    ) -> Optional[_EntryCandidate]:
        if not self.config.dynamic_risk_sizing:
            return candidate
        return self._resize_candidate(
            candidate,
            starting_equity=starting_equity,
            realized_pnl=state.realized_pnl,
            open_positions=state.open_positions,
            pending_entries=state.pending_entries,
            last_bar_by_symbol=state.last_bar_by_symbol,
        )

    @staticmethod
    def _current_simulated_equity(
        state: _DayState,
        starting_equity: float,
    ) -> float:
        unrealized_pnl = sum(
            _unrealized_position_pnl(
                position,
                state.last_bar_by_symbol.get(symbol),
            )
            for symbol, position in state.open_positions.items()
        )
        return max(0.0, starting_equity + state.realized_pnl + unrealized_pnl)

    @staticmethod
    def _committed_notional(state: _DayState) -> float:
        return sum(
            position.signal.notional for position in state.open_positions.values()
        ) + sum(pending.signal.notional for pending in state.pending_entries.values())

    @staticmethod
    def _simulated_broker_account(
        observed_at: datetime,
        equity: float,
        available_power: float,
    ) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            broker="simulated",
            observed_at=observed_at,
            equity=equity,
            buying_power=available_power,
            day_trade_buying_power=available_power,
            trading_blocked=False,
            shorting_enabled=True,
            margin_status=MarginStatus.SIMULATED,
        )

    def _force_close_positions(self, state: _DayState) -> None:
        for symbol, position in list(state.open_positions.items()):
            bar = state.last_bar_by_symbol.get(symbol)
            if bar is None:
                continue
            exit_price = self._exit_fill_price(position.signal.side, float(bar.close))
            trade = _close_trade(
                position.signal,
                bar,
                exit_price,
                "end_of_day_forced_exit",
                exit_event_price=float(bar.close),
            )
            state.trades.append(trade)
            state.realized_pnl += trade.pnl
            state.open_positions.pop(symbol, None)

    def _build_daily_result(
        self,
        state: _DayState,
        *,
        trading_day: date,
        starting_equity: float,
    ) -> DayTradeDailyResult:
        ending_equity = starting_equity + state.realized_pnl
        return DayTradeDailyResult(
            strategy=self.config.strategy_name,
            day=trading_day,
            trades=len(state.trades),
            pnl=state.realized_pnl,
            return_pct=(state.realized_pnl / starting_equity if starting_equity else 0.0),
            ending_equity=ending_equity,
            errors=0,
            stopped_reason=state.stopped_reason,
        )

    def _entry_candidate(
        self,
        bar: MinuteBar,
        opening_range: _OpeningRange,
        effective_vwap: float,
        feature_book: IntradayFeatureBook,
        security_id: str = "",
    ) -> Optional[_EntryCandidate]:
        context = feature_book.latest(bar.symbol, bar.timestamp)
        side = "long"
        if self.hypothesis_config is not None:
            if context is None:
                return None
            decision = evaluate_entry_filters(
                context=context,
                config=self.hypothesis_config,
                signal_at=bar.timestamp,
                opening_range_width=opening_range.high - opening_range.low,
                proposed_price=float(bar.close),
            )
            if not decision.allowed:
                return None
            side = self.hypothesis_config.side
        if self.config.stop_basis != "fixed_pct" and context is None:
            return None
        if self.hypothesis_config is None and self.config.stop_basis == "fixed_pct":
            signal_bar = replace(bar, vwap=effective_vwap)
            signal = self.strategy.entry_signal(signal_bar, opening_range)
        else:
            signal = self.strategy.entry_signal(
                bar,
                opening_range,
                effective_vwap,
                side,
                context.atr if context is not None else None,
            )
        if signal is None:
            return None
        if security_id:
            signal = replace(signal, security_id=security_id)
        if signal.side == "short" and context is not None:
            signal = replace(
                signal,
                annualized_borrow_rate=context.annualized_borrow_rate,
            )
        return _EntryCandidate(
            score=_opening_candidate_score(opening_range),
            signal=signal,
            opening_range=opening_range,
            context=context,
        )

    def _resize_candidate(
        self,
        candidate: _EntryCandidate,
        *,
        starting_equity: float,
        realized_pnl: float,
        open_positions: Dict[str, _OpenPosition],
        pending_entries: Dict[str, _PendingEntry],
        last_bar_by_symbol: Dict[str, MinuteBar],
    ) -> Optional[_EntryCandidate]:
        unrealized_pnl = sum(
            _unrealized_position_pnl(
                position,
                last_bar_by_symbol.get(symbol),
            )
            for symbol, position in open_positions.items()
        )
        open_risk = sum(
            _signal_open_risk(position.signal)
            for position in open_positions.values()
        ) + sum(
            _signal_open_risk(pending.signal)
            for pending in pending_entries.values()
        )
        state = PortfolioHeatState(
            settled_equity=starting_equity,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            open_risk=open_risk,
        )
        sizing = size_intraday_position(
            entry_price=candidate.signal.entry_price,
            stop_price=candidate.signal.stop_price,
            state=state,
            config=DayTradeRiskSizingConfig(
                per_trade_risk_fraction=self.config.per_trade_risk_fraction,
                max_portfolio_heat_fraction=self.config.max_portfolio_heat_fraction,
                correlation_penalty=self.config.correlation_penalty,
                max_position_notional_fraction=(
                    1.0 / max(1, self.config.max_concurrent_positions)
                ),
            ),
            max_abs_correlation=(
                candidate.context.max_abs_correlation
                if candidate.context is not None
                else 0.0
            ),
        )
        quantity = min(candidate.signal.quantity, sizing.quantity)
        if quantity <= 0:
            return None
        signal = replace(
            candidate.signal,
            quantity=quantity,
            notional=quantity * candidate.signal.entry_price,
        )
        return replace(candidate, signal=signal)

    def _fill_pending_entry(
        self,
        pending: _PendingEntry,
        bar: MinuteBar,
    ) -> Optional[_OpenPosition]:
        raw_entry = float(bar.open)
        if raw_entry <= 0:
            return None
        side = pending.signal.side
        entry_price = self._entry_fill_price(side, raw_entry)
        if entry_price <= 0:
            return None
        stop = _entry_stop(
            self.config,
            entry_price,
            pending.opening_range,
            side,
            pending.context.atr if pending.context is not None else None,
        )
        if (side == "long" and stop >= entry_price) or (
            side == "short" and stop <= entry_price
        ):
            return None
        per_share_risk = abs(entry_price - stop)
        if per_share_risk <= 0:
            return None
        risk_quantity = self.config.per_trade_risk / per_share_risk
        notional_cap = self.config.daytrade_capital / max(1, self.config.max_concurrent_positions)
        notional_quantity = notional_cap / entry_price
        reserved_quantity = pending.signal.notional / entry_price
        volume_quantity = max(0.0, float(bar.volume)) * self.config.max_volume_participation
        quantity = min(
            risk_quantity,
            notional_quantity,
            reserved_quantity,
            volume_quantity,
        )
        if quantity <= 0:
            return None
        direction = 1.0 if side == "long" else -1.0
        signal = DayTradeSignal(
            strategy=pending.signal.strategy,
            symbol=pending.signal.symbol,
            timestamp=bar.timestamp,
            entry_price=entry_price,
            stop_price=stop,
            target_price=(
                entry_price + direction * per_share_risk * self.config.reward_risk
            ),
            quantity=quantity,
            notional=quantity * entry_price,
            reason=pending.signal.reason,
            side=side,
            annualized_borrow_rate=pending.signal.annualized_borrow_rate,
            security_id=pending.signal.security_id,
            entry_event_price=raw_entry,
        )
        return _open_position_from_signal(signal)

    @property
    def _cost_rate(self) -> float:
        """Commissions and slippage are both per-side execution costs."""

        return (self.config.cost_bps + self.config.slippage_bps) / 10_000.0

    def _entry_fill_price(self, side: str, raw_price: float) -> float:
        rate = self._cost_rate
        multiplier = 1.0 - rate if side == "short" else 1.0 + rate
        return max(0.0, float(raw_price) * multiplier)

    def _exit_fill_price(self, side: str, raw_price: float) -> float:
        rate = self._cost_rate
        multiplier = 1.0 + rate if side == "short" else 1.0 - rate
        return max(0.0, float(raw_price) * multiplier)

    def _sell_fill_price(self, raw_price: float) -> float:
        return self._exit_fill_price("long", raw_price)

    def _loss_circuit_breaker_reason(
        self,
        losing_trades: int,
        consecutive_losing_trades: int,
    ) -> str:
        if (
            self.config.max_losing_trades_per_day > 0
            and losing_trades >= self.config.max_losing_trades_per_day
        ):
            return "max_losing_trades_per_day"
        if (
            self.config.max_consecutive_losing_trades_per_day > 0
            and consecutive_losing_trades >= self.config.max_consecutive_losing_trades_per_day
        ):
            return "max_consecutive_losing_trades_per_day"
        return ""

    def _active_symbols_for_day(
        self,
        trading_day: date,
        bars: Sequence[MinuteBar],
        opening_ranges: Dict[Tuple[date, str], _OpeningRange],
        *,
        universe_snapshot: Optional[DailyUniverseSnapshot] = None,
    ) -> set:
        day_symbols = {bar.symbol.upper() for bar in bars}
        if universe_snapshot is not None:
            if universe_snapshot.trading_day != trading_day:
                raise ValueError("daily universe snapshot does not match trading day")
            day_symbols.intersection_update(universe_snapshot.active_tickers)
        if self.config.symbol_filter == "none":
            return day_symbols
        if self.config.symbol_filter != "opening-range":
            raise ValueError("unknown daytrade symbol_filter: %s" % self.config.symbol_filter)

        candidates = []
        for symbol in sorted(day_symbols):
            opening_range = opening_ranges.get((trading_day, symbol))
            if not opening_range:
                continue
            range_pct = _opening_range_pct(opening_range)
            momentum = _opening_momentum(opening_range)
            if range_pct < self.config.min_opening_range_pct:
                continue
            if opening_range.average_dollar_volume < self.config.min_opening_dollar_volume:
                continue
            if self.config.require_positive_opening_momentum and momentum <= 0.0:
                continue
            score = _opening_candidate_score(opening_range)
            candidates.append((score, symbol))

        limit = max(1, min(int(self.config.max_active_symbols), int(self.config.max_symbols)))
        ranked = sorted(candidates, key=lambda item: (-item[0], item[1]))
        return {symbol for _score, symbol in ranked[:limit]}

    def _exit_for_bar(
        self,
        position: _OpenPosition,
        bar: MinuteBar,
        below_vwap_counts: Dict[str, int],
        effective_vwap: Optional[float] = None,
    ) -> Tuple[
        _OpenPosition,
        str,
        Optional[float],
        Optional[float],
    ]:
        position = self._position_with_adjusted_stop(position)
        side = position.signal.side
        if side == "long" and float(bar.low) <= position.current_stop:
            raw_exit = min(float(bar.open), position.current_stop)
            return (
                position,
                "stop_loss",
                raw_exit,
                self._exit_fill_price(side, raw_exit),
            )
        if side == "short" and float(bar.high) >= position.current_stop:
            raw_exit = max(float(bar.open), position.current_stop)
            return (
                position,
                "stop_loss",
                raw_exit,
                self._exit_fill_price(side, raw_exit),
            )
        if side == "long" and float(bar.high) >= position.signal.target_price:
            raw_exit = max(float(bar.open), position.signal.target_price)
            return (
                position,
                "take_profit",
                raw_exit,
                self._exit_fill_price(side, raw_exit),
            )
        if side == "short" and float(bar.low) <= position.signal.target_price:
            raw_exit = min(float(bar.open), position.signal.target_price)
            return (
                position,
                "take_profit",
                raw_exit,
                self._exit_fill_price(side, raw_exit),
            )
        if _local_time(bar.timestamp) >= _parse_time(self.config.force_exit):
            raw_exit = float(bar.close)
            return (
                position,
                "forced_exit",
                raw_exit,
                self._exit_fill_price(side, raw_exit),
            )
        if self.config.max_holding_minutes > 0:
            held = (bar.timestamp - position.signal.timestamp).total_seconds() / 60.0
            if held >= self.config.max_holding_minutes:
                raw_exit = float(bar.close)
                return (
                    position,
                    "max_holding_minutes",
                    raw_exit,
                    self._exit_fill_price(side, raw_exit),
                )

        symbol = position.signal.symbol.upper()
        selected_vwap = _effective_vwap(bar) if effective_vwap is None else effective_vwap
        count, should_exit = update_vwap_hysteresis(
            previous_below_count=below_vwap_counts.get(symbol, 0),
            close=float(bar.close),
            session_vwap=float(selected_vwap),
            hysteresis_bps=self.config.vwap_hysteresis_bps,
            required_bars=self.config.vwap_exit_bars,
            side=side,
        )
        below_vwap_counts[symbol] = count
        if should_exit:
            raw_exit = float(bar.close)
            return (
                position,
                "vwap_exit",
                raw_exit,
                self._exit_fill_price(side, raw_exit),
            )
        favorable_price = float(bar.high) if side == "long" else float(bar.low)
        return (
            _position_with_favorable_water(position, favorable_price),
            "",
            None,
            None,
        )

    def _position_with_adjusted_stop(self, position: _OpenPosition) -> _OpenPosition:
        stop = position.current_stop
        side = position.signal.side
        direction = 1.0 if side == "long" else -1.0
        if self.config.breakeven_after_r > 0:
            trigger = position.signal.entry_price + (
                direction * position.initial_risk * self.config.breakeven_after_r
            )
            triggered = (
                position.high_water >= trigger
                if side == "long"
                else position.high_water <= trigger
            )
            if triggered:
                stop = (
                    max(stop, position.signal.entry_price)
                    if side == "long"
                    else min(stop, position.signal.entry_price)
                )
        if self.config.trailing_stop_after_r > 0:
            trigger = position.signal.entry_price + (
                direction * position.initial_risk * self.config.trailing_stop_after_r
            )
            triggered = (
                position.high_water >= trigger
                if side == "long"
                else position.high_water <= trigger
            )
            if triggered:
                trailing = position.high_water - direction * position.initial_risk
                stop = max(stop, trailing) if side == "long" else min(stop, trailing)
        if stop == position.current_stop:
            return position
        return _OpenPosition(
            signal=position.signal,
            current_stop=stop,
            high_water=position.high_water,
            initial_risk=position.initial_risk,
        )


class DayTradeTradeJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append_all(self, trades: Sequence[DayTradeTrade]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            for trade in trades:
                file.write(json.dumps(_trade_to_payload(trade), sort_keys=True) + "\n")

    def write_all(self, trades: Sequence[DayTradeTrade]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            for trade in trades:
                file.write(json.dumps(_trade_to_payload(trade), sort_keys=True) + "\n")

    def read_all(self) -> List[DayTradeTrade]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as file:
            return [_trade_from_payload(json.loads(line)) for line in file if line.strip()]


class DayTradeDailyJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append_all(self, results: Sequence[DayTradeDailyResult]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            for result in results:
                file.write(json.dumps(_daily_to_payload(result), sort_keys=True) + "\n")

    def write_all(self, results: Sequence[DayTradeDailyResult]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            for result in results:
                file.write(json.dumps(_daily_to_payload(result), sort_keys=True) + "\n")

    def read_all(self) -> List[DayTradeDailyResult]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as file:
            return [_daily_from_payload(json.loads(line)) for line in file if line.strip()]


class DayTradeMinuteCache:
    def __init__(self, root: Path, source: str):
        self.root = Path(root)
        self.source_name = str(source)
        self.source = _cache_safe_name(source)
        self.provider, self.feed = _cache_provider_and_feed(self.source_name)

    def write_all(
        self,
        bars: Sequence[MinuteBar],
        coverage_start: Optional[date] = None,
        coverage_end: Optional[date] = None,
        expected_sessions: Optional[Sequence[date]] = None,
        request_start: Optional[date] = None,
        request_end: Optional[date] = None,
    ) -> List[Path]:
        if request_start is not None or request_end is not None:
            if coverage_start is not None or coverage_end is not None:
                raise ValueError("use request_start/request_end or coverage_start/coverage_end, not both")
            coverage_start, coverage_end = request_start, request_end
        if (coverage_start is None) != (coverage_end is None):
            raise ValueError("coverage_start and coverage_end must be provided together")
        if coverage_start is not None and coverage_start > coverage_end:
            raise ValueError("coverage_start must not be after coverage_end")
        grouped: Dict[Tuple[str, str], List[MinuteBar]] = {}
        seen_input = set()
        for bar in bars:
            normalized = _normalize_minute_bar(bar)
            key = (normalized.symbol, normalized.timestamp.isoformat())
            if key in seen_input:
                raise ValueError("duplicate minute-bar timestamp in input: %s %s" % key)
            seen_input.add(key)
            grouped.setdefault((normalized.symbol, _month_key(_local_day(normalized.timestamp))), []).append(normalized)

        written = []
        for (symbol, month), items in grouped.items():
            path = self._path(symbol, month)
            path.parent.mkdir(parents=True, exist_ok=True)
            previous_manifest = self._load_manifest(symbol, month)
            if not self._manifest_is_current(previous_manifest, symbol, month):
                previous_manifest = None
            existing = (
                self._read_month(symbol, month)
                if path.exists() and previous_manifest is not None
                else []
            )
            unique = {
                item.timestamp.isoformat(): item
                for item in sorted([*existing, *items], key=lambda value: value.timestamp)
            }
            ordered = list(unique.values())
            content = "".join(
                json.dumps(_minute_bar_to_payload(item), sort_keys=True) + "\n"
                for item in ordered
            )
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            inferred_start = min(_local_day(item.timestamp) for item in items)
            inferred_end = max(_local_day(item.timestamp) for item in items)
            requested_start = coverage_start or inferred_start
            requested_end = coverage_end or inferred_end
            previous_ranges = []
            previous_sessions = []
            previous_request_start = requested_start
            previous_request_end = requested_end
            if previous_manifest is not None:
                try:
                    previous_ranges = _merge_coverage_ranges(
                        previous_manifest.get("coverage_ranges", [])
                    )
                    previous_sessions = [
                        date.fromisoformat(value).isoformat()
                        for value in previous_manifest.get("expected_sessions", [])
                    ]
                    previous_request_start = date.fromisoformat(
                        previous_manifest.get(
                            "request_start", previous_manifest["coverage_start"]
                        )
                    )
                    previous_request_end = date.fromisoformat(
                        previous_manifest.get(
                            "request_end", previous_manifest["coverage_end"]
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    previous_ranges = []
                    previous_sessions = []
                    previous_request_start = requested_start
                    previous_request_end = requested_end
            manifest_request_start = min(previous_request_start, requested_start)
            manifest_request_end = max(previous_request_end, requested_end)
            ranges = list(previous_ranges)
            month_start = date.fromisoformat(month + "-01")
            range_start = max(requested_start, month_start)
            range_end = min(requested_end, _month_end(month_start))
            if range_start <= range_end:
                ranges.append({"start": range_start.isoformat(), "end": range_end.isoformat()})
            ranges = _merge_coverage_ranges(ranges)
            declared_sessions = {
                value.isoformat()
                for value in (expected_sessions or ())
                if _month_key(value) == month
            }
            declared_sessions.update(previous_sessions)
            observed_sessions = sorted({_local_day(item.timestamp).isoformat() for item in ordered})
            manifest = {
                "schema_version": DAYTRADE_MINUTE_CACHE_SCHEMA_VERSION,
                "provider": self.provider,
                "feed": self.feed,
                "source": self.source_name,
                "symbol": symbol,
                "month": month,
                "coverage_start": ranges[0]["start"],
                "coverage_end": ranges[-1]["end"],
                "request_start": manifest_request_start.isoformat(),
                "request_end": manifest_request_end.isoformat(),
                "coverage_ranges": ranges,
                "expected_sessions": sorted(declared_sessions),
                "observed_sessions": observed_sessions,
                "unique_bars": len(ordered),
                "content_hash": content_hash,
            }
            _atomic_write_pair(
                path,
                content,
                self._manifest_path(symbol, month),
                json.dumps(manifest, sort_keys=True) + "\n",
            )
            written.append(path)
        return written

    def read(self, symbols: Sequence[str], start: date, end: date) -> List[MinuteBar]:
        if start > end:
            raise ValueError("start must not be after end")
        bars = []
        seen = set()
        for symbol in symbols:
            for month in _month_keys_between(start, end):
                path = self._path(symbol, month)
                if not path.exists():
                    continue
                for bar in self._read_month(symbol.upper(), month):
                    bar_day = _local_day(bar.timestamp)
                    if start <= bar_day <= end:
                        key = (bar.symbol, bar.timestamp.isoformat())
                        if key in seen:
                            raise ValueError("duplicate minute-bar timestamp: %s %s" % key)
                        seen.add(key)
                        bars.append(bar)
        return sorted(bars, key=lambda item: (item.timestamp, item.symbol))

    def coverage(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        expected_sessions: Optional[Sequence[date]] = None,
    ) -> dict:
        if start > end:
            raise ValueError("start must not be after end")
        missing = []
        reasons = []
        hashes = []
        for symbol in symbols:
            for month in _month_keys_between(start, end):
                reason, content_hash = self._month_coverage_reason(
                    symbol.upper(),
                    month,
                    start,
                    end,
                    expected_sessions,
                )
                if reason:
                    missing.append((symbol.upper(), month))
                    reasons.append(reason)
                elif content_hash:
                    hashes.append((symbol.upper(), month, content_hash))
        dataset_hash = hashlib.sha256(
            json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "complete": not missing,
            "missing_months": missing,
            "reasons": reasons,
            "content_hash": dataset_hash,
        }

    def missing_months(
        self,
        symbols: Sequence[str],
        start: date,
        end: date,
        expected_sessions: Optional[Sequence[date]] = None,
    ) -> List[Tuple[str, str]]:
        return self.coverage(symbols, start, end, expected_sessions)["missing_months"]

    def _month_coverage_reason(
        self,
        symbol: str,
        month: str,
        start: date,
        end: date,
        expected_sessions: Optional[Sequence[date]],
    ) -> Tuple[str, str]:
        path = self._path(symbol, month)
        if not path.exists():
            return "file_missing:%s:%s" % (symbol, month), ""
        try:
            manifest = self._load_manifest(symbol, month)
            if manifest is None:
                return "manifest_missing:%s:%s" % (symbol, month), ""
            bars = self._read_month(symbol, month)
            if manifest.get("schema_version") != DAYTRADE_MINUTE_CACHE_SCHEMA_VERSION:
                return "schema_mismatch:%s:%s" % (symbol, month), ""
            if manifest.get("provider") != self.provider or manifest.get("feed", "") != self.feed:
                return "source_mismatch:%s:%s" % (symbol, month), ""
            if manifest.get("symbol") != symbol or manifest.get("month") != month:
                return "identity_mismatch:%s:%s" % (symbol, month), ""
            if int(manifest.get("unique_bars", -1)) != len(bars):
                return "bar_count_mismatch:%s:%s" % (symbol, month), ""
            observed = {_local_day(bar.timestamp) for bar in bars}
            month_start = date.fromisoformat(month + "-01")
            requested_start = max(start, month_start)
            requested_end = min(end, _month_end(month_start))
            manifest_start = date.fromisoformat(
                manifest.get("request_start", manifest["coverage_start"])
            )
            manifest_end = date.fromisoformat(
                manifest.get("request_end", manifest["coverage_end"])
            )
            if requested_start < manifest_start or requested_end > manifest_end:
                return "coverage_gap:%s:%s" % (symbol, month), ""
            declared = {
                date.fromisoformat(value)
                for value in manifest.get("expected_sessions", [])
                if requested_start <= date.fromisoformat(value) <= requested_end
            }
            if not declared.issubset(observed):
                return "expected_session_missing:%s:%s" % (symbol, month), ""
            if expected_sessions is not None:
                required = {
                    value
                    for value in expected_sessions
                    if requested_start <= value <= requested_end
                }
                if not required.issubset(observed):
                    return "expected_session_missing:%s:%s" % (symbol, month), ""
            elif not _ranges_cover(
                manifest.get("coverage_ranges", []),
                requested_start,
                requested_end,
            ):
                return "coverage_gap:%s:%s" % (symbol, month), ""
            return "", str(manifest.get("content_hash", ""))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            return "integrity_error:%s:%s:%s" % (symbol, month, error), ""

    def _read_month(self, symbol: str, month: str) -> List[MinuteBar]:
        path = self._path(symbol, month)
        raw = path.read_bytes()
        manifest = self._load_manifest(symbol, month)
        if manifest is not None:
            actual_hash = hashlib.sha256(raw).hexdigest()
            if actual_hash != manifest.get("content_hash"):
                raise ValueError("content hash mismatch for %s %s" % (symbol, month))
        bars = []
        seen = set()
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            bar = _minute_bar_from_payload(json.loads(line))
            key = bar.timestamp.isoformat()
            if key in seen:
                raise ValueError("duplicate minute-bar timestamp for %s %s" % (symbol, month))
            if bar.symbol != symbol.upper() or _month_key(_local_day(bar.timestamp)) != month:
                raise ValueError("minute-bar outside manifest identity for %s %s" % (symbol, month))
            seen.add(key)
            bars.append(bar)
        return bars

    def _load_manifest(self, symbol: str, month: str) -> Optional[dict]:
        path = self._manifest_path(symbol, month)
        if not path.exists():
            return None
        manifest = json.loads(path.read_text(encoding="utf-8"))
        return manifest if isinstance(manifest, dict) else None

    def _manifest_is_current(self, manifest, symbol: str, month: str) -> bool:
        return bool(
            isinstance(manifest, dict)
            and manifest.get("schema_version") == DAYTRADE_MINUTE_CACHE_SCHEMA_VERSION
            and manifest.get("provider") == self.provider
            and manifest.get("feed", "") == self.feed
            and manifest.get("symbol") == symbol.upper()
            and manifest.get("month") == month
        )

    def _path(self, symbol: str, month: str) -> Path:
        return self.root / self.source / symbol.upper() / ("%s.jsonl" % month)

    def _manifest_path(self, symbol: str, month: str) -> Path:
        return self.root / self.source / symbol.upper() / ("%s.manifest.json" % month)


def minute_bar_from_row(row: dict) -> MinuteBar:
    timestamp = row.get("timestamp")
    if isinstance(timestamp, str):
        timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return MinuteBar(
        symbol=str(row["ticker"]).upper(),
        timestamp=timestamp,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume", 0.0)),
        vwap=float(row["vwap"]) if row.get("vwap") is not None else None,
    )


def minute_bar_to_row(bar: MinuteBar) -> dict:
    normalized = _normalize_minute_bar(bar)
    return {
        "ticker": normalized.symbol,
        "timestamp": normalized.timestamp,
        "open": normalized.open,
        "high": normalized.high,
        "low": normalized.low,
        "close": normalized.close,
        "volume": normalized.volume,
        "vwap": normalized.vwap,
    }


def build_daytrade_historical_evidence(result: DayTradeBacktestResult) -> RequirementEvidence:
    overnight = _overnight_count(result.trades)
    errors = sum(item.errors for item in result.daily_results)
    passed = bool(result.daily_results) and result.trade_count > 0 and errors == 0 and overnight == 0
    return RequirementEvidence(
        "daytrade_historical_backtest",
        passed=passed,
        detail=(
            "sessions=%d trades=%d cumulative_return=%.6f annual_return=%.6f sharpe=%.6f "
            "max_drawdown=%.6f win_rate=%.6f profit_factor=%s overnight=%d errors=%d"
            % (
                len(result.daily_results),
                result.trade_count,
                result.cumulative_return,
                result.annual_return,
                result.sharpe,
                result.max_drawdown,
                result.win_rate,
                _format_float(result.profit_factor),
                overnight,
                errors,
            )
        ),
    )


def build_daytrade_soak_evidence(
    daily_results: Sequence[DayTradeDailyResult],
    trades: Sequence[DayTradeTrade],
    requirement_id: str,
    min_sessions: int = 20,
    max_drawdown: float = 0.05,
) -> RequirementEvidence:
    status = summarize_daytrade_status(
        daily_results=daily_results,
        trades=trades,
        min_sessions=min_sessions,
        max_drawdown=max_drawdown,
    )
    return RequirementEvidence(
        requirement_id,
        passed=bool(status["passed"]),
        detail=(
            "sessions=%d trades=%d errors=%d max_drawdown=%.6f win_rate=%.6f "
            "profit_factor=%s overnight=%d reasons=%s"
            % (
                status["sessions"],
                status["trade_count"],
                status["errors"],
                status["max_drawdown"],
                status["win_rate"],
                _format_float(status["profit_factor"]),
                status["overnight_trades"],
                ",".join(status["reasons"]),
            )
        ),
    )


def build_daytrade_walk_forward_evidence(result: DayTradeOptimizationResult) -> RequirementEvidence:
    baseline = result.baseline
    validation = result.validation
    statistical_validation = result.statistical_validation
    dsr = statistical_validation.get("deflated_sharpe_ratio", {})
    pbo = statistical_validation.get("probability_of_backtest_overfitting", {})
    statistics_passed = bool(
        statistical_validation.get("available")
        and dsr.get("available")
        and _probability_meets(dsr.get("confidence"), minimum=0.95)
        and pbo.get("available")
        and _probability_meets(pbo.get("probability"), maximum=0.05)
    )
    passed = (
        validation.trade_count >= 100
        and validation.sharpe > baseline.sharpe
        and validation.profit_factor > baseline.profit_factor
        and validation.max_drawdown <= baseline.max_drawdown
        and validation.cumulative_return >= baseline.cumulative_return * 0.90
        and not result.fragile
        and result.completed_trial_count == result.declared_trial_count
        and statistics_passed
    )
    return RequirementEvidence(
        "daytrade_walk_forward_optimization",
        passed=passed,
        detail=(
            "best_config=%s baseline_sharpe=%.6f validation_sharpe=%.6f "
            "baseline_pf=%s validation_pf=%s baseline_return=%.6f validation_return=%.6f "
            "baseline_max_drawdown=%.6f validation_max_drawdown=%.6f validation_trades=%d fragile=%s"
            " declared_trials=%d completed_trials=%d statistical_validation=%s"
            " dsr_confidence=%s pbo=%s"
            % (
                result.best_config_name,
                baseline.sharpe,
                validation.sharpe,
                _format_float(baseline.profit_factor),
                _format_float(validation.profit_factor),
                baseline.cumulative_return,
                validation.cumulative_return,
                baseline.max_drawdown,
                validation.max_drawdown,
                validation.trade_count,
                str(result.fragile).lower(),
                result.declared_trial_count,
                result.completed_trial_count,
                str(bool(statistical_validation.get("available"))).lower(),
                dsr.get("confidence", "unavailable"),
                pbo.get("probability", "unavailable"),
            )
        ),
    )


def _probability_meets(value, *, minimum=0.0, maximum=1.0) -> bool:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(probability) and minimum <= probability <= maximum


def generate_daytrade_walk_forward_windows(
    start: date,
    end: date,
    train_months: int = 3,
    validation_months: int = 1,
    label_horizon_days: int = 1,
    embargo_days: int = 1,
) -> List[DayTradeWalkForwardWindow]:
    for value, name in (
        (label_horizon_days, "label_horizon_days"),
        (embargo_days, "embargo_days"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    windows: List[DayTradeWalkForwardWindow] = []
    train_start = date(start.year, start.month, 1)
    while True:
        train_end_month = _add_months(train_start, train_months - 1)
        validation_start = _add_months(train_start, train_months)
        train_end = min(
            _month_end(train_end_month),
            validation_start - timedelta(days=label_horizon_days + 1),
            end,
        )
        validation_end_month = _add_months(validation_start, validation_months - 1)
        validation_end = min(_month_end(validation_end_month), end)
        if validation_start > end:
            break
        if validation_end >= start:
            windows.append(
                DayTradeWalkForwardWindow(
                    train_start=max(train_start, start),
                    train_end=train_end,
                    validation_start=validation_start,
                    validation_end=validation_end,
                    purge_days=label_horizon_days,
                    embargo_days=embargo_days,
                )
            )
        train_start = _add_months(train_start, validation_months)
    return [
        item
        for item in windows
        if item.train_start <= item.train_end and item.validation_start <= item.validation_end
    ]


def run_daytrade_walk_forward_optimization(
    bars: Sequence[MinuteBar],
    start: date,
    end: date,
    base_config: DayTradeConfig = DayTradeConfig(),
    train_months: int = 3,
    validation_months: int = 1,
    max_candidates: int = 250,
    feature_snapshots: Sequence[IntradayFeatureSnapshot] = (),
    experiment_registry: Optional[ExperimentRegistry] = None,
    candidate_configs: Optional[Sequence[Tuple[str, DayTradeConfig]]] = None,
    universe_schedule: Optional[DayTradeUniverseSchedule] = None,
    historical_trial_artifact: Optional[Mapping[str, object]] = None,
    adaptive_validation_artifact: Optional[
        DayTradeAdaptiveValidationArtifact
    ] = None,
    evaluation_dataset_hash: Optional[str] = None,
) -> DayTradeOptimizationResult:
    if (
        historical_trial_artifact is not None
        and adaptive_validation_artifact is not None
    ):
        raise ValueError(
            "DayTrade optimization cannot combine same-period historical returns "
            "with frozen adaptive validation"
        )
    if adaptive_validation_artifact is not None and not isinstance(
        adaptive_validation_artifact,
        DayTradeAdaptiveValidationArtifact,
    ):
        raise TypeError(
            "adaptive_validation_artifact must be a verified "
            "DayTradeAdaptiveValidationArtifact"
        )
    clean_bars = [_normalize_minute_bar(bar) for bar in bars]
    normalized_universe_schedule = _normalize_universe_schedule(universe_schedule)
    _require_universe_schedule_coverage(
        normalized_universe_schedule,
        (_local_day(bar.timestamp) for bar in clean_bars),
    )
    windows = generate_daytrade_walk_forward_windows(start, end, train_months, validation_months)
    registry = experiment_registry or preregistered_daytrade_experiments()
    registered_trial_ids = {
        trial_id for record in registry.records for trial_id in record.trial_ids
    }
    candidate_pool = (
        list(candidate_configs)
        if candidate_configs is not None
        else _daytrade_candidate_configs(base_config)
    )
    if not candidate_pool:
        raise ValueError("candidate_configs must not be empty")
    candidate_names = [name for name, _config in candidate_pool]
    if len(set(candidate_names)) != len(candidate_names):
        raise ValueError("candidate config names must be unique")
    feature_coverage_complete = _feature_coverage_complete(
        clean_bars,
        feature_snapshots,
        start,
        end,
        entry_start=min(_parse_time(item.entry_start) for _name, item in candidate_pool),
        entry_end=max(_parse_time(item.entry_end) for _name, item in candidate_pool),
        universe_schedule=normalized_universe_schedule,
    )
    if not feature_coverage_complete:
        candidate_pool = [
            item for item in candidate_pool if not _candidate_requires_features(item[0])
        ]
    if not candidate_pool:
        raise ValueError("feature coverage is incomplete for every candidate config")
    candidates = _sample_candidates(candidate_pool, max_candidates)
    executed_trial_ids = tuple(
        sorted(name for name, _config in candidates if name in registered_trial_ids)
    )
    experiment_id_by_trial = {
        trial_id: record.experiment_id
        for record in registry.records
        for trial_id in record.trial_ids
    }
    for trial_id in executed_trial_ids:
        registry.record_trial(experiment_id_by_trial[trial_id], trial_id)
    ranking_stats: Dict[str, dict] = {}
    selected_validation_trades: List[DayTradeTrade] = []
    selected_validation_daily: List[DayTradeDailyResult] = []
    trial_validation_daily: Dict[str, List[DayTradeDailyResult]] = {
        name: [] for name, _config in candidates
    }
    trial_validation_trades: Dict[str, List[DayTradeTrade]] = {
        name: [] for name, _config in candidates
    }
    trial_validation_windows: Dict[str, List[dict]] = {
        name: [] for name, _config in candidates
    }
    window_payloads = []

    for window in windows:
        train_bars = _bars_in_range(clean_bars, window.train_start, window.train_end)
        validation_bars = _bars_in_range(clean_bars, window.validation_start, window.validation_end)
        if not train_bars or not validation_bars:
            continue
        train_results = []
        for name, config in candidates:
            train_result = _run_candidate_backtest(
                name,
                config,
                train_bars,
                feature_snapshots,
                normalized_universe_schedule,
            )
            score = daytrade_optimization_score(train_result)
            train_results.append((score, name, config, train_result))
        train_results.sort(key=lambda item: (item[0], item[1]), reverse=True)
        best_train_score, best_name, best_config, best_train = train_results[0]
        validation_results = {
            name: _run_candidate_backtest(
                name,
                config,
                validation_bars,
                feature_snapshots,
                normalized_universe_schedule,
            )
            for name, config in candidates
        }
        for name, candidate_result in validation_results.items():
            trial_validation_daily[name].extend(candidate_result.daily_results)
            trial_validation_trades[name].extend(candidate_result.trades)
            trial_validation_windows[name].append(
                {
                    "validation_start": window.validation_start.isoformat(),
                    "validation_end": window.validation_end.isoformat(),
                    **_result_metrics(candidate_result),
                }
            )
        validation_result = validation_results[best_name]
        validation_score = daytrade_optimization_score(validation_result)
        stats = ranking_stats.setdefault(
            best_name,
            {"name": best_name, "config": daytrade_config_to_payload(best_config), "windows": 0, "score_sum": 0.0},
        )
        stats["windows"] += 1
        stats["score_sum"] += validation_score
        selected_validation_trades.extend(validation_result.trades)
        selected_validation_daily.extend(validation_result.daily_results)
        window_payloads.append(
            {
                "train_start": window.train_start.isoformat(),
                "train_end": window.train_end.isoformat(),
                "validation_start": window.validation_start.isoformat(),
                "validation_end": window.validation_end.isoformat(),
                "purge_days": window.purge_days,
                "embargo_days": window.embargo_days,
                "selected_config": best_name,
                "train_score": best_train_score,
                "validation_score": validation_score,
                "train": _result_metrics(best_train),
                "validation": _result_metrics(validation_result),
            }
        )

    rankings = []
    for stats in ranking_stats.values():
        average_score = stats["score_sum"] / max(1, stats["windows"])
        rankings.append(
            {
                "name": stats["name"],
                "average_validation_score": average_score,
                "windows": stats["windows"],
                "config": stats["config"],
            }
        )
    rankings.sort(key=lambda item: (item["average_validation_score"], item["name"]), reverse=True)
    best_config_payload = rankings[0]["config"] if rankings else daytrade_config_to_payload(base_config)
    best_config_name = rankings[0]["name"] if rankings else "baseline_top10"
    validation_daily = _rebuild_continuous_daily_equity(
        selected_validation_daily,
        initial_capital=base_config.daytrade_capital,
    )
    validation = _build_backtest_result(
        strategy=base_config.strategy_name,
        trades=selected_validation_trades,
        daily_results=validation_daily,
        initial_capital=base_config.daytrade_capital,
    )
    continuous_trial_daily = {
        name: _rebuild_continuous_daily_equity(
            daily_results,
            initial_capital=base_config.daytrade_capital,
        )
        for name, daily_results in trial_validation_daily.items()
    }
    statistical_target_daily = validation_daily
    if len(executed_trial_ids) == 1:
        statistical_target_daily = continuous_trial_daily.get(
            executed_trial_ids[0],
            validation_daily,
        )
    h9_candidate_name = "daytrade-h9-rvol-reward-risk:trial-01"
    candidate_config_by_name = dict(candidates)
    h9_fixed_results: Dict[str, DayTradeBacktestResult] = {}
    h9_full_interval_bars: List[MinuteBar] = []
    walk_forward_diagnostic = None
    if h9_candidate_name in candidate_config_by_name:
        # H3 and H9 are frozen before this run, so their canonical admission
        # sample is the complete requested interval.  The 3/1 walk-forward
        # schedule remains useful diagnostics, but trimming the fixed pair to
        # its validation months silently reduced a 24-month holdout to 21
        # months and made the formal gate impossible to satisfy.
        walk_forward_statistics = _daytrade_statistical_validation(
            statistical_target_daily,
            continuous_trial_daily,
            global_declared_trial_count=max(len(continuous_trial_daily) - 1, 0),
        )
        walk_forward_diagnostic = {
            "purpose": "selection_diagnostic_only",
            "evaluation_scope": "walk_forward_validation_windows",
            "observation_dates": walk_forward_statistics.get(
                "observation_dates", []
            ),
            "selected_metrics": _result_metrics(validation),
            "statistical_validation": walk_forward_statistics,
            "trial_metrics": {
                name: _result_metrics(
                    _build_backtest_result(
                        strategy=config.strategy_name,
                        trades=trial_validation_trades[name],
                        daily_results=continuous_trial_daily[name],
                        initial_capital=base_config.daytrade_capital,
                    )
                )
                for name, config in candidates
            },
            "trial_windows": trial_validation_windows,
        }
        h9_full_interval_bars = _bars_in_range(clean_bars, start, end)
        h9_fixed_results = _run_fixed_candidate_results(
            candidates,
            h9_full_interval_bars,
            feature_snapshots=feature_snapshots,
            universe_schedule=normalized_universe_schedule,
            initial_capital=base_config.daytrade_capital,
        )
        continuous_trial_daily = {
            name: result.daily_results
            for name, result in h9_fixed_results.items()
        }
        statistical_target_daily = h9_fixed_results[
            h9_candidate_name
        ].daily_results
    artifact_metadata = None
    if historical_trial_artifact is not None:
        historical_daily, completed_trials, artifact_metadata = (
            _validated_historical_trial_returns(
                historical_trial_artifact,
                registry=registry,
                observation_dates=tuple(item.day for item in statistical_target_daily),
                initial_capital=base_config.daytrade_capital,
                evaluation_dataset_hash=evaluation_dataset_hash,
            )
        )
        continuous_trial_daily.update(historical_daily)
        registry.import_known_experiments((), completed_trials=completed_trials)
    adaptive_validation_metadata = None
    if adaptive_validation_artifact is not None:
        completed_trials = tuple(
            (record.experiment_id, trial_id)
            for record in registry.known_records
            for trial_id in record.trial_ids
        )
        if len(completed_trials) != adaptive_validation_artifact.completed_trial_count:
            raise ValueError(
                "adaptive validation trial count does not match the global registry"
            )
        registry.import_known_experiments((), completed_trials=completed_trials)
        adaptive_validation_metadata = adaptive_validation_artifact.metadata()
    statistical_validation = _daytrade_statistical_validation(
        statistical_target_daily,
        continuous_trial_daily,
        global_declared_trial_count=registry.global_declared_trial_count,
    )
    statistical_validation["trial_configs"] = {
        name: daytrade_config_to_payload(config)
        for name, config in candidates
    }
    statistical_validation["trial_metrics"] = {
        name: _result_metrics(
            h9_fixed_results[name]
            if h9_fixed_results
            else _build_backtest_result(
                strategy=config.strategy_name,
                trades=trial_validation_trades[name],
                daily_results=continuous_trial_daily[name],
                initial_capital=base_config.daytrade_capital,
            )
        )
        for name, config in candidates
    }
    statistical_validation["trial_windows"] = trial_validation_windows
    statistical_validation["search_strategy_count"] = len(candidates)
    if artifact_metadata is not None:
        statistical_validation["historical_trial_artifact"] = artifact_metadata
    if adaptive_validation_metadata is not None:
        # Only selection evidence is inherited.  Trial return histories stay
        # outside the sealed dataset; all H3/H9 financial metrics below are
        # still generated from the current full sealed interval.
        statistical_validation.pop("reason", None)
        statistical_validation.update(
            {
                "available": True,
                "statistical_source": "signed_adaptive_validation_artifact",
                "statistics_scope": "adaptive_development_2023_2024",
                "adaptive_validation_artifact": adaptive_validation_metadata,
                "deflated_sharpe_ratio": {
                    "available": True,
                    "confidence": adaptive_validation_artifact.dsr_confidence,
                    "trial_count": adaptive_validation_artifact.declared_trial_count,
                    "source": "signed_adaptive_validation_artifact",
                },
                "probability_of_backtest_overfitting": {
                    "available": True,
                    "probability": adaptive_validation_artifact.pbo,
                    "strategy_count": adaptive_validation_artifact.declared_trial_count + 1,
                    "source": "signed_adaptive_validation_artifact",
                },
            }
        )
    validation_days = {item.day for item in validation_daily}
    if h9_candidate_name in candidate_config_by_name:
        statistical_validation["evaluation_scope"] = "full_request_interval"
        statistical_validation["request_start"] = start.isoformat()
        statistical_validation["request_end"] = end.isoformat()
        statistical_validation["walk_forward_diagnostic"] = (
            walk_forward_diagnostic
        )
        fixed_candidate_evidence, stress_result = (
            _build_h9_fixed_candidate_oos_evidence(
                candidate_name=h9_candidate_name,
                candidate_config=candidate_config_by_name[h9_candidate_name],
                candidate_daily=h9_fixed_results[h9_candidate_name].daily_results,
                candidate_trades=h9_fixed_results[h9_candidate_name].trades,
                oos_bars=h9_full_interval_bars,
                feature_snapshots=feature_snapshots,
                universe_schedule=normalized_universe_schedule,
                initial_capital=base_config.daytrade_capital,
            )
        )
        statistical_validation["fixed_candidate_oos_evidence"] = (
            fixed_candidate_evidence
        )
        statistical_validation["canonical_execution_inputs"] = (
            _build_h9_canonical_execution_inputs(
                baseline=h9_fixed_results[candidates[0][0]],
                candidate=h9_fixed_results[h9_candidate_name],
                stress=stress_result,
            )
        )
    baseline_name, baseline_config = candidates[0]
    baseline_raw = _run_candidate_backtest(
        baseline_name,
        baseline_config,
        [bar for bar in clean_bars if _local_day(bar.timestamp) in validation_days],
        feature_snapshots,
        normalized_universe_schedule,
    )
    baseline = _build_backtest_result(
        strategy=baseline_raw.strategy,
        trades=baseline_raw.trades,
        daily_results=_rebuild_continuous_daily_equity(
            baseline_raw.daily_results,
            initial_capital=base_config.daytrade_capital,
        ),
        initial_capital=base_config.daytrade_capital,
    )
    best_config = daytrade_config_from_payload(best_config_payload, base_config=base_config)
    return DayTradeOptimizationResult(
        best_config=best_config,
        best_config_name=best_config_name,
        baseline=baseline,
        validation=validation,
        rankings=rankings,
        windows=window_payloads,
        fragile=_is_fragile(selected_validation_trades),
        declared_trial_count=registry.global_declared_trial_count,
        completed_trial_count=registry.global_completed_trial_count,
        executed_trial_ids=executed_trial_ids,
        feature_coverage_complete=feature_coverage_complete,
        statistical_validation=statistical_validation,
    )


def _run_fixed_candidate_results(
    candidates: Sequence[Tuple[str, DayTradeConfig]],
    bars: Sequence[MinuteBar],
    *,
    feature_snapshots: Sequence[IntradayFeatureSnapshot],
    universe_schedule: Optional[DayTradeUniverseSchedule],
    initial_capital: float,
) -> Dict[str, DayTradeBacktestResult]:
    """Evaluate preregistered configs on one shared, complete interval."""

    results = {}
    for name, config in candidates:
        raw = _run_candidate_backtest(
            name,
            config,
            bars,
            feature_snapshots,
            universe_schedule,
        )
        results[name] = _build_backtest_result(
            strategy=raw.strategy,
            trades=raw.trades,
            daily_results=_rebuild_continuous_daily_equity(
                raw.daily_results,
                initial_capital=initial_capital,
            ),
            initial_capital=initial_capital,
        )
    return results


def daytrade_optimization_score(result: DayTradeBacktestResult) -> float:
    profit_factor = 3.0 if isinf(result.profit_factor) else max(0.0, min(result.profit_factor, 3.0))
    profit_factor_score = profit_factor / 3.0
    return_score = max(-1.0, min(result.cumulative_return / 0.05, 1.0))
    drawdown_penalty = max(0.0, min(result.max_drawdown / 0.05, 2.0))
    return result.sharpe * 0.45 + profit_factor_score * 0.30 + return_score * 0.15 - drawdown_penalty * 0.10


def compare_daytrade_configs(
    bars: Sequence[MinuteBar],
    baseline_config: DayTradeConfig,
    walk_forward_config: DayTradeConfig,
    *,
    walk_forward_name: str = "walk_forward_best",
    feature_snapshots: Sequence[IntradayFeatureSnapshot] = (),
    universe_schedule: Optional[DayTradeUniverseSchedule] = None,
) -> dict:
    normalized_universe_schedule = _normalize_universe_schedule(universe_schedule)
    configs = {
        "baseline_top10": baseline_config,
        "opening_range_filter4": replace(
            baseline_config,
            symbol_filter="opening-range",
            max_active_symbols=4,
            require_positive_opening_momentum=True,
        ),
        "walk_forward_best": walk_forward_config,
    }
    payload = {}
    for name, config in configs.items():
        candidate_name = walk_forward_name if name == "walk_forward_best" else name
        payload[name] = evaluate_daytrade_config(
            bars,
            config,
            candidate_name=candidate_name,
            feature_snapshots=feature_snapshots,
            universe_schedule=normalized_universe_schedule,
        )
    return payload


def evaluate_daytrade_config(
    bars: Sequence[MinuteBar],
    config: DayTradeConfig,
    *,
    candidate_name: str,
    feature_snapshots: Sequence[IntradayFeatureSnapshot] = (),
    universe_schedule: Optional[DayTradeUniverseSchedule] = None,
) -> dict:
    normalized_universe_schedule = _normalize_universe_schedule(universe_schedule)
    result = _run_candidate_backtest(
        candidate_name,
        config,
        bars,
        feature_snapshots,
        normalized_universe_schedule,
    )
    payload = {
        **_result_metrics(result),
        "by_month": _pnl_by_month(result.trades),
        "by_symbol": _pnl_by_symbol(result.trades),
        "daily_returns": [
            {
                "day": item.day.isoformat(),
                "return_pct": item.return_pct,
                "pnl": item.pnl,
                "ending_equity": item.ending_equity,
            }
            for item in result.daily_results
        ],
        "config": daytrade_config_to_payload(config),
        "candidate_name": candidate_name,
    }
    if normalized_universe_schedule is not None:
        payload["by_security_id"] = result.pnl_by_security_id
    return payload


def daytrade_candidate_requires_features(candidate_name: str) -> bool:
    return _candidate_requires_features(candidate_name)


def daytrade_candidate_feature_requirements(
    candidate_name: str,
) -> IntradayFeatureRequirements:
    """Return the minimum feature set for a named preregistered candidate."""

    return IntradayFeatureRequirements.for_candidate(candidate_name)


def daytrade_feature_requirements_for_candidate(
    candidate_name: str,
) -> IntradayFeatureRequirements:
    """Alias with noun-first naming for feature-builder call sites."""

    return daytrade_candidate_feature_requirements(candidate_name)


def daytrade_config_to_payload(config: DayTradeConfig) -> dict:
    if not isinstance(config, DayTradeConfig):
        raise TypeError("config must be a DayTradeConfig")
    return {item.name: getattr(config, item.name) for item in fields(config)}


def daytrade_config_from_payload(item: dict, base_config: DayTradeConfig = DayTradeConfig()) -> DayTradeConfig:
    payload = daytrade_config_to_payload(base_config)
    payload.update(item)
    return DayTradeConfig(**payload)


def summarize_daytrade_status(
    daily_results: Sequence[DayTradeDailyResult],
    trades: Sequence[DayTradeTrade],
    min_sessions: int = 20,
    max_drawdown: float = 0.05,
) -> dict:
    sessions = len(daily_results)
    errors = sum(item.errors for item in daily_results)
    max_dd = _max_drawdown_from_daily(daily_results)
    overnight = _overnight_count(trades)
    reasons = []
    if sessions < min_sessions:
        reasons.append("duration")
    if errors:
        reasons.append("runtime_errors")
    if max_dd > max_drawdown:
        reasons.append("drawdown")
    if overnight:
        reasons.append("overnight_positions")
    wins = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0)
    return {
        "passed": not reasons,
        "sessions": sessions,
        "remaining_sessions": max(0, min_sessions - sessions),
        "errors": errors,
        "max_drawdown": max_dd,
        "max_allowed_drawdown": max_drawdown,
        "trade_count": len(trades),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": profit_factor,
        "average_holding_minutes": _average_holding_minutes(trades),
        "overnight_trades": overnight,
        "reasons": reasons,
    }


def _build_backtest_result(
    strategy: str,
    trades: List[DayTradeTrade],
    daily_results: List[DayTradeDailyResult],
    initial_capital: float,
) -> DayTradeBacktestResult:
    ending_equity = daily_results[-1].ending_equity if daily_results else initial_capital
    returns = [item.return_pct for item in daily_results]
    wins = [trade.pnl for trade in trades if trade.pnl > 0]
    losses = [trade.pnl for trade in trades if trade.pnl < 0]
    profit_factor = sum(wins) / abs(sum(losses)) if losses else (float("inf") if wins else 0.0)
    return DayTradeBacktestResult(
        strategy=strategy,
        trades=trades,
        daily_results=daily_results,
        initial_capital=initial_capital,
        ending_equity=ending_equity,
        cumulative_return=ending_equity / initial_capital - 1.0 if initial_capital else 0.0,
        annual_return=_annualized_return(returns),
        sharpe=_sharpe(returns),
        max_drawdown=_max_drawdown_from_daily(daily_results),
        win_rate=len(wins) / len(trades) if trades else 0.0,
        profit_factor=profit_factor,
        average_holding_minutes=_average_holding_minutes(trades),
        trade_count=len(trades),
    )


def _rebuild_continuous_daily_equity(
    daily_results: Sequence[DayTradeDailyResult],
    initial_capital: float,
) -> List[DayTradeDailyResult]:
    by_day = {item.day: item for item in daily_results}
    equity = float(initial_capital)
    continuous = []
    for day in sorted(by_day):
        item = by_day[day]
        starting_equity = equity
        pnl = float(item.pnl)
        equity += pnl
        continuous.append(
            replace(
                item,
                pnl=pnl,
                return_pct=pnl / starting_equity if starting_equity else 0.0,
                ending_equity=equity,
            )
        )
    return continuous


def _result_metrics(result: DayTradeBacktestResult) -> dict:
    return {
        "sessions": len(result.daily_results),
        "trade_count": result.trade_count,
        "initial_capital": result.initial_capital,
        "ending_equity": result.ending_equity,
        "cumulative_return": result.cumulative_return,
        "annual_return": result.annual_return,
        "sharpe": result.sharpe,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "average_holding_minutes": result.average_holding_minutes,
        "gross_leverage": _maximum_realized_gross_leverage(result),
    }


def _maximum_realized_gross_leverage(result: DayTradeBacktestResult) -> float:
    """Return conservative peak entry notional divided by realized equity.

    Entries at the same timestamp are applied before exits.  This can only
    overstate, never understate, an ambiguous same-event exposure and is
    therefore suitable for an admission ceiling.
    """

    events = []
    for trade in result.trades:
        notional = abs(float(trade.notional))
        events.append((trade.entry_timestamp, 0, notional, 0.0))
        events.append((trade.exit_timestamp, 1, -notional, float(trade.pnl)))
    gross_notional = 0.0
    equity = float(result.initial_capital)
    peak = 0.0
    for _timestamp, _event_order, notional_delta, realized_pnl in sorted(
        events,
        key=lambda item: (item[0], item[1]),
    ):
        gross_notional = max(0.0, gross_notional + notional_delta)
        equity += realized_pnl
        if equity <= 0.0:
            return float("inf")
        peak = max(peak, gross_notional / equity)
    return peak


def _build_h9_fixed_candidate_oos_evidence(
    *,
    candidate_name: str,
    candidate_config: DayTradeConfig,
    candidate_daily: Sequence[DayTradeDailyResult],
    candidate_trades: Sequence[DayTradeTrade],
    oos_bars: Sequence[MinuteBar],
    feature_snapshots: Sequence[IntradayFeatureSnapshot],
    universe_schedule: Optional[Mapping[date, DailyUniverseSnapshot]],
    initial_capital: float,
) -> tuple[dict, DayTradeBacktestResult]:
    stress_config = replace(candidate_config, cost_bps=12.0)
    stress_raw = _run_candidate_backtest(
        candidate_name,
        stress_config,
        oos_bars,
        feature_snapshots,
        universe_schedule,
    )
    stress_result = _build_backtest_result(
        strategy=stress_raw.strategy,
        trades=stress_raw.trades,
        daily_results=_rebuild_continuous_daily_equity(
            stress_raw.daily_results,
            initial_capital=initial_capital,
        ),
        initial_capital=initial_capital,
    )
    quarters = _quarterly_compounded_returns(candidate_daily)
    concentration = _profit_concentration(candidate_trades)
    evidence = {
        "candidate_name": candidate_name,
        "stress_12bps": {
            "available": True,
            "cost_bps": 12.0,
            **_result_metrics(stress_result),
        },
        "quarter_consistency": {
            "available": bool(quarters),
            "reason": None if quarters else "no fixed-candidate OOS quarters",
            "quarter_returns": quarters,
            "positive_quarter_fraction": (
                sum(value > 0.0 for value in quarters.values()) / len(quarters)
                if quarters
                else None
            ),
        },
        "profit_concentration": concentration,
    }
    return evidence, stress_result


def _build_h9_canonical_execution_inputs(
    *,
    baseline: DayTradeBacktestResult,
    candidate: DayTradeBacktestResult,
    stress: DayTradeBacktestResult,
) -> dict:
    """Preserve the three immutable H9 execution arms before summarization.

    Formal evidence must be reconstructable from fills and the daily ledger,
    not from rounded performance summaries.  All three arms therefore come
    directly from their complete-interval backtest results and must cover the
    same observations and starting capital.
    """

    arms = {
        "baseline_5bps": baseline,
        "candidate_5bps": candidate,
        "candidate_12bps": stress,
    }
    observation_dates = tuple(item.day for item in candidate.daily_results)
    if observation_dates != tuple(sorted(set(observation_dates))):
        raise ValueError("H9 candidate daily results must be uniquely ordered")
    for name, result in arms.items():
        dates = tuple(item.day for item in result.daily_results)
        if dates != observation_dates:
            raise ValueError(f"H9 raw execution dates are not aligned: {name}")
        if result.initial_capital != candidate.initial_capital:
            raise ValueError(f"H9 raw execution capital is not aligned: {name}")
    return {
        "initial_capital": _canonical_execution_number(
            candidate.initial_capital,
            "initial_capital",
        ),
        "observation_dates": [item.isoformat() for item in observation_dates],
        **{
            name: _daytrade_execution_arm_payload(result)
            for name, result in arms.items()
        },
    }


def _daytrade_execution_arm_payload(result: DayTradeBacktestResult) -> dict:
    observation_dates = {item.day for item in result.daily_results}
    daily = [
        {
            "day": item.day.isoformat(),
            "pnl": _canonical_execution_number(item.pnl, "daily pnl"),
            "ending_equity": _canonical_execution_number(
                item.ending_equity,
                "daily ending_equity",
            ),
        }
        for item in result.daily_results
    ]
    trades = []
    for trade in result.trades:
        if trade.day not in observation_dates:
            raise ValueError("H9 trade day is missing from the daily ledger")
        if trade.exit_timestamp < trade.entry_timestamp:
            raise ValueError("H9 trade exit precedes its entry")
        trades.append(
            {
                "day": trade.day.isoformat(),
                "entry_timestamp": trade.entry_timestamp.isoformat(),
                "exit_timestamp": trade.exit_timestamp.isoformat(),
                "side": trade.side,
                "entry_event_price": _canonical_execution_positive_number(
                    trade.entry_event_price,
                    "trade entry_event_price",
                ),
                "exit_event_price": _canonical_execution_positive_number(
                    trade.exit_event_price,
                    "trade exit_event_price",
                ),
                "entry_price": _canonical_execution_number(
                    trade.entry_price,
                    "trade entry_price",
                ),
                "exit_price": _canonical_execution_number(
                    trade.exit_price,
                    "trade exit_price",
                ),
                "quantity": _canonical_execution_number(
                    trade.quantity,
                    "trade quantity",
                ),
                "notional": _canonical_execution_number(
                    trade.notional,
                    "trade notional",
                ),
                "borrow_cost": _canonical_execution_number(
                    trade.borrow_cost,
                    "trade borrow_cost",
                ),
                "pnl": _canonical_execution_number(trade.pnl, "trade pnl"),
                "security_id": trade.security_id,
            }
        )
    return {"daily": daily, "trades": trades}


def _canonical_execution_number(value: float, name: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"H9 raw execution {name} must be finite")
    return number


def _canonical_execution_positive_number(value: float, name: str) -> float:
    number = _canonical_execution_number(value, name)
    if number <= 0.0:
        raise ValueError(f"H9 raw execution {name} must be positive")
    return number


def _quarterly_compounded_returns(
    daily_results: Sequence[DayTradeDailyResult],
) -> dict[str, float]:
    grouped: Dict[str, List[float]] = {}
    for item in daily_results:
        quarter = f"{item.day.year}-Q{((item.day.month - 1) // 3) + 1}"
        grouped.setdefault(quarter, []).append(float(item.return_pct))
    output = {}
    for quarter, returns in sorted(grouped.items()):
        growth = 1.0
        for value in returns:
            growth *= 1.0 + value
        output[quarter] = growth - 1.0
    return output


def _profit_concentration(trades: Sequence[DayTradeTrade]) -> dict:
    pnl_by_security: Dict[str, float] = {}
    for trade in trades:
        identity = trade.security_id or trade.symbol
        pnl_by_security[identity] = pnl_by_security.get(identity, 0.0) + trade.pnl
    positive_profit = {
        identity: pnl
        for identity, pnl in pnl_by_security.items()
        if pnl > 0.0
    }
    gross_positive_profit = sum(positive_profit.values())
    if gross_positive_profit <= 0.0:
        return {
            "available": False,
            "reason": "fixed candidate has no positive OOS security profit",
            "gross_positive_profit": gross_positive_profit,
            "profit_by_security_id": dict(sorted(pnl_by_security.items())),
            "top_symbol_profit_contribution": None,
            "top_two_profit_contribution": None,
        }
    ranked = sorted(positive_profit.values(), reverse=True)
    return {
        "available": True,
        "reason": None,
        "gross_positive_profit": gross_positive_profit,
        "profit_by_security_id": dict(sorted(pnl_by_security.items())),
        "top_symbol_profit_contribution": ranked[0] / gross_positive_profit,
        "top_two_profit_contribution": sum(ranked[:2]) / gross_positive_profit,
    }


def _validated_historical_trial_returns(
    artifact: Mapping[str, object],
    *,
    registry: ExperimentRegistry,
    observation_dates: Sequence[date],
    initial_capital: float,
    evaluation_dataset_hash: Optional[str],
):
    if not isinstance(artifact, Mapping):
        raise ValueError("historical trial artifact must be a mapping")
    payload = dict(artifact)
    signature = payload.pop("signature", None)
    content_hash = payload.pop("content_hash", None)
    permit_dataset_hash = payload.pop("permit_dataset_hash", None)
    if payload.get("artifact_version") != HISTORICAL_TRIAL_ARTIFACT_VERSION:
        raise ValueError(
            "historical trial artifact_version must be "
            f"{HISTORICAL_TRIAL_ARTIFACT_VERSION}"
        )
    if not evaluation_dataset_hash or permit_dataset_hash != evaluation_dataset_hash:
        raise ValueError("historical trial artifact does not match permit dataset identity")
    if content_hash != _hash_payload_for_artifact(payload):
        raise ValueError("historical trial artifact content_hash mismatch")
    issuer = payload.get("issuer")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError("historical trial artifact requires issuer")
    trusted_keys = _trusted_experiment_artifact_keys()
    trusted_key = trusted_keys.get(issuer)
    if trusted_key is None:
        raise ValueError("trusted historical experiment issuer key is missing")
    if not isinstance(signature, str) or not hmac.compare_digest(
        hmac.new(
            trusted_key,
            str(content_hash).encode("ascii"),
            hashlib.sha256,
        ).hexdigest(),
        signature,
    ):
        raise ValueError("historical trial artifact signature is invalid")
    historical_identity = {
        name: payload.get(name)
        for name in (
            "source_dataset_hash",
            "minute_data_hash",
            "calendar_hash",
            "construction_evidence_hash",
        )
    }
    for name, value in historical_identity.items():
        _artifact_sha256_digest(value, f"historical trial artifact {name}")
    expected_historical_dataset_hash = _hash_payload_for_artifact(
        historical_identity
    )
    if (
        payload.get("historical_evaluation_dataset_hash")
        != expected_historical_dataset_hash
    ):
        raise ValueError(
            "historical trial artifact evaluation dataset identity mismatch"
        )
    raw_dates = payload.get("observation_dates")
    if not isinstance(raw_dates, list) or any(not isinstance(item, str) for item in raw_dates):
        raise ValueError("historical trial observation_dates must be an ISO date array")
    dates = tuple(date.fromisoformat(item) for item in raw_dates)
    if dates != tuple(observation_dates) or dates != tuple(sorted(set(dates))):
        raise ValueError("historical trial dates must align exactly with fixed H9 OOS dates")

    local_ids = {
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    }
    historical_records = tuple(
        record
        for record in registry.known_records
        if not set(record.trial_ids).intersection(local_ids)
    )
    expected_ids = {
        trial_id
        for record in historical_records
        for trial_id in record.trial_ids
    }
    experiment_by_trial = {
        trial_id: record.experiment_id
        for record in historical_records
        for trial_id in record.trial_ids
    }
    completed = payload.get("completed_trial_ids")
    if (
        not isinstance(completed, list)
        or len(completed) != len(set(completed))
        or set(completed) != expected_ids
    ):
        raise ValueError("historical completed_trial_ids must exactly cover H1-H8")
    raw_returns = payload.get("trial_daily_returns")
    if not isinstance(raw_returns, dict) or set(raw_returns) != expected_ids:
        raise ValueError("historical trial_daily_returns must exactly cover H1-H8")
    execution_evidence = payload.get("trial_execution_evidence")
    if not isinstance(execution_evidence, dict) or set(execution_evidence) != expected_ids:
        raise ValueError(
            "historical trial execution evidence must exactly cover H1-H8"
        )
    validate_historical_trial_execution_proofs(
        execution_evidence,
        completed_trial_ids=tuple(sorted(expected_ids)),
        experiment_by_trial=experiment_by_trial,
        observation_dates=raw_dates,
        trial_daily_returns=raw_returns,
        historical_dataset_hash=expected_historical_dataset_hash,
        trusted_keys=trusted_keys,
    )

    historical_daily = {}
    for trial_id in sorted(expected_ids):
        values = raw_returns[trial_id]
        if not isinstance(values, list) or len(values) != len(dates):
            raise ValueError("historical trial return lengths must align with OOS dates")
        try:
            returns = tuple(float(value) for value in values)
        except (TypeError, ValueError) as error:
            raise ValueError("historical trial returns must be numeric") from error
        if any(not isfinite(value) or value <= -1.0 for value in returns):
            raise ValueError("historical trial returns must be finite and greater than -1")
        equity = float(initial_capital)
        daily_results = []
        for trading_day, return_pct in zip(dates, returns):
            pnl = equity * return_pct
            equity += pnl
            daily_results.append(
                DayTradeDailyResult(
                    strategy="daytrade_historical_trial",
                    day=trading_day,
                    trades=0,
                    pnl=pnl,
                    return_pct=return_pct,
                    ending_equity=equity,
                )
            )
        historical_daily[trial_id] = daily_results
    completed_pairs = tuple(
        (experiment_by_trial[trial_id], trial_id)
        for trial_id in sorted(expected_ids)
    )
    return historical_daily, completed_pairs, {
        "available": True,
        "content_hash": content_hash,
        "issuer": issuer,
        "signature": signature,
        "permit_dataset_hash": permit_dataset_hash,
        "historical_evaluation_dataset_hash": expected_historical_dataset_hash,
        "completed_trial_count": len(completed_pairs),
        "observation_count": len(dates),
    }


def _hash_payload_for_artifact(payload) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _trusted_experiment_artifact_keys() -> dict[str, bytes]:
    raw = os.environ.get("STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON", "").strip()
    if not raw:
        raise ValueError(
            "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON must configure trusted issuer keys"
        )
    try:
        payload = json.loads(raw)
        keys = {
            str(issuer): bytes.fromhex(value)
            for issuer, value in payload.items()
        }
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(
            "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON must be a JSON object of hex keys"
        ) from error
    if not keys or any(len(key) < 16 for key in keys.values()):
        raise ValueError(
            "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON keys must contain at least 16 bytes"
        )
    return keys


def _artifact_sha256_digest(value, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _daytrade_statistical_validation(
    selected_daily: Sequence[DayTradeDailyResult],
    trial_daily: Dict[str, Sequence[DayTradeDailyResult]],
    *,
    global_declared_trial_count: int | None = None,
) -> dict:
    """Build an auditable, aligned OOS matrix for DSR and CSCV/PBO."""

    observation_dates = tuple(item.day for item in selected_daily)
    selected_returns = tuple(float(item.return_pct) for item in selected_daily)
    payload = {
        "observation_dates": [item.isoformat() for item in observation_dates],
        "selected_daily_returns": list(selected_returns),
        "trial_daily_returns": {},
        "trial_sharpes_annual": {},
        "deflated_sharpe_ratio": {"available": False},
        "probability_of_backtest_overfitting": {"available": False},
    }
    local_declared_trials = max(len(trial_daily) - 1, 0)
    global_declared_trials = (
        local_declared_trials
        if global_declared_trial_count is None
        else int(global_declared_trial_count)
    )
    if global_declared_trials < local_declared_trials:
        raise ValueError("global_declared_trial_count cannot omit local trials")
    payload["local_declared_trial_count"] = local_declared_trials
    payload["global_declared_trial_count"] = global_declared_trials
    if not observation_dates:
        payload["reason"] = "no validation observations"
        payload["available"] = False
        return payload

    aligned_returns = {}
    for name, daily_results in sorted(trial_daily.items()):
        dates = tuple(item.day for item in daily_results)
        if dates != observation_dates:
            payload["reason"] = "trial validation dates are not aligned: %s" % name
            payload["available"] = False
            return payload
        returns = tuple(float(item.return_pct) for item in daily_results)
        aligned_returns[name] = returns
        payload["trial_daily_returns"][name] = list(returns)
        payload["trial_sharpes_annual"][name] = _sharpe(returns)

    if global_declared_trials > local_declared_trials:
        reason = (
            "immutable aligned H1-H8 return histories are missing; "
            "global DSR/PBO cannot reset at H9"
        )
        payload["deflated_sharpe_ratio"] = {
            "available": False,
            "reason": reason,
            "trial_count": global_declared_trials,
        }
        payload["probability_of_backtest_overfitting"] = {
            "available": False,
            "reason": reason,
            "strategy_count": global_declared_trials + 1,
        }
        payload["reason"] = reason
        payload["available"] = False
        return payload

    try:
        dsr = deflated_sharpe_ratio(
            selected_returns,
            tuple(payload["trial_sharpes_annual"].values()),
            periods_per_year=252,
        )
    except ValueError as error:
        payload["deflated_sharpe_ratio"] = {
            "available": False,
            "reason": str(error),
        }
    else:
        payload["deflated_sharpe_ratio"] = {
            "available": True,
            **asdict(dsr),
        }

    partition = _daytrade_cscv_partition(aligned_returns)
    if partition is None:
        payload["probability_of_backtest_overfitting"] = {
            "available": False,
            "reason": "OOS observations cannot form equal even CSCV folds",
        }
    else:
        try:
            pbo = cscv_probability_of_backtest_overfitting(
                partition.trial_returns,
                folds=partition.folds,
            )
        except ValueError as error:
            payload["probability_of_backtest_overfitting"] = {
                "available": False,
                "reason": str(error),
            }
        else:
            payload["probability_of_backtest_overfitting"] = {
                "available": True,
                # CSCV drops the remainder; report it so a trimmed sample is
                # never mistaken for the full observation set.
                "dropped_observations": partition.dropped_observations,
                "observation_dates": len(observation_dates),
                **asdict(pbo),
            }

    payload["available"] = bool(
        payload["deflated_sharpe_ratio"]["available"]
        and payload["probability_of_backtest_overfitting"]["available"]
    )
    return payload


DAYTRADE_CSCV_FOLD_PREFERENCE = (8, 6, 4, 2)


def _daytrade_cscv_partition(aligned_returns):
    """Select DayTrade CSCV folds, trimming the remainder explicitly.

    Requiring the raw OOS observation count to already divide into equal even
    folds switched the PBO gate off whenever that count was odd.  CSCV drops
    the remainder anyway, so trim it and report how much was dropped.
    """

    try:
        return cscv_partition(
            aligned_returns,
            fold_preference=DAYTRADE_CSCV_FOLD_PREFERENCE,
        )
    except ValueError:
        return None


def _candidate_requires_features(name: str) -> bool:
    return name.startswith((
        "daytrade-h3-pre-entry-confirmation:",
        "daytrade-h4-risk-scale:",
        "daytrade-h6-market-confirmation:",
        "daytrade-h7-rvol-entry-window:",
        "daytrade-h8-rvol-risk-scale:",
        "daytrade-h9-rvol-reward-risk:",
    ))


def _feature_coverage_complete(
    bars: Sequence[MinuteBar],
    feature_snapshots: Sequence[IntradayFeatureSnapshot],
    start: date,
    end: date,
    *,
    entry_start: time,
    entry_end: time,
    universe_schedule: Optional[Mapping[date, DailyUniverseSnapshot]] = None,
) -> bool:
    if not feature_snapshots:
        return False
    required = {
        (bar.symbol.upper(), bar.timestamp)
        for bar in bars
        if start <= _local_day(bar.timestamp) <= end
        and entry_start <= _local_time(bar.timestamp) < entry_end
        and (
            universe_schedule is None
            or (
                _local_day(bar.timestamp) in universe_schedule
                and bar.symbol.upper()
                in universe_schedule[_local_day(bar.timestamp)].active_tickers
            )
        )
    }
    provided = {
        (snapshot.symbol, snapshot.observed_at)
        for snapshot in feature_snapshots
        if start <= _local_day(snapshot.observed_at) <= end
    }
    return bool(required) and required.issubset(provided)


def _run_candidate_backtest(
    name: str,
    config: DayTradeConfig,
    bars: Sequence[MinuteBar],
    feature_snapshots: Sequence[IntradayFeatureSnapshot],
    universe_schedule: Optional[DayTradeUniverseSchedule] = None,
) -> DayTradeBacktestResult:
    if not _candidate_requires_features(name):
        if universe_schedule is None:
            return DayTradeBacktester(config).run(bars)
        return DayTradeBacktester(config).run(
            bars,
            universe_schedule=universe_schedule,
        )
    rvol_only = name.startswith(
        (
            "daytrade-h3-pre-entry-confirmation:",
            "daytrade-h9-rvol-reward-risk:",
        )
    )
    hypothesis = DayTradeHypothesisConfig(
        min_relative_volume=(
            config.min_relative_volume
            if name.startswith((
                "daytrade-h3-pre-entry-confirmation:",
                "daytrade-h7-rvol-entry-window:",
                "daytrade-h8-rvol-risk-scale:",
                "daytrade-h9-rvol-reward-risk:",
            ))
            else 0.0
        ),
        min_directional_spy_return=(
            -1.0 if rvol_only else config.min_directional_spy_return
        ),
        min_directional_qqq_return=(
            -1.0 if rvol_only else config.min_directional_qqq_return
        ),
        min_directional_sector_strength=(
            -1.0 if rvol_only else config.min_directional_sector_strength
        ),
    )
    run_kwargs = {"feature_snapshots": feature_snapshots}
    if universe_schedule is not None:
        run_kwargs["universe_schedule"] = universe_schedule
    return DayTradeBacktester(
        config,
        hypothesis_config=hypothesis,
    ).run(
        bars,
        **run_kwargs,
    )


def _daytrade_candidate_configs(base_config: DayTradeConfig) -> List[Tuple[str, DayTradeConfig]]:
    registry = preregistered_daytrade_experiments()
    candidates = [("baseline", base_config)]
    variants = {
        "daytrade-h1-session-vwap": (
            replace(base_config, cumulative_vwap=False, vwap_exit_bars=0),
            replace(base_config, cumulative_vwap=True, vwap_exit_bars=3, vwap_hysteresis_bps=0.0),
            replace(base_config, cumulative_vwap=True, vwap_exit_bars=3, vwap_hysteresis_bps=5.0),
        ),
        "daytrade-h2-opening-window": tuple(
            replace(base_config, entry_end=value)
            for value in ("10:00", "10:30", "11:00")
        ),
        "daytrade-h3-pre-entry-confirmation": tuple(
            replace(base_config, min_relative_volume=value)
            for value in (1.0, 1.5, 2.0)
        ),
        "daytrade-h4-risk-scale": tuple(
            replace(base_config, stop_basis=value)
            for value in ("fixed_pct", "atr", "opening_range")
        ),
    }
    for record in registry.records:
        for trial_id, config in zip(record.trial_ids, variants[record.experiment_id]):
            candidates.append((trial_id, config))
    return candidates


def daytrade_followup_candidate_configs(
    base_config: DayTradeConfig = DayTradeConfig(),
) -> List[Tuple[str, DayTradeConfig]]:
    """Build only preregistered follow-up candidates, never optimizer inputs."""

    registry = preregistered_daytrade_followup_experiments()
    configs = (
        replace(
            base_config,
            symbol_filter="opening-range",
            max_active_symbols=4,
            require_positive_opening_momentum=True,
        ),
    )
    trial_ids = tuple(
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    )
    if len(trial_ids) != len(configs):
        raise RuntimeError("daytrade follow-up registry and configs are inconsistent")
    return list(zip(trial_ids, configs))


def daytrade_h6_candidate_configs(
    base_config: DayTradeConfig = DayTradeConfig(),
) -> List[Tuple[str, DayTradeConfig]]:
    """Build the three preregistered SPY/QQQ confirmation thresholds."""

    registry = preregistered_daytrade_h6_experiments()
    thresholds = (0.0, 0.0005, 0.001)
    configs = tuple(
        replace(
            base_config,
            min_directional_spy_return=threshold,
            min_directional_qqq_return=threshold,
        )
        for threshold in thresholds
    )
    trial_ids = tuple(
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    )
    if len(trial_ids) != len(configs):
        raise RuntimeError("daytrade H6 registry and configs are inconsistent")
    return list(zip(trial_ids, configs))


def daytrade_h3_candidate_configs(
    base_config: DayTradeConfig = DayTradeConfig(),
) -> List[Tuple[str, DayTradeConfig]]:
    """Build only the original three preregistered RVOL thresholds."""

    registry = preregistered_daytrade_h3_experiments()
    configs = tuple(
        replace(base_config, min_relative_volume=threshold)
        for threshold in (1.0, 1.5, 2.0)
    )
    trial_ids = tuple(
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    )
    if len(trial_ids) != len(configs):
        raise RuntimeError("daytrade H3 registry and configs are inconsistent")
    return list(zip(trial_ids, configs))


def daytrade_h7_candidate_configs(
    base_config: DayTradeConfig = DayTradeConfig(),
) -> List[Tuple[str, DayTradeConfig]]:
    """Build the two frozen RVOL-2 entry-window interaction trials."""

    registry = preregistered_daytrade_h7_experiments()
    configs = tuple(
        replace(
            base_config,
            min_relative_volume=2.0,
            entry_end=entry_end,
        )
        for entry_end in ("11:00", "11:30")
    )
    trial_ids = tuple(
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    )
    if len(trial_ids) != len(configs):
        raise RuntimeError("daytrade H7 registry and configs are inconsistent")
    return list(zip(trial_ids, configs))


def daytrade_h8_candidate_configs(
    base_config: DayTradeConfig = DayTradeConfig(),
) -> List[Tuple[str, DayTradeConfig]]:
    """Build the two frozen RVOL-2 volatility-scaled stop trials."""

    registry = preregistered_daytrade_h8_experiments()
    configs = tuple(
        replace(
            base_config,
            min_relative_volume=2.0,
            stop_basis=stop_basis,
        )
        for stop_basis in ("atr", "opening_range")
    )
    trial_ids = tuple(
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    )
    if len(trial_ids) != len(configs):
        raise RuntimeError("daytrade H8 registry and configs are inconsistent")
    return list(zip(trial_ids, configs))


def daytrade_h9_candidate_configs(
    base_config: DayTradeConfig = DayTradeConfig(
        min_relative_volume=2.0,
        reward_risk=1.5,
    ),
) -> List[Tuple[str, DayTradeConfig]]:
    """Build the single frozen 2R trial without changing any other field."""

    if not isinstance(base_config, DayTradeConfig):
        raise TypeError("base_config must be a DayTradeConfig")
    if base_config.min_relative_volume != 2.0:
        raise ValueError("daytrade H9 baseline must use min_relative_volume=2.0")
    if base_config.reward_risk != 1.5:
        raise ValueError("daytrade H9 baseline must use reward_risk=1.5")
    registry = preregistered_daytrade_h9_experiments()
    candidate = replace(base_config, reward_risk=2.0)
    changed_fields = tuple(
        item.name
        for item in fields(DayTradeConfig)
        if getattr(base_config, item.name) != getattr(candidate, item.name)
    )
    if changed_fields != ("reward_risk",):
        raise RuntimeError(
            "daytrade H9 candidate must differ only in reward_risk"
        )
    trial_ids = tuple(
        trial_id
        for record in registry.records
        for trial_id in record.trial_ids
    )
    if len(trial_ids) != 1:
        raise RuntimeError("daytrade H9 registry must declare exactly one trial")
    return [(trial_ids[0], candidate)]


def _sample_candidates(
    candidates: Sequence[Tuple[str, DayTradeConfig]],
    max_candidates: int,
) -> List[Tuple[str, DayTradeConfig]]:
    if max_candidates <= 0 or len(candidates) <= max_candidates:
        return list(candidates)
    if max_candidates == 1:
        return [candidates[0]]
    step = (len(candidates) - 1) / float(max_candidates - 1)
    indexes = sorted({int(round(step * item)) for item in range(max_candidates)})
    return [candidates[index] for index in indexes]


def _normalize_universe_schedule(
    schedule: Optional[DayTradeUniverseSchedule],
) -> Optional[Dict[date, DailyUniverseSnapshot]]:
    if schedule is None:
        return None
    if isinstance(schedule, Mapping):
        entries = tuple(schedule.items())
    else:
        if isinstance(schedule, (str, bytes)):
            raise TypeError("universe_schedule must contain DailyUniverseSnapshot values")
        try:
            snapshots = tuple(schedule)
        except TypeError as error:
            raise TypeError(
                "universe_schedule must be a sequence or date mapping"
            ) from error
        entries = tuple((None, snapshot) for snapshot in snapshots)
    output: Dict[date, DailyUniverseSnapshot] = {}
    for trading_day, snapshot in entries:
        if not isinstance(snapshot, DailyUniverseSnapshot):
            raise TypeError(
                "universe_schedule must contain DailyUniverseSnapshot values"
            )
        if trading_day is None:
            trading_day = snapshot.trading_day
        if isinstance(trading_day, datetime) or not isinstance(trading_day, date):
            raise ValueError("universe schedule keys must be dates")
        if snapshot.trading_day != trading_day:
            raise ValueError("universe schedule key does not match snapshot trading_day")
        if trading_day in output:
            raise ValueError("duplicate daily universe snapshot")
        output[trading_day] = snapshot
    return output


def _require_universe_schedule_coverage(
    schedule: Optional[Mapping[date, DailyUniverseSnapshot]],
    trading_days: Iterable[date],
) -> None:
    if schedule is None:
        return
    missing_days = sorted(set(trading_days).difference(schedule))
    if missing_days:
        raise ValueError(
            "universe schedule is missing trading days: "
            + ",".join(item.isoformat() for item in missing_days)
        )


def _bars_in_range(bars: Sequence[MinuteBar], start: date, end: date) -> List[MinuteBar]:
    return [
        bar
        for bar in bars
        if start <= _local_day(bar.timestamp) <= end
    ]


def _is_fragile(trades: Sequence[DayTradeTrade]) -> bool:
    total_positive = sum(trade.pnl for trade in trades if trade.pnl > 0)
    if total_positive <= 0:
        return True
    month_pnl = _pnl_by_month(trades)
    symbol_pnl = _pnl_by_symbol(trades)
    top_month = max([value for value in month_pnl.values() if value > 0] or [0.0])
    top_symbol = max([value["pnl"] for value in symbol_pnl.values() if value["pnl"] > 0] or [0.0])
    return top_month / total_positive > 0.70 or top_symbol / total_positive > 0.70


def _pnl_by_month(trades: Sequence[DayTradeTrade]) -> Dict[str, float]:
    output: Dict[str, float] = {}
    for trade in trades:
        key = "%04d-%02d" % (trade.day.year, trade.day.month)
        output[key] = output.get(key, 0.0) + trade.pnl
    return output


def _pnl_by_symbol(trades: Sequence[DayTradeTrade]) -> Dict[str, dict]:
    output: Dict[str, dict] = {}
    for trade in trades:
        item = output.setdefault(trade.symbol, {"pnl": 0.0, "trades": 0, "wins": 0, "win_rate": 0.0})
        item["pnl"] += trade.pnl
        item["trades"] += 1
        if trade.pnl > 0:
            item["wins"] += 1
    for item in output.values():
        item["win_rate"] = item["wins"] / item["trades"] if item["trades"] else 0.0
    return output


def _pnl_by_security_id(trades: Sequence[DayTradeTrade]) -> Dict[str, dict]:
    output: Dict[str, dict] = {}
    for trade in trades:
        stable_id = trade.security_id or trade.symbol
        item = output.setdefault(
            stable_id,
            {"pnl": 0.0, "trades": 0, "wins": 0, "win_rate": 0.0},
        )
        item["pnl"] += trade.pnl
        item["trades"] += 1
        if trade.pnl > 0:
            item["wins"] += 1
    for item in output.values():
        item["win_rate"] = item["wins"] / item["trades"] if item["trades"] else 0.0
    return output


def _close_trade(
    signal: DayTradeSignal,
    bar: MinuteBar,
    exit_price: float,
    exit_reason: str,
    *,
    exit_event_price: float,
) -> DayTradeTrade:
    direction = 1.0 if signal.side == "long" else -1.0
    holding_minutes = (bar.timestamp - signal.timestamp).total_seconds() / 60.0
    gross_pnl = direction * (
        float(exit_price) - signal.entry_price
    ) * signal.quantity
    borrow_cost = 0.0
    if signal.side == "short" and signal.annualized_borrow_rate > 0:
        borrow_cost = (
            signal.notional
            * signal.annualized_borrow_rate
            * max(holding_minutes, 1.0)
            / (252.0 * 390.0)
        )
    pnl = gross_pnl - borrow_cost
    return DayTradeTrade(
        strategy=signal.strategy,
        symbol=signal.symbol,
        day=_local_day(signal.timestamp),
        entry_timestamp=signal.timestamp,
        exit_timestamp=bar.timestamp,
        side=signal.side,
        entry_price=signal.entry_price,
        exit_price=float(exit_price),
        quantity=signal.quantity,
        pnl=pnl,
        return_pct=pnl / signal.notional if signal.notional else 0.0,
        exit_reason=exit_reason,
        notional=signal.notional,
        holding_minutes=holding_minutes,
        borrow_cost=borrow_cost,
        security_id=signal.security_id,
        entry_event_price=signal.entry_event_price,
        exit_event_price=float(exit_event_price),
    )


def _open_position_from_signal(signal: DayTradeSignal) -> _OpenPosition:
    return _OpenPosition(
        signal=signal,
        current_stop=signal.stop_price,
        high_water=signal.entry_price,
        initial_risk=abs(signal.entry_price - signal.stop_price),
    )


def _position_with_favorable_water(position: _OpenPosition, price: float) -> _OpenPosition:
    high_water = (
        max(position.high_water, float(price))
        if position.signal.side == "long"
        else min(position.high_water, float(price))
    )
    if high_water == position.high_water:
        return position
    return _OpenPosition(
        signal=position.signal,
        current_stop=position.current_stop,
        high_water=high_water,
        initial_risk=position.initial_risk,
    )


def _entry_stop(
    config: DayTradeConfig,
    entry_price: float,
    opening_range: _OpeningRange,
    side: str,
    atr: Optional[float],
) -> float:
    if config.stop_basis == "fixed_pct":
        if side == "long":
            return max(
                entry_price * (1.0 - config.stop_loss_pct),
                opening_range.low,
            )
        return min(
            entry_price * (1.0 + config.stop_loss_pct),
            opening_range.high,
        )
    if atr is None:
        raise ValueError("volatility-scaled stop requires an entry-time ATR")
    multiplier = (
        config.atr_stop_multiple
        if config.stop_basis == "atr"
        else config.opening_range_stop_multiple
    )
    scaled = risk_scaled_stop(
        entry_price=entry_price,
        side=side,
        atr=atr,
        opening_range_width=opening_range.high - opening_range.low,
        basis=config.stop_basis,
        multiplier=multiplier,
    )
    return (
        max(scaled, opening_range.low)
        if side == "long"
        else min(scaled, opening_range.high)
    )


def _signal_open_risk(signal: DayTradeSignal) -> float:
    return abs(signal.entry_price - signal.stop_price) * signal.quantity


def _unrealized_position_pnl(
    position: _OpenPosition,
    latest_bar: Optional[MinuteBar],
) -> float:
    if latest_bar is None:
        return 0.0
    direction = 1.0 if position.signal.side == "long" else -1.0
    return direction * (
        float(latest_bar.close) - position.signal.entry_price
    ) * position.signal.quantity


def _normalize_minute_bar(bar: MinuteBar) -> MinuteBar:
    timestamp = bar.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return MinuteBar(
        symbol=bar.symbol.upper(),
        timestamp=timestamp,
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
        vwap=float(bar.vwap) if bar.vwap is not None else None,
    )


def _effective_vwap(bar: MinuteBar) -> float:
    return float(bar.vwap) if bar.vwap is not None else float(bar.close)


def _selected_vwap(
    bar: MinuteBar,
    session_vwap_by_event: Dict[Tuple[str, datetime], float],
) -> float:
    return session_vwap_by_event.get(
        (bar.symbol.upper(), bar.timestamp),
        _effective_vwap(bar),
    )


def _opening_range_pct(opening_range: _OpeningRange) -> float:
    return (opening_range.high - opening_range.low) / opening_range.low if opening_range.low > 0 else 0.0


def _opening_momentum(opening_range: _OpeningRange) -> float:
    return opening_range.last_close / opening_range.first_open - 1.0 if opening_range.first_open > 0 else 0.0


def _opening_candidate_score(opening_range: _OpeningRange) -> float:
    """Ranks simultaneous candidates using opening-window information only."""

    return (
        _opening_range_pct(opening_range)
        * max(opening_range.average_dollar_volume, 1.0)
        * (1.0 + max(_opening_momentum(opening_range), 0.0))
    )


def _local_day(timestamp: datetime) -> date:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return timestamp.astimezone(EASTERN).date()


def _local_time(timestamp: datetime) -> time:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=ZoneInfo("UTC"))
    return timestamp.astimezone(EASTERN).time().replace(tzinfo=None)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def _annualized_return(returns: Sequence[float], periods_per_year: int = 252) -> float:
    if not returns:
        return 0.0
    growth = 1.0
    for value in returns:
        growth *= 1.0 + float(value)
    if growth <= 0:
        return -1.0
    return growth ** (periods_per_year / len(returns)) - 1.0


def _sharpe(returns: Sequence[float], periods_per_year: int = 252) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / len(returns)
    stddev = variance ** 0.5
    return mean / stddev * (periods_per_year ** 0.5) if stddev else 0.0


def _max_drawdown_from_daily(daily_results: Sequence[DayTradeDailyResult]) -> float:
    if not daily_results:
        return 0.0
    peak = daily_results[0].ending_equity - daily_results[0].pnl
    max_drawdown = 0.0
    for item in daily_results:
        peak = max(peak, item.ending_equity)
        drawdown = 1.0 - item.ending_equity / peak if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown


def _average_holding_minutes(trades: Sequence[DayTradeTrade]) -> float:
    return sum(trade.holding_minutes for trade in trades) / len(trades) if trades else 0.0


def _overnight_count(trades: Sequence[DayTradeTrade]) -> int:
    return sum(1 for trade in trades if _local_day(trade.entry_timestamp) != _local_day(trade.exit_timestamp))


def _minute_bar_to_payload(bar: MinuteBar) -> dict:
    return {
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "vwap": bar.vwap,
    }


def _minute_bar_from_payload(item: dict) -> MinuteBar:
    return MinuteBar(
        symbol=str(item["symbol"]).upper(),
        timestamp=datetime.fromisoformat(str(item["timestamp"]).replace("Z", "+00:00")),
        open=float(item["open"]),
        high=float(item["high"]),
        low=float(item["low"]),
        close=float(item["close"]),
        volume=float(item.get("volume", 0.0)),
        vwap=float(item["vwap"]) if item.get("vwap") is not None else None,
    )


def _cache_safe_name(value: str) -> str:
    return str(value).replace("/", "_").replace(":", "_")


def _cache_provider_and_feed(source: str) -> Tuple[str, str]:
    base, separator, feed = str(source).partition(":")
    normalized = base.lower()
    if normalized.startswith("alpaca"):
        provider = "alpaca"
    elif normalized.startswith("polygon"):
        provider = "polygon"
    elif normalized.startswith("injected"):
        provider = "injected"
    else:
        provider = normalized or "unknown"
    return provider, feed if separator else ""


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".%s." % path.name,
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _atomic_write_pair(
    data_path: Path,
    data_content: str,
    manifest_path: Path,
    manifest_content: str,
) -> None:
    data_existed = data_path.exists()
    manifest_existed = manifest_path.exists()
    old_data = data_path.read_bytes() if data_existed else b""
    old_manifest = manifest_path.read_bytes() if manifest_existed else b""
    try:
        _atomic_write_text(data_path, data_content)
        _atomic_write_text(manifest_path, manifest_content)
    except Exception:
        _restore_atomic_file(data_path, data_existed, old_data)
        _restore_atomic_file(manifest_path, manifest_existed, old_manifest)
        raise


def _restore_atomic_file(path: Path, existed: bool, content: bytes) -> None:
    if not existed:
        if path.exists():
            path.unlink()
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix=".%s.rollback." % path.name,
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary = Path(file.name)
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _merge_coverage_ranges(ranges: Sequence[dict]) -> List[dict]:
    parsed = sorted(
        (date.fromisoformat(item["start"]), date.fromisoformat(item["end"]))
        for item in ranges
    )
    merged: List[Tuple[date, date]] = []
    for start, end in parsed:
        if start > end:
            continue
        if not merged or start > merged[-1][1] + timedelta(days=1):
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return [
        {"start": start.isoformat(), "end": end.isoformat()}
        for start, end in merged
    ]


def _ranges_cover(ranges: Sequence[dict], start: date, end: date) -> bool:
    cursor = start
    for item in _merge_coverage_ranges(ranges):
        range_start = date.fromisoformat(item["start"])
        range_end = date.fromisoformat(item["end"])
        if range_end < cursor:
            continue
        if range_start > cursor:
            return False
        cursor = range_end + timedelta(days=1)
        if cursor > end:
            return True
    return cursor > end


def _month_key(value: date) -> str:
    return "%04d-%02d" % (value.year, value.month)


def _month_keys_between(start: date, end: date) -> List[str]:
    keys = []
    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while current <= final:
        keys.append(_month_key(current))
        current = _add_months(current, 1)
    return keys


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, 1)


def _month_end(value: date) -> date:
    next_month = _add_months(date(value.year, value.month, 1), 1)
    return next_month - timedelta(days=1)


def _trade_to_payload(trade: DayTradeTrade) -> dict:
    payload = {
        "strategy": trade.strategy,
        "symbol": trade.symbol,
        "day": trade.day.isoformat(),
        "entry_timestamp": trade.entry_timestamp.isoformat(),
        "exit_timestamp": trade.exit_timestamp.isoformat(),
        "side": trade.side,
        "entry_event_price": trade.entry_event_price,
        "exit_event_price": trade.exit_event_price,
        "entry_price": trade.entry_price,
        "exit_price": trade.exit_price,
        "quantity": trade.quantity,
        "pnl": trade.pnl,
        "return_pct": trade.return_pct,
        "exit_reason": trade.exit_reason,
        "notional": trade.notional,
        "holding_minutes": trade.holding_minutes,
        "borrow_cost": trade.borrow_cost,
    }
    if trade.security_id:
        payload["security_id"] = trade.security_id
    return payload


def _trade_from_payload(item: dict) -> DayTradeTrade:
    return DayTradeTrade(
        strategy=item["strategy"],
        symbol=item["symbol"],
        day=date.fromisoformat(item["day"]),
        entry_timestamp=datetime.fromisoformat(item["entry_timestamp"]),
        exit_timestamp=datetime.fromisoformat(item["exit_timestamp"]),
        side=item["side"],
        entry_event_price=float(item.get("entry_event_price", 0.0)),
        exit_event_price=float(item.get("exit_event_price", 0.0)),
        entry_price=float(item["entry_price"]),
        exit_price=float(item["exit_price"]),
        quantity=float(item["quantity"]),
        pnl=float(item["pnl"]),
        return_pct=float(item["return_pct"]),
        exit_reason=item["exit_reason"],
        notional=float(item["notional"]),
        holding_minutes=float(item["holding_minutes"]),
        borrow_cost=float(item.get("borrow_cost", 0.0)),
        security_id=item.get("security_id", ""),
    )


def _daily_to_payload(result: DayTradeDailyResult) -> dict:
    return {
        "strategy": result.strategy,
        "day": result.day.isoformat(),
        "trades": result.trades,
        "pnl": result.pnl,
        "return_pct": result.return_pct,
        "ending_equity": result.ending_equity,
        "errors": result.errors,
        "stopped_reason": result.stopped_reason,
    }


def _daily_from_payload(item: dict) -> DayTradeDailyResult:
    return DayTradeDailyResult(
        strategy=item["strategy"],
        day=date.fromisoformat(item["day"]),
        trades=int(item["trades"]),
        pnl=float(item["pnl"]),
        return_pct=float(item["return_pct"]),
        ending_equity=float(item["ending_equity"]),
        errors=int(item.get("errors", 0)),
        stopped_reason=item.get("stopped_reason", ""),
    )


def _format_float(value: float) -> str:
    if isinf(value):
        return "inf"
    return "%.6f" % value
