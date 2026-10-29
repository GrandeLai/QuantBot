"""Point-in-time normal-trading strategy and portfolio construction."""

from .domain import (
    NormalAllocationReport,
    NormalAllocationRequest,
    NormalDrawdownPolicy,
    NormalPortfolioConfig,
    NormalRegimeContext,
)
from .portfolio import NormalPortfolioAllocator
from .provider import (
    NORMAL_BASELINE,
    NORMAL_QUALITY,
    NormalFactorExperiment,
    PointInTimeMarketDataPort,
    PointInTimeNormalRequestProvider,
)
from .strategy import PointInTimeNormalStrategy
from .research_adapter import (
    ResearchDailyBar,
    ResearchNormalDataView,
    ResearchTrustKeys,
)
from .experiments import (
    NORMAL_H1_EXPERIMENT_ID,
    NORMAL_H2_EXPERIMENT_ID,
    preregistered_normal_experiments,
)
from .backtest import (
    NormalBacktestPoint,
    NormalBacktestResult,
    NormalBacktestSession,
    PointInTimeNormalBacktester,
    require_implemented_execution_price,
)
from .experiment_runner import (
    NormalH1AdapterOutput,
    NormalH1Decision,
    NormalH1ExperimentInputs,
    NormalH1ExperimentResult,
    NormalH2Decision,
    NormalH2ExperimentResult,
    NORMAL_H2_BASELINE_CONFIG,
    NORMAL_H2_CANDIDATE_CONFIG,
    normal_backtest_sessions_hash,
    run_normal_h1_experiment,
    run_normal_h2_experiment,
)
from .historical_trials import (
    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    NORMAL_H2_AUDIT_ARTIFACT_PATH_ENV,
    NORMAL_TRIAL_KEYS_ENV,
    NormalAdaptiveValidationArtifact,
    NormalH2HistoricalAudit,
    load_normal_adaptive_validation_artifact,
    load_normal_h2_historical_audit,
    normal_trial_return_history_hash,
    trusted_normal_trial_keys,
    verify_normal_adaptive_validation_artifact,
)
from .protocol import (
    NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW,
    NORMAL_RESEARCH_WINDOW,
    NORMAL_SEALED_WINDOW,
    require_exact_normal_experiment_window,
)

__all__ = (
    "NormalAllocationReport",
    "NormalAllocationRequest",
    "NormalDrawdownPolicy",
    "NormalPortfolioAllocator",
    "NormalPortfolioConfig",
    "NormalRegimeContext",
    "NormalFactorExperiment",
    "NORMAL_BASELINE",
    "NORMAL_QUALITY",
    "PointInTimeMarketDataPort",
    "PointInTimeNormalRequestProvider",
    "PointInTimeNormalStrategy",
    "ResearchDailyBar",
    "ResearchNormalDataView",
    "ResearchTrustKeys",
    "preregistered_normal_experiments",
    "NormalBacktestPoint",
    "NormalBacktestResult",
    "NormalBacktestSession",
    "PointInTimeNormalBacktester",
    "require_implemented_execution_price",
    "NormalH1Decision",
    "NormalH1AdapterOutput",
    "NormalH1ExperimentInputs",
    "NormalH1ExperimentResult",
    "NormalH2Decision",
    "NormalH2ExperimentResult",
    "NORMAL_H1_EXPERIMENT_ID",
    "NORMAL_H2_EXPERIMENT_ID",
    "NORMAL_H2_BASELINE_CONFIG",
    "NORMAL_H2_CANDIDATE_CONFIG",
    "normal_backtest_sessions_hash",
    "run_normal_h1_experiment",
    "run_normal_h2_experiment",
    "NORMAL_H2_AUDIT_ARTIFACT_PATH_ENV",
    "NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV",
    "NORMAL_TRIAL_KEYS_ENV",
    "NormalH2HistoricalAudit",
    "NormalAdaptiveValidationArtifact",
    "load_normal_adaptive_validation_artifact",
    "load_normal_h2_historical_audit",
    "normal_trial_return_history_hash",
    "trusted_normal_trial_keys",
    "verify_normal_adaptive_validation_artifact",
    "NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW",
    "NORMAL_RESEARCH_WINDOW",
    "NORMAL_SEALED_WINDOW",
    "require_exact_normal_experiment_window",
)
