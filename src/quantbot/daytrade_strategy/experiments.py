"""Pre-registration for one-variable DayTrade hypothesis experiments."""

from __future__ import annotations

from datetime import datetime, timezone

from ..experiment_registry import ExperimentRegistry


DAYTRADE_PREREGISTERED_AT = datetime(2026, 7, 11, tzinfo=timezone.utc)
DAYTRADE_FOLLOWUP_PREREGISTERED_AT = datetime(2026, 7, 17, tzinfo=timezone.utc)
DAYTRADE_H6_PREREGISTERED_AT = datetime(2026, 7, 17, 2, 58, tzinfo=timezone.utc)
DAYTRADE_H7_PREREGISTERED_AT = datetime(2026, 7, 17, 7, 11, 44, tzinfo=timezone.utc)
DAYTRADE_H8_PREREGISTERED_AT = datetime(2026, 7, 17, 7, 31, 23, tzinfo=timezone.utc)
DAYTRADE_H9_PREREGISTERED_AT = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)


def preregistered_daytrade_experiments() -> ExperimentRegistry:
    registry = ExperimentRegistry()
    _register_single(
        registry,
        experiment_id="daytrade-h1-session-vwap",
        component="session_vwap",
        parameter="vwap_variant",
        values=("off", "cumulative", "cumulative_hysteresis"),
    )
    _register_single(
        registry,
        experiment_id="daytrade-h2-opening-window",
        component="opening_window",
        parameter="entry_end",
        values=("10:00", "10:30", "11:00"),
    )
    _register_single(
        registry,
        experiment_id="daytrade-h3-pre-entry-confirmation",
        component="pre_entry_confirmation",
        parameter="min_relative_volume",
        values=(1.0, 1.5, 2.0),
    )
    _register_single(
        registry,
        experiment_id="daytrade-h4-risk-scale",
        component="volatility_scaled_stop",
        parameter="stop_basis",
        values=("fixed_pct", "atr", "opening_range"),
    )
    return registry


def preregistered_daytrade_followup_experiments() -> ExperimentRegistry:
    """Return later hypotheses without changing the frozen H1-H4 search ledger."""

    registry = ExperimentRegistry()
    registry.register_single_family(
        experiment_id="daytrade-h5-opening-range-top4",
        strategy_family="daytrade",
        baseline_components=("opening_range_breakout",),
        candidate_components=("opening_range_breakout", "opening_range_top_k"),
        parameter_name="filter_configuration",
        parameter_values=("opening_range_top4_positive_momentum",),
        preregistered_at=DAYTRADE_FOLLOWUP_PREREGISTERED_AT,
    )
    return registry


def preregistered_daytrade_h3_experiments() -> ExperimentRegistry:
    """Return only the original, preregistered RVOL family."""

    registry = ExperimentRegistry()
    _register_single(
        registry,
        experiment_id="daytrade-h3-pre-entry-confirmation",
        component="pre_entry_confirmation",
        parameter="min_relative_volume",
        values=(1.0, 1.5, 2.0),
    )
    return registry


def preregistered_daytrade_h6_experiments() -> ExperimentRegistry:
    """Preregister the index-confirmation family separately from H1-H5."""

    registry = ExperimentRegistry()
    registry.register_single_family(
        experiment_id="daytrade-h6-market-confirmation",
        strategy_family="daytrade",
        baseline_components=("opening_range_breakout",),
        candidate_components=("opening_range_breakout", "market_confirmation"),
        parameter_name="minimum_spy_qqq_return",
        parameter_values=(0.0, 0.0005, 0.001),
        preregistered_at=DAYTRADE_H6_PREREGISTERED_AT,
    )
    return registry


def preregistered_daytrade_h7_experiments() -> ExperimentRegistry:
    """Preregister RVOL-2 entry-window interactions after H3 was frozen."""

    registry = ExperimentRegistry()
    registry.register_single_family(
        experiment_id="daytrade-h7-rvol-entry-window",
        strategy_family="daytrade",
        baseline_components=("opening_range_breakout", "rvol_2_0"),
        candidate_components=(
            "opening_range_breakout",
            "rvol_2_0",
            "extended_entry_window",
        ),
        parameter_name="entry_end",
        parameter_values=("11:00", "11:30"),
        preregistered_at=DAYTRADE_H7_PREREGISTERED_AT,
    )
    return registry


def preregistered_daytrade_h8_experiments() -> ExperimentRegistry:
    """Preregister volatility-scaled stops on the frozen H3 candidate."""

    registry = ExperimentRegistry()
    registry.register_single_family(
        experiment_id="daytrade-h8-rvol-risk-scale",
        strategy_family="daytrade",
        baseline_components=(
            "opening_range_breakout",
            "rvol_2_0",
        ),
        candidate_components=(
            "opening_range_breakout",
            "rvol_2_0",
            "volatility_scaled_stop",
        ),
        parameter_name="stop_basis",
        parameter_values=("atr", "opening_range"),
        preregistered_at=DAYTRADE_H8_PREREGISTERED_AT,
    )
    return registry


def preregistered_daytrade_h9_experiments() -> ExperimentRegistry:
    """Preregister one 2R target trial on the frozen ORB + RVOL-2 baseline."""

    registry = ExperimentRegistry()
    registry.register_single_family(
        experiment_id="daytrade-h9-rvol-reward-risk",
        strategy_family="daytrade",
        baseline_components=(
            "opening_range_breakout",
            "rvol_2_0",
            "reward_risk_1_5",
        ),
        candidate_components=(
            "opening_range_breakout",
            "rvol_2_0",
            "reward_risk_1_5",
            "reward_risk_2_0",
        ),
        parameter_name="reward_risk",
        parameter_values=(2.0,),
        preregistered_at=DAYTRADE_H9_PREREGISTERED_AT,
    )
    return registry


def preregistered_daytrade_global_experiments() -> ExperimentRegistry:
    """Return H9 plus the immutable H1-H8 multiplicity ledger.

    Historical trial identities are imported for global DSR/PBO accounting,
    but completion is deliberately not inferred from preregistration.  Only an
    immutable aligned-return artifact may prove those historical executions.
    """

    registry = preregistered_daytrade_h9_experiments()
    historical_registries = (
        preregistered_daytrade_experiments(),
        preregistered_daytrade_followup_experiments(),
        preregistered_daytrade_h6_experiments(),
        preregistered_daytrade_h7_experiments(),
        preregistered_daytrade_h8_experiments(),
    )
    for historical in historical_registries:
        registry.import_known_experiments(historical.known_records)
    if registry.global_declared_trial_count != 21:
        raise RuntimeError("DayTrade H1-H9 global ledger must declare 21 trials")
    if registry.global_completed_trial_count != 0:
        raise RuntimeError("DayTrade historical trials require execution evidence")
    return registry


def _register_single(registry, *, experiment_id, component, parameter, values):
    registry.register_single_family(
        experiment_id=experiment_id,
        strategy_family="daytrade",
        baseline_components=("opening_range_breakout",),
        candidate_components=("opening_range_breakout", component),
        parameter_name=parameter,
        parameter_values=values,
        preregistered_at=DAYTRADE_PREREGISTERED_AT,
    )


__all__ = (
    "DAYTRADE_FOLLOWUP_PREREGISTERED_AT",
    "DAYTRADE_H6_PREREGISTERED_AT",
    "DAYTRADE_H7_PREREGISTERED_AT",
    "DAYTRADE_H8_PREREGISTERED_AT",
    "DAYTRADE_H9_PREREGISTERED_AT",
    "DAYTRADE_PREREGISTERED_AT",
    "preregistered_daytrade_experiments",
    "preregistered_daytrade_followup_experiments",
    "preregistered_daytrade_h3_experiments",
    "preregistered_daytrade_h6_experiments",
    "preregistered_daytrade_h7_experiments",
    "preregistered_daytrade_h8_experiments",
    "preregistered_daytrade_h9_experiments",
    "preregistered_daytrade_global_experiments",
)
