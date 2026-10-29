"""Versioned policy for formal paper and live acceptance evidence."""

from dataclasses import dataclass
from typing import Optional

from .acceptance_validation import is_finite_number, is_nonnegative_integer


ACCEPTANCE_POLICY_VERSION = "quantbot.acceptance.v1"
POLICY_VERSION = ACCEPTANCE_POLICY_VERSION
policy_version = ACCEPTANCE_POLICY_VERSION

FORMAL_ACCEPTANCE_REQUIREMENTS = frozenset(
    {
        "normal_strategy_admission",
        "daytrade_strategy_admission",
        "paper_trading_4_weeks",
        "paper_vs_backtest_deviation",
        "paper_reconciliation_60_sessions",
        "live_trading_soak",
    }
)


@dataclass(frozen=True)
class AcceptancePolicy:
    policy_version: str = ACCEPTANCE_POLICY_VERSION
    min_paper_weeks: int = 4
    max_paper_performance_deviation: float = 0.30
    min_live_sessions: int = 20
    max_live_drawdown: float = 0.15

    def __post_init__(self) -> None:
        if not self.policy_version:
            raise ValueError("policy_version is required")
        if not is_nonnegative_integer(self.min_paper_weeks) or self.min_paper_weeks < 1:
            raise ValueError("min_paper_weeks must be a positive integer")
        if not is_nonnegative_integer(self.min_live_sessions) or self.min_live_sessions < 1:
            raise ValueError("min_live_sessions must be a positive integer")
        if (
            not is_finite_number(self.max_paper_performance_deviation)
            or self.max_paper_performance_deviation < 0.0
        ):
            raise ValueError("max_paper_performance_deviation must be finite and non-negative")
        if (
            not is_finite_number(self.max_live_drawdown)
            or not 0.0 <= self.max_live_drawdown <= 1.0
        ):
            raise ValueError("max_live_drawdown must be finite and between 0 and 1")

    def effective(
        self,
        *,
        min_paper_weeks: Optional[int] = None,
        max_paper_performance_deviation: Optional[float] = None,
        min_live_sessions: Optional[int] = None,
        max_live_drawdown: Optional[float] = None,
    ) -> "AcceptancePolicy":
        requested = AcceptancePolicy(
            policy_version=self.policy_version,
            min_paper_weeks=(
                self.min_paper_weeks if min_paper_weeks is None else min_paper_weeks
            ),
            max_paper_performance_deviation=(
                self.max_paper_performance_deviation
                if max_paper_performance_deviation is None
                else max_paper_performance_deviation
            ),
            min_live_sessions=(
                self.min_live_sessions if min_live_sessions is None else min_live_sessions
            ),
            max_live_drawdown=(
                self.max_live_drawdown if max_live_drawdown is None else max_live_drawdown
            ),
        )
        return AcceptancePolicy(
            policy_version=self.policy_version,
            min_paper_weeks=max(self.min_paper_weeks, requested.min_paper_weeks),
            max_paper_performance_deviation=min(
                self.max_paper_performance_deviation,
                requested.max_paper_performance_deviation,
            ),
            min_live_sessions=max(self.min_live_sessions, requested.min_live_sessions),
            max_live_drawdown=min(self.max_live_drawdown, requested.max_live_drawdown),
        )

    def paper_config(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "min_weeks": self.min_paper_weeks,
            "max_performance_deviation": self.max_paper_performance_deviation,
        }

    def live_config(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "min_sessions": self.min_live_sessions,
            "max_drawdown": self.max_live_drawdown,
        }


CURRENT_ACCEPTANCE_POLICY = AcceptancePolicy()
