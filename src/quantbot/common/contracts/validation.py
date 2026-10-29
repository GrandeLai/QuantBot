"""时间序列验证窗口配置（构建函数 build_walk_forward_windows 留在量化模块）."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimeSeriesValidationConfig:
    """Walk-forward / embargo 验证参数."""

    train_size: int
    test_size: int
    step_size: int
    embargo_size: int = 0


@dataclass(frozen=True, slots=True)
class ValidationWindow:
    """单个时间序列训练/验证窗口."""

    train_start: int
    train_end: int
    test_start: int
    test_end: int
