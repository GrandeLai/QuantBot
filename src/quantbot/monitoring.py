"""Performance monitoring and daily pipeline orchestration."""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PerformanceDegradationAction:
    manual_review: bool
    position_scale: float
    threshold: float
    reason: str = ""


class PerformanceMonitor:
    def __init__(self, historical_ir_mean: float, historical_ir_std: float, sigma_threshold: float = 1.5):
        self.historical_ir_mean = historical_ir_mean
        self.historical_ir_std = historical_ir_std
        self.sigma_threshold = sigma_threshold

    def degradation_threshold(self) -> float:
        return self.historical_ir_mean - self.sigma_threshold * self.historical_ir_std

    def is_degraded(self, recent_ir: float) -> bool:
        return recent_ir < self.degradation_threshold()

    def position_scale(self, recent_ir: float) -> float:
        return 0.5 if self.is_degraded(recent_ir) else 1.0

    def has_consecutive_degradation(self, recent_irs, required_months: int = 3) -> bool:
        if len(recent_irs) < required_months:
            return False
        return all(self.is_degraded(value) for value in recent_irs[-required_months:])

    def degradation_action(self, recent_irs, required_months: int = 3) -> PerformanceDegradationAction:
        if self.has_consecutive_degradation(recent_irs, required_months=required_months):
            return PerformanceDegradationAction(
                manual_review=True,
                position_scale=0.5,
                threshold=self.degradation_threshold(),
                reason="three_month_ir_degradation",
            )
        return PerformanceDegradationAction(
            manual_review=False,
            position_scale=1.0,
            threshold=self.degradation_threshold(),
        )


@dataclass(frozen=True)
class DailyPipelineStage:
    name: str
    ok: bool
    error: str = ""


@dataclass(frozen=True)
class DailyPipelineHealthReport:
    ok: bool
    stages: list


class DailyPipeline:
    def __init__(
        self,
        data_update: Callable[[], None],
        factor_compute: Callable[[], None],
        signal_generate: Callable[[], None],
        execute: Callable[[], None],
    ):
        self.data_update = data_update
        self.factor_compute = factor_compute
        self.signal_generate = signal_generate
        self.execute = execute

    def run(self) -> None:
        self.data_update()
        self.factor_compute()
        self.signal_generate()
        self.execute()

    def run_with_report(self) -> DailyPipelineHealthReport:
        stages = []
        for name, step in (
            ("data_update", self.data_update),
            ("factor_compute", self.factor_compute),
            ("signal_generate", self.signal_generate),
            ("execute", self.execute),
        ):
            try:
                step()
            except Exception as error:
                stages.append(DailyPipelineStage(name=name, ok=False, error=str(error)))
                return DailyPipelineHealthReport(ok=False, stages=stages)
            stages.append(DailyPipelineStage(name=name, ok=True))
        return DailyPipelineHealthReport(ok=True, stages=stages)
