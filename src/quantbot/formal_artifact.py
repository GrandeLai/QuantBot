"""Canonical raw execution artifacts for formal strategy admission.

The experiment report is a presentation layer.  Formal promotion is rebuilt
from immutable ledger/return/fill facts in this module, never from summary
metrics supplied by a strategy runner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from math import fsum, isfinite, sqrt
from types import MappingProxyType
from typing import Mapping

from .analytics import performance_summary
from .evidence import hash_payload
from .experiment_execution import ExperimentExecutionIdentity
from .normal.experiments import NORMAL_H1_EXPERIMENT_ID, NORMAL_H2_EXPERIMENT_ID
from .statistical_validation import (
    cscv_partition,
    cscv_probability_of_backtest_overfitting,
    deflated_sharpe_ratio,
)
from .strategy_admission import (
    DayTradeAdmissionMetrics,
    EvaluationIdentity,
    NormalAdmissionMetrics,
)
from .validation_protocol import DataAccessPermit


FORMAL_EXECUTION_ARTIFACT_VERSION = 1
DAYTRADE_H9_EXPERIMENT_ID = "daytrade-h9-rvol-reward-risk"
_NORMAL_FIELDS = frozenset(
    {
        "artifact_version",
        "experiment_id",
        "cost_model_payload",
        "baseline",
        "candidate",
        "stress_candidate",
        "statistical_observation_dates",
        "statistical_trials",
    }
)
_DAYTRADE_FIELDS = frozenset(
    {
        "artifact_version",
        "experiment_id",
        "cost_model_payload",
        "initial_capital",
        "observation_dates",
        "baseline_5bps",
        "candidate_5bps",
        "candidate_12bps",
        "statistical_observation_dates",
        "selected_trial_id",
        "statistical_trials",
    }
)
_NORMAL_SERIES_FIELDS = frozenset({"initial_equity", "points"})
_NORMAL_POINT_FIELDS = frozenset(
    {
        "date",
        "portfolio_return",
        "benchmark_return",
        "equity",
        "fees",
        "turnover",
        "pre_trade_equity",
        "traded_notional",
        "weights",
        "security_pnl",
    }
)
_DAYTRADE_SERIES_FIELDS = frozenset({"daily", "trades"})
_DAYTRADE_DAILY_FIELDS = frozenset({"day", "pnl", "ending_equity"})
_DAYTRADE_TRADE_FIELDS = frozenset(
    {
        "day",
        "entry_timestamp",
        "exit_timestamp",
        "side",
        "entry_event_price",
        "exit_event_price",
        "entry_price",
        "exit_price",
        "quantity",
        "notional",
        "borrow_cost",
        "pnl",
        "security_id",
    }
)


@dataclass(frozen=True, slots=True)
class VerifiedExecutionArtifact:
    """Verifier output consumed by the formal evidence materializer."""

    artifact_hash: str
    admission_metrics: NormalAdmissionMetrics | DayTradeAdmissionMetrics
    report_metric_projection: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "report_metric_projection",
            MappingProxyType(dict(self.report_metric_projection)),
        )


def verify_canonical_execution_artifact(
    payload: object,
    *,
    artifact_hash: object,
    execution_identity: ExperimentExecutionIdentity,
    permit: DataAccessPermit,
) -> VerifiedExecutionArtifact:
    """Parse exact raw facts, verify their hash, and recompute admission."""

    if not isinstance(payload, Mapping):
        raise ValueError("canonical_execution_artifact must be an object")
    item = dict(payload)
    supplied_hash = _digest(artifact_hash, "canonical_execution_artifact_hash")
    actual_hash = hash_payload(item)
    if supplied_hash != actual_hash:
        raise PermissionError("canonical execution artifact hash is invalid")
    if item.get("artifact_version") != FORMAL_EXECUTION_ARTIFACT_VERSION:
        raise ValueError("canonical execution artifact version is unsupported")
    if item.get("experiment_id") != execution_identity.experiment_id:
        raise PermissionError("canonical execution artifact experiment is inconsistent")
    cost_payload = item.get("cost_model_payload")
    if not isinstance(cost_payload, Mapping):
        raise ValueError("canonical execution artifact cost_model_payload is missing")
    if hash_payload(dict(cost_payload)) != execution_identity.cost_model_hash:
        raise PermissionError("cost_model_payload does not match execution identity")
    identity = EvaluationIdentity(
        start=permit.request_start,
        end=permit.request_end,
        dataset_hash=permit.dataset_hash,
        cost_model_hash=permit.cost_model_hash,
        config_hash=permit.config_hash,
        code_hash=permit.code_hash,
    )
    if execution_identity.experiment_id == NORMAL_H1_EXPERIMENT_ID:
        metrics, projection = _verify_normal(item, cost_payload, identity, permit)
    elif execution_identity.experiment_id == DAYTRADE_H9_EXPERIMENT_ID:
        metrics, projection = _verify_daytrade(item, cost_payload, identity, permit)
    else:
        raise PermissionError("canonical artifact experiment is not formally active")
    return VerifiedExecutionArtifact(actual_hash, metrics, projection)


def _verify_normal(item, cost_payload, identity, permit):
    _exact_fields(item, _NORMAL_FIELDS, "normal canonical execution artifact")
    _exact_cost_model(
        cost_payload,
        {
            "execution": "next_open",
            "cost_bps_per_side": 5.0,
            "stress_cost_bps_per_side": 10.0,
            "slippage_bps_per_side": 0.0,
        },
        "Normal",
    )
    baseline = _normal_series(item["baseline"], "baseline", cost_bps=5.0)
    candidate = _normal_series(item["candidate"], "candidate", cost_bps=5.0)
    stress = _normal_series(
        item["stress_candidate"],
        "stress_candidate",
        cost_bps=10.0,
    )
    for arm in (candidate, stress):
        if arm["dates"] != baseline["dates"]:
            raise PermissionError("normal artifact arms must use identical dates")
        if arm["benchmark_returns"] != baseline["benchmark_returns"]:
            raise PermissionError("normal artifact arms must use identical benchmark returns")
    _require_dates_within_permit(
        baseline["dates"],
        permit,
        "normal sealed observation_dates",
    )
    stat_dates = _date_array(
        item["statistical_observation_dates"],
        "normal statistical_observation_dates",
        minimum=4,
    )
    trials = _trial_matrix(item["statistical_trials"], stat_dates)
    _require_dates_in_window(
        stat_dates,
        start=date(2021, 1, 1),
        end=date(2024, 12, 31),
        name="normal adaptive statistical_observation_dates",
    )
    expected_trials = {NORMAL_H1_EXPERIMENT_ID, NORMAL_H2_EXPERIMENT_ID}
    if set(trials) != expected_trials:
        raise ValueError("normal statistical_trials must contain exactly H1 and H2")
    h1_returns = trials[NORMAL_H1_EXPERIMENT_ID]
    trial_sharpes = tuple(_sharpe(values) for values in trials.values())
    dsr = deflated_sharpe_ratio(h1_returns, trial_sharpes)
    partition = cscv_partition(trials)
    if partition is None:
        raise ValueError("normal canonical artifact cannot compute PBO")
    pbo = cscv_probability_of_backtest_overfitting(
        partition.trial_returns,
        folds=partition.folds,
    )
    positive_windows = _positive_window_fraction(candidate["returns"], 63)
    concentration = _top_profit_contribution(candidate["pnl"], 3)
    metrics = NormalAdmissionMetrics(
        candidate_identity=identity,
        baseline_identity=identity,
        net_cagr=candidate["summary"].annual_return,
        baseline_net_cagr=baseline["summary"].annual_return,
        sharpe=candidate["summary"].sharpe,
        baseline_sharpe=baseline["summary"].sharpe,
        net_excess_annual=candidate["summary"].excess_alpha_annual,
        max_drawdown=candidate["summary"].max_drawdown,
        annual_turnover=candidate["annual_turnover"],
        stressed_net_excess_annual=stress["summary"].excess_alpha_annual,
        positive_window_fraction=positive_windows,
        dsr_confidence=dsr.confidence,
        pbo=pbo.probability,
        top_three_profit_contribution=concentration,
        gross_leverage=candidate["gross_leverage"],
        declared_trials=2,
        completed_trials=2,
        sealed_data_access_permit=permit,
    )
    projection = {
        "declared_trials": 2,
        "completed_trials": 2,
        "decision": {
            "positive_window_fraction": positive_windows,
            "top_three_profit_contribution": concentration,
        },
        "baseline": {
            "annual_return": baseline["summary"].annual_return,
            "sharpe": baseline["summary"].sharpe,
        },
        "candidate": {
            "annual_return": candidate["summary"].annual_return,
            "sharpe": candidate["summary"].sharpe,
            "excess_alpha_annual": candidate["summary"].excess_alpha_annual,
            "max_drawdown": candidate["summary"].max_drawdown,
            "annual_turnover": candidate["annual_turnover"],
        },
        "stress_candidate": {
            "excess_alpha_annual": stress["summary"].excess_alpha_annual,
        },
        "dsr_confidence": dsr.confidence,
        "pbo": pbo.probability,
        "gross_leverage": candidate["gross_leverage"],
    }
    return metrics, projection


def _normal_series(payload, name, *, cost_bps):
    item = _mapping(payload, f"normal {name}")
    _exact_fields(item, _NORMAL_SERIES_FIELDS, f"normal {name}")
    initial = _number(item["initial_equity"], f"normal {name}.initial_equity", minimum=0.0, strict=True)
    raw_points = item["points"]
    if not isinstance(raw_points, list) or len(raw_points) < 2:
        raise ValueError(f"normal {name}.points must contain at least two rows")
    dates = []
    returns = []
    benchmarks = []
    turnover = []
    pnl: dict[str, float] = {}
    previous = initial
    gross_leverage = 0.0
    for index, raw in enumerate(raw_points):
        point = _mapping(raw, f"normal {name}.points[{index}]")
        _exact_fields(point, _NORMAL_POINT_FIELDS, f"normal {name}.points[{index}]")
        day = _day(point["date"], f"normal {name} date")
        portfolio_return = _return(point["portfolio_return"], f"normal {name} return")
        benchmark_return = _return(point["benchmark_return"], f"normal {name} benchmark return")
        equity = _number(point["equity"], f"normal {name} equity", minimum=0.0, strict=True)
        _close(equity, previous * (1.0 + portfolio_return), f"normal {name} equity")
        fees = _number(point["fees"], f"normal {name} fees", minimum=0.0)
        point_turnover = _number(
            point["turnover"], f"normal {name} turnover", minimum=0.0
        )
        pre_trade_equity = _number(
            point["pre_trade_equity"],
            f"normal {name} pre_trade_equity",
            minimum=0.0,
            strict=True,
        )
        traded_notional = _number(
            point["traded_notional"],
            f"normal {name} traded_notional",
            minimum=0.0,
        )
        _close(
            fees,
            traded_notional * cost_bps / 10_000.0,
            f"normal {name} fees/traded_notional",
        )
        _close(
            point_turnover,
            0.5 * traded_notional / pre_trade_equity,
            f"normal {name} turnover/traded_notional",
        )
        turnover.append(point_turnover)
        weights = _number_mapping(point["weights"], f"normal {name} weights")
        gross_leverage = max(gross_leverage, fsum(abs(value) for value in weights.values()))
        point_security_pnl = _number_mapping(
            point["security_pnl"], f"normal {name} security_pnl"
        )
        _close(
            fsum(point_security_pnl.values()),
            equity - previous,
            f"normal {name} security_pnl/equity",
        )
        for security_id, value in point_security_pnl.items():
            pnl[security_id] = pnl.get(security_id, 0.0) + value
        dates.append(day)
        returns.append(portfolio_return)
        benchmarks.append(benchmark_return)
        previous = equity
    _ascending_unique(dates, f"normal {name} dates")
    years = len(returns) / 252.0
    return {
        "dates": tuple(dates),
        "returns": tuple(returns),
        "benchmark_returns": tuple(benchmarks),
        "summary": performance_summary(returns, benchmarks),
        "annual_turnover": fsum(turnover) / years,
        "gross_leverage": gross_leverage,
        "pnl": pnl,
    }


def _verify_daytrade(item, cost_payload, identity, permit):
    _exact_fields(item, _DAYTRADE_FIELDS, "DayTrade canonical execution artifact")
    expected_keys = {
        "execution",
        "cost_bps_per_side",
        "stress_cost_bps_per_side",
        "max_volume_participation",
    }
    if set(cost_payload) != expected_keys:
        raise PermissionError("DayTrade cost_model_payload fields are not canonical")
    if (
        cost_payload.get("execution") != "next_available_minute_event"
        or _number(cost_payload.get("cost_bps_per_side"), "DayTrade baseline cost") != 5.0
        or _number(cost_payload.get("stress_cost_bps_per_side"), "DayTrade stress cost") != 12.0
    ):
        raise PermissionError("DayTrade cost model must be exact 5/12 bps")
    _number(
        cost_payload.get("max_volume_participation"),
        "DayTrade max_volume_participation",
        minimum=0.0,
        maximum=1.0,
        strict=True,
    )
    initial = _number(item["initial_capital"], "DayTrade initial_capital", minimum=0.0, strict=True)
    observation_dates = _date_array(item["observation_dates"], "DayTrade observation_dates", minimum=2)
    baseline = _daytrade_series(
        item["baseline_5bps"], "baseline_5bps", initial, cost_bps=5.0
    )
    candidate = _daytrade_series(
        item["candidate_5bps"], "candidate_5bps", initial, cost_bps=5.0
    )
    stress = _daytrade_series(
        item["candidate_12bps"], "candidate_12bps", initial, cost_bps=12.0
    )
    for arm in (baseline, candidate, stress):
        if arm["dates"] != observation_dates:
            raise PermissionError("DayTrade artifact arms must use observation_dates exactly")
    _require_dates_within_permit(
        observation_dates,
        permit,
        "DayTrade sealed observation_dates",
    )
    stat_dates = _date_array(item["statistical_observation_dates"], "DayTrade statistical_observation_dates", minimum=4)
    trials = _trial_matrix(item["statistical_trials"], stat_dates)
    _require_dates_in_window(
        stat_dates,
        start=date(2023, 1, 1),
        end=date(2024, 12, 31),
        name="DayTrade adaptive statistical_observation_dates",
    )
    selected_id = item["selected_trial_id"]
    if not isinstance(selected_id, str) or selected_id not in trials:
        raise ValueError("DayTrade selected_trial_id is absent from statistical_trials")
    declared_trials = len(trials) - 1
    if declared_trials != 21:
        raise ValueError("DayTrade statistical_trials must contain baseline plus 21 trials")
    selected_returns = trials[selected_id]
    dsr = deflated_sharpe_ratio(
        selected_returns,
        tuple(_sharpe(values) for values in trials.values()),
    )
    partition = cscv_partition(trials, fold_preference=(8, 6, 4, 2))
    if partition is None:
        raise ValueError("DayTrade canonical artifact cannot compute PBO")
    pbo = cscv_probability_of_backtest_overfitting(
        partition.trial_returns,
        folds=partition.folds,
    )
    quarters = _quarterly_returns(candidate["dates"], candidate["returns"])
    positive_quarters = sum(value > 0.0 for value in quarters.values()) / len(quarters)
    top_one = _top_profit_contribution(candidate["pnl_by_security"], 1)
    top_two = _top_profit_contribution(candidate["pnl_by_security"], 2)
    metrics = DayTradeAdmissionMetrics(
        candidate_identity=identity,
        baseline_identity=identity,
        oos_months=len({(value.year, value.month) for value in observation_dates}),
        oos_trades=len(candidate["trades"]),
        net_return_after_5bps=candidate["cumulative_return"],
        profit_factor=candidate["profit_factor"],
        sharpe=candidate["sharpe"],
        max_drawdown=candidate["max_drawdown"],
        annual_net_return=candidate["annual_return"],
        baseline_annual_net_return=baseline["annual_return"],
        baseline_sharpe=baseline["sharpe"],
        baseline_max_drawdown=baseline["max_drawdown"],
        stressed_12bps_cumulative_return=stress["cumulative_return"],
        stressed_12bps_max_drawdown=stress["max_drawdown"],
        positive_quarter_fraction=positive_quarters,
        top_symbol_profit_contribution=top_one,
        top_two_profit_contribution=top_two,
        dsr_confidence=dsr.confidence,
        pbo=pbo.probability,
        gross_leverage=candidate["gross_leverage"],
        declared_trials=declared_trials,
        completed_trials=declared_trials,
        sealed_data_access_permit=permit,
    )
    values = {
        "oos_months": metrics.oos_months,
        "oos_trades": metrics.oos_trades,
        "net_return_after_5bps": metrics.net_return_after_5bps,
        "profit_factor": metrics.profit_factor,
        "sharpe": metrics.sharpe,
        "max_drawdown": metrics.max_drawdown,
        "annual_net_return": metrics.annual_net_return,
        "baseline_annual_net_return": metrics.baseline_annual_net_return,
        "baseline_sharpe": metrics.baseline_sharpe,
        "baseline_max_drawdown": metrics.baseline_max_drawdown,
        "stressed_12bps_cumulative_return": metrics.stressed_12bps_cumulative_return,
        "stressed_12bps_max_drawdown": metrics.stressed_12bps_max_drawdown,
        "positive_quarter_fraction": metrics.positive_quarter_fraction,
        "top_symbol_profit_contribution": metrics.top_symbol_profit_contribution,
        "top_two_profit_contribution": metrics.top_two_profit_contribution,
        "dsr_confidence": metrics.dsr_confidence,
        "pbo": metrics.pbo,
        "gross_leverage": metrics.gross_leverage,
        "declared_trials": metrics.declared_trials,
        "completed_trials": metrics.completed_trials,
    }
    return metrics, {"fixed_candidate_evaluation": {"admission_metrics": values}}


def _daytrade_series(payload, name, initial, *, cost_bps):
    item = _mapping(payload, f"DayTrade {name}")
    _exact_fields(item, _DAYTRADE_SERIES_FIELDS, f"DayTrade {name}")
    raw_daily = item["daily"]
    raw_trades = item["trades"]
    if not isinstance(raw_daily, list) or not raw_daily:
        raise ValueError(f"DayTrade {name}.daily must not be empty")
    if not isinstance(raw_trades, list):
        raise ValueError(f"DayTrade {name}.trades must be an array")
    dates = []
    returns = []
    equity = initial
    pnl_by_day: dict[date, float] = {}
    trades = []
    pnl_by_security: dict[str, float] = {}
    events = []
    for index, raw in enumerate(raw_trades):
        trade = _mapping(raw, f"DayTrade {name}.trades[{index}]")
        _exact_fields(trade, _DAYTRADE_TRADE_FIELDS, f"DayTrade {name}.trades[{index}]")
        day = _day(trade["day"], f"DayTrade {name} trade day")
        entry = _timestamp(trade["entry_timestamp"], f"DayTrade {name} entry_timestamp")
        exit_time = _timestamp(trade["exit_timestamp"], f"DayTrade {name} exit_timestamp")
        if exit_time < entry:
            raise ValueError(f"DayTrade {name} trade exits before entry")
        side = trade["side"]
        if side not in {"long", "short"}:
            raise ValueError(f"DayTrade {name} trade side is invalid")
        entry_event_price = _number(
            trade["entry_event_price"],
            "entry_event_price",
            minimum=0.0,
            strict=True,
        )
        exit_event_price = _number(
            trade["exit_event_price"],
            "exit_event_price",
            minimum=0.0,
            strict=True,
        )
        entry_price = _number(trade["entry_price"], "entry_price", minimum=0.0, strict=True)
        exit_price = _number(trade["exit_price"], "exit_price", minimum=0.0, strict=True)
        quantity = _number(trade["quantity"], "quantity", minimum=0.0, strict=True)
        notional = _number(trade["notional"], "notional", minimum=0.0, strict=True)
        borrow = _number(trade["borrow_cost"], "borrow_cost", minimum=0.0)
        pnl = _number(trade["pnl"], "pnl")
        security_id = trade["security_id"]
        if not isinstance(security_id, str) or not security_id.strip():
            raise ValueError(f"DayTrade {name} trade security_id is required")
        _close(notional, entry_price * quantity, f"DayTrade {name} trade notional")
        cost_rate = cost_bps / 10_000.0
        expected_entry = entry_event_price * (
            1.0 + cost_rate if side == "long" else 1.0 - cost_rate
        )
        expected_exit = exit_event_price * (
            1.0 - cost_rate if side == "long" else 1.0 + cost_rate
        )
        _close(entry_price, expected_entry, f"DayTrade {name} entry fill cost")
        _close(exit_price, expected_exit, f"DayTrade {name} exit fill cost")
        gross = (
            (exit_price - entry_price) * quantity
            if side == "long"
            else (entry_price - exit_price) * quantity
        )
        _close(pnl, gross - borrow, f"DayTrade {name} trade pnl")
        pnl_by_day[day] = pnl_by_day.get(day, 0.0) + pnl
        pnl_by_security[security_id] = pnl_by_security.get(security_id, 0.0) + pnl
        events.append((entry, 0, notional, 0.0))
        events.append((exit_time, 1, -notional, pnl))
        trades.append(trade)
    for index, raw in enumerate(raw_daily):
        daily = _mapping(raw, f"DayTrade {name}.daily[{index}]")
        _exact_fields(daily, _DAYTRADE_DAILY_FIELDS, f"DayTrade {name}.daily[{index}]")
        day = _day(daily["day"], f"DayTrade {name} daily day")
        pnl = _number(daily["pnl"], f"DayTrade {name} daily pnl")
        ending = _number(daily["ending_equity"], f"DayTrade {name} ending_equity", minimum=0.0, strict=True)
        _close(pnl, pnl_by_day.get(day, 0.0), f"DayTrade {name} daily/trade pnl")
        _close(ending, equity + pnl, f"DayTrade {name} ending_equity")
        returns.append(pnl / equity)
        dates.append(day)
        equity = ending
    if set(pnl_by_day).difference(dates):
        raise ValueError(f"DayTrade {name} trades contain an unledgered day")
    _ascending_unique(dates, f"DayTrade {name} dates")
    gross_notional = 0.0
    realized_equity = initial
    peak_leverage = 0.0
    for _timestamp_value, _order, notional_delta, realized_pnl in sorted(events):
        gross_notional = max(0.0, gross_notional + notional_delta)
        realized_equity += realized_pnl
        if realized_equity <= 0.0:
            raise ValueError(f"DayTrade {name} realized equity is non-positive")
        peak_leverage = max(peak_leverage, gross_notional / realized_equity)
    wins = [float(trade["pnl"]) for trade in trades if float(trade["pnl"]) > 0.0]
    losses = [float(trade["pnl"]) for trade in trades if float(trade["pnl"]) < 0.0]
    profit_factor = fsum(wins) / abs(fsum(losses)) if losses else (1e12 if wins else 0.0)
    return {
        "dates": tuple(dates),
        "returns": tuple(returns),
        "trades": tuple(trades),
        "cumulative_return": equity / initial - 1.0,
        "annual_return": _annualized_return(returns),
        "sharpe": _sharpe(returns),
        "max_drawdown": _max_drawdown(returns),
        "profit_factor": profit_factor,
        "gross_leverage": peak_leverage,
        "pnl_by_security": pnl_by_security,
    }


def _trial_matrix(payload, dates):
    item = _mapping(payload, "statistical_trials")
    if not item:
        raise ValueError("statistical_trials must not be empty")
    output = {}
    for name, raw in item.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("statistical trial ids must be non-empty strings")
        if not isinstance(raw, list) or len(raw) != len(dates):
            raise ValueError(f"statistical trial {name} is not date-aligned")
        output[name] = tuple(_return(value, f"statistical trial {name}") for value in raw)
    return dict(sorted(output.items()))


def _exact_cost_model(payload, expected, name):
    if set(payload) != set(expected):
        raise PermissionError(f"{name} cost_model_payload fields are not canonical")
    for key, value in expected.items():
        supplied = payload.get(key)
        if isinstance(value, float):
            supplied = _number(supplied, f"{name} cost_model_payload.{key}")
        if supplied != value:
            raise PermissionError(f"{name} cost_model_payload.{key} is not canonical")


def _mapping(value, name):
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _exact_fields(item, expected, name):
    if set(item) != expected:
        raise ValueError(f"{name} fields are invalid")


def _number(value, name, *, minimum=None, maximum=None, strict=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and (result <= minimum if strict else result < minimum):
        comparator = "greater than" if strict else "at least"
        raise ValueError(f"{name} must be {comparator} {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return result


def _return(value, name):
    return _number(value, name, minimum=-1.0)


def _number_mapping(value, name):
    item = _mapping(value, name)
    output = {}
    for key, raw in item.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-empty strings")
        output[key] = _number(raw, f"{name}.{key}")
    return output


def _digest(value, name):
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _day(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a canonical ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical ISO date") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date")
    return parsed


def _date_array(value, name, *, minimum):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    dates = tuple(_day(item, name) for item in value)
    if len(dates) < minimum:
        raise ValueError(f"{name} requires at least {minimum} observations")
    _ascending_unique(dates, name)
    return dates


def _timestamp(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a canonical ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a canonical ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical timezone-aware ISO timestamp")
    return parsed


def _ascending_unique(values, name):
    if tuple(values) != tuple(sorted(set(values))):
        raise ValueError(f"{name} must be strictly ascending and unique")


def _require_dates_within_permit(values, permit, name):
    if not values or values[0] < permit.request_start or values[-1] > permit.request_end:
        raise PermissionError(f"{name} must stay inside the permit request interval")


def _require_dates_in_window(values, *, start, end, name):
    if not values or values[0] < start or values[-1] > end:
        raise PermissionError(f"{name} must stay inside {start.isoformat()}..{end.isoformat()}")


def _close(actual, expected, name):
    tolerance = max(1.0, abs(actual), abs(expected)) * 1e-10
    if abs(actual - expected) > tolerance:
        raise PermissionError(f"{name} is not conserved by raw facts")


def _annualized_return(returns):
    growth = 1.0
    for value in returns:
        growth *= 1.0 + value
    return -1.0 if growth <= 0.0 else growth ** (252.0 / len(returns)) - 1.0


def _sharpe(returns):
    if len(returns) < 2:
        return 0.0
    mean = fsum(returns) / len(returns)
    variance = fsum((value - mean) ** 2 for value in returns) / len(returns)
    return mean / sqrt(variance) * sqrt(252.0) if variance > 0.0 else 0.0


def _max_drawdown(returns):
    equity = peak = 1.0
    maximum = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        maximum = max(maximum, 1.0 - equity / peak)
    return maximum


def _positive_window_fraction(returns, window):
    if len(returns) < window:
        return 0.0
    values = [
        fsum(returns[index : index + window])
        for index in range(len(returns) - window + 1)
    ]
    return sum(value > 0.0 for value in values) / len(values)


def _top_profit_contribution(values, count):
    ranked = sorted((value for value in values.values() if value > 0.0), reverse=True)
    total = fsum(ranked)
    return fsum(ranked[:count]) / total if total > 0.0 else 1.0


def _quarterly_returns(dates, returns):
    grouped: dict[tuple[int, int], list[float]] = {}
    for day, value in zip(dates, returns):
        grouped.setdefault((day.year, (day.month - 1) // 3 + 1), []).append(value)
    output = {}
    for key, values in grouped.items():
        growth = 1.0
        for value in values:
            growth *= 1.0 + value
        output[key] = growth - 1.0
    if not output:
        raise ValueError("DayTrade canonical artifact has no quarters")
    return output


__all__ = (
    "DAYTRADE_H9_EXPERIMENT_ID",
    "FORMAL_EXECUTION_ARTIFACT_VERSION",
    "VerifiedExecutionArtifact",
    "verify_canonical_execution_artifact",
)
