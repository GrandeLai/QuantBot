"""Frozen-path entry ablations; event evidence never grants strategy admission."""

from copy import deepcopy
from datetime import datetime, timedelta
from math import isfinite
from numbers import Real

import numpy as np

from .entry_context import FACTOR_IDS, factor_pass
from .time_series import holm_adjust


PROTOCOL = {
    "version": "conditional_entry_screen_v1",
    "cost_bps": [15, 30],
    "windows": 3,
    "minimum_feature_coverage": 0.95,
    "minimum_kept_per_window": 5,
    "minimum_total_kept": 30,
    "minimum_total_filtered": 10,
    "minimum_positive_windows": 2,
    "bootstrap_replicates": 4999,
    "bootstrap_block_sessions": 20,
    "bootstrap_seed": 1729,
    "familywise_alpha": 0.05,
    "maximum_pool_per_market": 2,
}
MARKETS = ("crypto", "stocks")


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("session timestamps must be UTC ISO strings")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0):
        raise ValueError("session timestamps must be timezone-aware UTC")
    return stamp


def _number(value, field, *, missing=False):
    if missing and value is None:
        return None
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError(f"{field} must be numeric")
    value = float(value)
    if not isfinite(value):
        if missing:
            return None
        raise ValueError(f"{field} must be finite")
    return value


def _inputs(market, events_by_cost, calendars):
    if market not in MARKETS:
        raise ValueError("unknown market")
    if set(events_by_cost) != set(PROTOCOL["cost_bps"]):
        raise ValueError("both frozen costs 15 and 30 bps are required")
    if len(calendars) != 3 or "continuous" in calendars:
        raise ValueError("exactly three non-continuous windows are required")
    grids = {}
    for window, values in calendars.items():
        if not isinstance(window, str) or not window or not values:
            raise ValueError("each window needs a name and session calendar")
        stamps = [_stamp(value) for value in values]
        if any(right <= left for left, right in zip(stamps, stamps[1:])):
            raise ValueError("session calendars must be strictly increasing")
        grids[window] = stamps
    windows = sorted(grids, key=lambda window: grids[window][0])
    if any(grids[left][-1] >= grids[right][0] for left, right in zip(windows, windows[1:])):
        raise ValueError("window calendars must not overlap")
    indexes = {window: {at: i for i, at in enumerate(grids[window])} for window in windows}
    events = {}
    for cost, rows in events_by_cost.items():
        events[cost] = {window: [] for window in windows}
        for row in rows:
            window = row["window"]
            if window not in grids:
                raise ValueError("event references an unknown window")
            stamp = _stamp(row["signal_at"])
            if stamp not in indexes[window]:
                raise ValueError("event signal date is not in its frozen session calendar")
            if set(row["factors"]) != set(FACTOR_IDS):
                raise ValueError("event factors must match the four frozen factor IDs")
            events[cost][window].append({
                "index": indexes[window][stamp],
                "account_contribution": _number(row["account_contribution"], "account_contribution"),
                "net_return": _number(row["net_return"], "net_return"),
                "factors": {factor: _number(row["factors"][factor], factor, missing=True)
                            for factor in FACTOR_IDS},
            })
    return windows, grids, events


def _fold(window, calendar, events, factor):
    contributions = np.array([row["account_contribution"] for row in events], dtype=float)
    net_returns = np.array([row["net_return"] for row in events], dtype=float)
    valid = np.array([row["factors"][factor] is not None for row in events], dtype=bool)
    keep = np.array([factor_pass(factor, row["factors"][factor]) for row in events], dtype=bool)
    count, kept = len(events), int(keep.sum())
    coverage = float(valid.mean()) if count else 0.0
    kept_sum = float(contributions[keep].sum())
    delta_sum = float(-contributions[~keep].sum())
    reasons = []
    if coverage < PROTOCOL["minimum_feature_coverage"]:
        reasons.append("insufficient_feature_coverage")
    if kept < PROTOCOL["minimum_kept_per_window"]:
        reasons.append("insufficient_kept_events")
    dropped = contributions[~keep]
    fold = {
        "window": window, "first_session": calendar[0].isoformat(),
        "last_session": calendar[-1].isoformat(), "sessions": len(calendar),
        "baseline_events": count, "valid_feature_events": int(valid.sum()),
        "missing_feature_events": int((~valid).sum()), "feature_coverage": coverage,
        "kept_events": kept, "filtered_events": count-kept,
        "baseline_contribution": float(contributions.sum()),
        "kept_contribution": kept_sum, "incremental_contribution": delta_sum,
        "kept_mean_contribution_per_event": kept_sum/kept if kept else None,
        "increment_per_baseline_event": delta_sum/count if count else None,
        "baseline_mean_net_return": float(net_returns.mean()) if count else None,
        "kept_mean_net_return": float(net_returns[keep].mean()) if kept else None,
        "foregone_profit_contribution": float(dropped[dropped > 0].sum()),
        "avoided_loss_contribution": float(-dropped[dropped < 0].sum()),
        "filtered_winners": int((dropped > 0).sum()),
        "filtered_losers": int((dropped < 0).sum()),
        "jointly_positive": kept_sum > 0 and delta_sum > 0, "reasons": reasons,
    }
    # Four additive series on the actual calendar, not a compressed event index.
    # Non-event sessions have zero COUNTS as well as zero contributions.
    grid = np.zeros((len(calendar), 4))
    for i, event in enumerate(events):
        grid[event["index"]] += (1, int(keep[i]), contributions[i] if keep[i] else 0,
                                0 if keep[i] else -contributions[i])
    return fold, grid


def _circular_sums(grid, width):
    extended = np.concatenate((grid, grid[:width-1]))
    prefix = np.vstack((np.zeros((1, grid.shape[1])), extended.cumsum(axis=0)))
    return prefix[width:width+len(grid)] - prefix[:len(grid)]


def _statistic(samples, observed, invalid):
    missing = int(invalid.sum())
    # Centered one-sided bootstrap, matching the existing factor-study definition.
    p_value = (1 + np.count_nonzero((samples-observed >= observed) | invalid)) / (len(samples)+1)
    return {
        "observed": observed, "p_value": float(p_value),
        "ci_low": None if missing else float(np.quantile(samples, 0.025)),
        "ci_high": None if missing else float(np.quantile(samples, 0.975)),
        "invalid_replicates": missing,
    }


def _bootstrap(grids):
    replicates = PROTOCOL["bootstrap_replicates"]
    rng = np.random.default_rng(PROTOCOL["bootstrap_seed"])
    sampled = np.zeros((replicates, 4))
    for grid in grids:
        full, remainder = divmod(len(grid), PROTOCOL["bootstrap_block_sessions"])
        starts = rng.integers(0, len(grid), size=(replicates, full + bool(remainder)))
        if full:
            sums = _circular_sums(grid, PROTOCOL["bootstrap_block_sessions"])
            sampled += sums[starts[:, :full]].sum(axis=1)
        if remainder:
            sampled += _circular_sums(grid, remainder)[starts[:, -1]]
    observed = sum((grid.sum(axis=0) for grid in grids), np.zeros(4))
    invalid = (sampled[:, 0] == 0) | (sampled[:, 1] == 0)
    net, delta = np.full(replicates, np.nan), np.full(replicates, np.nan)
    np.divide(sampled[:, 2], sampled[:, 1], out=net, where=~invalid)
    np.divide(sampled[:, 3], sampled[:, 0], out=delta, where=~invalid)
    net_stat = _statistic(net, float(observed[2]/observed[1]), invalid)
    delta_stat = _statistic(delta, float(observed[3]/observed[0]), invalid)
    return {
        "status": "available", "replicates": replicates,
        "block_sessions": PROTOCOL["bootstrap_block_sessions"], "seed": PROTOCOL["bootstrap_seed"],
        "window_resampling": "independent_circular_blocks_then_pool_additive_event_totals",
        "net_contribution_per_kept_event": net_stat,
        "increment_per_baseline_event": delta_stat,
        "p_value": max(net_stat["p_value"], delta_stat["p_value"]),
    }


def _cost_result(cost, windows, calendars, events, factor):
    folds, grids = [], []
    for window in windows:
        fold, grid = _fold(window, calendars[window], events[window], factor)
        folds.append(fold)
        grids.append(grid)
    reasons = [f"{fold['window']}:{reason}" for fold in folds for reason in fold["reasons"]]
    total_fields = ("baseline_events", "kept_events", "filtered_events", "missing_feature_events",
                    "baseline_contribution", "kept_contribution", "incremental_contribution",
                    "foregone_profit_contribution", "avoided_loss_contribution", "filtered_winners", "filtered_losers")
    totals = {key: sum(fold[key] for fold in folds) for key in total_fields}
    if totals["kept_events"] < PROTOCOL["minimum_total_kept"]:
        reasons.append("insufficient_total_kept_events")
    if totals["filtered_events"] < PROTOCOL["minimum_total_filtered"]:
        reasons.append("insufficient_filtered_events")
    insufficient = bool(reasons)
    joint_positive = sum(fold["jointly_positive"] for fold in folds)
    if joint_positive < PROTOCOL["minimum_positive_windows"]:
        reasons.append("fewer_than_two_jointly_positive_windows")
    net_average = totals["kept_contribution"]/len(folds)
    delta_average = totals["incremental_contribution"]/len(folds)
    if net_average <= 0:
        reasons.append("nonpositive_mean_window_net_contribution")
    if delta_average <= 0:
        reasons.append("nonpositive_mean_window_increment")
    bootstrap = {"status": "insufficient_evidence", "p_value": 1.0}
    if not insufficient:
        bootstrap = _bootstrap(grids)
        for metric, reason in (("net_contribution_per_kept_event", "net_contribution_uncertain"),
                               ("increment_per_baseline_event", "increment_uncertain")):
            lower = bootstrap[metric]["ci_low"]
            if lower is None or lower <= 0:
                reasons.append(reason)
    return {
        "cost_bps": cost, "folds": folds, "totals": totals,
        "jointly_positive_windows": joint_positive,
        "mean_window_net_contribution": net_average, "mean_window_increment": delta_average,
        "bootstrap": bootstrap, "raw_p_value": bootstrap["p_value"],
        "p_value": 1.0 if reasons else bootstrap["p_value"], "reasons": reasons,
        "status": "insufficient_evidence" if insufficient else ("rejected" if reasons else "pending_multiplicity"),
    }


def evaluate_market(market, events_by_cost, calendars):
    """Evaluate exactly four frozen filters on supplied complete baseline episodes.

    Caller excludes boundary-crossing/incomplete cycles and supplies three distinct
    window accounts. Continuous accounts are diagnostics, never additional samples.
    account_contribution is net cycle PnL divided by that account's initial equity.
    Labels are not recomputed here; all analysis stays on the saved baseline path.
    """
    windows, calendars, events = _inputs(market, events_by_cost, calendars)
    results = []
    for factor in FACTOR_IDS:
        costs = [_cost_result(cost, windows, calendars, events[cost], factor)
                 for cost in PROTOCOL["cost_bps"]]
        reasons = [f"cost_{cost['cost_bps']}:{reason}" for cost in costs for reason in cost["reasons"]]
        results.append({
            "key": f"{market}:{factor}", "market": market, "factor": factor,
            "costs": costs, "raw_p_value": max(cost["raw_p_value"] for cost in costs),
            "p_value": max(cost["p_value"] for cost in costs), "reasons": reasons,
            "status": "insufficient_evidence" if any(cost["status"] == "insufficient_evidence" for cost in costs)
                      else ("rejected" if reasons else "pending_multiplicity"),
        })
    return results


def finalize_pool(all_rows, prior_p_values):
    """Jointly correct all eight hypotheses; select by preregistered ID order."""
    rows = deepcopy(list(all_rows))
    expected = {(market, factor) for market in MARKETS for factor in FACTOR_IDS}
    if len(rows) != len(expected) or {(row["market"], row["factor"]) for row in rows} != expected:
        raise ValueError("the complete eight-hypothesis, two-market family is required")
    prior = [_number(p, "prior p_value") for p in prior_p_values]
    current = [_number(row["p_value"], "p_value") for row in rows]
    if any(not 0 <= p <= 1 for p in current+prior):
        raise ValueError("p values must lie in [0, 1]")
    adjusted = holm_adjust(current+prior)[:len(rows)]
    for row, value in zip(rows, adjusted, strict=True):
        row["holm_p_value"] = value
        if value > PROTOCOL["familywise_alpha"]:
            row["reasons"].append("multiple_testing_not_passed")
        if not row["reasons"]:
            row["status"] = "eligible_development_entry_factor"
        elif row["status"] != "insufficient_evidence":
            row["status"] = "rejected"
        row["selected"] = False
    pool = {market: [] for market in MARKETS}
    lookup = {(row["market"], row["factor"]): row for row in rows}
    for market in MARKETS:
        for factor in FACTOR_IDS:
            row = lookup[(market, factor)]
            if row["status"] != "eligible_development_entry_factor":
                continue
            if len(pool[market]) < PROTOCOL["maximum_pool_per_market"]:
                pool[market].append(factor)
                row["selected"] = True
            else:
                row["status"] = "pool_limit"
    return {
        "stage": "development_event_screen", "protocol": deepcopy(PROTOCOL),
        "results": [lookup[(market, factor)] for market in MARKETS for factor in FACTOR_IDS],
        "pool_by_market": pool, "declared_hypotheses": len(rows),
        "prior_hypotheses": len(prior), "correction_family_size": len(rows)+len(prior),
        "multiplicity_scope": "conservative_development_penalty_not_complete_selection_bias_correction",
        "formal_admission": False, "trading_enabled": False, "model_calls": 0,
        "limitations": [
            "Saved baseline entry paths only; skipping entries can change subsequent portfolio decisions.",
            "Non-event sessions preserve time spacing but never count as independent trades.",
            "Historical exposure remains development data; no independent holdout was evaluated.",
            "A shortlisted factor still requires full account replay with unchanged execution and risk rules.",
        ],
    }
