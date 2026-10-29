"""Strict runtime configuration loading for backtest, paper, and live runs."""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any


# Observed maximum-drawdown to annualized-volatility ratio across this
# project's own normal research and adaptive windows ran 1.5x to 2.6x. The
# multiple is a declared constant rather than a tunable so a drawdown ceiling
# cannot be quietly relaxed until a candidate passes.

# Broker identities the package can actually drive.  Kept as a literal rather
# than imported from quantbot.execution so configuration loading stays free of
# the execution stack; the pairing is asserted by the test suite.
SUPPORTED_BROKERS = ("Alpaca", "IBKR")


def _validated_string(path: str, value: object, *, choices: set[str] | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected a string")
    if not value.strip():
        raise ValueError(f"{path}: must not be empty")
    if choices is not None and value not in choices:
        rendered = ", ".join(sorted(choices))
        raise ValueError(f"{path}: must be one of {rendered}")
    return value


def _validated_bool(path: str, value: object) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path}: expected a boolean")
    return value


def _validated_int(path: str, value: object, *, minimum: int) -> int:
    if type(value) is not int:
        raise ValueError(f"{path}: expected an integer")
    if value < minimum:
        raise ValueError(f"{path}: must be at least {minimum}")
    return value


def _validated_number(
    path: str,
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{path}: expected a number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError(f"{path}: must be a finite number") from None
    if not math.isfinite(result):
        raise ValueError(f"{path}: must be finite")
    if minimum is not None:
        below_minimum = result <= minimum if minimum_exclusive else result < minimum
        if below_minimum:
            relation = "greater than" if minimum_exclusive else "at least"
            raise ValueError(f"{path}: must be {relation} {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{path}: must be at most {maximum}")
    return result


def _validated_unit_interval(path: str, value: object) -> float:
    return _validated_number(
        path,
        value,
        minimum=0.0,
        maximum=1.0,
        minimum_exclusive=True,
    )


_CLOCK_PATTERN = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


def _clock_minutes(path: str, value: object) -> int:
    clock = _validated_string(path, value)
    if not _CLOCK_PATTERN.fullmatch(clock):
        raise ValueError(f"{path}: expected HH:MM in 24-hour time")
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


@dataclass(frozen=True)
class DataRuntimeConfig:
    root: Path = Path("data")
    daily_feed: str = "sip"
    minute_feed: str = "sip"
    schema_version: int = 1
    require_complete_coverage: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise ValueError("data.root: expected a Path")
        _validated_string("data.daily_feed", self.daily_feed)
        _validated_string("data.minute_feed", self.minute_feed)
        _validated_int("data.schema_version", self.schema_version, minimum=1)
        complete = _validated_bool(
            "data.require_complete_coverage",
            self.require_complete_coverage,
        )
        if not complete:
            raise ValueError(
                "data.require_complete_coverage: disabling complete coverage is not permitted"
            )


@dataclass(frozen=True)
class NormalRuntimeConfig:
    benchmark: str = "SPY"
    universe_mode: str = "point_in_time"
    execution_price: str = "next_open"
    cost_bps_per_side: float = 5.0
    lookback_days: int = 252
    # The PIT strategy's 5% single-name cap needs at least 20 names to make
    # 100% gross exposure mathematically attainable.  Legacy OOS smoke tests
    # retain their independent Top-5 default in MomentumOOSBacktestConfig.
    top_n: int = 20
    residual_estimation_months: int = 36
    residual_skip_months: int = 1
    covariance_shrinkage: float = 0.50
    no_trade_band: float = 0.0025
    turnover_penalty: float = 0.50
    exit_rank_buffer: int = 5
    max_adv_participation: float = 0.01
    momentum_rebound_multiplier: float = 0.50
    # Capital-efficiency controls.  Defaults reproduce the historical
    # shrink-only allocator, so an existing TOML keeps its exact behaviour.
    redistribute_constrained_capital: bool = False
    volatility_targeting: str = "cap_only"
    min_funded_names: int = 0

    def __post_init__(self) -> None:
        _validated_string("normal.benchmark", self.benchmark)
        _validated_bool(
            "normal.redistribute_constrained_capital",
            self.redistribute_constrained_capital,
        )
        _validated_string(
            "normal.volatility_targeting",
            self.volatility_targeting,
            choices={"cap_only", "two_sided"},
        )
        _validated_int("normal.min_funded_names", self.min_funded_names, minimum=0)
        # "static_mega_cap" exists so the legacy smoke path can declare what
        # it actually uses; it can never satisfy final admission.
        _validated_string(
            "normal.universe_mode",
            self.universe_mode,
            choices={"point_in_time", "static_mega_cap"},
        )
        # Only next-open execution is implemented; offering "vwap" made the
        # configuration describe behaviour the code does not have.
        _validated_string(
            "normal.execution_price",
            self.execution_price,
            choices={"next_open"},
        )
        object.__setattr__(
            self,
            "cost_bps_per_side",
            _validated_number(
                "normal.cost_bps_per_side",
                self.cost_bps_per_side,
                minimum=0.0,
            ),
        )
        _validated_int("normal.lookback_days", self.lookback_days, minimum=1)
        _validated_int("normal.top_n", self.top_n, minimum=1)
        _validated_int(
            "normal.residual_estimation_months",
            self.residual_estimation_months,
            minimum=13,
        )
        _validated_int("normal.residual_skip_months", self.residual_skip_months, minimum=0)
        _validated_int("normal.exit_rank_buffer", self.exit_rank_buffer, minimum=0)
        for field_name in ("covariance_shrinkage", "no_trade_band"):
            object.__setattr__(
                self,
                field_name,
                _validated_number(
                    f"normal.{field_name}",
                    getattr(self, field_name),
                    minimum=0.0,
                    maximum=1.0,
                ),
            )
        object.__setattr__(
            self,
            "turnover_penalty",
            _validated_number(
                "normal.turnover_penalty",
                self.turnover_penalty,
                minimum=0.0,
            ),
        )
        for field_name in ("max_adv_participation", "momentum_rebound_multiplier"):
            object.__setattr__(
                self,
                field_name,
                _validated_unit_interval(
                    f"normal.{field_name}",
                    getattr(self, field_name),
                ),
            )


@dataclass(frozen=True)
class DayTradeRuntimeConfig:
    entry_start: str = "09:45"
    entry_end: str = "10:30"
    cumulative_vwap: bool = True
    side: str = "long"
    vwap_hysteresis_bps: float = 5.0
    stop_basis: str = "atr"
    atr_stop_multiple: float = 0.75
    opening_range_stop_multiple: float = 0.75
    min_opening_range_atr: float = 0.25
    max_opening_range_atr: float = 1.50
    min_relative_volume: float = 1.50
    min_directional_gap: float = 0.0
    max_directional_gap: float = 0.05
    min_premarket_volume: float = 0.0
    min_directional_spy_return: float = 0.0
    min_directional_qqq_return: float = 0.0
    min_directional_sector_strength: float = 0.0
    max_short_gap_abs: float = 0.05
    max_portfolio_heat_fraction: float = 0.02
    correlation_penalty: float = 0.50
    cost_bps_per_side: float = 5.0
    max_volume_participation: float = 0.10
    live_submission_enabled: bool = False
    # Enforcement of risk.max_daytrade_drawdown, which was carried into
    # DayTradeConfig and read by nothing.  Opt-in so frozen H3/H9 results stay
    # reproducible.
    enforce_drawdown_halt: bool = False

    def __post_init__(self) -> None:
        start_minutes = _clock_minutes("daytrade.entry_start", self.entry_start)
        end_minutes = _clock_minutes("daytrade.entry_end", self.entry_end)
        if start_minutes >= end_minutes:
            raise ValueError(
                "daytrade.entry_start/daytrade.entry_end: start must be before end"
            )
        _validated_bool("daytrade.cumulative_vwap", self.cumulative_vwap)
        _validated_string("daytrade.side", self.side, choices={"long", "short"})
        _validated_string(
            "daytrade.stop_basis",
            self.stop_basis,
            choices={"atr", "opening_range"},
        )
        non_negative_fields = (
            "vwap_hysteresis_bps",
            "min_opening_range_atr",
            "min_relative_volume",
            "min_premarket_volume",
            "max_short_gap_abs",
            "correlation_penalty",
        )
        for field_name in non_negative_fields:
            maximum = 1.0 if field_name == "correlation_penalty" else None
            object.__setattr__(
                self,
                field_name,
                _validated_number(
                    f"daytrade.{field_name}",
                    getattr(self, field_name),
                    minimum=0.0,
                    maximum=maximum,
                ),
            )
        for field_name in (
            "atr_stop_multiple",
            "opening_range_stop_multiple",
            "max_opening_range_atr",
            "max_portfolio_heat_fraction",
        ):
            maximum = 1.0 if field_name == "max_portfolio_heat_fraction" else None
            object.__setattr__(
                self,
                field_name,
                _validated_number(
                    f"daytrade.{field_name}",
                    getattr(self, field_name),
                    minimum=0.0,
                    maximum=maximum,
                    minimum_exclusive=True,
                ),
            )
        for field_name in (
            "min_directional_gap",
            "max_directional_gap",
            "min_directional_spy_return",
            "min_directional_qqq_return",
            "min_directional_sector_strength",
        ):
            object.__setattr__(
                self,
                field_name,
                _validated_number(
                    f"daytrade.{field_name}",
                    getattr(self, field_name),
                ),
            )
        if self.max_opening_range_atr < self.min_opening_range_atr:
            raise ValueError(
                "daytrade.max_opening_range_atr: must not be below min_opening_range_atr"
            )
        if self.max_directional_gap < self.min_directional_gap:
            raise ValueError(
                "daytrade.max_directional_gap: must not be below min_directional_gap"
            )
        object.__setattr__(
            self,
            "cost_bps_per_side",
            _validated_number(
                "daytrade.cost_bps_per_side",
                self.cost_bps_per_side,
                minimum=0.0,
            ),
        )
        object.__setattr__(
            self,
            "max_volume_participation",
            _validated_number(
                "daytrade.max_volume_participation",
                self.max_volume_participation,
                minimum=0.0,
                maximum=1.0,
                minimum_exclusive=True,
            ),
        )
        live_enabled = _validated_bool(
            "daytrade.live_submission_enabled",
            self.live_submission_enabled,
        )
        if live_enabled:
            raise ValueError(
                "daytrade.live_submission_enabled: live submission cannot be granted by configuration"
            )
        _validated_bool(
            "daytrade.enforce_drawdown_halt",
            self.enforce_drawdown_halt,
        )


@dataclass(frozen=True)
class PortfolioRuntimeConfig:
    initial_cash: float = 100_000.0
    normal_fraction: float = 0.90
    daytrade_fraction: float = 0.10

    def __post_init__(self) -> None:
        initial_cash = _validated_number(
            "portfolio.initial_cash",
            self.initial_cash,
            minimum=0.0,
            minimum_exclusive=True,
        )
        normal_fraction = _validated_number(
            "portfolio.normal_fraction",
            self.normal_fraction,
            minimum=0.0,
        )
        daytrade_fraction = _validated_number(
            "portfolio.daytrade_fraction",
            self.daytrade_fraction,
            minimum=0.0,
        )
        if not math.isclose(
            normal_fraction + daytrade_fraction,
            1.0,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "portfolio.normal_fraction/portfolio.daytrade_fraction: values must sum to 1.0"
            )
        object.__setattr__(self, "initial_cash", initial_cash)
        object.__setattr__(self, "normal_fraction", normal_fraction)
        object.__setattr__(self, "daytrade_fraction", daytrade_fraction)


@dataclass(frozen=True)
class RiskRuntimeConfig:
    max_single_stock_weight: float = 0.05
    max_sector_weight: float = 0.25
    max_portfolio_vol_annual: float = 0.15
    max_normal_drawdown: float = 0.15
    max_daytrade_drawdown: float = 0.05
    max_market_beta_exposure: float = 1.0

    def __post_init__(self) -> None:
        for field_name in (
            "max_single_stock_weight",
            "max_sector_weight",
            "max_portfolio_vol_annual",
            "max_normal_drawdown",
            "max_daytrade_drawdown",
        ):
            object.__setattr__(
                self,
                field_name,
                _validated_unit_interval(
                    f"risk.{field_name}",
                    getattr(self, field_name),
                ),
            )
        object.__setattr__(
            self,
            "max_market_beta_exposure",
            _validated_number(
                "risk.max_market_beta_exposure",
                self.max_market_beta_exposure,
                minimum=0.0,
                minimum_exclusive=True,
            ),
        )


@dataclass(frozen=True)
class ExecutionRuntimeConfig:
    primary_broker: str = "Alpaca"
    backup_broker: str = "IBKR"
    base_url: str = "https://paper-api.alpaca.markets"
    slippage_bps_per_side: float = 0.0

    def __post_init__(self) -> None:
        # Free-string broker names let a configuration name a broker the
        # package cannot drive; restrict them to the implemented adapters.
        _validated_string(
            "execution.primary_broker",
            self.primary_broker,
            choices=set(SUPPORTED_BROKERS),
        )
        _validated_string(
            "execution.backup_broker",
            self.backup_broker,
            choices=set(SUPPORTED_BROKERS),
        )
        if self.primary_broker == self.backup_broker:
            raise ValueError(
                "execution.backup_broker: failover requires a different broker "
                "from execution.primary_broker"
            )
        _validated_string("execution.base_url", self.base_url)
        object.__setattr__(
            self,
            "slippage_bps_per_side",
            _validated_number(
                "execution.slippage_bps_per_side",
                self.slippage_bps_per_side,
                minimum=0.0,
            ),
        )


@dataclass(frozen=True)
class ValidationRuntimeConfig:
    normal_min_net_excess_annual: float = 0.05
    normal_min_sharpe: float = 1.2
    # Backtest acceptance ceiling for realized drawdown.  This is a strategy
    # selection threshold and belongs here, not in [risk]: `risk.max_normal_drawdown`
    # is the operational review/halt trigger for a running book.  The value is
    # deliberately frozen at the original 15% admission requirement.
    normal_max_drawdown: float = 0.15
    daytrade_min_oos_months: int = 24
    daytrade_min_oos_trades: int = 300
    min_paper_sessions: int = 60

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "normal_max_drawdown",
            _validated_number(
                "validation.normal_max_drawdown",
                self.normal_max_drawdown,
                minimum=0.05,
                maximum=0.15,
            ),
        )
        object.__setattr__(
            self,
            "normal_min_net_excess_annual",
            _validated_number(
                "validation.normal_min_net_excess_annual",
                self.normal_min_net_excess_annual,
                minimum=0.05,
            ),
        )
        object.__setattr__(
            self,
            "normal_min_sharpe",
            _validated_number(
                "validation.normal_min_sharpe",
                self.normal_min_sharpe,
                minimum=1.2,
            ),
        )
        _validated_int(
            "validation.daytrade_min_oos_months",
            self.daytrade_min_oos_months,
            minimum=24,
        )
        _validated_int(
            "validation.daytrade_min_oos_trades",
            self.daytrade_min_oos_trades,
            minimum=300,
        )
        _validated_int(
            "validation.min_paper_sessions",
            self.min_paper_sessions,
            minimum=60,
        )


class _ImmutableFacade:
    __slots__ = ()

    def __setattr__(self, name: str, value: object) -> None:
        raise FrozenInstanceError(f"cannot assign to field '{name}'")


class _SecretProvider(_ImmutableFacade):
    """Process-only credentials with no generic serialization surface."""

    __slots__ = ("__api_key", "__secret_key")

    def __init__(self, api_key: object = "", secret_key: object = "") -> None:
        object.__setattr__(
            self,
            "_SecretProvider__api_key",
            _validated_string_secret("environment.ALPACA_API_KEY", api_key),
        )
        object.__setattr__(
            self,
            "_SecretProvider__secret_key",
            _validated_string_secret("environment.ALPACA_SECRET_KEY", secret_key),
        )

    @property
    def api_key(self) -> str:
        return self.__api_key

    @property
    def secret_key(self) -> str:
        return self.__secret_key

    def __repr__(self) -> str:
        return "_SecretProvider(<redacted>)"


class BrokerRuntimeConfig(_ImmutableFacade):
    """Read-only compatibility view over execution config and process secrets."""

    __slots__ = ("_primary", "_base_url", "_backup", "_secrets")

    def __init__(
        self,
        primary: object,
        base_url: object,
        api_key: object = "",
        secret_key: object = "",
        backup: object = "IBKR",
    ) -> None:
        self._initialize(
            primary=primary,
            base_url=base_url,
            backup=backup,
            secrets=_SecretProvider(api_key, secret_key),
        )

    @classmethod
    def _from_provider(
        cls,
        *,
        primary: object,
        base_url: object,
        backup: object,
        secrets: _SecretProvider,
    ) -> BrokerRuntimeConfig:
        if not isinstance(secrets, _SecretProvider):
            raise ValueError("environment: invalid secret provider")
        instance = cls.__new__(cls)
        instance._initialize(
            primary=primary,
            base_url=base_url,
            backup=backup,
            secrets=secrets,
        )
        return instance

    def _initialize(
        self,
        *,
        primary: object,
        base_url: object,
        backup: object,
        secrets: _SecretProvider,
    ) -> None:
        object.__setattr__(self, "_primary", _validated_string("broker.primary", primary))
        object.__setattr__(self, "_base_url", _validated_string("broker.base_url", base_url))
        object.__setattr__(self, "_backup", _validated_string("broker.backup", backup))
        object.__setattr__(self, "_secrets", secrets)

    @property
    def primary(self) -> str:
        return self._primary

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def backup(self) -> str:
        return self._backup

    @property
    def api_key(self) -> str:
        return self._secrets.api_key

    @property
    def secret_key(self) -> str:
        return self._secrets.secret_key

    def __repr__(self) -> str:
        return (
            "BrokerRuntimeConfig("
            f"primary={self.primary!r}, base_url={self.base_url!r}, "
            f"backup={self.backup!r}, credentials=<redacted>)"
        )


class RuntimeConfig(_ImmutableFacade):
    """Immutable runtime configuration with process credentials kept private."""

    __slots__ = (
        "_data",
        "_normal",
        "_daytrade",
        "_portfolio",
        "_risk",
        "_execution",
        "_validation",
        "_secrets",
    )

    def __init__(
        self,
        data: DataRuntimeConfig,
        normal: NormalRuntimeConfig,
        daytrade: DayTradeRuntimeConfig,
        portfolio: PortfolioRuntimeConfig,
        risk: RiskRuntimeConfig,
        execution: ExecutionRuntimeConfig,
        validation: ValidationRuntimeConfig,
        *,
        _secret_provider: _SecretProvider | None = None,
    ) -> None:
        sections = (
            ("data", data, DataRuntimeConfig),
            ("normal", normal, NormalRuntimeConfig),
            ("daytrade", daytrade, DayTradeRuntimeConfig),
            ("portfolio", portfolio, PortfolioRuntimeConfig),
            ("risk", risk, RiskRuntimeConfig),
            ("execution", execution, ExecutionRuntimeConfig),
            ("validation", validation, ValidationRuntimeConfig),
        )
        for section_name, section, expected_type in sections:
            if type(section) is not expected_type:
                raise ValueError(
                    f"{section_name}: expected {expected_type.__name__}"
                )
            object.__setattr__(self, f"_{section_name}", section)
        if _secret_provider is None:
            _secret_provider = _SecretProvider()
        if not isinstance(_secret_provider, _SecretProvider):
            raise ValueError("environment: invalid secret provider")
        object.__setattr__(self, "_secrets", _secret_provider)

    def normal_acceptance_thresholds(self):
        """Admission thresholds for the normal family, driven by [validation]."""

        from .strategy_admission import NormalAcceptanceThresholds

        return NormalAcceptanceThresholds.from_runtime(self._validation)

    def daytrade_acceptance_thresholds(self):
        """Admission thresholds for the DayTrade family, driven by [validation]."""

        from .strategy_admission import DayTradeAcceptanceThresholds

        return DayTradeAcceptanceThresholds.from_runtime(self._validation)

    def normal_capital(self) -> float:
        """Capital allocated to the normal sleeve.

        The 90/10 split was enforced only on the DayTrade side, which scales by
        ``daytrade_fraction`` inside ``DayTradeConfig``.  The normal runners ran
        on full account capital, so the sleeve's contribution to account return
        was overstated by the reciprocal of ``normal_fraction``.
        """

        return self._portfolio.initial_cash * self._portfolio.normal_fraction

    def daytrade_capital(self) -> float:
        """Capital allocated to the DayTrade sleeve."""

        return self._portfolio.initial_cash * self._portfolio.daytrade_fraction

    def normal_cost_bps_per_side(self) -> float:
        """Total per-side normal execution cost, commissions plus slippage."""

        return self._normal.cost_bps_per_side + self._execution.slippage_bps_per_side

    def daytrade_cost_bps_per_side(self) -> float:
        """Total per-side DayTrade execution cost, commissions plus slippage."""

        return self._daytrade.cost_bps_per_side + self._execution.slippage_bps_per_side

    def paper_promotion_min_sessions(self) -> int:
        """Unique reconciled paper sessions required before live promotion."""

        return self._validation.min_paper_sessions

    @property
    def data(self) -> DataRuntimeConfig:
        return self._data

    @property
    def normal(self) -> NormalRuntimeConfig:
        return self._normal

    @property
    def daytrade(self) -> DayTradeRuntimeConfig:
        return self._daytrade

    @property
    def portfolio(self) -> PortfolioRuntimeConfig:
        return self._portfolio

    @property
    def risk(self) -> RiskRuntimeConfig:
        return self._risk

    @property
    def execution(self) -> ExecutionRuntimeConfig:
        return self._execution

    @property
    def validation(self) -> ValidationRuntimeConfig:
        return self._validation

    @property
    def broker(self) -> BrokerRuntimeConfig:
        return BrokerRuntimeConfig._from_provider(
            primary=self.execution.primary_broker,
            backup=self.execution.backup_broker,
            base_url=self.execution.base_url,
            secrets=self._secrets,
        )

    def public_payload(self) -> dict[str, dict[str, object]]:
        """Return stable, JSON-compatible configuration data without credentials."""

        return {
            "data": {
                "root": str(self.data.root),
                "daily_feed": self.data.daily_feed,
                "minute_feed": self.data.minute_feed,
                "schema_version": self.data.schema_version,
                "require_complete_coverage": self.data.require_complete_coverage,
            },
            "normal": {
                "benchmark": self.normal.benchmark,
                "universe_mode": self.normal.universe_mode,
                "execution_price": self.normal.execution_price,
                "cost_bps_per_side": self.normal.cost_bps_per_side,
                "lookback_days": self.normal.lookback_days,
                "top_n": self.normal.top_n,
                "residual_estimation_months": self.normal.residual_estimation_months,
                "residual_skip_months": self.normal.residual_skip_months,
                "covariance_shrinkage": self.normal.covariance_shrinkage,
                "no_trade_band": self.normal.no_trade_band,
                "turnover_penalty": self.normal.turnover_penalty,
                "exit_rank_buffer": self.normal.exit_rank_buffer,
                "max_adv_participation": self.normal.max_adv_participation,
                "momentum_rebound_multiplier": self.normal.momentum_rebound_multiplier,
                "redistribute_constrained_capital": self.normal.redistribute_constrained_capital,
                "volatility_targeting": self.normal.volatility_targeting,
                "min_funded_names": self.normal.min_funded_names,
            },
            "daytrade": {
                "entry_start": self.daytrade.entry_start,
                "entry_end": self.daytrade.entry_end,
                "cumulative_vwap": self.daytrade.cumulative_vwap,
                "side": self.daytrade.side,
                "vwap_hysteresis_bps": self.daytrade.vwap_hysteresis_bps,
                "stop_basis": self.daytrade.stop_basis,
                "atr_stop_multiple": self.daytrade.atr_stop_multiple,
                "opening_range_stop_multiple": self.daytrade.opening_range_stop_multiple,
                "min_opening_range_atr": self.daytrade.min_opening_range_atr,
                "max_opening_range_atr": self.daytrade.max_opening_range_atr,
                "min_relative_volume": self.daytrade.min_relative_volume,
                "min_directional_gap": self.daytrade.min_directional_gap,
                "max_directional_gap": self.daytrade.max_directional_gap,
                "min_premarket_volume": self.daytrade.min_premarket_volume,
                "min_directional_spy_return": self.daytrade.min_directional_spy_return,
                "min_directional_qqq_return": self.daytrade.min_directional_qqq_return,
                "min_directional_sector_strength": self.daytrade.min_directional_sector_strength,
                "max_short_gap_abs": self.daytrade.max_short_gap_abs,
                "max_portfolio_heat_fraction": self.daytrade.max_portfolio_heat_fraction,
                "correlation_penalty": self.daytrade.correlation_penalty,
                "cost_bps_per_side": self.daytrade.cost_bps_per_side,
                "max_volume_participation": self.daytrade.max_volume_participation,
                "live_submission_enabled": self.daytrade.live_submission_enabled,
                "enforce_drawdown_halt": self.daytrade.enforce_drawdown_halt,
            },
            "portfolio": {
                "initial_cash": self.portfolio.initial_cash,
                "normal_fraction": self.portfolio.normal_fraction,
                "daytrade_fraction": self.portfolio.daytrade_fraction,
            },
            "risk": {
                "max_single_stock_weight": self.risk.max_single_stock_weight,
                "max_sector_weight": self.risk.max_sector_weight,
                "max_portfolio_vol_annual": self.risk.max_portfolio_vol_annual,
                "max_normal_drawdown": self.risk.max_normal_drawdown,
                "max_daytrade_drawdown": self.risk.max_daytrade_drawdown,
                "max_market_beta_exposure": self.risk.max_market_beta_exposure,
            },
            "execution": {
                "primary_broker": self.execution.primary_broker,
                "backup_broker": self.execution.backup_broker,
                "base_url": self.execution.base_url,
                "slippage_bps_per_side": self.execution.slippage_bps_per_side,
            },
            "validation": {
                "normal_min_net_excess_annual": self.validation.normal_min_net_excess_annual,
                "normal_min_sharpe": self.validation.normal_min_sharpe,
                "normal_max_drawdown": self.validation.normal_max_drawdown,
                "daytrade_min_oos_months": self.validation.daytrade_min_oos_months,
                "daytrade_min_oos_trades": self.validation.daytrade_min_oos_trades,
                "min_paper_sessions": self.validation.min_paper_sessions,
            },
        }

    def __repr__(self) -> str:
        return (
            "RuntimeConfig("
            f"data={self.data!r}, normal={self.normal!r}, daytrade={self.daytrade!r}, "
            f"portfolio={self.portfolio!r}, risk={self.risk!r}, "
            f"execution={self.execution!r}, validation={self.validation!r})"
        )


def load_runtime_config(
    path: Path,
    environ: Mapping[str, str] | None = None,
) -> RuntimeConfig:
    """Load a runtime config, rejecting ambiguous, unknown, or unsafe values."""

    raw = _load_toml(Path(path))
    _reject_toml_secrets(raw)
    _validate_structure(raw)

    if environ is None:
        environment: Mapping[str, str] = os.environ
    elif not isinstance(environ, Mapping):
        raise ValueError("environment: expected a mapping")
    else:
        environment = environ
    secrets = _SecretProvider(
        api_key=_environment_secret(environment, "ALPACA_API_KEY"),
        secret_key=_environment_secret(environment, "ALPACA_SECRET_KEY"),
    )

    data_values = dict(_section(raw, "data"))
    if "root" in data_values:
        data_values["root"] = Path(
            _validated_string("data.root", data_values["root"])
        )
    if "STOCKBOT_DATA_ROOT" in environment:
        data_values["root"] = Path(
            _validated_string(
                "environment.STOCKBOT_DATA_ROOT",
                environment["STOCKBOT_DATA_ROOT"],
            )
        )

    execution_values = _canonical_execution_section(raw)
    try:
        execution_config = ExecutionRuntimeConfig(**execution_values)
    except ValueError as error:
        if "broker" not in raw:
            raise
        raise ValueError(_legacy_execution_error(str(error))) from None

    return RuntimeConfig(
        data=DataRuntimeConfig(**data_values),
        normal=NormalRuntimeConfig(**dict(_section(raw, "normal"))),
        daytrade=DayTradeRuntimeConfig(**dict(_section(raw, "daytrade"))),
        portfolio=PortfolioRuntimeConfig(**dict(_section(raw, "portfolio"))),
        risk=RiskRuntimeConfig(**dict(_section(raw, "risk"))),
        execution=execution_config,
        validation=ValidationRuntimeConfig(**dict(_section(raw, "validation"))),
        _secret_provider=secrets,
    )


def _validated_string_secret(path: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{path}: expected a string")
    return value


def _environment_secret(environment: Mapping[str, str], name: str) -> str:
    return _validated_string_secret(f"environment.{name}", environment.get(name, ""))


def _load_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


_CANONICAL_KEYS = {
    "data": {"root", "daily_feed", "minute_feed", "schema_version", "require_complete_coverage"},
    "normal": {
        "benchmark",
        "universe_mode",
        "execution_price",
        "cost_bps_per_side",
        "lookback_days",
        "top_n",
        "residual_estimation_months",
        "residual_skip_months",
        "covariance_shrinkage",
        "no_trade_band",
        "turnover_penalty",
        "exit_rank_buffer",
        "max_adv_participation",
        "momentum_rebound_multiplier",
        "redistribute_constrained_capital",
        "volatility_targeting",
        "min_funded_names",
    },
    "daytrade": {
        "entry_start",
        "entry_end",
        "cumulative_vwap",
        "side",
        "vwap_hysteresis_bps",
        "stop_basis",
        "atr_stop_multiple",
        "opening_range_stop_multiple",
        "min_opening_range_atr",
        "max_opening_range_atr",
        "min_relative_volume",
        "min_directional_gap",
        "max_directional_gap",
        "min_premarket_volume",
        "min_directional_spy_return",
        "min_directional_qqq_return",
        "min_directional_sector_strength",
        "max_short_gap_abs",
        "max_portfolio_heat_fraction",
        "correlation_penalty",
        "cost_bps_per_side",
        "max_volume_participation",
        "live_submission_enabled",
        "enforce_drawdown_halt",
    },
    "portfolio": {"initial_cash", "normal_fraction", "daytrade_fraction"},
    "risk": {
        "max_single_stock_weight",
        "max_sector_weight",
        "max_portfolio_vol_annual",
        "max_normal_drawdown",
        "max_daytrade_drawdown",
        "max_market_beta_exposure",
    },
    "execution": {"primary_broker", "backup_broker", "base_url", "slippage_bps_per_side"},
    "validation": {
        "normal_min_net_excess_annual",
        "normal_min_sharpe",
        "normal_max_drawdown",
        "daytrade_min_oos_months",
        "daytrade_min_oos_trades",
        "min_paper_sessions",
    },
}
_LEGACY_BROKER_KEYS = {"primary", "backup", "base_url"}


def _reject_toml_secrets(value: object, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            nested_path = path + (str(key),)
            if key in {"api_key", "secret_key"}:
                raise ValueError(
                    "%s: secrets are only accepted from environment variables"
                    % ".".join(nested_path)
                )
            _reject_toml_secrets(nested, nested_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_toml_secrets(nested, path + (str(index),))


def _validate_structure(raw: Mapping[str, object]) -> None:
    allowed_sections = set(_CANONICAL_KEYS) | {"broker"}
    for section_name, section in raw.items():
        if section_name not in allowed_sections:
            raise ValueError(f"{section_name}: unknown configuration section")
        if not isinstance(section, Mapping):
            raise ValueError(f"{section_name}: expected a TOML table")
        allowed_keys = (
            _LEGACY_BROKER_KEYS
            if section_name == "broker"
            else _CANONICAL_KEYS[section_name]
        )
        for key in section:
            if key not in allowed_keys:
                raise ValueError(f"{section_name}.{key}: unknown configuration key")
    if "broker" in raw and "execution" in raw:
        raise ValueError(
            "broker/execution: legacy and canonical sections cannot be used together"
        )


def _section(raw: Mapping[str, object], name: str) -> Mapping[str, object]:
    section = raw.get(name, {})
    if not isinstance(section, Mapping):
        raise ValueError(f"{name}: expected a TOML table")
    return section


def _canonical_execution_section(raw: Mapping[str, object]) -> dict[str, object]:
    if "execution" in raw:
        return dict(_section(raw, "execution"))
    if "broker" not in raw:
        return {}
    legacy = _section(raw, "broker")
    translated: dict[str, object] = {}
    for legacy_key, canonical_key in (
        ("primary", "primary_broker"),
        ("backup", "backup_broker"),
        ("base_url", "base_url"),
    ):
        if legacy_key in legacy:
            translated[canonical_key] = legacy[legacy_key]
    return translated


def _legacy_execution_error(message: str) -> str:
    for canonical_path, legacy_path in (
        ("execution.primary_broker", "broker.primary"),
        ("execution.backup_broker", "broker.backup"),
        ("execution.base_url", "broker.base_url"),
    ):
        if message.startswith(canonical_path):
            return legacy_path + message[len(canonical_path) :]
    return message
