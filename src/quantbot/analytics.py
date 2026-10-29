"""Portfolio performance analytics."""

from dataclasses import dataclass
from math import isfinite
from typing import List, Sequence


@dataclass(frozen=True)
class PerformanceSummary:
    annual_return: float
    benchmark_annual_return: float
    excess_alpha_annual: float
    annual_volatility: float
    sharpe: float
    max_drawdown: float


@dataclass(frozen=True)
class StrategyAcceptanceDecision:
    passed: bool
    annual_cost_drag: float
    reasons: List[str]
    net_excess_alpha_annual: float = 0.0


@dataclass(frozen=True)
class CrossBacktestValidationDecision:
    passed: bool
    alpha_deviation: float
    sharpe_deviation: float
    reasons: List[str]


class CrossBacktestValidationGate:
    def __init__(self, max_alpha_deviation: float = 0.01, max_sharpe_deviation: float = 0.20):
        self.max_alpha_deviation = max_alpha_deviation
        self.max_sharpe_deviation = max_sharpe_deviation

    def evaluate(self, primary, secondary) -> CrossBacktestValidationDecision:
        primary_alpha = float(primary["excess_alpha_annual"])
        secondary_alpha = float(secondary["excess_alpha_annual"])
        primary_sharpe = float(primary["sharpe"])
        secondary_sharpe = float(secondary["sharpe"])
        alpha_inputs_finite = isfinite(primary_alpha) and isfinite(secondary_alpha)
        sharpe_inputs_finite = isfinite(primary_sharpe) and isfinite(secondary_sharpe)
        alpha_deviation = (
            abs(primary_alpha - secondary_alpha) if alpha_inputs_finite else float("inf")
        )
        sharpe_deviation = (
            abs(primary_sharpe - secondary_sharpe) if sharpe_inputs_finite else float("inf")
        )
        reasons: List[str] = []
        if not alpha_inputs_finite or not sharpe_inputs_finite:
            reasons.append("non_finite_metrics")
        if alpha_inputs_finite and alpha_deviation > self.max_alpha_deviation:
            reasons.append("alpha_deviation")
        if sharpe_inputs_finite and sharpe_deviation > self.max_sharpe_deviation:
            reasons.append("sharpe_deviation")
        return CrossBacktestValidationDecision(
            passed=not reasons,
            alpha_deviation=alpha_deviation,
            sharpe_deviation=sharpe_deviation,
            reasons=reasons,
        )


class StrategyAcceptanceGate:
    def __init__(
        self,
        min_excess_alpha_annual: float = 0.05,
        min_sharpe: float = 1.0,
        max_drawdown: float = 0.20,
        max_annual_turnover: float = 4.0,
        max_annual_cost_drag: float = 0.006,
    ):
        self.min_excess_alpha_annual = min_excess_alpha_annual
        self.min_sharpe = min_sharpe
        self.max_drawdown = max_drawdown
        self.max_annual_turnover = max_annual_turnover
        self.max_annual_cost_drag = max_annual_cost_drag

    @classmethod
    def from_system_config(
        cls,
        config,
        max_drawdown: float = 0.20,
        max_annual_cost_drag: float = 0.006,
    ):
        return cls(
            min_excess_alpha_annual=float(config.market.target_alpha_annual),
            min_sharpe=float(config.market.target_sharpe),
            max_drawdown=max_drawdown,
            max_annual_turnover=float(config.execution.target_turnover_annual),
            max_annual_cost_drag=max_annual_cost_drag,
        )

    def evaluate(
        self,
        summary: PerformanceSummary,
        annual_turnover: float,
        two_way_cost_rate: float,
        summary_includes_costs: bool = False,
    ) -> StrategyAcceptanceDecision:
        reasons: List[str] = []
        metric_values = _summary_values(summary)
        summary_finite = all(isfinite(value) for value in metric_values)
        turnover_finite = isfinite(float(annual_turnover))
        cost_rate_finite = isfinite(float(two_way_cost_rate))
        if not summary_finite:
            reasons.append("non_finite_summary")
        if summary_finite and float(getattr(summary, "annual_volatility", 0.0)) < 0:
            reasons.append("negative_volatility")
        if summary_finite and summary.max_drawdown < 0:
            reasons.append("negative_drawdown")
        if not turnover_finite:
            reasons.append("non_finite_turnover")
        elif annual_turnover < 0:
            reasons.append("negative_turnover")
        if not cost_rate_finite:
            reasons.append("non_finite_cost_rate")
        elif two_way_cost_rate < 0:
            reasons.append("negative_cost_rate")

        valid_cost_inputs = (
            turnover_finite
            and cost_rate_finite
            and annual_turnover >= 0
            and two_way_cost_rate >= 0
        )
        annual_cost_drag = annual_turnover * two_way_cost_rate if valid_cost_inputs else 0.0
        if not isfinite(annual_cost_drag):
            reasons.append("non_finite_cost_drag")
            annual_cost_drag = 0.0
        net_excess_alpha = (
            summary.excess_alpha_annual
            if summary_includes_costs
            else summary.excess_alpha_annual - annual_cost_drag
        ) if summary_finite else 0.0
        if valid_cost_inputs and not summary_includes_costs and annual_cost_drag > 0:
            reasons.append("gross_summary_not_admissible")
        if summary_finite and net_excess_alpha < self.min_excess_alpha_annual:
            reasons.append("excess_alpha")
        if summary_finite and summary.sharpe < self.min_sharpe:
            reasons.append("sharpe")
        if summary_finite and summary.max_drawdown >= 0 and summary.max_drawdown > self.max_drawdown:
            reasons.append("drawdown")
        if turnover_finite and annual_turnover >= 0 and annual_turnover > self.max_annual_turnover:
            reasons.append("turnover")
        if valid_cost_inputs and annual_cost_drag > self.max_annual_cost_drag:
            reasons.append("cost_drag")
        return StrategyAcceptanceDecision(
            passed=not reasons,
            annual_cost_drag=annual_cost_drag,
            reasons=reasons,
            net_excess_alpha_annual=net_excess_alpha,
        )


def performance_summary(
    daily_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    periods_per_year: int = 252,
) -> PerformanceSummary:
    clean_returns = _validate_returns(daily_returns, "daily_returns")
    clean_benchmark_returns = _validate_returns(benchmark_returns, "benchmark_returns")
    if not isfinite(float(periods_per_year)) or periods_per_year <= 0:
        raise ValueError("periods_per_year must be finite and positive")
    annual_return = _annualized_return(clean_returns, periods_per_year)
    benchmark_annual_return = _annualized_return(clean_benchmark_returns, periods_per_year)
    daily_stddev = _stddev(clean_returns)
    annual_volatility = daily_stddev * (periods_per_year ** 0.5)
    mean_daily = sum(clean_returns) / len(clean_returns) if clean_returns else 0.0
    sharpe = mean_daily / daily_stddev * (periods_per_year ** 0.5) if daily_stddev else 0.0
    summary = PerformanceSummary(
        annual_return=annual_return,
        benchmark_annual_return=benchmark_annual_return,
        excess_alpha_annual=annual_return - benchmark_annual_return,
        annual_volatility=annual_volatility,
        sharpe=sharpe,
        max_drawdown=_max_drawdown(clean_returns),
    )
    if not all(isfinite(value) for value in _summary_values(summary)):
        raise ValueError("calculated performance metrics must be finite")
    return summary


def _summary_values(summary) -> tuple:
    return (
        float(getattr(summary, "annual_return", 0.0)),
        float(getattr(summary, "benchmark_annual_return", 0.0)),
        float(summary.excess_alpha_annual),
        float(getattr(summary, "annual_volatility", 0.0)),
        float(summary.sharpe),
        float(summary.max_drawdown),
    )


def _validate_returns(values: Sequence[float], name: str) -> List[float]:
    clean = [float(value) for value in values]
    if not all(isfinite(value) for value in clean):
        raise ValueError("%s must contain only finite returns" % name)
    if any(value < -1.0 for value in clean):
        raise ValueError("%s cannot contain returns below -1" % name)
    return clean


def _annualized_return(returns: Sequence[float], periods_per_year: int) -> float:
    if not returns:
        return 0.0
    growth = 1.0
    for value in returns:
        growth *= 1 + value
    return growth ** (periods_per_year / len(returns)) - 1


def _stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5


def _max_drawdown(returns: Sequence[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = 1 - equity / peak if peak else 0.0
        max_drawdown = max(max_drawdown, drawdown)
    return max_drawdown
