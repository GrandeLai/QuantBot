"""预注册价格行为实验执行层。

本包只生成候选配置的内存副本和不可覆盖的实验产物，不会修改
``config.yaml``，也不会绕过治理层的发布门禁。
"""

from .runner import (
    ALLOWED_CHANGE_PATHS,
    ConfigChange,
    ExperimentArtifact,
    ExperimentValidationError,
    SnapshotVerification,
    ValidationEvidence,
    build_candidate_config,
    default_backtest_runner,
    derive_experiment_id,
    run_registered_experiment,
    verify_config_snapshot,
    write_experiment_artifact,
)

__all__ = [
    "ALLOWED_CHANGE_PATHS",
    "ConfigChange",
    "ExperimentArtifact",
    "ExperimentValidationError",
    "SnapshotVerification",
    "ValidationEvidence",
    "build_candidate_config",
    "default_backtest_runner",
    "derive_experiment_id",
    "run_registered_experiment",
    "verify_config_snapshot",
    "write_experiment_artifact",
]
