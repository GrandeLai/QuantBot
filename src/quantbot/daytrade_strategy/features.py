"""Point-in-time construction and lookup for intraday feature snapshots."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from math import isfinite, sqrt
from types import MappingProxyType
from typing import Iterable, Mapping, Protocol
from zoneinfo import ZoneInfo

from .domain import IntradayFeatureSnapshot, aware


EASTERN = ZoneInfo("US/Eastern")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
PREMARKET_OPEN = time(4, 0)
REGULAR_MINUTES = 390

DEFAULT_DAYTRADE_SECTOR_PROXIES = MappingProxyType(
    {
        "AAPL": "XLK",
        "MSFT": "XLK",
        "NVDA": "SMH",
        "AMZN": "XLY",
        "META": "XLC",
        "GOOGL": "XLC",
        "AMD": "SMH",
        "TSLA": "XLY",
        "AVGO": "SMH",
        "NFLX": "XLC",
    }
)


@dataclass(frozen=True, slots=True)
class IntradayFeatureRequirements:
    """Select which point-in-time features a hypothesis actually consumes.

    The default keeps the historical full-feature behavior.  RVOL-only trials
    can opt out of market/proxy inputs and the cross-symbol correlation scan.
    Unrequested snapshot fields receive neutral values and must not be used by
    the corresponding hypothesis.
    """

    atr: bool = True
    relative_volume: bool = True
    gap_fraction: bool = True
    premarket_volume: bool = True
    spy_return: bool = True
    qqq_return: bool = True
    sector_relative_strength: bool = True
    max_abs_correlation: bool = True

    def __post_init__(self) -> None:
        for name in (
            "atr",
            "relative_volume",
            "gap_fraction",
            "premarket_volume",
            "spy_return",
            "qqq_return",
            "sector_relative_strength",
            "max_abs_correlation",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} requirement must be a boolean")

    @classmethod
    def all(cls) -> "IntradayFeatureRequirements":
        return cls()

    @classmethod
    def rvol_only(cls) -> "IntradayFeatureRequirements":
        return cls(
            atr=False,
            relative_volume=True,
            gap_fraction=False,
            premarket_volume=False,
            spy_return=False,
            qqq_return=False,
            sector_relative_strength=False,
            max_abs_correlation=False,
        )

    @classmethod
    def relative_volume_only(cls) -> "IntradayFeatureRequirements":
        return cls.rvol_only()

    @classmethod
    def for_candidate(cls, candidate_name: str) -> "IntradayFeatureRequirements":
        if not isinstance(candidate_name, str) or not candidate_name.strip():
            raise ValueError("candidate_name must be a non-empty string")
        name = candidate_name.strip()
        if name.startswith(
            (
                "daytrade-h3-pre-entry-confirmation:",
                "daytrade-h9-rvol-reward-risk:",
            )
        ):
            return cls.rvol_only()
        return cls()

    @property
    def requires_market_symbols(self) -> bool:
        return self.spy_return or self.qqq_return

    @property
    def requires_sector_proxies(self) -> bool:
        return self.sector_relative_strength

    def to_payload(self) -> dict[str, bool]:
        return {
            "atr": self.atr,
            "relative_volume": self.relative_volume,
            "gap_fraction": self.gap_fraction,
            "premarket_volume": self.premarket_volume,
            "spy_return": self.spy_return,
            "qqq_return": self.qqq_return,
            "sector_relative_strength": self.sector_relative_strength,
            "max_abs_correlation": self.max_abs_correlation,
        }


class IntradayBar(Protocol):
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class IntradayFeatureBuildConfig:
    """Immutable inputs for deterministic point-in-time feature generation."""

    symbols: tuple[str, ...]
    sector_proxy_by_symbol: Mapping[str, str] = field(default_factory=dict)
    spy_symbol: str = "SPY"
    qqq_symbol: str = "QQQ"
    atr_lookback_sessions: int = 14
    relative_volume_lookback_sessions: int = 20
    correlation_lookback_sessions: int = 20
    observation_start: time = time(9, 45)
    observation_end: time = time(11, 0)
    requirements: IntradayFeatureRequirements = field(
        default_factory=IntradayFeatureRequirements
    )

    def __post_init__(self) -> None:
        symbols = tuple(_normalized_symbol(value, "symbols") for value in self.symbols)
        if not symbols:
            raise ValueError("symbols must not be empty")
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique")
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "spy_symbol", _normalized_symbol(self.spy_symbol, "spy_symbol"))
        object.__setattr__(self, "qqq_symbol", _normalized_symbol(self.qqq_symbol, "qqq_symbol"))
        if not isinstance(self.requirements, IntradayFeatureRequirements):
            raise TypeError("requirements must be an IntradayFeatureRequirements")
        proxies = {
            _normalized_symbol(symbol, "sector proxy symbol"): _normalized_symbol(
                proxy,
                "sector proxy value",
            )
            for symbol, proxy in self.sector_proxy_by_symbol.items()
        }
        missing = [
            symbol
            for symbol in symbols
            if self.requirements.requires_sector_proxies and symbol not in proxies
        ]
        if missing:
            raise ValueError(
                "sector proxy mapping required for: " + ",".join(sorted(missing))
            )
        object.__setattr__(self, "sector_proxy_by_symbol", MappingProxyType(proxies))
        for name in (
            "atr_lookback_sessions",
            "relative_volume_lookback_sessions",
            "correlation_lookback_sessions",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not isinstance(self.observation_start, time) or not isinstance(
            self.observation_end,
            time,
        ):
            raise ValueError("observation_start and observation_end must be times")
        if not REGULAR_OPEN <= self.observation_start < self.observation_end <= REGULAR_CLOSE:
            raise ValueError("feature observation window must be inside regular hours")

    @property
    def required_symbols(self) -> tuple[str, ...]:
        values = list(self.symbols)
        if self.requirements.spy_return:
            values.append(self.spy_symbol)
        if self.requirements.qqq_return:
            values.append(self.qqq_symbol)
        if self.requirements.requires_sector_proxies:
            values.extend(
                self.sector_proxy_by_symbol[symbol] for symbol in self.symbols
            )
        return tuple(dict.fromkeys(values))

    @property
    def warmup_sessions(self) -> int:
        values = [0]
        if self.requirements.atr:
            values.append(self.atr_lookback_sessions)
        if self.requirements.relative_volume:
            values.append(self.relative_volume_lookback_sessions)
        if self.requirements.gap_fraction:
            values.append(1)
        if self.requirements.max_abs_correlation:
            values.append(self.correlation_lookback_sessions + 1)
        return max(values)


@dataclass(frozen=True, slots=True)
class _FeatureBar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True, slots=True)
class _SessionData:
    day: date
    regular_bars: tuple[_FeatureBar, ...]
    regular_open: float
    regular_close: float
    regular_high: float
    regular_low: float
    premarket_volume: float
    cumulative_volume: tuple[float, ...]
    latest_close: tuple[float | None, ...]


class IntradayFeatureBook:
    """Latest-known same-session lookup; future and stale rows are invisible."""

    def __init__(self, snapshots: Iterable[IntradayFeatureSnapshot] = ()) -> None:
        grouped: dict[str, list[IntradayFeatureSnapshot]] = {}
        seen = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, IntradayFeatureSnapshot):
                raise TypeError("feature snapshots must contain IntradayFeatureSnapshot values")
            key = (snapshot.symbol, snapshot.observed_at)
            if key in seen:
                raise ValueError("duplicate intraday feature snapshot")
            seen.add(key)
            grouped.setdefault(snapshot.symbol, []).append(snapshot)
        self._snapshots = MappingProxyType(
            {
                symbol: tuple(sorted(values, key=lambda item: item.observed_at))
                for symbol, values in grouped.items()
            }
        )
        self._timestamps = MappingProxyType(
            {
                symbol: tuple(item.observed_at for item in values)
                for symbol, values in self._snapshots.items()
            }
        )

    def latest(self, symbol: str, observed_at) -> IntradayFeatureSnapshot | None:
        timestamp = aware(observed_at, "observed_at")
        normalized = str(symbol).strip().upper()
        values = self._snapshots.get(normalized, ())
        timestamps = self._timestamps.get(normalized, ())
        index = bisect_right(timestamps, timestamp) - 1
        if index < 0:
            return None
        snapshot = values[index]
        if _market_day(snapshot.observed_at) != _market_day(timestamp):
            return None
        return snapshot


def build_intraday_feature_snapshots(
    bars: Iterable[IntradayBar],
    config: IntradayFeatureBuildConfig,
) -> tuple[IntradayFeatureSnapshot, ...]:
    """Build prefix-stable snapshots using only data visible at each timestamp."""

    if not isinstance(config, IntradayFeatureBuildConfig):
        raise TypeError("config must be an IntradayFeatureBuildConfig")
    requirements = config.requirements
    sessions_by_symbol = _sessions_by_symbol(bars, set(config.required_symbols))
    returns_by_symbol = (
        {
            symbol: _daily_returns(sessions)
            for symbol, sessions in sessions_by_symbol.items()
        }
        if requirements.max_abs_correlation
        else {}
    )
    required_history = [0]
    if requirements.atr:
        required_history.append(config.atr_lookback_sessions)
    if requirements.relative_volume:
        required_history.append(config.relative_volume_lookback_sessions)
    if requirements.gap_fraction:
        required_history.append(1)
    minimum_history = max(required_history)
    snapshots = []
    for symbol in config.symbols:
        sessions = sessions_by_symbol.get(symbol, ())
        proxy_symbol = (
            config.sector_proxy_by_symbol[symbol]
            if requirements.sector_relative_strength
            else None
        )
        for index, session in enumerate(sessions):
            if index < minimum_history:
                continue
            spy_session = (
                _session_for_day(
                    sessions_by_symbol.get(config.spy_symbol, ()),
                    session.day,
                )
                if requirements.spy_return
                else None
            )
            qqq_session = (
                _session_for_day(
                    sessions_by_symbol.get(config.qqq_symbol, ()),
                    session.day,
                )
                if requirements.qqq_return
                else None
            )
            proxy_session = (
                _session_for_day(
                    sessions_by_symbol.get(proxy_symbol, ()),
                    session.day,
                )
                if proxy_symbol is not None
                else None
            )
            if (
                requirements.spy_return
                and spy_session is None
                or requirements.qqq_return
                and qqq_session is None
                or requirements.sector_relative_strength
                and proxy_session is None
            ):
                continue
            atr = (
                _prior_atr(sessions, index, config.atr_lookback_sessions)
                if requirements.atr
                else 1.0
            )
            correlation = (
                _maximum_prior_correlation(
                    symbol,
                    session.day,
                    config.symbols,
                    returns_by_symbol,
                    config.correlation_lookback_sessions,
                )
                if requirements.max_abs_correlation
                else 0.0
            )
            previous_close = (
                sessions[index - 1].regular_close
                if requirements.gap_fraction
                else None
            )
            for bar in session.regular_bars:
                observed_time = _market_time(bar.timestamp)
                if not config.observation_start <= observed_time < config.observation_end:
                    continue
                minute_index = _regular_minute_index(observed_time)
                relative_volume = (
                    _relative_volume(
                        sessions,
                        index,
                        minute_index,
                        config.relative_volume_lookback_sessions,
                    )
                    if requirements.relative_volume
                    else 0.0
                )
                spy_return = (
                    _session_return_at(spy_session, minute_index)
                    if requirements.spy_return and spy_session is not None
                    else 0.0
                )
                qqq_return = (
                    _session_return_at(qqq_session, minute_index)
                    if requirements.qqq_return and qqq_session is not None
                    else 0.0
                )
                proxy_return = (
                    _session_return_at(proxy_session, minute_index)
                    if requirements.sector_relative_strength
                    and proxy_session is not None
                    else 0.0
                )
                if (
                    requirements.relative_volume
                    and relative_volume is None
                    or requirements.spy_return
                    and spy_return is None
                    or requirements.qqq_return
                    and qqq_return is None
                    or requirements.sector_relative_strength
                    and proxy_return is None
                ):
                    continue
                sector_relative_strength = (
                    bar.close / session.regular_open - 1.0 - proxy_return
                    if requirements.sector_relative_strength
                    else 0.0
                )
                snapshots.append(
                    IntradayFeatureSnapshot(
                        symbol=symbol,
                        observed_at=bar.timestamp,
                        atr=atr,
                        relative_volume=relative_volume,
                        gap_fraction=(
                            session.regular_open / previous_close - 1.0
                            if previous_close is not None
                            else 0.0
                        ),
                        premarket_volume=(
                            session.premarket_volume
                            if requirements.premarket_volume
                            else 0.0
                        ),
                        spy_return=spy_return,
                        qqq_return=qqq_return,
                        sector_relative_strength=sector_relative_strength,
                        max_abs_correlation=correlation,
                    )
                )
    return tuple(sorted(snapshots, key=lambda item: (item.observed_at, item.symbol)))


def intraday_feature_config_to_payload(config: IntradayFeatureBuildConfig) -> dict:
    if not isinstance(config, IntradayFeatureBuildConfig):
        raise TypeError("config must be an IntradayFeatureBuildConfig")
    return {
        "symbols": list(config.symbols),
        "sector_proxy_by_symbol": dict(sorted(config.sector_proxy_by_symbol.items())),
        "spy_symbol": config.spy_symbol,
        "qqq_symbol": config.qqq_symbol,
        "atr_lookback_sessions": config.atr_lookback_sessions,
        "relative_volume_lookback_sessions": config.relative_volume_lookback_sessions,
        "correlation_lookback_sessions": config.correlation_lookback_sessions,
        "observation_start": config.observation_start.strftime("%H:%M"),
        "observation_end": config.observation_end.strftime("%H:%M"),
        "requirements": config.requirements.to_payload(),
        "required_symbols": list(config.required_symbols),
    }


def _sessions_by_symbol(
    bars: Iterable[IntradayBar],
    required_symbols: set[str],
) -> dict[str, tuple[_SessionData, ...]]:
    grouped: dict[tuple[str, date], list[_FeatureBar]] = {}
    seen = set()
    for raw_bar in bars:
        bar = _normalize_bar(raw_bar)
        if bar.symbol not in required_symbols:
            continue
        key = (bar.symbol, bar.timestamp)
        if key in seen:
            raise ValueError(
                "duplicate feature bar: %s %s" % (bar.symbol, bar.timestamp.isoformat())
            )
        seen.add(key)
        grouped.setdefault((bar.symbol, _market_day(bar.timestamp)), []).append(bar)
    output: dict[str, list[_SessionData]] = {}
    for (symbol, day), values in grouped.items():
        session = _build_session(day, values)
        if session is not None:
            output.setdefault(symbol, []).append(session)
    return {
        symbol: tuple(sorted(values, key=lambda item: item.day))
        for symbol, values in output.items()
    }


def _build_session(day: date, bars: list[_FeatureBar]) -> _SessionData | None:
    ordered = sorted(bars, key=lambda item: item.timestamp)
    regular = tuple(
        bar for bar in ordered if REGULAR_OPEN <= _market_time(bar.timestamp) < REGULAR_CLOSE
    )
    if not regular:
        return None
    premarket_volume = sum(
        bar.volume
        for bar in ordered
        if PREMARKET_OPEN <= _market_time(bar.timestamp) < REGULAR_OPEN
    )
    volume_by_minute = [0.0] * REGULAR_MINUTES
    close_by_minute: list[float | None] = [None] * REGULAR_MINUTES
    for bar in regular:
        index = _regular_minute_index(_market_time(bar.timestamp))
        volume_by_minute[index] += bar.volume
        close_by_minute[index] = bar.close
    cumulative_volume = []
    latest_close = []
    total_volume = 0.0
    last_close = None
    for index in range(REGULAR_MINUTES):
        total_volume += volume_by_minute[index]
        if close_by_minute[index] is not None:
            last_close = close_by_minute[index]
        cumulative_volume.append(total_volume)
        latest_close.append(last_close)
    return _SessionData(
        day=day,
        regular_bars=regular,
        regular_open=regular[0].open,
        regular_close=regular[-1].close,
        regular_high=max(bar.high for bar in regular),
        regular_low=min(bar.low for bar in regular),
        premarket_volume=premarket_volume,
        cumulative_volume=tuple(cumulative_volume),
        latest_close=tuple(latest_close),
    )


def _normalize_bar(raw_bar: IntradayBar) -> _FeatureBar:
    try:
        symbol = _normalized_symbol(raw_bar.symbol, "bar symbol")
        timestamp = raw_bar.timestamp
        if not isinstance(timestamp, datetime):
            raise ValueError("bar timestamp must be a datetime")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        values = {
            name: float(getattr(raw_bar, name))
            for name in ("open", "high", "low", "close", "volume")
        }
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("invalid intraday feature bar") from error
    for name in ("open", "high", "low", "close"):
        if not isfinite(values[name]) or values[name] <= 0:
            raise ValueError(f"bar {name} must be positive and finite")
    if not isfinite(values["volume"]) or values["volume"] < 0:
        raise ValueError("bar volume must be non-negative and finite")
    if values["low"] > values["high"]:
        raise ValueError("bar low must not exceed high")
    return _FeatureBar(symbol=symbol, timestamp=timestamp, **values)


def _prior_atr(
    sessions: tuple[_SessionData, ...],
    current_index: int,
    lookback: int,
) -> float:
    values = []
    for index in range(current_index - lookback, current_index):
        session = sessions[index]
        previous_close = sessions[index - 1].regular_close if index > 0 else None
        true_range = session.regular_high - session.regular_low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(session.regular_high - previous_close),
                abs(session.regular_low - previous_close),
            )
        values.append(true_range)
    return sum(values) / len(values)


def _relative_volume(
    sessions: tuple[_SessionData, ...],
    current_index: int,
    minute_index: int,
    lookback: int,
) -> float | None:
    history = [
        sessions[index].cumulative_volume[minute_index]
        for index in range(current_index - lookback, current_index)
    ]
    average = sum(history) / len(history)
    if average <= 0:
        return None
    return sessions[current_index].cumulative_volume[minute_index] / average


def _session_return_at(session: _SessionData, minute_index: int) -> float | None:
    close = session.latest_close[minute_index]
    if close is None:
        return None
    return close / session.regular_open - 1.0


def _daily_returns(sessions: tuple[_SessionData, ...]) -> dict[date, float]:
    output = {}
    for previous, current in zip(sessions, sessions[1:]):
        output[current.day] = current.regular_close / previous.regular_close - 1.0
    return output


def _maximum_prior_correlation(
    symbol: str,
    current_day: date,
    trade_symbols: tuple[str, ...],
    returns_by_symbol: dict[str, dict[date, float]],
    lookback: int,
) -> float:
    target = returns_by_symbol.get(symbol, {})
    correlations = []
    for other_symbol in trade_symbols:
        if other_symbol == symbol:
            continue
        other = returns_by_symbol.get(other_symbol, {})
        days = sorted(day for day in target.keys() & other.keys() if day < current_day)[-lookback:]
        if len(days) < 3:
            continue
        correlation = _pearson(
            [target[day] for day in days],
            [other[day] for day in days],
        )
        correlations.append(abs(correlation))
    return min(1.0, max(correlations, default=0.0))


def _pearson(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    covariance = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left, right)
    )
    left_variance = sum((value - left_mean) ** 2 for value in left)
    right_variance = sum((value - right_mean) ** 2 for value in right)
    denominator = sqrt(left_variance * right_variance)
    if denominator <= 0:
        return 0.0
    return max(-1.0, min(1.0, covariance / denominator))


def _session_for_day(
    sessions: tuple[_SessionData, ...],
    day: date,
) -> _SessionData | None:
    return next((session for session in sessions if session.day == day), None)


def _normalized_symbol(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip().upper()


def _market_day(value: datetime) -> date:
    return aware(value, "timestamp").astimezone(EASTERN).date()


def _market_time(value: datetime) -> time:
    return aware(value, "timestamp").astimezone(EASTERN).time().replace(tzinfo=None)


def _regular_minute_index(value: time) -> int:
    return (value.hour * 60 + value.minute) - (REGULAR_OPEN.hour * 60 + REGULAR_OPEN.minute)


__all__ = (
    "DEFAULT_DAYTRADE_SECTOR_PROXIES",
    "IntradayFeatureBook",
    "IntradayFeatureBuildConfig",
    "IntradayFeatureRequirements",
    "build_intraday_feature_snapshots",
    "intraday_feature_config_to_payload",
)
