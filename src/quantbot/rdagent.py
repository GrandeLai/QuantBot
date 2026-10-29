"""RD-Agent integration boundary and retry loop."""

from dataclasses import dataclass
from typing import Callable, List, Optional

from .factors.evaluation import FactorMetrics, passes_admission


@dataclass(frozen=True)
class FactorExperimentResult:
    accepted: bool
    metrics: FactorMetrics | None
    error: Optional[str] = None
    economic_rationale: str = ""


@dataclass(frozen=True)
class RDAgentAdmissionDecision:
    passed: bool
    reasons: List[str]


@dataclass(frozen=True)
class MonthlyResearchReport:
    period: str
    accepted_factors: List[str]
    rejected_factors: List[str]
    retired_factors: List[str]

    def to_dict(self):
        summary = (
            "RD-Agent research report %s: accepted=%s; rejected=%s; retired=%s"
            % (
                self.period,
                ",".join(self.accepted_factors),
                ",".join(self.rejected_factors),
                ",".join(self.retired_factors),
            )
        )
        return {
            "period": self.period,
            "accepted_count": len(self.accepted_factors),
            "rejected_count": len(self.rejected_factors),
            "retired_count": len(self.retired_factors),
            "accepted_factors": list(self.accepted_factors),
            "rejected_factors": list(self.rejected_factors),
            "retired_factors": list(self.retired_factors),
            "summary": summary,
        }


class RDDiscoveryLoop:
    def __init__(self, max_iterations: int, runner: Callable[[str], FactorExperimentResult]):
        self.max_iterations = max_iterations
        self.runner = runner

    def run(self, factor_description: str) -> FactorExperimentResult:
        last_result = None
        for _attempt in range(self.max_iterations):
            result = self.runner(factor_description)
            last_result = result
            if result.accepted and passes_admission(result.metrics):
                return result
        return last_result


def evaluate_rd_agent_admission(
    result: FactorExperimentResult,
    manual_reviewed: bool,
    min_rationale_chars: int = 20,
) -> RDAgentAdmissionDecision:
    reasons: List[str] = []
    if not result.accepted or not passes_admission(result.metrics):
        reasons.append("statistical_admission")
    if len(result.economic_rationale.strip()) < min_rationale_chars:
        reasons.append("economic_rationale")
    if not manual_reviewed:
        reasons.append("manual_review")
    return RDAgentAdmissionDecision(passed=not reasons, reasons=reasons)
