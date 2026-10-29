"""Single-variable evaluation for the preregistered normal H1 experiment."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import fsum
from types import MappingProxyType
from typing import Sequence

from ..data import BundleReadEvidence, NORMAL_H1_REQUIRED_TABLES
from ..evidence import hash_payload
from ..statistical_validation import (
    DeflatedSharpeResult,
    ProbabilityOfBacktestOverfittingResult,
    cscv_partition,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)
from .backtest import NormalBacktestResult, NormalBacktestSession, PointInTimeNormalBacktester
from .domain import NormalPortfolioConfig
from .experiments import (
    NORMAL_H1_EXPERIMENT_ID,
    NORMAL_H2_EXPERIMENT_ID,
    preregistered_normal_experiments,
)
from .portfolio import NormalPortfolioAllocator
from .historical_trials import (
    NormalAdaptiveValidationArtifact,
    NormalH2HistoricalAudit,
)
from .provider import (
    NormalFactorExperiment,
    PointInTimeMarketDataPort,
    PointInTimeNormalRequestProvider,
)
from ..strategy_admission import (
    NORMAL_ACCEPTANCE_MAX_DRAWDOWN,
    NORMAL_ACCEPTANCE_THRESHOLDS,
    NormalAcceptanceThresholds,
)
from ..validation_protocol import DataAccessPermit


_NORMAL_H1_INPUT_SEAL = object()
_NORMAL_H1_UNIVERSES = ("RUSSELL1000", "SP500")
_NORMAL_H1_ASSET_LOOKBACK_SESSIONS = 252
_NORMAL_H1_ADV_LOOKBACK_SESSIONS = 20
_NORMAL_H1_MINIMUM_ADV_DOLLARS = 5_000_000.0
# Backtest acceptance ceiling for realized drawdown.  Imported rather than
# redeclared so the research runner and the formal admission gate cannot drift
# apart.  It is a strategy-selection threshold and is deliberately not
# ``risk.max_normal_drawdown``, the operational review/halt trigger.


class _NormalH1MarketDataView:
    """Core-owned, cached liquidity/history view shared by both H1 arms."""

    def __init__(self, delegate: PointInTimeMarketDataPort) -> None:
        self._delegate = delegate
        self._cache = {}

    @staticmethod
    def _master_key(security_master) -> str:
        from ..data import canonical_security_master_snapshot_hash

        return canonical_security_master_snapshot_hash(security_master)

    def monthly_returns(self, security_master):
        key = ("monthly_returns", self._master_key(security_master))
        if key not in self._cache:
            self._cache[key] = self._delegate.monthly_returns(security_master)
        return self._cache[key]

    def asset_return_history(self, security_master, *, lookback_sessions):
        if lookback_sessions != _NORMAL_H1_ASSET_LOOKBACK_SESSIONS:
            raise ValueError("normal H1 asset lookback is frozen at 252 sessions")
        key = ("asset_return_history", self._master_key(security_master))
        if key not in self._cache:
            raw = self._delegate.asset_return_history(
                security_master,
                lookback_sessions=_NORMAL_H1_ASSET_LOOKBACK_SESSIONS,
            )
            filtered = {}
            for security_id, values in raw.items():
                history = tuple(values)
                if len(history) >= _NORMAL_H1_ASSET_LOOKBACK_SESSIONS:
                    filtered[security_id] = history[-_NORMAL_H1_ASSET_LOOKBACK_SESSIONS:]
            self._cache[key] = MappingProxyType(dict(sorted(filtered.items())))
        return self._cache[key]

    def sleeve_return_history(
        self,
        security_master,
        sleeves,
        *,
        lookback_months,
    ):
        names = tuple(item.name for item in sleeves)
        key = (
            "sleeve_return_history",
            self._master_key(security_master),
            names,
            lookback_months,
        )
        if key not in self._cache:
            raw = self._delegate.sleeve_return_history(
                security_master,
                sleeves,
                lookback_months=lookback_months,
            )
            if set(raw) != set(names):
                raise ValueError(
                    "normal H1 sleeve histories must exactly match requested sleeves"
                )
            master_key = self._master_key(security_master)
            for name, values in raw.items():
                history = tuple(values)
                history_key = (
                    "sleeve_return_history_value",
                    master_key,
                    name,
                    lookback_months,
                )
                previous = self._cache.get(history_key)
                if previous is not None and previous != history:
                    raise PermissionError(
                        "normal H1 adapter changed a shared sleeve history between arms"
                    )
                self._cache[history_key] = history
            self._cache[key] = MappingProxyType(
                {
                    name: self._cache[
                        (
                            "sleeve_return_history_value",
                            master_key,
                            name,
                            lookback_months,
                        )
                    ]
                    for name in sorted(names)
                }
            )
        return self._cache[key]

    def market_betas(self, security_master, *, lookback_sessions):
        key = (
            "market_betas",
            self._master_key(security_master),
            lookback_sessions,
        )
        if key not in self._cache:
            self._cache[key] = MappingProxyType(
                dict(
                    sorted(
                        self._delegate.market_betas(
                            security_master,
                            lookback_sessions=lookback_sessions,
                        ).items()
                    )
                )
            )
        return self._cache[key]

    def average_dollar_volume(self, security_master, *, lookback_sessions):
        if lookback_sessions != _NORMAL_H1_ADV_LOOKBACK_SESSIONS:
            raise ValueError("normal H1 ADV lookback is frozen at 20 sessions")
        key = ("average_dollar_volume", self._master_key(security_master))
        if key not in self._cache:
            raw = self._delegate.average_dollar_volume(
                security_master,
                lookback_sessions=_NORMAL_H1_ADV_LOOKBACK_SESSIONS,
            )
            filtered = {
                security_id: value
                for security_id, value in raw.items()
                if float(value) >= _NORMAL_H1_MINIMUM_ADV_DOLLARS
            }
            self._cache[key] = MappingProxyType(dict(sorted(filtered.items())))
        return self._cache[key]

    def regime_context(self, security_master):
        key = ("regime_context", self._master_key(security_master))
        if key not in self._cache:
            self._cache[key] = self._delegate.regime_context(security_master)
        return self._cache[key]


@dataclass(frozen=True, slots=True)
class NormalH1Decision:
    passed: bool
    reasons: tuple[str, ...]
    cagr_improvement: float
    sharpe_improvement: float
    positive_window_fraction: float
    top_three_profit_contribution: float


@dataclass(frozen=True, slots=True)
class NormalH1ExperimentResult:
    baseline: NormalBacktestResult
    candidate: NormalBacktestResult
    stressed_baseline: NormalBacktestResult
    stressed_candidate: NormalBacktestResult
    dsr: DeflatedSharpeResult | None
    pbo: ProbabilityOfBacktestOverfittingResult | None
    declared_trials: int
    completed_trials: int
    decision: NormalH1Decision
    pbo_dropped_observations: int = 0
    historical_h2_audit_hash: str | None = None
    adaptive_validation_hash: str | None = None


@dataclass(frozen=True, slots=True)
class NormalH1AdapterOutput:
    """Untrusted physical-column mapping returned by a deployment adapter."""

    market_data: PointInTimeMarketDataPort
    sessions: tuple[NormalBacktestSession, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.market_data, PointInTimeMarketDataPort):
            raise TypeError("market_data must implement PointInTimeMarketDataPort")
        sessions = tuple(self.sessions)
        if not sessions or any(not isinstance(item, NormalBacktestSession) for item in sessions):
            raise ValueError("sessions must contain NormalBacktestSession values")
        object.__setattr__(self, "sessions", sessions)


@dataclass(frozen=True, slots=True)
class NormalH1ExperimentInputs:
    """Core-sealed shared data view for both arms of the normal experiment."""

    market_data: PointInTimeMarketDataPort
    sessions: tuple[NormalBacktestSession, ...]
    permit: DataAccessPermit
    read_evidence: BundleReadEvidence
    adapter_code_hash: str
    session_hash: str
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._seal is not _NORMAL_H1_INPUT_SEAL:
            raise PermissionError("normal H1 inputs must be sealed by the core")
        if not isinstance(self.market_data, PointInTimeMarketDataPort):
            raise TypeError("market_data must implement PointInTimeMarketDataPort")
        if not isinstance(self.permit, DataAccessPermit):
            raise TypeError("permit must be a DataAccessPermit")
        if not isinstance(self.read_evidence, BundleReadEvidence):
            raise TypeError("read_evidence must be BundleReadEvidence")
        _sha256(self.adapter_code_hash, "adapter_code_hash")
        _sha256(self.session_hash, "session_hash")
        sessions = tuple(self.sessions)
        if not sessions or any(not isinstance(item, NormalBacktestSession) for item in sessions):
            raise ValueError("sessions must contain NormalBacktestSession values")
        object.__setattr__(self, "sessions", sessions)
        self.read_evidence.assert_covers(
            permit=self.permit,
            dataset_hash=self.permit.dataset_hash,
            strategy_family="normal",
            required_tables=NORMAL_H1_REQUIRED_TABLES,
        )
        _validate_normal_sessions(sessions, self.permit, self.read_evidence)
        if self.session_hash != normal_backtest_sessions_hash(sessions):
            raise ValueError("session_hash does not match the normal backtest sessions")

    @classmethod
    def from_adapter_output(
        cls,
        output: NormalH1AdapterOutput,
        *,
        permit: DataAccessPermit,
        read_evidence: BundleReadEvidence,
        adapter_code_hash: str,
    ) -> "NormalH1ExperimentInputs":
        if not isinstance(output, NormalH1AdapterOutput):
            raise TypeError("adapter must return NormalH1AdapterOutput")
        return cls(
            market_data=_NormalH1MarketDataView(output.market_data),
            sessions=output.sessions,
            permit=permit,
            read_evidence=read_evidence,
            adapter_code_hash=adapter_code_hash,
            session_hash=normal_backtest_sessions_hash(output.sessions),
            _seal=_NORMAL_H1_INPUT_SEAL,
        )

    @property
    def provenance_verified(self) -> bool:
        try:
            self.read_evidence.assert_covers(
                permit=self.permit,
                dataset_hash=self.permit.dataset_hash,
                strategy_family="normal",
                required_tables=NORMAL_H1_REQUIRED_TABLES,
            )
            _validate_normal_sessions(
                self.sessions,
                self.permit,
                self.read_evidence,
            )
        except (OSError, PermissionError, TypeError, ValueError):
            return False
        return (
            self.read_evidence.provenance_verified
            and self.session_hash == normal_backtest_sessions_hash(self.sessions)
        )

    def providers(self) -> tuple[PointInTimeNormalRequestProvider, PointInTimeNormalRequestProvider]:
        formal = self.permit.mode == "sealed"
        return (
            PointInTimeNormalRequestProvider(
                self.market_data,
                experiment=NormalFactorExperiment(include_quality=False),
                formal_admission=formal,
            ),
            PointInTimeNormalRequestProvider(
                self.market_data,
                experiment=NormalFactorExperiment(include_quality=True),
                formal_admission=formal,
            ),
        )


def run_normal_h1_experiment(
    *,
    baseline_provider: PointInTimeNormalRequestProvider,
    candidate_provider: PointInTimeNormalRequestProvider,
    sessions: Sequence[NormalBacktestSession],
    portfolio_config: NormalPortfolioConfig | None = None,
    initial_equity: float = 100_000.0,
    cost_bps_per_side: float = 5.0,
    stress_cost_bps_per_side: float = 10.0,
    window_sessions: int = 63,
    thresholds: NormalAcceptanceThresholds = NORMAL_ACCEPTANCE_THRESHOLDS,
    historical_h2_audit: NormalH2HistoricalAudit | None = None,
    adaptive_validation: NormalAdaptiveValidationArtifact | None = None,
) -> NormalH1ExperimentResult:
    """Evaluate exactly residual momentum versus residual momentum + quality.

    Both arms receive the exact same immutable session tuple and allocator
    configuration.  No parameter search or leverage adjustment occurs here.
    """

    if not isinstance(baseline_provider, PointInTimeNormalRequestProvider):
        raise TypeError("baseline_provider must be PointInTimeNormalRequestProvider")
    if not isinstance(candidate_provider, PointInTimeNormalRequestProvider):
        raise TypeError("candidate_provider must be PointInTimeNormalRequestProvider")
    if baseline_provider.experiment != NormalFactorExperiment(include_quality=False):
        raise ValueError("normal H1 baseline must be residual momentum only")
    if candidate_provider.experiment != NormalFactorExperiment(include_quality=True):
        raise ValueError("normal H1 candidate must add only the quality sleeve")
    sessions = tuple(sessions)
    if not sessions:
        raise ValueError("normal H1 sessions must not be empty")
    if isinstance(window_sessions, bool) or not isinstance(window_sessions, int) or window_sessions < 2:
        raise ValueError("window_sessions must be an integer of at least two")
    if historical_h2_audit is not None and not isinstance(
        historical_h2_audit, NormalH2HistoricalAudit
    ):
        raise TypeError(
            "historical_h2_audit must be a verified NormalH2HistoricalAudit"
        )
    if adaptive_validation is not None and not isinstance(
        adaptive_validation, NormalAdaptiveValidationArtifact
    ):
        raise TypeError(
            "adaptive_validation must be a verified NormalAdaptiveValidationArtifact"
        )
    if historical_h2_audit is not None and adaptive_validation is not None:
        raise ValueError("normal H1 cannot combine development and sealed statistics")
    if adaptive_validation is not None and not baseline_provider.formal_admission:
        raise PermissionError(
            "normal adaptive validation statistics may be consumed only by sealed H1"
        )
    if historical_h2_audit is not None and baseline_provider.formal_admission:
        raise PermissionError(
            "sealed normal H1 cannot recompute statistics from a development H2 audit"
        )
    registry = preregistered_normal_experiments()
    if _declared_trials(registry, NORMAL_H1_EXPERIMENT_ID) != 1:
        raise RuntimeError("normal H1 registry must contain exactly one trial")
    config = portfolio_config or NormalPortfolioConfig(top_n=20)
    if config.top_n != 20:
        raise ValueError("normal PIT H1 top_n is frozen at 20")
    # The H1 protocol config hash does not cover allocator capital-efficiency
    # settings, so they must stay at the declared baseline.  Changing them
    # changes the strategy and needs its own preregistered experiment.
    if config.redistribute_constrained_capital:
        raise ValueError("normal PIT H1 redistribute_constrained_capital is frozen at false")
    if config.volatility_targeting != "cap_only":
        raise ValueError("normal PIT H1 volatility_targeting is frozen at cap_only")
    if config.min_funded_names != 0:
        raise ValueError("normal PIT H1 min_funded_names is frozen at 0")
    if baseline_provider.residual_skip_months != 1:
        raise ValueError("normal PIT H1 residual_skip_months is frozen at 1")
    _require_same_normal_data_identity(baseline_provider, candidate_provider)

    baseline = _run(
        baseline_provider,
        sessions,
        config,
        initial_equity,
        cost_bps_per_side,
    )
    candidate = _run(
        candidate_provider,
        sessions,
        config,
        initial_equity,
        cost_bps_per_side,
    )
    stressed_baseline = _run(
        baseline_provider,
        sessions,
        config,
        initial_equity,
        stress_cost_bps_per_side,
    )
    stressed_candidate = _run(
        candidate_provider,
        sessions,
        config,
        initial_equity,
        stress_cost_bps_per_side,
    )

    reasons = []
    cagr_improvement = candidate.summary.annual_return - baseline.summary.annual_return
    sharpe_improvement = candidate.summary.sharpe - baseline.summary.sharpe
    positive_windows = _positive_window_fraction(candidate.daily_returns, window_sessions)
    concentration = _top_profit_contribution(candidate.pnl_by_security_id, 3)
    minimums = (
        (cagr_improvement, thresholds.min_baseline_cagr_improvement, "baseline_cagr_improvement"),
        (sharpe_improvement, thresholds.min_baseline_sharpe_improvement, "baseline_sharpe_improvement"),
        (candidate.summary.excess_alpha_annual, thresholds.min_net_excess_annual, "net_excess_annual"),
        (candidate.summary.sharpe, thresholds.min_sharpe, "sharpe"),
        (
            stressed_candidate.summary.excess_alpha_annual,
            thresholds.min_stressed_net_excess_annual,
            "two_x_cost_stress",
        ),
        (positive_windows, thresholds.min_positive_window_fraction, "positive_windows"),
    )
    reasons.extend(name for value, floor, name in minimums if value < floor)
    maximums = (
        (candidate.summary.max_drawdown, thresholds.max_drawdown, "max_drawdown"),
        (candidate.annual_turnover, thresholds.max_annual_turnover, "annual_turnover"),
        (concentration, thresholds.max_top_three_profit_contribution, "top_three_concentration"),
    )
    reasons.extend(name for value, ceiling, name in maximums if value > ceiling)

    dsr = None
    pbo = None
    pbo_dropped = 0
    completed_trials = 1
    if adaptive_validation is not None:
        completed_trials = registry.total_declared_trials
        dsr = adaptive_validation.dsr
        pbo = adaptive_validation.pbo_result
    elif historical_h2_audit is None:
        reasons.extend(("incomplete_trial_ledger", "dsr_unavailable"))
    else:
        candidate_dates = tuple(point.trading_date for point in candidate.points)
        if candidate_dates != historical_h2_audit.observation_dates:
            reasons.extend(
                (
                    "historical_trial_date_mismatch",
                    "incomplete_trial_ledger",
                    "dsr_unavailable",
                )
            )
        else:
            completed_trials = registry.total_declared_trials
            try:
                dsr = deflated_sharpe_ratio(
                    candidate.daily_returns,
                    (candidate.summary.sharpe, historical_h2_audit.sharpe_annual),
                )
            except ValueError:
                reasons.append("dsr_unavailable")
    if dsr is not None and dsr.confidence < 0.95:
        reasons.append("dsr_confidence")

    if adaptive_validation is None:
        partition = (
            None
            if historical_h2_audit is None
            or tuple(point.trading_date for point in candidate.points)
            != historical_h2_audit.observation_dates
            else _normal_cscv_partition(
                {
                    NORMAL_H1_EXPERIMENT_ID: candidate.daily_returns,
                    NORMAL_H2_EXPERIMENT_ID: historical_h2_audit.daily_returns,
                }
            )
        )
        if partition is None:
            reasons.append("pbo_unavailable")
        else:
            pbo_dropped = partition.dropped_observations
            try:
                pbo = cscv_probability_of_backtest_overfitting(
                    partition.trial_returns,
                    folds=partition.folds,
                )
            except ValueError:
                reasons.append("pbo_unavailable")
    if pbo is not None and pbo.probability > 0.05:
        reasons.append("pbo")

    reasons = tuple(dict.fromkeys(reasons))
    return NormalH1ExperimentResult(
        baseline=baseline,
        candidate=candidate,
        stressed_baseline=stressed_baseline,
        stressed_candidate=stressed_candidate,
        dsr=dsr,
        pbo=pbo,
        declared_trials=registry.total_declared_trials,
        completed_trials=completed_trials,
        pbo_dropped_observations=pbo_dropped,
        historical_h2_audit_hash=(
            None
            if historical_h2_audit is None
            else historical_h2_audit.content_hash
        ),
        adaptive_validation_hash=(
            None
            if adaptive_validation is None
            else adaptive_validation.content_hash
        ),
        decision=NormalH1Decision(
            passed=not reasons,
            reasons=reasons,
            cagr_improvement=cagr_improvement,
            sharpe_improvement=sharpe_improvement,
            positive_window_fraction=positive_windows,
            top_three_profit_contribution=concentration,
        ),
    )


NORMAL_H2_BASELINE_CONFIG = NormalPortfolioConfig(top_n=20)
NORMAL_H2_CANDIDATE_CONFIG = NormalPortfolioConfig(
    top_n=20,
    redistribute_constrained_capital=True,
    volatility_targeting="two_sided",
    min_funded_names=20,
)


@dataclass(frozen=True, slots=True)
class NormalH2Decision:
    passed: bool
    reasons: tuple[str, ...]
    cagr_improvement: float
    sharpe_improvement: float
    positive_window_fraction: float
    top_three_profit_contribution: float
    baseline_capital_utilization: float
    candidate_capital_utilization: float


@dataclass(frozen=True, slots=True)
class NormalH2ExperimentResult:
    baseline: NormalBacktestResult
    candidate: NormalBacktestResult
    stressed_baseline: NormalBacktestResult
    stressed_candidate: NormalBacktestResult
    dsr: DeflatedSharpeResult | None
    pbo: ProbabilityOfBacktestOverfittingResult | None
    declared_trials: int
    completed_trials: int
    decision: NormalH2Decision
    pbo_dropped_observations: int = 0


def run_normal_h2_experiment(
    *,
    provider: PointInTimeNormalRequestProvider,
    sessions: Sequence[NormalBacktestSession],
    initial_equity: float = 100_000.0,
    cost_bps_per_side: float = 5.0,
    stress_cost_bps_per_side: float = 10.0,
    window_sessions: int = 63,
    thresholds: NormalAcceptanceThresholds = NORMAL_ACCEPTANCE_THRESHOLDS,
    historical_audit_replay: bool = False,
) -> NormalH2ExperimentResult:
    """Evaluate constrained-capital redeployment against the frozen allocator.

    The single declared variable is the allocator's capital-efficiency profile.
    Both arms use one provider instance, so the signal, universe, sessions,
    costs and risk limits are identical by construction and the only thing
    that can differ is how much of the permitted exposure is actually held.
    """

    if not isinstance(provider, PointInTimeNormalRequestProvider):
        raise TypeError("provider must be PointInTimeNormalRequestProvider")
    if provider.experiment != NormalFactorExperiment(include_quality=False):
        raise ValueError("normal H2 must use the residual momentum baseline sleeve")
    if provider.residual_skip_months != 1:
        raise ValueError("normal H2 residual_skip_months is frozen at 1")
    sessions = tuple(sessions)
    if not sessions:
        raise ValueError("normal H2 sessions must not be empty")
    if isinstance(window_sessions, bool) or not isinstance(window_sessions, int) or window_sessions < 2:
        raise ValueError("window_sessions must be an integer of at least two")
    if not isinstance(historical_audit_replay, bool):
        raise TypeError("historical_audit_replay must be a boolean")
    registry = preregistered_normal_experiments()
    if _declared_trials(registry, NORMAL_H2_EXPERIMENT_ID) != 1:
        raise RuntimeError("normal H2 registry must contain exactly one trial")
    if not historical_audit_replay:
        registry.require_candidate_selectable(NORMAL_H2_EXPERIMENT_ID)

    baseline = _run(
        provider, sessions, NORMAL_H2_BASELINE_CONFIG, initial_equity, cost_bps_per_side
    )
    candidate = _run(
        provider, sessions, NORMAL_H2_CANDIDATE_CONFIG, initial_equity, cost_bps_per_side
    )
    stressed_baseline = _run(
        provider,
        sessions,
        NORMAL_H2_BASELINE_CONFIG,
        initial_equity,
        stress_cost_bps_per_side,
    )
    stressed_candidate = _run(
        provider,
        sessions,
        NORMAL_H2_CANDIDATE_CONFIG,
        initial_equity,
        stress_cost_bps_per_side,
    )

    reasons = []
    cagr_improvement = candidate.summary.annual_return - baseline.summary.annual_return
    sharpe_improvement = candidate.summary.sharpe - baseline.summary.sharpe
    positive_windows = _positive_window_fraction(candidate.daily_returns, window_sessions)
    concentration = _top_profit_contribution(candidate.pnl_by_security_id, 3)
    baseline_utilization = _mean_gross_exposure(baseline)
    candidate_utilization = _mean_gross_exposure(candidate)
    minimums = (
        (cagr_improvement, thresholds.min_baseline_cagr_improvement, "baseline_cagr_improvement"),
        (candidate.summary.excess_alpha_annual, thresholds.min_net_excess_annual, "net_excess_annual"),
        (candidate.summary.sharpe, thresholds.min_sharpe, "sharpe"),
        (
            stressed_candidate.summary.excess_alpha_annual,
            thresholds.min_stressed_net_excess_annual,
            "two_x_cost_stress",
        ),
        (positive_windows, thresholds.min_positive_window_fraction, "positive_windows"),
    )
    reasons.extend(name for value, floor, name in minimums if value < floor)
    # Redeploying capital must not be paid for with a worse risk-adjusted
    # return: the whole hypothesis is that the discarded budget was earning
    # nothing, not that more exposure is always better.
    if sharpe_improvement < -0.05:
        reasons.append("sharpe_degradation")
    if candidate_utilization <= baseline_utilization:
        reasons.append("no_capital_redeployment")
    maximums = (
        (candidate.summary.max_drawdown, thresholds.max_drawdown, "max_drawdown"),
        (candidate.annual_turnover, thresholds.max_annual_turnover, "annual_turnover"),
        (concentration, thresholds.max_top_three_profit_contribution, "top_three_concentration"),
    )
    reasons.extend(name for value, ceiling, name in maximums if value > ceiling)

    # H2 is an immutable completed/rejected experiment.  Its audit replay
    # exports the exact dated return history for the independently signed
    # two-trial ledger; it can never become a selectable candidate and the
    # residual-momentum baseline must not masquerade as a second trial.
    dsr = None
    pbo = None
    pbo_dropped = 0
    reasons.append("completed_rejected")

    reasons = tuple(dict.fromkeys(reasons))
    return NormalH2ExperimentResult(
        baseline=baseline,
        candidate=candidate,
        stressed_baseline=stressed_baseline,
        stressed_candidate=stressed_candidate,
        dsr=dsr,
        pbo=pbo,
        declared_trials=registry.total_declared_trials,
        completed_trials=1,
        pbo_dropped_observations=pbo_dropped,
        decision=NormalH2Decision(
            passed=not reasons,
            reasons=reasons,
            cagr_improvement=cagr_improvement,
            sharpe_improvement=sharpe_improvement,
            positive_window_fraction=positive_windows,
            top_three_profit_contribution=concentration,
            baseline_capital_utilization=baseline_utilization,
            candidate_capital_utilization=candidate_utilization,
        ),
    )


def _declared_trials(registry, experiment_id):
    for record in registry.records:
        if record.experiment_id == experiment_id:
            return record.declared_trial_count
    raise RuntimeError(f"experiment {experiment_id} is not preregistered")


def _mean_gross_exposure(result: NormalBacktestResult) -> float:
    if not result.points:
        return 0.0
    return fsum(
        fsum(point.weights.values()) for point in result.points
    ) / len(result.points)


def _run(provider, sessions, config, initial_equity, cost_bps):
    return PointInTimeNormalBacktester(
        provider,
        NormalPortfolioAllocator(config),
        initial_equity=initial_equity,
        cost_bps_per_side=cost_bps,
    ).run(sessions)


def _require_same_normal_data_identity(baseline, candidate):
    if baseline.market_data is not candidate.market_data:
        raise ValueError("normal H1 arms must share one point-in-time market-data view")
    for name in (
        "capital",
        "formal_admission",
        "residual_estimation_months",
        "residual_skip_months",
        "asset_lookback_sessions",
        "adv_lookback_sessions",
        "sleeve_history_months",
    ):
        if getattr(baseline, name) != getattr(candidate, name):
            raise ValueError(f"normal H1 provider identity differs at {name}")
    if baseline.asset_lookback_sessions != _NORMAL_H1_ASSET_LOOKBACK_SESSIONS:
        raise ValueError("normal H1 asset lookback is frozen at 252 sessions")
    if baseline.adv_lookback_sessions != _NORMAL_H1_ADV_LOOKBACK_SESSIONS:
        raise ValueError("normal H1 ADV lookback is frozen at 20 sessions")


def _positive_window_fraction(returns, window_sessions):
    if len(returns) < window_sessions:
        return 0.0
    windows = tuple(
        fsum(returns[index : index + window_sessions])
        for index in range(0, len(returns) - window_sessions + 1)
    )
    return sum(value > 0 for value in windows) / len(windows) if windows else 0.0


def _top_profit_contribution(values, count):
    positive = sorted((float(value) for value in values.values() if value > 0), reverse=True)
    total = fsum(positive)
    if total <= 0:
        return 1.0
    return fsum(positive[:count]) / total


def _normal_cscv_partition(trial_returns):
    """Select normal CSCV folds, trimming the remainder explicitly.

    Requiring the raw session count to already divide into equal even folds
    switched the PBO gate off whenever that count was odd, which is roughly
    half of all runs.  CSCV drops the remainder anyway.
    """

    try:
        return cscv_partition(trial_returns)
    except ValueError:
        return None


def normal_backtest_sessions_hash(sessions: Sequence[NormalBacktestSession]) -> str:
    """Hash every price, action and point-in-time snapshot used by an H1 run."""

    from ..data import canonical_security_master_snapshot_hash

    payload = []
    for session in tuple(sessions):
        if not isinstance(session, NormalBacktestSession):
            raise TypeError("sessions must contain NormalBacktestSession values")
        snapshot = session.signal_snapshot
        master = None if snapshot is None else snapshot.security_master
        payload.append(
            {
                "trading_date": session.trading_date,
                "open_prices": dict(session.open_prices),
                "close_prices": dict(session.close_prices),
                "benchmark_return": session.benchmark_return,
                "split_ratios": dict(session.split_ratios),
                "cash_distributions": dict(session.cash_distributions),
                "signal_observed_at": (
                    None if snapshot is None else snapshot.observed_at
                ),
                "security_master_hash": (
                    None
                    if master is None
                    else canonical_security_master_snapshot_hash(master)
                ),
                "security_master_dataset_hash": (
                    None if master is None else master.source_dataset_hash
                ),
            }
        )
    return hash_payload(payload)


def _validate_normal_sessions(sessions, permit, read_evidence) -> None:
    dates = tuple(item.trading_date for item in sessions)
    if dates != tuple(sorted(set(dates))):
        raise ValueError("normal H1 sessions must have unique ascending dates")
    expected_dates = read_evidence.calendar_sessions(
        start=permit.request_start,
        end=permit.request_end,
    )
    if not expected_dates:
        raise ValueError("normal H1 requested interval has no verified trading sessions")
    if dates != expected_dates:
        raise PermissionError(
            "normal H1 sessions do not exactly match the core-observed trading calendar"
        )
    snapshots = tuple(
        item.signal_snapshot for item in sessions if item.signal_snapshot is not None
    )
    if not snapshots:
        raise ValueError("normal H1 sessions must contain at least one signal snapshot")
    for snapshot in snapshots:
        master = snapshot.security_master
        if master is None:
            raise ValueError("normal H1 signal snapshots require a security master")
        if master.source_dataset_hash != permit.dataset_hash:
            raise PermissionError(
                "normal H1 security master is bound to a different dataset"
            )
        if master.universes != _NORMAL_H1_UNIVERSES:
            raise PermissionError(
                "normal H1 security master must use exactly the historical "
                "SP500 and RUSSELL1000 union"
            )


def _sha256(value, name):
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


__all__ = (
    "NormalH1AdapterOutput",
    "NormalH1Decision",
    "NormalH1ExperimentInputs",
    "NormalH1ExperimentResult",
    "normal_backtest_sessions_hash",
    "run_normal_h1_experiment",
)
