"""Immutable experiment outcomes used to govern future candidate selection.

Dispositions are intentionally separate from the preregistration ledger.  A
retired experiment must remain in that ledger so its trials continue to count
towards global DSR/PBO multiplicity, even though it may no longer be selected
as a new return candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


class ExperimentDisposition(str, Enum):
    """Lifecycle outcome of an experiment family."""

    PREREGISTERED = "preregistered"
    CANDIDATE_SURVIVED = "candidate_survived"
    COMPLETED_REJECTED = "completed_rejected"
    RETIRED = "retired"
    ADMITTED = "admitted"

    @property
    def allows_candidate_selection(self) -> bool:
        """Whether the experiment may participate in its next frozen test."""

        return self in {
            ExperimentDisposition.PREREGISTERED,
            ExperimentDisposition.CANDIDATE_SURVIVED,
        }

    @property
    def is_terminal(self) -> bool:
        return self in {
            ExperimentDisposition.COMPLETED_REJECTED,
            ExperimentDisposition.RETIRED,
            ExperimentDisposition.ADMITTED,
        }


@dataclass(frozen=True, slots=True)
class ExperimentDispositionRecord:
    """A code-owned, immutable outcome and its stable rationale."""

    experiment_id: str
    disposition: ExperimentDisposition
    rationale: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.experiment_id, "experiment_id"),
            (self.rationale, "rationale"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.disposition, ExperimentDisposition):
            raise TypeError("disposition must be an ExperimentDisposition")


def _record(
    experiment_id: str,
    disposition: ExperimentDisposition,
    rationale: str,
) -> ExperimentDispositionRecord:
    return ExperimentDispositionRecord(
        experiment_id=experiment_id,
        disposition=disposition,
        rationale=rationale,
    )


FIXED_EXPERIMENT_DISPOSITIONS: Mapping[
    str, ExperimentDispositionRecord
] = MappingProxyType({
    "normal-h1-quality-diversification": _record(
        "normal-h1-quality-diversification",
        ExperimentDisposition.PREREGISTERED,
        "awaiting the single frozen PIT quality-diversification evaluation",
    ),
    "normal-h2-capital-efficiency": _record(
        "normal-h2-capital-efficiency",
        ExperimentDisposition.COMPLETED_REJECTED,
        (
            "capital deployment improved, but research and adaptive development "
            "failed net excess, Sharpe, cost-stress, and overfitting gates"
        ),
    ),
    "daytrade-h3-pre-entry-confirmation": _record(
        "daytrade-h3-pre-entry-confirmation",
        ExperimentDisposition.CANDIDATE_SURVIVED,
        "RVOL-2 survived development only and remains the frozen H9 baseline",
    ),
    "daytrade-h4-risk-scale": _record(
        "daytrade-h4-risk-scale",
        ExperimentDisposition.RETIRED,
        "risk-scale candidates failed net-return and stress-cost acceptance",
    ),
    "daytrade-h5-opening-range-top4": _record(
        "daytrade-h5-opening-range-top4",
        ExperimentDisposition.RETIRED,
        "Top-4 filtering failed net-return, stability, and concentration acceptance",
    ),
    "daytrade-h6-market-confirmation": _record(
        "daytrade-h6-market-confirmation",
        ExperimentDisposition.RETIRED,
        "market confirmation failed return, Sharpe, and profit-factor acceptance",
    ),
    "daytrade-h7-rvol-entry-window": _record(
        "daytrade-h7-rvol-entry-window",
        ExperimentDisposition.RETIRED,
        "extended entry windows degraded return, Sharpe, and profit factor",
    ),
    "daytrade-h8-rvol-risk-scale": _record(
        "daytrade-h8-rvol-risk-scale",
        ExperimentDisposition.RETIRED,
        "volatility-scaled stops failed DSR/PBO and drawdown acceptance",
    ),
    "daytrade-h9-rvol-reward-risk": _record(
        "daytrade-h9-rvol-reward-risk",
        ExperimentDisposition.PREREGISTERED,
        "awaiting the single frozen 2R evaluation against the H3 baseline",
    ),
})


def fixed_experiment_disposition(
    experiment_id: str,
) -> ExperimentDispositionRecord | None:
    """Return a fixed outcome without creating mutable runtime state."""

    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ValueError("experiment_id must be a non-empty string")
    return FIXED_EXPERIMENT_DISPOSITIONS.get(experiment_id.strip())


__all__ = (
    "ExperimentDisposition",
    "ExperimentDispositionRecord",
    "FIXED_EXPERIMENT_DISPOSITIONS",
    "fixed_experiment_disposition",
)
