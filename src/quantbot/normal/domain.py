"""Immutable inputs and outputs for normal-trading portfolio construction."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence

from ..data import SecurityMasterSnapshot, canonical_security_master_snapshot_hash
from ..factors import FactorSleeve
from ..regime import Regime
from ..trading import TargetPortfolio


def _number(value, name, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be finite") from error
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _float_mapping(values, name, *, non_negative=False):
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    result = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-empty strings")
        normalized = key.strip()
        if normalized in result:
            raise ValueError(f"{name} contains duplicate normalized keys")
        result[normalized] = _number(
            value,
            f"{name}[{normalized}]",
            minimum=0.0 if non_negative else None,
        )
    return MappingProxyType(dict(sorted(result.items())))


def _history_mapping(values, name):
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    result = {}
    for key, history in values.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-empty strings")
        normalized = key.strip()
        if normalized in result:
            raise ValueError(f"{name} contains duplicate normalized keys")
        if isinstance(history, (str, bytes)):
            raise ValueError(f"{name}[{normalized}] must be a numeric sequence")
        sequence = tuple(_number(item, f"{name}[{normalized}]") for item in history)
        if len(sequence) < 12:
            raise ValueError(f"{name}[{normalized}] requires at least 12 observations")
        result[normalized] = sequence
    return MappingProxyType(dict(sorted(result.items())))


@dataclass(frozen=True, slots=True)
class NormalPortfolioConfig:
    top_n: int = 20
    exit_rank_buffer: int = 5
    target_gross_exposure: float = 1.0
    max_single_stock_weight: float = 0.05
    max_sector_weight: float = 0.25
    max_market_beta_exposure: float = 1.0
    target_volatility_annual: float = 0.15
    max_adv_participation: float = 0.01
    covariance_shrinkage: float = 0.50
    no_trade_band: float = 0.0025
    turnover_penalty: float = 0.50
    periods_per_year: int = 252
    momentum_rebound_multiplier: float = 0.50
    # Capital-efficiency controls.  The defaults reproduce the historical
    # shrink-only behaviour exactly, so frozen experiment identities stay
    # reproducible; enabling either one is a new candidate configuration.
    redistribute_constrained_capital: bool = False
    volatility_targeting: str = "cap_only"
    min_funded_names: int = 0

    def __post_init__(self):
        _positive_integer(self.top_n, "top_n")
        if not isinstance(self.redistribute_constrained_capital, bool):
            raise ValueError("redistribute_constrained_capital must be a boolean")
        if self.volatility_targeting not in {"cap_only", "two_sided"}:
            raise ValueError("volatility_targeting must be cap_only or two_sided")
        if (
            isinstance(self.min_funded_names, bool)
            or not isinstance(self.min_funded_names, int)
            or self.min_funded_names < 0
        ):
            raise ValueError("min_funded_names must be a non-negative integer")
        if isinstance(self.exit_rank_buffer, bool) or not isinstance(self.exit_rank_buffer, int) or self.exit_rank_buffer < 0:
            raise ValueError("exit_rank_buffer must be a non-negative integer")
        for name in (
            "target_gross_exposure",
            "max_single_stock_weight",
            "max_sector_weight",
            "max_adv_participation",
            "covariance_shrinkage",
            "no_trade_band",
            "momentum_rebound_multiplier",
        ):
            object.__setattr__(self, name, _number(getattr(self, name), name, minimum=0.0, maximum=1.0))
        for name in ("max_market_beta_exposure", "target_volatility_annual"):
            value = _number(getattr(self, name), name, minimum=0.0)
            if value == 0:
                raise ValueError(f"{name} must be positive")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "turnover_penalty", _number(self.turnover_penalty, "turnover_penalty", minimum=0.0))
        _positive_integer(self.periods_per_year, "periods_per_year")

    @classmethod
    def from_runtime(cls, normal, risk):
        """Translate canonical TOML sections without coupling to their classes."""

        return cls(
            top_n=normal.top_n,
            exit_rank_buffer=normal.exit_rank_buffer,
            max_single_stock_weight=risk.max_single_stock_weight,
            max_sector_weight=risk.max_sector_weight,
            max_market_beta_exposure=risk.max_market_beta_exposure,
            target_volatility_annual=risk.max_portfolio_vol_annual,
            max_adv_participation=normal.max_adv_participation,
            covariance_shrinkage=normal.covariance_shrinkage,
            no_trade_band=normal.no_trade_band,
            turnover_penalty=normal.turnover_penalty,
            momentum_rebound_multiplier=normal.momentum_rebound_multiplier,
            redistribute_constrained_capital=normal.redistribute_constrained_capital,
            volatility_targeting=normal.volatility_targeting,
            min_funded_names=normal.min_funded_names,
        )


@dataclass(frozen=True, slots=True)
class NormalDrawdownPolicy:
    """De-risking schedule that enforces a declared maximum drawdown.

    ``risk.max_normal_drawdown`` was parsed, hashed and then read by nothing:
    no normal strategy path enforced it.  A long-only book run at a 15%
    annualized volatility target will breach a 15% drawdown limit in any
    ordinary equity bear market, so the limit has to be enforced by cutting
    exposure rather than by assuming the volatility target implies it.

    The band between ``recovery_drawdown`` and ``review_drawdown`` is
    deliberate hysteresis: without it the book re-levers and de-levers on every
    small oscillation around the threshold and pays the turnover twice.
    """

    review_drawdown: float = 0.15
    review_scale: float = 0.50
    halt_drawdown: float = 0.225
    halt_scale: float = 0.0
    recovery_drawdown: float = 0.10

    def __post_init__(self):
        for name in (
            "review_drawdown",
            "review_scale",
            "halt_drawdown",
            "halt_scale",
            "recovery_drawdown",
        ):
            object.__setattr__(
                self, name, _number(getattr(self, name), name, minimum=0.0, maximum=1.0)
            )
        if self.review_drawdown <= 0:
            raise ValueError("review_drawdown must be positive")
        if self.halt_drawdown < self.review_drawdown:
            raise ValueError("halt_drawdown must not precede review_drawdown")
        if self.halt_scale > self.review_scale:
            raise ValueError("halt_scale must not exceed review_scale")
        if self.recovery_drawdown > self.review_drawdown:
            raise ValueError("recovery_drawdown must not exceed review_drawdown")

    @classmethod
    def from_runtime(cls, risk):
        """Derive the schedule from the declared normal drawdown limit."""

        limit = _number(risk.max_normal_drawdown, "max_normal_drawdown", minimum=0.0)
        if limit <= 0:
            raise ValueError("max_normal_drawdown must be positive")
        return cls(
            review_drawdown=limit,
            review_scale=0.50,
            halt_drawdown=min(1.0, limit * 1.5),
            halt_scale=0.0,
            recovery_drawdown=limit * (2.0 / 3.0),
        )

    def exposure_scale(self, drawdown: float, previous_scale: float) -> float:
        """Return the exposure multiplier for the current drawdown."""

        drawdown = _number(drawdown, "drawdown", minimum=0.0)
        previous = _number(previous_scale, "previous_scale", minimum=0.0, maximum=1.0)
        if drawdown >= self.halt_drawdown:
            return self.halt_scale
        if drawdown >= self.review_drawdown:
            return min(previous, self.review_scale)
        if drawdown <= self.recovery_drawdown:
            return 1.0
        return previous


@dataclass(frozen=True, slots=True)
class NormalRegimeContext:
    regime: Regime
    confidence: float = 1.0
    high_volatility_rebound: bool = False

    def __post_init__(self):
        if not isinstance(self.regime, Regime):
            raise ValueError("regime must be a Regime")
        object.__setattr__(self, "confidence", _number(self.confidence, "confidence", minimum=0.0, maximum=1.0))
        if not isinstance(self.high_volatility_rebound, bool):
            raise ValueError("high_volatility_rebound must be a boolean")


@dataclass(frozen=True, slots=True)
class NormalAllocationRequest:
    security_master: SecurityMasterSnapshot
    sleeves: tuple[FactorSleeve, ...]
    sleeve_confidences: Mapping[str, float]
    sleeve_return_history: Mapping[str, Sequence[float]]
    asset_return_history: Mapping[str, Sequence[float]]
    current_weights: Mapping[str, float]
    market_betas: Mapping[str, float]
    adv_dollars: Mapping[str, float]
    capital: float
    regime: NormalRegimeContext

    def __post_init__(self):
        if not isinstance(self.security_master, SecurityMasterSnapshot):
            raise TypeError("security_master must be a SecurityMasterSnapshot")
        sleeves = tuple(self.sleeves)
        if not sleeves or any(not isinstance(item, FactorSleeve) for item in sleeves):
            raise ValueError("sleeves must contain FactorSleeve values")
        names = tuple(item.name for item in sleeves)
        if len(set(names)) != len(names):
            raise ValueError("sleeve names must be unique")
        expected_hash = canonical_security_master_snapshot_hash(self.security_master)
        active = set(self.security_master.security_ids)
        for sleeve in sleeves:
            if sleeve.security_master_hash != expected_hash:
                raise ValueError("every sleeve must bind to the request security master")
            if set(sleeve.scores).union(sleeve.missing_security_ids) != active:
                raise ValueError("every sleeve must explicitly cover the active universe")
        object.__setattr__(self, "sleeves", sleeves)
        confidences = _float_mapping(self.sleeve_confidences, "sleeve_confidences", non_negative=True)
        sleeve_history = _history_mapping(self.sleeve_return_history, "sleeve_return_history")
        if set(confidences) != set(names) or set(sleeve_history) != set(names):
            raise ValueError("sleeve confidence/history keys must exactly match sleeve names")
        if not any(confidences.values()):
            raise ValueError("at least one sleeve confidence must be positive")
        if len({len(values) for values in sleeve_history.values()}) != 1:
            raise ValueError("sleeve return histories must be aligned")
        object.__setattr__(self, "sleeve_confidences", confidences)
        object.__setattr__(self, "sleeve_return_history", sleeve_history)
        asset_history = _history_mapping(self.asset_return_history, "asset_return_history")
        if not set(asset_history).issubset(active):
            raise ValueError("asset_return_history contains a security outside the active universe")
        if asset_history and len({len(values) for values in asset_history.values()}) != 1:
            raise ValueError("asset return histories must be aligned")
        object.__setattr__(self, "asset_return_history", asset_history)
        for field_name, non_negative in (
            ("current_weights", True),
            ("market_betas", False),
            ("adv_dollars", True),
        ):
            values = _float_mapping(getattr(self, field_name), field_name, non_negative=non_negative)
            if not set(values).issubset(active):
                raise ValueError(f"{field_name} contains a security outside the active universe")
            object.__setattr__(self, field_name, values)
        if sum(self.current_weights.values()) > 1.0 + 1e-12:
            raise ValueError("current_weights cannot contain hidden leverage")
        capital = _number(self.capital, "capital", minimum=0.0)
        if capital == 0:
            raise ValueError("capital must be positive")
        object.__setattr__(self, "capital", capital)
        if not isinstance(self.regime, NormalRegimeContext):
            raise TypeError("regime must be a NormalRegimeContext")


@dataclass(frozen=True, slots=True)
class NormalAllocationReport:
    targets: TargetPortfolio
    sleeve_weights: Mapping[str, float]
    combined_scores: Mapping[str, float]
    selected_security_ids: tuple[str, ...]
    expected_turnover: float
    annualized_volatility: float
    exclusions: tuple[str, ...]
    # Diagnostics that make constrained-capital and risk-budget leakage
    # observable instead of silent.  ``capital_utilization`` is realized gross
    # exposure over the regime-permitted target; ``risk_utilization`` is
    # realized annualized volatility over the configured target.
    target_exposure: float = 0.0
    capital_utilization: float = 0.0
    risk_utilization: float = 0.0

    def __post_init__(self):
        if not isinstance(self.targets, TargetPortfolio):
            raise TypeError("targets must be a TargetPortfolio")
        object.__setattr__(self, "sleeve_weights", _float_mapping(self.sleeve_weights, "sleeve_weights", non_negative=True))
        object.__setattr__(self, "combined_scores", _float_mapping(self.combined_scores, "combined_scores"))
        object.__setattr__(self, "selected_security_ids", tuple(self.selected_security_ids))
        object.__setattr__(self, "expected_turnover", _number(self.expected_turnover, "expected_turnover", minimum=0.0))
        object.__setattr__(self, "annualized_volatility", _number(self.annualized_volatility, "annualized_volatility", minimum=0.0))
        object.__setattr__(self, "exclusions", tuple(sorted(set(self.exclusions))))
        for field_name in ("target_exposure", "capital_utilization", "risk_utilization"):
            object.__setattr__(
                self,
                field_name,
                _number(getattr(self, field_name), field_name, minimum=0.0),
            )
