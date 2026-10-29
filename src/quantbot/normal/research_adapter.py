"""Development-free research view for the normal strategy.

This adapter turns plain daily bars into the point-in-time artifacts the normal
provider consumes: a dataset-derived security master, a signed monthly
total-return dataset, and the liquidity/risk histories the allocator needs.

Every artifact it produces is ``DataQualityTier.DEVELOPMENT_FREE`` and is
sealed with caller-supplied research keys, so it can drive research runs and
can never satisfy final admission:

- ``require_final_admission`` rejects a development-free quality tier outright.
- The security master is never ``admission_verified``.
- Free daily bars carry survivorship bias and have no point-in-time universe
  membership, so the universe here is a fixed present-day list rather than a
  reconstructed historical one.

The module takes no third-party dependency.  Callers download bars however
they like and hand over ``ResearchDailyBar`` values, which keeps this path
inside the offline test suite.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import fsum, isfinite, sqrt
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from ..data import (
    DataQualityTier,
    MarketCapObservation,
    MonthlyTotalReturnIndexObservation,
    PointInTimeDataManifest,
    PointInTimeDataset,
    PointInTimeTotalReturnDataset,
    PointInTimeTotalReturnManifest,
    RETURN_ADMISSION_REQUIRED_COMPONENTS,
    SecurityClassification,
    SecurityIdentity,
    TradingSession,
    UniverseMembership,
    canonical_dataset_hash,
    canonical_total_return_dataset_hash,
    sign_point_in_time_coverage,
    sign_trading_calendar,
)
from ..regime import Regime
from .backtest import NormalBacktestSession
from .domain import NormalRegimeContext


UTC = timezone.utc
RESEARCH_UNIVERSE = "research"
BENCHMARK_SECURITY_ID = "BENCHMARK"
_CLOSE_HOUR = 21


def _month_end(value: date) -> date:
    return date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def _previous_month_end(value: date) -> date:
    if value.month == 1:
        return date(value.year - 1, 12, 31)
    return _month_end(date(value.year, value.month - 1, 1))


def _shift_month_end(value: date, offset: int) -> date:
    ordinal = value.year * 12 + value.month - 1 + offset
    year, index = divmod(ordinal, 12)
    return _month_end(date(year, index + 1, 1))


def _month_sequence(start: date, end: date) -> tuple[date, ...]:
    result = []
    current = _month_end(start)
    while current <= end:
        result.append(current)
        current = _shift_month_end(current, 1)
    return tuple(result)


def _instant(day: date, hour: int = _CLOSE_HOUR) -> datetime:
    return datetime.combine(day, time(hour, tzinfo=UTC))


@dataclass(frozen=True, slots=True)
class ResearchDailyBar:
    """One free-tier daily bar.

    ``adjusted_close`` must be split and dividend adjusted; it is the only
    input used to build total returns.  ``close`` is the raw traded price and
    drives execution and dollar volume.
    """

    security_id: str
    trading_date: date
    close: float
    adjusted_close: float
    volume: float

    def __post_init__(self) -> None:
        if not isinstance(self.security_id, str) or not self.security_id.strip():
            raise ValueError("security_id must be a non-empty string")
        object.__setattr__(self, "security_id", self.security_id.strip())
        if not isinstance(self.trading_date, date):
            raise TypeError("trading_date must be a date")
        for name in ("close", "adjusted_close"):
            value = float(getattr(self, name))
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
            object.__setattr__(self, name, value)
        volume = float(self.volume)
        if not isfinite(volume) or volume < 0:
            raise ValueError("volume must be finite and non-negative")
        object.__setattr__(self, "volume", volume)


@dataclass(frozen=True, slots=True)
class ResearchTrustKeys:
    """Research-only signing material, supplied by the caller."""

    calendar_issuer: str
    calendar_key: bytes
    verifier: str
    verifier_key: bytes

    def __post_init__(self) -> None:
        for name in ("calendar_issuer", "verifier"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        for name in ("calendar_key", "verifier_key"):
            value = getattr(self, name)
            if not isinstance(value, (bytes, bytearray)) or not value:
                raise ValueError(f"{name} must be non-empty bytes")
            object.__setattr__(self, name, bytes(value))

    @property
    def calendar_keys(self) -> Mapping[str, bytes]:
        return MappingProxyType({self.calendar_issuer: self.calendar_key})

    @property
    def verifier_keys(self) -> Mapping[str, bytes]:
        return MappingProxyType({self.verifier: self.verifier_key})


def _grouped_bars(bars: Iterable[ResearchDailyBar]):
    grouped: dict[str, dict[date, ResearchDailyBar]] = {}
    for bar in bars:
        if not isinstance(bar, ResearchDailyBar):
            raise TypeError("bars must contain ResearchDailyBar values")
        series = grouped.setdefault(bar.security_id, {})
        if bar.trading_date in series:
            raise ValueError(
                f"duplicate bar for {bar.security_id} on {bar.trading_date.isoformat()}"
            )
        series[bar.trading_date] = bar
    if not grouped:
        raise ValueError("bars must not be empty")
    return grouped


class ResearchNormalDataView:
    """A complete development-free normal research view over daily bars."""

    def __init__(
        self,
        bars: Iterable[ResearchDailyBar],
        *,
        sectors: Mapping[str, str],
        shares_outstanding: Mapping[str, float],
        trust_keys: ResearchTrustKeys,
        benchmark_security_id: str = BENCHMARK_SECURITY_ID,
        provider: str = "research-free-daily-bars",
        asset_lookback_sessions: int = 252,
        adv_lookback_sessions: int = 20,
        regime: Regime = Regime.R1_BULL_LOW_VOL,
        regime_confidence: float = 1.0,
    ) -> None:
        self._series = _grouped_bars(bars)
        self.benchmark_security_id = benchmark_security_id
        if benchmark_security_id not in self._series:
            raise ValueError("bars must include the benchmark security")
        self.security_ids = tuple(
            sorted(key for key in self._series if key != benchmark_security_id)
        )
        if not self.security_ids:
            raise ValueError("bars must include at least one investable security")
        missing_sectors = [key for key in self.security_ids if key not in sectors]
        if missing_sectors:
            raise ValueError("missing sector for: " + ",".join(sorted(missing_sectors)))
        self.sectors = {key: str(sectors[key]).strip() for key in self.security_ids}
        missing_shares = [key for key in self.security_ids if key not in shares_outstanding]
        if missing_shares:
            raise ValueError(
                "missing shares_outstanding for: " + ",".join(sorted(missing_shares))
            )
        # Free data exposes only a current share count.  Holding it fixed makes
        # market capitalization move with price, which is enough for the size
        # control in sector/log-cap neutralization but is explicitly not a
        # point-in-time share count.
        self.shares_outstanding = {}
        for key in self.security_ids:
            value = float(shares_outstanding[key])
            if not isfinite(value) or value <= 0:
                raise ValueError(f"shares_outstanding[{key}] must be finite and positive")
            self.shares_outstanding[key] = value
        if not isinstance(trust_keys, ResearchTrustKeys):
            raise TypeError("trust_keys must be ResearchTrustKeys")
        self.trust_keys = trust_keys
        self.provider = provider
        self.asset_lookback_sessions = int(asset_lookback_sessions)
        self.adv_lookback_sessions = int(adv_lookback_sessions)
        if not isinstance(regime, Regime):
            raise TypeError("regime must be a Regime")
        self._regime_context = NormalRegimeContext(regime, confidence=regime_confidence)

        self.sessions = tuple(sorted(self._series[benchmark_security_id]))
        if len(self.sessions) < self.asset_lookback_sessions + 2:
            raise ValueError("benchmark history is shorter than the asset lookback")
        self._session_index = {day: index for index, day in enumerate(self.sessions)}
        self._daily_returns = self._build_daily_returns()
        self._master_dataset, self._master_dataset_hash = self._build_master_dataset()
        self._calendar = self._build_calendar()
        self._snapshot_cache: dict[datetime, object] = {}
        self._returns_cache: dict[str, object] = {}
        self._dataset_cache: dict[date, object] = {}
        self._rows_cache = None

    # ---- construction -------------------------------------------------

    def _build_daily_returns(self):
        result: dict[str, dict[date, float]] = {}
        for security_id, series in self._series.items():
            ordered = sorted(series)
            values: dict[date, float] = {}
            for previous, current in zip(ordered, ordered[1:]):
                before = series[previous].adjusted_close
                values[current] = series[current].adjusted_close / before - 1.0
            result[security_id] = values
        return result

    def _coverage(self):
        return self.sessions[0], self.sessions[-1]

    def _build_master_dataset(self):
        start, end = self._coverage()
        known = _instant(start, 0)
        identities = tuple(
            SecurityIdentity(security_id, start, None, known)
            for security_id in self.security_ids
        )
        classifications = tuple(
            SecurityClassification(
                security_id,
                security_id,
                "XNYS",
                self.sectors[security_id],
                start,
                None,
                known,
            )
            for security_id in self.security_ids
        )
        # Free data has no historical membership record.  Declaring one fixed
        # present-day universe makes the survivorship bias explicit instead of
        # pretending a reconstructed index existed.
        memberships = tuple(
            UniverseMembership(
                RESEARCH_UNIVERSE,
                security_id,
                start,
                None,
                known,
                "free-data-fixed-universe-not-point-in-time",
            )
            for security_id in self.security_ids
        )
        market_caps = self._market_cap_observations()
        digest = canonical_dataset_hash(
            provider=self.provider,
            dataset_id="research-security-master-v1",
            feed="free",
            schema_version="1",
            quality_tier=DataQualityTier.DEVELOPMENT_FREE,
            effective_date_timezone="UTC",
            coverage_start=start,
            coverage_end=end,
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            fundamentals=(),
            market_caps=market_caps,
            corporate_actions=(),
        )
        dataset = PointInTimeDataset(
            manifest=PointInTimeDataManifest(
                provider=self.provider,
                dataset_id="research-security-master-v1",
                feed="free",
                schema_version="1",
                quality_tier=DataQualityTier.DEVELOPMENT_FREE,
                effective_date_timezone="UTC",
                coverage_start=start,
                coverage_end=end,
                content_hash=digest,
            ),
            identities=identities,
            classifications=classifications,
            memberships=memberships,
            market_caps=market_caps,
        )
        return dataset, digest

    def _market_cap_observations(self):
        """One capitalization observation per security per month-end session."""

        last_by_month = self._monthly_sessions()
        result = []
        for security_id in self.security_ids:
            series = self._series[security_id]
            shares = self.shares_outstanding[security_id]
            for session in sorted(last_by_month.values()):
                bar = series.get(session)
                if bar is None:
                    continue
                result.append(
                    MarketCapObservation(
                        security_id=security_id,
                        effective_on=session,
                        available_at=_instant(session),
                        market_cap=bar.close * shares,
                    )
                )
        return tuple(result)

    def _monthly_sessions(self):
        """Last observed session of every complete month in the sample."""

        last_by_month: dict[tuple[int, int], date] = {}
        for day in self.sessions:
            last_by_month[(day.year, day.month)] = day
        return last_by_month

    def _build_calendar(self):
        start, end = self._coverage()
        sessions = tuple(TradingSession(day, _instant(day)) for day in self.sessions)
        return sign_trading_calendar(
            exchange="XNYS",
            effective_date_timezone="UTC",
            issuer=self.trust_keys.calendar_issuer,
            issuer_version="research-1",
            generated_at=_instant(start, 0),
            coverage_start=start,
            coverage_end=end,
            missing_intervals=(),
            sessions=sessions,
            signing_key=self.trust_keys.calendar_key,
        )

    def _total_return_rows(self):
        if self._rows_cache is not None:
            return self._rows_cache
        self._rows_cache = self._compute_total_return_rows()
        return self._rows_cache

    def _compute_total_return_rows(self):
        last_by_month = self._monthly_sessions()
        periods = _month_sequence(self.sessions[0], self.sessions[-1])
        covered = tuple(
            period for period in periods if (period.year, period.month) in last_by_month
        )
        rows = []
        for security_id, series in self._series.items():
            level = 100.0
            ordered_returns = self._daily_returns[security_id]
            previous_session = None
            for period in covered:
                session = last_by_month[(period.year, period.month)]
                if security_id != self.benchmark_security_id and session not in series:
                    raise ValueError(
                        f"{security_id} lacks a month-end bar for {period.isoformat()}"
                    )
                if previous_session is not None:
                    window = [
                        value
                        for day, value in ordered_returns.items()
                        if previous_session < day <= session
                    ]
                    for value in window:
                        level *= 1.0 + value
                previous_session = session
                rows.append(
                    MonthlyTotalReturnIndexObservation(
                        security_id=security_id,
                        month_end=period,
                        index_level=level,
                        # The index level is only knowable after the month's
                        # final close, so that close is its availability time.
                        available_at=_instant(session),
                    )
                )
        return tuple(rows), covered

    def _returns_dataset_for(self, signal_date: date):
        """Build the total-return vintage that existed at ``signal_date``.

        One dataset covering the whole sample cannot be used: its adjustment
        vintage would postdate every historical snapshot, and
        ``_validate_snapshot_request`` rejects that outright.  It is right to
        reject it -- a single present-day download of adjusted closes embeds
        every later split and dividend.  Rebuilding a truncated vintage per
        rebalance bounds that leak to the adjustment factors themselves, which
        is the residual bias documented for this free-data path.
        """

        cutoff = _previous_month_end(signal_date)
        if cutoff in self._dataset_cache:
            return self._dataset_cache[cutoff]
        all_rows, periods = self._total_return_rows()
        periods = tuple(period for period in periods if period <= cutoff)
        if len(periods) < 2:
            raise ValueError("research returns require at least two complete months")
        rows = tuple(row for row in all_rows if row.month_end <= cutoff)
        dataset = self._seal_returns_dataset(rows, periods, cutoff)
        self._dataset_cache[cutoff] = dataset
        return dataset

    def _seal_returns_dataset(self, rows, periods, cutoff):
        coverage_start, coverage_end = periods[0], periods[-1]
        # The vintage is the close of the last session the dataset covers, not
        # the close of whichever session asked for it.  Deriving it from the
        # requester would give the same cutoff two different vintages and let a
        # later snapshot poison the cache for an earlier one.
        last_by_month = self._monthly_sessions()
        vintage = _instant(last_by_month[(cutoff.year, cutoff.month)])
        digest = canonical_total_return_dataset_hash(
            provider=self.provider,
            dataset_id="research-monthly-total-return-v1",
            feed="free",
            schema_version="1",
            quality_tier=DataQualityTier.DEVELOPMENT_FREE,
            effective_date_timezone="UTC",
            adjustment_vintage_at=vintage,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            security_master_dataset_hash=self._master_dataset_hash,
            reference_security_ids=(self.benchmark_security_id,),
            observations=rows,
        )
        counts = {
            period: len({row.security_id for row in rows if row.month_end == period})
            for period in periods
        }
        attestation = sign_point_in_time_coverage(
            verifier=self.trust_keys.verifier,
            verifier_version="research-1",
            generated_at=vintage + timedelta(days=1),
            provider=self.provider,
            dataset_id="research-monthly-total-return-v1",
            dataset_hash=digest,
            session_calendar_hash=self._calendar.content_hash,
            related_dataset_hashes={"security_master": self._master_dataset_hash},
            covered_universes=(RESEARCH_UNIVERSE,),
            session_security_counts=counts,
            source_artifact_hashes=(digest,),
            assertions={name: True for name in RETURN_ADMISSION_REQUIRED_COMPONENTS},
            record_counts={"total_return_index": len(rows)},
            missing_sessions=(),
            missing_intervals=(),
            signing_key=self.trust_keys.verifier_key,
        )
        manifest = PointInTimeTotalReturnManifest(
            provider=self.provider,
            dataset_id="research-monthly-total-return-v1",
            feed="free",
            schema_version="1",
            quality_tier=DataQualityTier.DEVELOPMENT_FREE,
            effective_date_timezone="UTC",
            adjustment_vintage_at=vintage,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
            security_master_dataset_hash=self._master_dataset_hash,
            reference_security_ids=(self.benchmark_security_id,),
            content_hash=digest,
            coverage_attestation=attestation,
        )
        return PointInTimeTotalReturnDataset(manifest=manifest, observations=rows)

    # ---- point-in-time views ------------------------------------------

    def security_master(self, signal_date: date):
        as_of = _instant(signal_date)
        if as_of not in self._snapshot_cache:
            self._snapshot_cache[as_of] = self._master_dataset.snapshot(
                as_of, (RESEARCH_UNIVERSE,)
            )
        return self._snapshot_cache[as_of]

    # ---- PointInTimeMarketDataPort ------------------------------------

    def monthly_returns(self, security_master):
        key = str(security_master.as_of)
        if key not in self._returns_cache:
            dataset = self._returns_dataset_for(security_master.effective_date)
            self._returns_cache[key] = dataset.snapshot(
                security_master=security_master,
                universe=RESEARCH_UNIVERSE,
                benchmark_security_id=self.benchmark_security_id,
                trading_calendar=self._calendar,
                trusted_calendar_keys=self.trust_keys.calendar_keys,
                trusted_verifier_keys=self.trust_keys.verifier_keys,
            )
        return self._returns_cache[key]

    def _window(self, signal_date: date, lookback_sessions: int) -> tuple[date, ...]:
        index = self._session_index.get(signal_date)
        if index is None:
            raise ValueError("signal date is not a trading session")
        start = max(0, index - lookback_sessions + 1)
        return self.sessions[start: index + 1]

    def asset_return_history(self, security_master, *, lookback_sessions):
        window = self._window(security_master.effective_date, lookback_sessions)
        result = {}
        for security_id in security_master.security_ids:
            returns = self._daily_returns[security_id]
            values = tuple(returns[day] for day in window if day in returns)
            if len(values) >= 12:
                result[security_id] = values
        if not result:
            return {}
        shortest = min(len(values) for values in result.values())
        return {key: values[-shortest:] for key, values in result.items()}

    def sleeve_return_history(self, security_master, sleeves, *, lookback_months):
        """Equal-weight long-short monthly return of each sleeve's own scores.

        The allocator only uses these histories for sleeve volatility and
        correlation, so a single-sleeve run needs a real but simple series.
        """

        last_by_month = self._monthly_sessions()
        signal_date = security_master.effective_date
        months = []
        cursor = _previous_month_end(signal_date)
        while len(months) < lookback_months:
            key = (cursor.year, cursor.month)
            if key in last_by_month:
                months.append(last_by_month[key])
            cursor = _shift_month_end(cursor, -1)
            if cursor < self.sessions[0]:
                break
        months.reverse()
        result = {}
        for sleeve in sleeves:
            ranked = sorted(sleeve.scores, key=lambda key: (-sleeve.scores[key], key))
            half = max(1, len(ranked) // 5)
            longs, shorts = ranked[:half], ranked[-half:]
            series = []
            for previous, current in zip(months, months[1:]):
                series.append(
                    self._basket_return(longs, previous, current)
                    - self._basket_return(shorts, previous, current)
                )
            if len(series) < 12:
                raise ValueError(
                    "research sleeve history needs at least 12 complete months"
                )
            result[sleeve.name] = tuple(series)
        shortest = min(len(values) for values in result.values())
        return {key: values[-shortest:] for key, values in result.items()}

    def _basket_return(self, security_ids, previous_session, current_session):
        values = []
        for security_id in security_ids:
            series = self._series.get(security_id)
            if series is None:
                continue
            before = series.get(previous_session)
            after = series.get(current_session)
            if before is None or after is None:
                continue
            values.append(after.adjusted_close / before.adjusted_close - 1.0)
        return fsum(values) / len(values) if values else 0.0

    def market_betas(self, security_master, *, lookback_sessions):
        window = self._window(security_master.effective_date, lookback_sessions)
        benchmark = self._daily_returns[self.benchmark_security_id]
        market = [benchmark[day] for day in window if day in benchmark]
        if len(market) < 12:
            return {}
        mean_market = fsum(market) / len(market)
        variance = fsum((value - mean_market) ** 2 for value in market) / len(market)
        result = {}
        for security_id in security_master.security_ids:
            returns = self._daily_returns[security_id]
            paired = [
                (returns[day], benchmark[day])
                for day in window
                if day in returns and day in benchmark
            ]
            if len(paired) < 12 or variance <= 0:
                continue
            mean_asset = fsum(value for value, _ in paired) / len(paired)
            covariance = fsum(
                (value - mean_asset) * (other - mean_market) for value, other in paired
            ) / len(paired)
            result[security_id] = covariance / variance
        return result

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        window = self._window(security_master.effective_date, lookback_sessions)
        result = {}
        for security_id in security_master.security_ids:
            series = self._series[security_id]
            values = [
                series[day].close * series[day].volume for day in window if day in series
            ]
            if values:
                result[security_id] = fsum(values) / len(values)
        return result

    def regime_context(self, security_master):
        del security_master
        return self._regime_context

    # ---- backtest sessions --------------------------------------------

    def backtest_sessions(
        self,
        *,
        start: date,
        end: date,
        rebalance: str = "monthly",
    ) -> tuple[NormalBacktestSession, ...]:
        """Build sessions whose signals are observed at a close and filled next open.

        Free daily bars have no open price, so the previous raw close is used
        as the execution price.  That is a documented simplification of the
        production next-open contract, applied identically to both arms.
        """

        if rebalance not in {"monthly", "none"}:
            raise ValueError("rebalance must be monthly or none")
        window = [day for day in self.sessions if start <= day <= end]
        if len(window) < 2:
            raise ValueError("session window must contain at least two sessions")
        signal_days = set()
        if rebalance == "monthly":
            last_by_month: dict[tuple[int, int], date] = {}
            for day in window[:-1]:
                last_by_month[(day.year, day.month)] = day
            signal_days = set(last_by_month.values())

        result = []
        for day in window:
            index = self._session_index[day]
            previous = self.sessions[index - 1]
            held = [
                security_id
                for security_id in self.security_ids
                if day in self._series[security_id]
                and previous in self._series[security_id]
            ]
            if not held:
                continue
            open_prices = {key: self._series[key][previous].close for key in held}
            close_prices = {key: self._series[key][day].close for key in held}
            benchmark = self._daily_returns[self.benchmark_security_id].get(day, 0.0)
            snapshot = None
            if day in signal_days:
                master = self.security_master(day)
                if set(master.security_ids).issubset(set(held)):
                    from ..trading import PointInTimeSnapshot

                    snapshot = PointInTimeSnapshot(
                        observed_at=master.as_of, security_master=master
                    )
            result.append(
                NormalBacktestSession(
                    trading_date=day,
                    open_prices=open_prices,
                    close_prices=close_prices,
                    benchmark_return=benchmark,
                    signal_snapshot=snapshot,
                )
            )
        return tuple(result)


def annualized_volatility(returns: Sequence[float], periods_per_year: int = 252) -> float:
    values = tuple(float(value) for value in returns)
    if len(values) < 2:
        return 0.0
    mean = fsum(values) / len(values)
    variance = fsum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(max(variance, 0.0) * periods_per_year)


__all__ = (
    "BENCHMARK_SECURITY_ID",
    "RESEARCH_UNIVERSE",
    "ResearchDailyBar",
    "ResearchNormalDataView",
    "ResearchTrustKeys",
    "annualized_volatility",
)
