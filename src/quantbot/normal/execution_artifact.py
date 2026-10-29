"""Canonical raw Normal execution facts for formal admission.

The presentation report contains derived summaries.  This module deliberately
serializes only the backtest ledger points and the already verified adaptive
trial histories so the formal verifier can recompute every admission metric.
"""

from __future__ import annotations

from typing import Mapping

from ..evidence import hash_payload
from .experiments import NORMAL_H1_EXPERIMENT_ID, NORMAL_H2_EXPERIMENT_ID
from .historical_trials import NormalAdaptiveValidationArtifact


NORMAL_EXECUTION_ARTIFACT_VERSION = 1


def normal_cost_model_payload(
    *,
    cost_bps_per_side: float,
    stress_cost_bps_per_side: float,
) -> dict[str, object]:
    """Return the exact Normal cost identity used by execution and evidence."""

    cost = float(cost_bps_per_side)
    stress = float(stress_cost_bps_per_side)
    if cost != 5.0:
        raise ValueError("normal cost_bps_per_side is frozen at 5.0")
    if stress != 10.0:
        raise ValueError("normal stress_cost_bps_per_side is frozen at 10.0")
    return {
        "execution": "next_open",
        "cost_bps_per_side": cost,
        "stress_cost_bps_per_side": stress,
        "slippage_bps_per_side": 0.0,
    }


def normal_backtest_series_payload(result) -> dict[str, object]:
    """Serialize one backtest arm directly from its ledger points."""

    return {
        "initial_equity": float(result.initial_equity),
        "points": [
            {
                "date": point.trading_date.isoformat(),
                "portfolio_return": float(point.portfolio_return),
                "benchmark_return": float(point.benchmark_return),
                "equity": float(point.equity),
                "fees": float(point.fees),
                "turnover": float(point.turnover),
                "pre_trade_equity": float(point.pre_trade_equity),
                "traded_notional": float(point.traded_notional),
                "weights": _number_mapping(point.weights),
                "security_pnl": _number_mapping(point.security_pnl),
            }
            for point in result.points
        ],
    }


def build_normal_canonical_execution_artifact(
    *,
    experiment_id: str,
    baseline,
    candidate,
    stress_candidate,
    adaptive_validation: NormalAdaptiveValidationArtifact,
    cost_model_payload: Mapping[str, object],
    expected_cost_model_hash: str,
) -> dict[str, object]:
    """Build raw formal facts and bind them to the claimed cost identity."""

    if experiment_id != NORMAL_H1_EXPERIMENT_ID:
        raise ValueError("only normal H1 can produce a formal execution artifact")
    if not isinstance(adaptive_validation, NormalAdaptiveValidationArtifact):
        raise TypeError(
            "adaptive_validation must be a verified NormalAdaptiveValidationArtifact"
        )
    normalized_cost = dict(cost_model_payload)
    if hash_payload(normalized_cost) != expected_cost_model_hash:
        raise PermissionError(
            "normal canonical cost_model_payload does not match the data permit"
        )
    return {
        "artifact_version": NORMAL_EXECUTION_ARTIFACT_VERSION,
        "experiment_id": experiment_id,
        "cost_model_payload": normalized_cost,
        "baseline": normal_backtest_series_payload(baseline),
        "candidate": normal_backtest_series_payload(candidate),
        "stress_candidate": normal_backtest_series_payload(stress_candidate),
        "statistical_observation_dates": [
            value.isoformat() for value in adaptive_validation.observation_dates
        ],
        "statistical_trials": {
            NORMAL_H1_EXPERIMENT_ID: list(adaptive_validation.h1_daily_returns),
            NORMAL_H2_EXPERIMENT_ID: list(adaptive_validation.h2_daily_returns),
        },
    }


def _number_mapping(values: Mapping[str, object]) -> dict[str, float]:
    return {
        str(key): float(value)
        for key, value in sorted(values.items(), key=lambda item: str(item[0]))
    }


__all__ = (
    "NORMAL_EXECUTION_ARTIFACT_VERSION",
    "build_normal_canonical_execution_artifact",
    "normal_backtest_series_payload",
    "normal_cost_model_payload",
)
