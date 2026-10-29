"""Point-in-time request provider for the normal-frequency strategy.

The provider is deliberately narrow: market-data adapters own storage and
time-alignment, factor functions own signal math, and the portfolio allocator
owns risk construction.  Keeping those boundaries explicit lets the same
provider run in research, paper, and live clocks without a second strategy
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol, Sequence, runtime_checkable

from ..data import PointInTimeMonthlyReturnSnapshot, SecurityMasterSnapshot
from ..factors import (
    FactorSleeve,
    neutralize_sleeve,
    quality_sleeve,
    residual_momentum_sleeve,
)
from ..trading import PointInTimeSnapshot
from .domain import NormalAllocationRequest, NormalRegimeContext


NORMAL_BASELINE = "residual_momentum_12_1_neutral"
NORMAL_QUALITY = "quality_profitability_neutral"


@runtime_checkable
class PointInTimeMarketDataPort(Protocol):
    """Inputs required to build one normal-trading allocation request.

    Every method receives the already materialized security-master snapshot so
    adapters cannot silently select a different universe or observation time.
    Returned histories must contain only observations visible at ``as_of``.
    """

    def monthly_returns(
        self, security_master: SecurityMasterSnapshot
    ) -> PointInTimeMonthlyReturnSnapshot:
        ...

    def asset_return_history(
        self,
        security_master: SecurityMasterSnapshot,
        *,
        lookback_sessions: int,
    ) -> Mapping[str, Sequence[float]]:
        ...

    def sleeve_return_history(
        self,
        security_master: SecurityMasterSnapshot,
        sleeves: Sequence[FactorSleeve],
        *,
        lookback_months: int,
    ) -> Mapping[str, Sequence[float]]:
        ...

    def market_betas(
        self,
        security_master: SecurityMasterSnapshot,
        *,
        lookback_sessions: int,
    ) -> Mapping[str, float]:
        ...

    def average_dollar_volume(
        self,
        security_master: SecurityMasterSnapshot,
        *,
        lookback_sessions: int,
    ) -> Mapping[str, float]:
        ...

    def regime_context(
        self, security_master: SecurityMasterSnapshot
    ) -> NormalRegimeContext:
        ...


@dataclass(frozen=True, slots=True)
class NormalFactorExperiment:
    """The only admitted normal experiment in the current research cycle."""

    include_quality: bool = False

    @property
    def name(self) -> str:
        return (
            "normal-h1-quality-diversification"
            if self.include_quality
            else "corrected-residual-momentum-baseline"
        )


class PointInTimeNormalRequestProvider:
    """Build complete factor/risk requests from one point-in-time snapshot."""

    def __init__(
        self,
        market_data: PointInTimeMarketDataPort,
        *,
        experiment: NormalFactorExperiment = NormalFactorExperiment(),
        capital: float = 100_000.0,
        formal_admission: bool = False,
        residual_estimation_months: int = 36,
        residual_skip_months: int = 1,
        asset_lookback_sessions: int = 252,
        adv_lookback_sessions: int = 20,
        sleeve_history_months: int = 36,
    ) -> None:
        if not isinstance(market_data, PointInTimeMarketDataPort):
            raise TypeError("market_data must implement PointInTimeMarketDataPort")
        if not isinstance(experiment, NormalFactorExperiment):
            raise TypeError("experiment must be a NormalFactorExperiment")
        if isinstance(capital, bool) or float(capital) <= 0:
            raise ValueError("capital must be positive")
        for value, name, minimum in (
            (residual_estimation_months, "residual_estimation_months", 13),
            (residual_skip_months, "residual_skip_months", 0),
            (asset_lookback_sessions, "asset_lookback_sessions", 12),
            (adv_lookback_sessions, "adv_lookback_sessions", 1),
            (sleeve_history_months, "sleeve_history_months", 12),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be at least {minimum}")
        if not isinstance(formal_admission, bool):
            raise ValueError("formal_admission must be a boolean")
        self.market_data = market_data
        self.experiment = experiment
        self.capital = float(capital)
        self.formal_admission = formal_admission
        self.residual_estimation_months = residual_estimation_months
        self.residual_skip_months = residual_skip_months
        self.asset_lookback_sessions = asset_lookback_sessions
        self.adv_lookback_sessions = adv_lookback_sessions
        self.sleeve_history_months = sleeve_history_months

    def build_request(
        self,
        snapshot: PointInTimeSnapshot,
        current_weights: Mapping[str, float],
    ) -> NormalAllocationRequest:
        if not isinstance(snapshot, PointInTimeSnapshot):
            raise TypeError("snapshot must be a PointInTimeSnapshot")
        master = snapshot.security_master
        if master is None:
            raise ValueError("normal provider requires a security master")
        if self.formal_admission and not master.admission_verified:
            # Reject before asking the port for any market data.  Formal runs
            # must not consume a protected window with an inadmissible master.
            raise ValueError("formal normal run requires an admitted security master")
        returns = self.market_data.monthly_returns(master)
        self._require_aligned_returns(master, returns)
        momentum = neutralize_sleeve(
            residual_momentum_sleeve(
                returns,
                master,
                window_months=12,
                skip_recent_months=self.residual_skip_months,
                estimation_window_months=self.residual_estimation_months,
            ),
            master,
        )
        sleeves = [momentum]
        if self.experiment.include_quality:
            sleeves.append(neutralize_sleeve(quality_sleeve(master), master))
        self._require_complete_coverage(master, sleeves)
        if self.formal_admission:
            returns.require_final_admission()
            for sleeve in sleeves:
                sleeve.require_final_admission()
        histories = self.market_data.sleeve_return_history(
            master,
            tuple(sleeves),
            lookback_months=self.sleeve_history_months,
        )
        return NormalAllocationRequest(
            security_master=master,
            sleeves=tuple(sleeves),
            sleeve_confidences={sleeve.name: 1.0 for sleeve in sleeves},
            sleeve_return_history=histories,
            asset_return_history=self.market_data.asset_return_history(
                master,
                lookback_sessions=self.asset_lookback_sessions,
            ),
            current_weights=current_weights,
            market_betas=self.market_data.market_betas(
                master,
                lookback_sessions=self.asset_lookback_sessions,
            ),
            adv_dollars=self.market_data.average_dollar_volume(
                master,
                lookback_sessions=self.adv_lookback_sessions,
            ),
            capital=self.capital,
            regime=self.market_data.regime_context(master),
        )

    @staticmethod
    def _require_aligned_returns(master, returns) -> None:
        if not isinstance(returns, PointInTimeMonthlyReturnSnapshot):
            raise TypeError("monthly_returns must return PointInTimeMonthlyReturnSnapshot")
        if returns.observed_at != master.as_of:
            raise ValueError("monthly returns and security master must share observed_at")
        if returns.security_master_hash != _master_hash(master):
            raise ValueError("monthly returns are bound to a different security master")

    @staticmethod
    def _require_complete_coverage(master, sleeves) -> None:
        expected = set(master.security_ids)
        if not expected:
            raise ValueError("normal experiment requires a non-empty universe")
        for sleeve in sleeves:
            if sleeve.missing_security_ids or set(sleeve.scores) != expected:
                raise ValueError(
                    f"normal experiment requires complete {sleeve.name} coverage"
                )


def _master_hash(master: SecurityMasterSnapshot) -> str:
    from ..data import canonical_security_master_snapshot_hash

    return canonical_security_master_snapshot_hash(master)


__all__ = (
    "NORMAL_BASELINE",
    "NORMAL_QUALITY",
    "NormalFactorExperiment",
    "PointInTimeMarketDataPort",
    "PointInTimeNormalRequestProvider",
)
