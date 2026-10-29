"""Investment universe construction."""

from dataclasses import dataclass
from datetime import date, datetime
from math import fsum, isfinite
from typing import Iterable, Mapping, Set

from .data.cleaning import filter_liquid_universe
from .data.point_in_time import (
    PointInTimeDataset,
    PointInTimePriceDataset,
    PointInTimeTradingCalendar,
)
from .daytrade_strategy import DailyUniverseSnapshot


@dataclass(frozen=True, slots=True)
class TradingSuspension:
    """Independent evidence that a daily bar may legitimately be absent."""

    security_id: str
    trading_date: date
    available_at: datetime
    source: str

    def __post_init__(self) -> None:
        for name in ("security_id", "source"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if isinstance(self.trading_date, datetime) or not isinstance(self.trading_date, date):
            raise ValueError("trading_date must be a date")
        if not isinstance(self.available_at, datetime) or self.available_at.tzinfo is None:
            raise ValueError("available_at must be timezone-aware")


class UniverseBuilder:
    def __init__(
        self,
        sp500: Set[str],
        russell1000: Set[str],
        min_avg_dollar_volume: float = 5_000_000,
        min_history_days: int = 252,
        lookback_days: int = 20,
    ):
        self.sp500 = set(sp500)
        self.russell1000 = set(russell1000)
        self.min_avg_dollar_volume = min_avg_dollar_volume
        self.min_history_days = min_history_days
        self.lookback_days = lookback_days

    def build(self, price_rows: Iterable[dict]):
        investable = self.sp500.union(self.russell1000)
        filtered_rows = [
            row for row in price_rows
            if row["ticker"] in investable
        ]
        return filter_liquid_universe(
            filtered_rows,
            min_avg_dollar_volume=self.min_avg_dollar_volume,
            min_history_days=self.min_history_days,
            lookback_days=self.lookback_days,
        )


@dataclass(frozen=True, slots=True)
class PointInTimeUniverseBuilder:
    """Apply liquidity filters only to securities and bars known at ``as_of``.

    Price rows are keyed by the vendor's stable ``security_id``.  Tickers are
    intentionally ignored because the same symbol may represent different
    securities at different points in history.
    """

    dataset: PointInTimeDataset
    universes: tuple[str, ...]
    min_avg_dollar_volume: float = 5_000_000
    min_history_days: int = 252
    lookback_days: int = 20

    def __post_init__(self) -> None:
        object.__setattr__(self, "universes", tuple(sorted(set(self.universes))))
        if not self.universes or any(not item for item in self.universes):
            raise ValueError("at least one non-empty universe is required")
        if isinstance(self.min_avg_dollar_volume, bool) or not isinstance(
            self.min_avg_dollar_volume, (int, float)
        ):
            raise ValueError("min_avg_dollar_volume must be finite and non-negative")
        try:
            minimum_dollar_volume = float(self.min_avg_dollar_volume)
        except (OverflowError, TypeError, ValueError) as error:
            raise ValueError(
                "min_avg_dollar_volume must be finite and non-negative"
            ) from error
        if not isfinite(minimum_dollar_volume) or minimum_dollar_volume < 0:
            raise ValueError("min_avg_dollar_volume must be finite and non-negative")
        object.__setattr__(
            self,
            "min_avg_dollar_volume",
            minimum_dollar_volume,
        )
        for name in ("min_history_days", "lookback_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    def build(
        self,
        as_of: datetime,
        price_dataset: PointInTimePriceDataset,
        *,
        trading_calendar: PointInTimeTradingCalendar,
        trusted_calendar_keys: Mapping[str, bytes],
    ) -> list[str]:
        if not isinstance(price_dataset, PointInTimePriceDataset):
            raise TypeError("price_dataset must be a PointInTimePriceDataset")
        if (
            price_dataset.manifest.effective_date_timezone
            != self.dataset.manifest.effective_date_timezone
        ):
            raise ValueError("price and security-master effective date timezones differ")
        price_dataset.validate_trading_calendar(
            trading_calendar,
            trusted_calendar_keys,
        )
        snapshot = self.dataset.snapshot(as_of, self.universes)
        trading_calendar.require_complete_coverage(
            snapshot.effective_date,
            snapshot.effective_date,
        )
        active_ids = set(snapshot.security_ids)
        completed_sessions = tuple(
            session
            for session in trading_calendar.completed_sessions(as_of)
            if session.session_date <= snapshot.effective_date
        )
        if len(completed_sessions) < self.lookback_days:
            return []
        required_recent_sessions = tuple(
            session.session_date for session in completed_sessions[-self.lookback_days :]
        )
        observed_sessions = {
            item.trading_date
            for item in price_dataset.observations
            if item.available_at <= as_of
        }
        if any(session not in observed_sessions for session in required_recent_sessions):
            raise ValueError("price data lacks trusted calendar session coverage")
        minimum_observations = max(self.min_history_days, self.lookback_days)

        eligible = []
        for security_id in active_ids:
            identity = snapshot.identity_by_security_id[security_id]
            observations = [
                item
                for item in price_dataset.observations_for(security_id)
                if item.available_at <= as_of
                and identity.listed_on <= item.trading_date <= snapshot.effective_date
            ]
            if len(observations) < minimum_observations:
                continue
            by_session = {item.trading_date: item for item in observations}
            if any(session not in by_session for session in required_recent_sessions):
                continue
            recent = [by_session[session] for session in required_recent_sessions]
            dollar_volumes = tuple(item.close * item.volume for item in recent)
            if any(not isfinite(value) for value in dollar_volumes):
                continue
            try:
                average_dollar_volume = fsum(dollar_volumes) / len(recent)
            except (OverflowError, ValueError):
                continue
            if not isfinite(average_dollar_volume):
                continue
            if average_dollar_volume >= self.min_avg_dollar_volume:
                eligible.append(security_id)
        return sorted(eligible)

    def build_daytrade_snapshot(
        self,
        trading_day: date,
        price_dataset: PointInTimePriceDataset,
        *,
        trading_calendar: PointInTimeTradingCalendar,
        trusted_calendar_keys: Mapping[str, bytes],
        top_n: int = 50,
        min_previous_close: float = 5.0,
        suspensions: Iterable[TradingSuspension] = (),
    ) -> DailyUniverseSnapshot:
        """Build the dynamic intraday universe using only prior-close facts.

        Securities are ranked by their prior 20-complete-session average dollar
        volume with stable security ID as the deterministic tie-breaker.  A
        missing daily row for a seasoned active constituent fails the entire
        snapshot unless an independently sourced suspension was already known.
        """

        if isinstance(trading_day, datetime) or not isinstance(trading_day, date):
            raise ValueError("trading_day must be a date")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
            raise ValueError("top_n must be a positive integer")
        if isinstance(min_previous_close, bool) or not isinstance(
            min_previous_close, (int, float)
        ):
            raise ValueError("min_previous_close must be finite and non-negative")
        minimum_close = float(min_previous_close)
        if not isfinite(minimum_close) or minimum_close < 0:
            raise ValueError("min_previous_close must be finite and non-negative")
        if not isinstance(price_dataset, PointInTimePriceDataset):
            raise TypeError("price_dataset must be a PointInTimePriceDataset")
        price_dataset.validate_trading_calendar(
            trading_calendar,
            trusted_calendar_keys,
        )
        trading_calendar.require_complete_coverage(trading_day, trading_day)
        session_dates = tuple(item.session_date for item in trading_calendar.sessions)
        try:
            trading_index = session_dates.index(trading_day)
        except ValueError as error:
            raise ValueError("trading_day is not a trusted trading-calendar session") from error
        if trading_index == 0:
            raise ValueError("trading_day has no prior trusted session")
        previous_session = trading_calendar.sessions[trading_index - 1]
        as_of = previous_session.close_at
        snapshot = self.dataset.snapshot(as_of, self.universes)
        active_ids = set(snapshot.security_ids)
        completed = tuple(
            item for item in trading_calendar.sessions[:trading_index]
            if item.close_at <= as_of
        )
        if len(completed) < self.min_history_days:
            return DailyUniverseSnapshot(trading_day, {})
        required_history = tuple(
            item.session_date for item in completed[-self.min_history_days :]
        )
        visible_suspensions = self._visible_suspensions(
            suspensions,
            as_of,
            set(session_dates),
        )
        ranked = []
        for security_id in sorted(active_ids):
            identity = snapshot.identity_by_security_id[security_id]
            if identity.listed_on > required_history[0]:
                continue
            observations = tuple(
                item
                for item in price_dataset.observations_for(security_id)
                if item.available_at <= as_of
                and identity.listed_on <= item.trading_date <= previous_session.session_date
            )
            by_session = {item.trading_date: item for item in observations}
            missing = tuple(day for day in required_history if day not in by_session)
            unexcused = tuple(
                day
                for day in missing
                if (security_id, day) not in visible_suspensions
            )
            if unexcused:
                raise ValueError(
                    "missing PIT daily price without suspension evidence: "
                    f"{security_id}:{unexcused[0].isoformat()}"
                )
            previous = by_session.get(previous_session.session_date)
            if previous is None:
                # A documented suspension is valid missingness, but the name
                # cannot satisfy the prior-close price/ranking gate that day.
                continue
            if previous.close < minimum_close:
                continue
            complete_rows = tuple(
                by_session[session.session_date]
                for session in completed
                if session.session_date in by_session
            )
            if len(complete_rows) < self.lookback_days:
                continue
            recent = complete_rows[-self.lookback_days :]
            average_dollar_volume = fsum(
                item.close * item.volume for item in recent
            ) / len(recent)
            if not isfinite(average_dollar_volume):
                continue
            if average_dollar_volume >= self.min_avg_dollar_volume:
                ranked.append((average_dollar_volume, security_id))
        selected = tuple(
            security_id
            for _adv, security_id in sorted(
                ranked,
                key=lambda item: (-item[0], item[1]),
            )[:top_n]
        )
        return DailyUniverseSnapshot(
            trading_day,
            {
                security_id: snapshot.ticker_by_security_id[security_id]
                for security_id in selected
            },
        )

    @staticmethod
    def _visible_suspensions(suspensions, as_of, calendar_sessions):
        if isinstance(suspensions, (str, bytes)):
            raise TypeError("suspensions must contain TradingSuspension values")
        visible = set()
        for item in tuple(suspensions):
            if not isinstance(item, TradingSuspension):
                raise TypeError("suspensions must contain TradingSuspension values")
            if item.trading_date not in calendar_sessions:
                raise ValueError("suspension date is not a trusted trading session")
            if item.available_at <= as_of:
                key = (item.security_id, item.trading_date)
                if key in visible:
                    raise ValueError("duplicate visible suspension evidence")
                visible.add(key)
        return frozenset(visible)


__all__ = (
    "PointInTimeUniverseBuilder",
    "TradingSuspension",
    "UniverseBuilder",
)
