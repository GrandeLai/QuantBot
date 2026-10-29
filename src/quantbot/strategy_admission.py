"""Executable promotion thresholds for normal and intraday strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite

from .acceptance_policy import ACCEPTANCE_POLICY_VERSION
from .audit import RequirementEvidence
from .exposure import ExposureLedger
from .trading import RunManifest
from .validation_protocol import DataAccessPermit


TOLERANCE = 1e-12


def _number(value, name, *, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    try:
        result = float(value)
    except (OverflowError, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite number") from error
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _digest(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _positive_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class EvaluationIdentity:
    start: date
    end: date
    dataset_hash: str
    cost_model_hash: str
    config_hash: str
    code_hash: str

    def __post_init__(self):
        for name in ("start", "end"):
            value = getattr(self, name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise TypeError(f"{name} must be a date")
        if self.end < self.start:
            raise ValueError("evaluation end must not precede start")
        _digest(self.dataset_hash, "dataset_hash")
        _digest(self.cost_model_hash, "cost_model_hash")
        _digest(self.config_hash, "config_hash")
        _digest(self.code_hash, "code_hash")


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    strategy_family: str
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NormalAdmissionMetrics:
    candidate_identity: EvaluationIdentity
    baseline_identity: EvaluationIdentity
    net_cagr: float
    baseline_net_cagr: float
    sharpe: float
    baseline_sharpe: float
    net_excess_annual: float
    max_drawdown: float
    annual_turnover: float
    stressed_net_excess_annual: float
    positive_window_fraction: float
    dsr_confidence: float
    pbo: float
    top_three_profit_contribution: float
    gross_leverage: float
    declared_trials: int
    completed_trials: int
    sealed_data_access_permit: DataAccessPermit

    def __post_init__(self):
        _validate_common_metrics(self)
        for name in (
            "net_cagr",
            "baseline_net_cagr",
            "sharpe",
            "baseline_sharpe",
            "net_excess_annual",
            "stressed_net_excess_annual",
        ):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        object.__setattr__(self, "annual_turnover", _number(self.annual_turnover, "annual_turnover", minimum=0.0))
        object.__setattr__(
            self,
            "top_three_profit_contribution",
            _number(
                self.top_three_profit_contribution,
                "top_three_profit_contribution",
                minimum=0.0,
                maximum=1.0,
            ),
        )


@dataclass(frozen=True, slots=True)
class DayTradeAdmissionMetrics:
    candidate_identity: EvaluationIdentity
    baseline_identity: EvaluationIdentity
    oos_months: int
    oos_trades: int
    net_return_after_5bps: float
    profit_factor: float
    sharpe: float
    max_drawdown: float
    annual_net_return: float
    baseline_annual_net_return: float
    baseline_sharpe: float
    baseline_max_drawdown: float
    stressed_12bps_cumulative_return: float
    stressed_12bps_max_drawdown: float
    positive_quarter_fraction: float
    top_symbol_profit_contribution: float
    top_two_profit_contribution: float
    dsr_confidence: float
    pbo: float
    gross_leverage: float
    declared_trials: int
    completed_trials: int
    sealed_data_access_permit: DataAccessPermit

    def __post_init__(self):
        _validate_common_metrics(self)
        _nonnegative_integer(self.oos_months, "oos_months")
        _nonnegative_integer(self.oos_trades, "oos_trades")
        for name in (
            "net_return_after_5bps",
            "sharpe",
            "annual_net_return",
            "baseline_annual_net_return",
            "baseline_sharpe",
            "stressed_12bps_cumulative_return",
        ):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        object.__setattr__(self, "profit_factor", _number(self.profit_factor, "profit_factor", minimum=0.0))
        object.__setattr__(self, "baseline_max_drawdown", _number(self.baseline_max_drawdown, "baseline_max_drawdown", minimum=0.0, maximum=1.0))
        object.__setattr__(self, "stressed_12bps_max_drawdown", _number(self.stressed_12bps_max_drawdown, "stressed_12bps_max_drawdown", minimum=0.0, maximum=1.0))
        for name in ("positive_quarter_fraction", "top_symbol_profit_contribution", "top_two_profit_contribution"):
            object.__setattr__(self, name, _number(getattr(self, name), name, minimum=0.0, maximum=1.0))


# Backtest/admission ceilings for realized drawdown.  These are strategy
# selection thresholds and are deliberately distinct from the operational
# review/halt triggers in [risk].  The normal ceiling preserves the original
# preregistered acceptance requirement; it must not be relaxed merely because
# a volatility target makes the requirement difficult to satisfy.
NORMAL_ACCEPTANCE_MAX_DRAWDOWN = 0.15
DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN = 0.05
DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN = 0.07


def _stricter_only(instance, floors, ceilings):
    """Reject any threshold looser than the declared admission policy.

    The policy is a floor, not a starting point.  Enforcing it on the type
    rather than at one call site means no construction path -- runtime TOML,
    a test helper, or a future caller -- can weaken admission.  Every field may
    only move in the stricter direction.
    """

    for name, floor in floors.items():
        value = getattr(instance, name)
        if isinstance(floor, int) and not isinstance(floor, bool):
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            if value < floor:
                raise ValueError(
                    f"{name} cannot weaken admission policy: must be at least {floor}"
                )
            continue
        number = _number(value, name)
        if number + TOLERANCE < floor:
            raise ValueError(
                f"{name} cannot weaken admission policy: must be at least {floor}"
            )
        object.__setattr__(instance, name, number)
    for name, ceiling in ceilings.items():
        number = _number(getattr(instance, name), name)
        if number > ceiling + TOLERANCE:
            raise ValueError(
                f"{name} cannot weaken admission policy: must be at most {ceiling}"
            )
        object.__setattr__(instance, name, number)


@dataclass(frozen=True, slots=True)
class NormalAcceptanceThresholds:
    """Every normal admission threshold in one place, stricter-only."""

    min_net_excess_annual: float = 0.05
    min_sharpe: float = 1.20
    min_stressed_net_excess_annual: float = 0.03
    min_positive_window_fraction: float = 0.60
    min_baseline_cagr_improvement: float = 0.02
    min_baseline_sharpe_improvement: float = 0.15
    max_drawdown: float = NORMAL_ACCEPTANCE_MAX_DRAWDOWN
    max_annual_turnover: float = 4.0
    max_top_three_profit_contribution: float = 0.50

    def __post_init__(self):
        _stricter_only(
            self,
            {
                "min_net_excess_annual": 0.05,
                "min_sharpe": 1.20,
                "min_stressed_net_excess_annual": 0.03,
                "min_positive_window_fraction": 0.60,
                "min_baseline_cagr_improvement": 0.02,
                "min_baseline_sharpe_improvement": 0.15,
            },
            {
                "max_drawdown": NORMAL_ACCEPTANCE_MAX_DRAWDOWN,
                "max_annual_turnover": 4.0,
                "max_top_three_profit_contribution": 0.50,
            },
        )

    @classmethod
    def from_runtime(cls, validation):
        """Map the [validation] section onto the thresholds it declares."""

        return cls(
            min_net_excess_annual=validation.normal_min_net_excess_annual,
            min_sharpe=validation.normal_min_sharpe,
            max_drawdown=validation.normal_max_drawdown,
        )


@dataclass(frozen=True, slots=True)
class DayTradeAcceptanceThresholds:
    """Every DayTrade admission threshold in one place, stricter-only."""

    min_oos_months: int = 24
    min_oos_trades: int = 300
    min_profit_factor: float = 1.20
    min_sharpe: float = 1.00
    min_baseline_return_improvement: float = 0.02
    min_baseline_sharpe_improvement: float = 0.25
    min_stressed_cumulative_return: float = -0.02
    min_positive_quarter_fraction: float = 0.60
    max_drawdown: float = DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN
    max_stressed_drawdown: float = DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN
    max_top_symbol_profit_contribution: float = 0.25
    max_top_two_profit_contribution: float = 0.50

    def __post_init__(self):
        _stricter_only(
            self,
            {
                "min_oos_months": 24,
                "min_oos_trades": 300,
                "min_profit_factor": 1.20,
                "min_sharpe": 1.00,
                "min_baseline_return_improvement": 0.02,
                "min_baseline_sharpe_improvement": 0.25,
                "min_stressed_cumulative_return": -0.02,
                "min_positive_quarter_fraction": 0.60,
            },
            {
                "max_drawdown": DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN,
                "max_stressed_drawdown": DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN,
                "max_top_symbol_profit_contribution": 0.25,
                "max_top_two_profit_contribution": 0.50,
            },
        )

    @classmethod
    def from_runtime(cls, validation):
        return cls(
            min_oos_months=validation.daytrade_min_oos_months,
            min_oos_trades=validation.daytrade_min_oos_trades,
        )


NORMAL_ACCEPTANCE_THRESHOLDS = NormalAcceptanceThresholds()
DAYTRADE_ACCEPTANCE_THRESHOLDS = DayTradeAcceptanceThresholds()


def evaluate_normal_admission(
    metrics: NormalAdmissionMetrics,
    thresholds: NormalAcceptanceThresholds = NORMAL_ACCEPTANCE_THRESHOLDS,
    *,
    _clock=None,
) -> AdmissionDecision:
    if not isinstance(metrics, NormalAdmissionMetrics):
        raise TypeError("metrics must be NormalAdmissionMetrics")
    if not isinstance(thresholds, NormalAcceptanceThresholds):
        raise TypeError("thresholds must be NormalAcceptanceThresholds")
    reasons = _common_reasons(metrics, "normal", _clock=_clock)
    minimums = (
        (
            metrics.net_cagr - metrics.baseline_net_cagr,
            thresholds.min_baseline_cagr_improvement,
            "baseline_cagr_improvement",
        ),
        (
            metrics.sharpe - metrics.baseline_sharpe,
            thresholds.min_baseline_sharpe_improvement,
            "baseline_sharpe_improvement",
        ),
        (metrics.net_excess_annual, thresholds.min_net_excess_annual, "net_excess_annual"),
        (metrics.sharpe, thresholds.min_sharpe, "sharpe"),
        (
            metrics.stressed_net_excess_annual,
            thresholds.min_stressed_net_excess_annual,
            "two_x_cost_stress",
        ),
        (
            metrics.positive_window_fraction,
            thresholds.min_positive_window_fraction,
            "positive_windows",
        ),
    )
    reasons.extend(name for value, floor, name in minimums if value + TOLERANCE < floor)
    maximums = (
        (metrics.max_drawdown, thresholds.max_drawdown, "max_drawdown"),
        (metrics.annual_turnover, thresholds.max_annual_turnover, "annual_turnover"),
        (
            metrics.top_three_profit_contribution,
            thresholds.max_top_three_profit_contribution,
            "top_three_concentration",
        ),
    )
    reasons.extend(name for value, ceiling, name in maximums if value > ceiling + TOLERANCE)
    return AdmissionDecision("normal", not reasons, tuple(reasons))


def evaluate_daytrade_admission(
    metrics: DayTradeAdmissionMetrics,
    thresholds: DayTradeAcceptanceThresholds = DAYTRADE_ACCEPTANCE_THRESHOLDS,
    *,
    _clock=None,
) -> AdmissionDecision:
    if not isinstance(metrics, DayTradeAdmissionMetrics):
        raise TypeError("metrics must be DayTradeAdmissionMetrics")
    if not isinstance(thresholds, DayTradeAcceptanceThresholds):
        raise TypeError("thresholds must be DayTradeAcceptanceThresholds")
    reasons = _common_reasons(metrics, "daytrade", _clock=_clock)
    if metrics.oos_months < thresholds.min_oos_months:
        reasons.append("oos_months")
    if metrics.oos_trades < thresholds.min_oos_trades:
        reasons.append("oos_trades")
    if metrics.net_return_after_5bps <= 0:
        reasons.append("five_bps_net_return")
    minimums = (
        (metrics.profit_factor, thresholds.min_profit_factor, "profit_factor"),
        (metrics.sharpe, thresholds.min_sharpe, "sharpe"),
        (
            metrics.annual_net_return - metrics.baseline_annual_net_return,
            thresholds.min_baseline_return_improvement,
            "baseline_return_improvement",
        ),
        (
            metrics.sharpe - metrics.baseline_sharpe,
            thresholds.min_baseline_sharpe_improvement,
            "baseline_sharpe_improvement",
        ),
        (
            metrics.positive_quarter_fraction,
            thresholds.min_positive_quarter_fraction,
            "positive_quarters",
        ),
    )
    reasons.extend(name for value, floor, name in minimums if value + TOLERANCE < floor)
    maximums = (
        (metrics.max_drawdown, thresholds.max_drawdown, "max_drawdown"),
        (
            metrics.top_symbol_profit_contribution,
            thresholds.max_top_symbol_profit_contribution,
            "single_symbol_concentration",
        ),
        (
            metrics.top_two_profit_contribution,
            thresholds.max_top_two_profit_contribution,
            "top_two_concentration",
        ),
    )
    reasons.extend(name for value, ceiling, name in maximums if value > ceiling + TOLERANCE)
    if metrics.max_drawdown > metrics.baseline_max_drawdown + TOLERANCE:
        reasons.append("baseline_drawdown_worsened")
    if (
        metrics.stressed_12bps_cumulative_return
        < thresholds.min_stressed_cumulative_return - TOLERANCE
        or metrics.stressed_12bps_max_drawdown
        > thresholds.max_stressed_drawdown + TOLERANCE
    ):
        reasons.append("twelve_bps_stress")
    return AdmissionDecision("daytrade", not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class CapitalAllocation:
    normal_fraction: float
    daytrade_fraction: float
    locked_to_baseline: bool


def capital_allocation(
    *,
    normal_admitted: bool,
    daytrade_admitted: bool,
    requested_normal_fraction: float = 0.90,
    requested_daytrade_fraction: float = 0.10,
) -> CapitalAllocation:
    if not isinstance(normal_admitted, bool) or not isinstance(daytrade_admitted, bool):
        raise ValueError("strategy admission flags must be booleans")
    if not (normal_admitted and daytrade_admitted):
        return CapitalAllocation(0.90, 0.10, True)
    normal = _number(requested_normal_fraction, "requested_normal_fraction", minimum=0.0, maximum=1.0)
    daytrade = _number(requested_daytrade_fraction, "requested_daytrade_fraction", minimum=0.0, maximum=1.0)
    if abs(normal + daytrade - 1.0) > TOLERANCE:
        raise ValueError("requested capital fractions must sum to one")
    return CapitalAllocation(normal, daytrade, False)


def build_normal_admission_evidence(
    metrics: NormalAdmissionMetrics,
    manifest: RunManifest,
    *,
    _clock=None,
) -> RequirementEvidence:
    return _build_admission_evidence(
        "normal_strategy_admission",
        evaluate_normal_admission(metrics, _clock=_clock),
        metrics.candidate_identity,
        manifest,
    )


def build_daytrade_admission_evidence(
    metrics: DayTradeAdmissionMetrics,
    manifest: RunManifest,
    *,
    _clock=None,
) -> RequirementEvidence:
    return _build_admission_evidence(
        "daytrade_strategy_admission",
        evaluate_daytrade_admission(metrics, _clock=_clock),
        metrics.candidate_identity,
        manifest,
    )


def _validate_common_metrics(metrics):
    if not isinstance(metrics.candidate_identity, EvaluationIdentity) or not isinstance(metrics.baseline_identity, EvaluationIdentity):
        raise TypeError("candidate and baseline identities must be EvaluationIdentity")
    for name in ("max_drawdown", "positive_window_fraction" if isinstance(metrics, NormalAdmissionMetrics) else "positive_quarter_fraction", "dsr_confidence", "pbo"):
        object.__setattr__(metrics, name, _number(getattr(metrics, name), name, minimum=0.0, maximum=1.0))
    object.__setattr__(metrics, "gross_leverage", _number(metrics.gross_leverage, "gross_leverage", minimum=0.0))
    _positive_integer(metrics.declared_trials, "declared_trials")
    _positive_integer(metrics.completed_trials, "completed_trials")
    if not isinstance(metrics.sealed_data_access_permit, DataAccessPermit):
        raise ValueError(
            "sealed_data_access_permit must be a signed DataAccessPermit"
        )


def _common_reasons(metrics, strategy_family, *, _clock=None):
    reasons = []
    if metrics.candidate_identity != metrics.baseline_identity:
        reasons.append("incomparable_baseline")
    if metrics.dsr_confidence + TOLERANCE < 0.95:
        reasons.append("dsr_confidence")
    if metrics.pbo > 0.05 + TOLERANCE:
        reasons.append("pbo")
    if metrics.completed_trials != metrics.declared_trials:
        reasons.append("incomplete_trial_ledger")
    identity = metrics.candidate_identity
    if not _sealed_permit_is_admissible(
        metrics.sealed_data_access_permit,
        strategy_family=strategy_family,
        identity=identity,
        _clock=_clock,
    ):
        reasons.append("sealed_holdout")
    if metrics.gross_leverage > 1.0 + TOLERANCE:
        reasons.append("leverage")
    return reasons


def _sealed_permit_is_admissible(
    permit,
    *,
    strategy_family,
    identity,
    _clock=None,
):
    if not permit.admits(
        strategy_family=strategy_family,
        start=identity.start,
        end=identity.end,
        dataset_hash=identity.dataset_hash,
        config_hash=identity.config_hash,
        cost_model_hash=identity.cost_model_hash,
        code_hash=identity.code_hash,
        _clock=_clock,
    ):
        return False
    try:
        ExposureLedger(_clock=_clock).verify_permit(permit)
    except (OSError, PermissionError, ValueError):
        return False
    return True


def _build_admission_evidence(requirement_id, decision, identity, manifest):
    if not isinstance(manifest, RunManifest):
        raise TypeError("manifest must be a RunManifest")
    if manifest.run_type != "sealed_backtest":
        raise ValueError("strategy admission evidence requires a sealed_backtest manifest")
    if manifest.dataset_hash != identity.dataset_hash:
        raise ValueError("manifest dataset_hash must match evaluation identity")
    if manifest.config_hash != identity.config_hash:
        raise ValueError("manifest config_hash must match evaluation identity")
    if manifest.code_hash != identity.code_hash:
        raise ValueError("manifest code_hash must match evaluation identity")
    if manifest.cost_model_hash != identity.cost_model_hash:
        raise ValueError("manifest cost_model_hash must match evaluation identity")
    if manifest.policy_version != ACCEPTANCE_POLICY_VERSION:
        raise ValueError("strategy admission manifest policy_version is not current")
    for name in (
        "input_materialization_hash",
        "output_hash",
        "report_hash",
        "cost_suite_hash",
    ):
        _digest(getattr(manifest, name), f"manifest {name}")
    if not isinstance(manifest.run_id, str) or not manifest.run_id.strip():
        raise ValueError("strategy admission manifest run_id is required")
    expected_experiment_id = (
        "normal-h1-quality-diversification"
        if requirement_id == "normal_strategy_admission"
        else "daytrade-h9-rvol-reward-risk"
    )
    if manifest.experiment_id != expected_experiment_id:
        raise ValueError(
            "strategy admission manifest experiment_id is not the active hypothesis"
        )
    detail = "passed" if decision.passed else "failed:" + ",".join(decision.reasons)
    return RequirementEvidence(
        requirement_id=requirement_id,
        passed=decision.passed,
        detail=detail,
        run_type=manifest.run_type,
        observed_at=manifest.observed_at.isoformat(),
        dataset_hash=manifest.dataset_hash,
        config_hash=manifest.config_hash,
        cost_model_hash=manifest.cost_model_hash,
        code_hash=manifest.code_hash,
        policy_version=manifest.policy_version,
        input_materialization_hash=manifest.input_materialization_hash,
        output_hash=manifest.output_hash,
        report_hash=manifest.report_hash,
        cost_suite_hash=manifest.cost_suite_hash,
        run_id=manifest.run_id,
        experiment_id=manifest.experiment_id,
    )


__all__ = (
    "AdmissionDecision",
    "CapitalAllocation",
    "DayTradeAdmissionMetrics",
    "EvaluationIdentity",
    "NormalAdmissionMetrics",
    "capital_allocation",
    "build_daytrade_admission_evidence",
    "build_normal_admission_evidence",
    "evaluate_daytrade_admission",
    "evaluate_normal_admission",
)
