"""RD-Agent runtime adapter."""

from math import isfinite

from ..factors.evaluation import FactorMetrics
from ..rdagent import FactorExperimentResult


class RDAgentQlibAdapter:
    def __init__(self, experiment_factory):
        self.experiment_factory = experiment_factory

    def run_factor_experiment(self, factor_description: str, market: str, validation_period):
        experiment = self.experiment_factory(
            factor_description=factor_description,
            market=market,
            validation_period=validation_period,
        )
        result = experiment.run()
        required = (
            "ic_mean", "icir", "ic_positive_rate", "long_short_return_annual",
            "decay_days", "oos_ic", "turnover_annual", "accepted",
        )
        missing = [name for name in required if not hasattr(result, name)]
        if missing:
            return FactorExperimentResult(False, None, "missing metrics: " + ", ".join(missing))
        values = [getattr(result, name) for name in required[:-1]]
        if (
            type(result.accepted) is not bool
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not isfinite(value) for value in values)
            or type(result.decay_days) is not int
        ):
            return FactorExperimentResult(False, None, "invalid metrics: finite numeric evidence required")
        metrics = FactorMetrics(
            name="rd_agent_factor",
            ic_mean=float(result.ic_mean),
            icir=float(result.icir),
            ic_positive_rate=result.ic_positive_rate,
            long_short_return_annual=result.long_short_return_annual,
            decay_days=result.decay_days,
            oos_ic=result.oos_ic,
            turnover_annual=result.turnover_annual,
        )
        return FactorExperimentResult(
            accepted=result.accepted,
            metrics=metrics,
            economic_rationale=str(getattr(result, "economic_rationale", factor_description)),
        )
