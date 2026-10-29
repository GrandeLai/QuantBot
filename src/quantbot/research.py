"""Self-developed factor research checklist."""

from dataclasses import dataclass
from datetime import date
from typing import Set


REQUIRED_RESEARCH_STEPS = set(range(1, 11))


@dataclass(frozen=True)
class FactorResearchChecklist:
    completed_steps: Set[int]
    signal_date: date
    data_release_date: date

    def is_admissible(self) -> bool:
        return (
            REQUIRED_RESEARCH_STEPS.issubset(self.completed_steps)
            and self.data_release_date <= self.signal_date
        )


@dataclass(frozen=True)
class LookAheadReviewChecklist:
    uses_publication_dates: bool
    signal_after_data_release: bool
    no_future_columns: bool
    train_validation_separated: bool
    survivor_bias_checked: bool
    adjusted_prices_only: bool

    @property
    def passed(self) -> bool:
        return not self.reasons()

    def reasons(self):
        return [
            name
            for name in (
                "uses_publication_dates",
                "signal_after_data_release",
                "no_future_columns",
                "train_validation_separated",
                "survivor_bias_checked",
                "adjusted_prices_only",
            )
            if not getattr(self, name)
        ]
