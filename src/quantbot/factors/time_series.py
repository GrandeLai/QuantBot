"""Finite, development-only factor screening; never strategy admission evidence."""

from collections import Counter

import numpy as np
import pandas as pd

from .crypto_panel import DAY, FACTORS, HORIZONS, SYMBOLS


PROTOCOL = {
    "version": "crypto_factor_screen_v1",
    "minimum_history_days": 1095,
    "minimum_training_observations": 365,
    "validation_folds": 3,
    "validation_days": 180,
    "embargo_days": 1,
    "minimum_validation_observations": 120,
    "minimum_feature_coverage": 0.95,
    "entry_quantile": 0.8,
    "one_way_cost_bps": 15.0,
    "stress_cost_multiplier": 2,
    "minimum_events_per_fold": 5,
    "minimum_total_events": 30,
    "minimum_positive_folds": 2,
    "bootstrap_replicates": 4999,
    "bootstrap_block_days": 20,
    "bootstrap_seed": 1729,
    "familywise_alpha": 0.05,
    "multiplicity": "Holm across all 51 preregistered alpha-symbol-horizon hypotheses, including failures",
    "maximum_abs_training_correlation": 0.65,
    "maximum_pool_size": 6,
    "directions": "fixed economic hypotheses; no validation-driven sign flips",
    "relative_diagnostics": "symbol gross return minus other symbol; diagnostic only, not rotation PnL or pool gate",
    "selection_scope": "development_factor_candidates_only",
}


def hypotheses():
    return [
        {"key": f"{definition.name}:{symbol}:{horizon}", "factor": definition.name,
         "symbol": symbol, "horizon": horizon, "role": definition.role,
         "applicable": symbol in definition.symbols,
         "inferential": definition.role == "alpha" and symbol in definition.symbols}
        for definition in FACTORS for symbol in SYMBOLS for horizon in HORIZONS
    ]


def study_protocol(validation_days=180, prior_hypotheses=0):
    """Only expand validation geometry; keep all factor/cost/admission thresholds fixed."""
    if type(validation_days) is not int or validation_days < PROTOCOL["validation_days"]:
        raise ValueError("validation_days must be an integer of at least 180")
    if type(prior_hypotheses) is not int or prior_hypotheses < 0:
        raise ValueError("prior_hypotheses must be a nonnegative integer")
    current = sum(item["inferential"] for item in hypotheses())
    return {
        **PROTOCOL, "version": "crypto_factor_screen_v2", "validation_days": validation_days,
        "prior_inferential_hypotheses": prior_hypotheses,
        "correction_family_size": current + prior_hypotheses,
        "multiplicity": "Holm across current and frozen prior registered factor hypotheses, including failures",
    }


def rank_ic(x, y):
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    if valid.sum() < 3:
        return None
    rx, ry = pd.Series(x[valid]).rank(), pd.Series(y[valid]).rank()
    if rx.nunique() < 2 or ry.nunique() < 2:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def _mean(values):
    return float(np.mean(values)) if len(values) else None


def block_evidence(returns, active, horizon, *, replicates=4999, seed=1729):
    """Circular moving-block bootstrap of paired (return, selection) event grids.

    Compare selected event means with both zero and the same calendar grid's
    unconditional mean. These are exploratory conditional-mean tests, not
    confidence bounds on account returns or out-of-sample trading performance.
    """
    returns, active = np.asarray(returns, float), np.asarray(active, bool)
    if len(returns) < 3 or active.sum() < 2 or not np.isfinite(returns).all():
        return {"p_value": 1.0, "net_ci_low": None, "lift_ci_low": None, "status": "insufficient_evidence"}
    selected_mean = float(returns[active].mean())
    lift = selected_mean - float(returns.mean())
    block = max(1, int(np.ceil(PROTOCOL["bootstrap_block_days"] / horizon)))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, len(returns), size=(replicates, int(np.ceil(len(returns)/block))))
    indexes = ((starts[..., None] + np.arange(block)) % len(returns)).reshape(replicates, -1)[:, :len(returns)]
    samples, masks = returns[indexes], active[indexes]
    counts = masks.sum(axis=1)
    # A replicate with no selected events contributes no affirmative evidence.
    means = np.full(replicates, np.nan)
    np.divide((samples*masks).sum(axis=1), counts, out=means, where=counts > 0)
    lifts = means - samples.mean(axis=1)
    missing = ~np.isfinite(means)
    p_net = (1 + np.count_nonzero((means-selected_mean >= selected_mean) | missing)) / (replicates+1)
    p_lift = (1 + np.count_nonzero((lifts-lift >= lift) | missing)) / (replicates+1)
    # Requiring both nulls to be rejected is an intersection-union test.
    return {"p_value": float(max(p_net, p_lift)),
            "net_ci_low": float(np.quantile(means, 0.025)) if not missing.any() else None,
            "lift_ci_low": float(np.quantile(lifts, 0.025)) if not missing.any() else None,
            "status": "available", "block_grid_points": block,
            "replicates": replicates, "net_mean": selected_mean, "net_lift": lift}


def holm_adjust(p_values):
    """Family-wise correction valid without assuming BTC/ETH independence."""
    order = sorted(range(len(p_values)), key=lambda i: p_values[i])
    result = [1.0] * len(order)
    previous = 0.0
    for rank, index in enumerate(order):
        previous = max(previous, min(1.0, (len(order)-rank)*p_values[index]))
        result[index] = previous
    return result


def _fold_statistics(frame, start, end, horizon, origin):
    train = frame[(frame.as_of < start) & (frame.exit_at < start - DAY)].dropna(subset=["value", "net_return"])
    window = frame[(frame.as_of >= start) & (frame.as_of < end)]
    eligible = window[window.exit_at < end].dropna(subset=["net_return"])
    valid = eligible.dropna(subset=["value"])
    coverage = len(valid)/len(eligible) if len(eligible) else 0.0
    threshold = float(train.value.quantile(PROTOCOL["entry_quantile"])) if len(train) else None
    reason = []
    if len(train) < PROTOCOL["minimum_training_observations"]:
        reason.append("insufficient_training")
    if len(valid) < PROTOCOL["minimum_validation_observations"]:
        reason.append("insufficient_validation")
    if coverage < PROTOCOL["minimum_feature_coverage"]:
        reason.append("missing_features")
    train_ic = rank_ic(train.value, train.gross_return)
    validation_ic = rank_ic(valid.value, valid.gross_return)
    if train_ic is None or validation_ic is None:
        reason.append("undefined_correlation")
    # Fixed global h-day grid: neither the score nor future returns pick event dates.
    grid = valid[((valid.as_of-origin).dt.days % horizon) == 0]
    active = grid.value.to_numpy() >= threshold if threshold is not None else np.zeros(len(grid), dtype=bool)
    if len(train) and train.value.nunique() < 2:
        reason.append("constant_training_factor")
    selected = grid[active]
    groups = []
    if len(train):
        edges = train.value.quantile([0.2, 0.4, 0.6, 0.8]).to_numpy()
        # Tied thresholds are retained, not jittered to manufacture quantiles.
        buckets = np.searchsorted(edges, valid.value, side="right")
        for bucket in range(5):
            part = valid[buckets == bucket]
            groups.append({"quantile": bucket+1, "observations": len(part),
                           "mean_net_event_return": _mean(part.net_return),
                           "overlapping_labels": True})
    result = {
        "start": start.isoformat(), "end_exclusive": end.isoformat(),
        "training_label_exit_before": (start-DAY).isoformat(),
        "training_last_label_exit": train.exit_at.max().isoformat() if len(train) else None,
        "training_observations": len(train), "validation_observations": len(valid),
        "feature_coverage": coverage, "boundary_or_missing_labels": len(window)-len(eligible),
        "train_rank_ic": train_ic, "rank_ic": validation_ic,
        "relative_rank_ic": rank_ic(valid.value, valid.relative_gross_return),
        "entry_threshold": threshold, "grid_events": len(grid), "selected_events": len(selected),
        "mean_net_event_return": _mean(selected.net_return),
        "mean_stress_event_return": _mean(selected.stress_return),
        "unconditional_net_event_return": _mean(grid.net_return),
        "quantile_diagnostics": groups, "reasons": reason,
    }
    return result, grid.net_return.to_numpy(), active, grid.stress_return.to_numpy()


def evaluate_panel(panel, labels, data, *, trial_ids, validation_days=180, prior_p_values=()):
    declared = hypotheses()
    prior_p_values = tuple(prior_p_values)
    protocol = study_protocol(validation_days, len(prior_p_values))
    if any(isinstance(p, bool) or not isinstance(p, (float, int)) or not np.isfinite(p)
           or not 0 <= p <= 1 for p in prior_p_values):
        raise ValueError("prior p values must be finite probabilities")
    if len(trial_ids) != len(declared):
        raise ValueError("trial ledger does not match frozen hypotheses")
    timeline = pd.Index(sorted(data.as_of.unique()))
    span = len(timeline)
    folds = protocol["validation_folds"]
    enough_windows = span >= folds*validation_days + 1
    starts = [timeline[span-folds*validation_days+i*validation_days] for i in range(folds)] if enough_windows else []
    results = []
    for hypothesis, trial_id in zip(declared, trial_ids, strict=True):
        part = panel[(panel.symbol == hypothesis["symbol"]) & (panel.factor == hypothesis["factor"])]
        target = labels[(labels.symbol == hypothesis["symbol"]) & (labels.horizon == hypothesis["horizon"])]
        frame = part.merge(target, on=["as_of", "symbol"], suffixes=("_factor", "_label")).sort_values("as_of")
        result = {**hypothesis, "trial_id": trial_id, "status": "rejected", "reasons": [],
                  "missing_features": dict(Counter(part.missing_reason.dropna())),
                  "coverage_all_history": float(part.value.notna().mean()), "folds": [],
                  "p_value": 1.0, "holm_p_value": None, "selected_events": 0,
                  "score_lag1_rank_correlation": rank_ic(part.value.iloc[1:], part.value.iloc[:-1]),
                  "bootstrap": None}
        if not hypothesis["applicable"]:
            result.update(status="not_applicable", reasons=["factor_symbol_scope"])
        elif not starts:
            result.update(status="insufficient_evidence", reasons=["insufficient_history_for_windows"])
        else:
            returns, active, stress = [], [], []
            for i, start in enumerate(starts):
                end = starts[i+1] if i+1 < len(starts) else timeline[-1]+DAY
                fold, r, a, s = _fold_statistics(frame, start, end, hypothesis["horizon"], starts[0])
                result["folds"].append(fold)
                returns.extend(r)
                active.extend(a)
                stress.extend(s)
            if not hypothesis["inferential"]:
                result.update(status="diagnostic_only", reasons=["state_or_risk_requires_strategy_ablation"])
            else:
                _screen(result, np.array(returns), np.array(active, dtype=bool), np.array(stress), span)
        results.append(result)
    alpha = [row for row in results if row["inferential"]]
    adjusted = holm_adjust([row["p_value"] for row in alpha] + list(prior_p_values))[:len(alpha)]
    for result, adjusted_p in zip(alpha, adjusted, strict=True):
        result["holm_p_value"] = adjusted_p
        if adjusted_p > PROTOCOL["familywise_alpha"]:
            result["reasons"].append("multiple_testing_not_passed")
        if not result["reasons"]:
            result["status"] = "eligible_development_factor"
    pool, correlations = _deduplicate(results, panel, starts[0] if starts else None)
    return {
        "status": "development_candidates" if pool else "empty_pool", "history_days": span,
        "declared_trials": len(results), "inferential_hypotheses": len(alpha),
        "validation_days": validation_days, "validation_folds": folds,
        "prior_inferential_hypotheses": len(prior_p_values),
        "correction_family_size": len(alpha) + len(prior_p_values),
        "multiplicity_scope": "frozen_registered_factor_family; unregistered/manual/strategy searches are not corrected",
        "pool": pool, "results": results, "training_correlations": correlations,
        "model_calls": 0, "strategy_backtests": 0, "trading_admission": False,
        "holdout_evaluated": False,
    }


def _screen(result, returns, active, stress, history_days):
    folds = result["folds"]
    reasons = result["reasons"]
    if history_days < PROTOCOL["minimum_history_days"]:
        reasons.append("less_than_three_years")
    if any(fold["reasons"] for fold in folds):
        reasons.append("insufficient_fold_evidence")
    if any(fold["train_rank_ic"] is None or fold["train_rank_ic"] <= 0 for fold in folds):
        reasons.append("training_direction_not_stable")
    positive = sum(
        fold["rank_ic"] is not None and fold["rank_ic"] > 0
        and fold["mean_net_event_return"] is not None and fold["mean_net_event_return"] > 0
        for fold in folds
    )
    result["positive_folds"] = positive
    if positive < PROTOCOL["minimum_positive_folds"]:
        reasons.append("validation_not_stable")
    result["selected_events"] = int(active.sum())
    if active.sum() < PROTOCOL["minimum_total_events"] or any(
        fold["selected_events"] < PROTOCOL["minimum_events_per_fold"] for fold in folds
    ):
        reasons.append("insufficient_nonoverlapping_events")
    result["mean_net_event_return"] = _mean(returns[active])
    result["mean_stress_event_return"] = _mean(stress[active])
    if not active.any() or stress[active].mean() <= 0:
        reasons.append("stress_cost_not_covered")
    if "insufficient_fold_evidence" in reasons or "insufficient_nonoverlapping_events" in reasons:
        result["status"] = "insufficient_evidence"
        return
    evidence = block_evidence(returns, active, result["horizon"],
                              replicates=PROTOCOL["bootstrap_replicates"], seed=PROTOCOL["bootstrap_seed"])
    result["bootstrap"] = evidence
    result["p_value"] = evidence["p_value"]
    if evidence["net_ci_low"] is None or evidence["net_ci_low"] <= 0:
        reasons.append("net_mean_uncertain")
    if evidence["lift_ci_low"] is None or evidence["lift_ci_low"] <= 0:
        reasons.append("no_supported_increment_over_unconditional_events")


def _deduplicate(results, panel, first_validation):
    if first_validation is None:
        return [], []
    training = panel[panel.as_of < first_validation-DAY]
    wide = training.pivot(index="as_of", columns=["symbol", "factor"], values="value")
    keys = sorted(wide.columns)
    correlations = []
    lookup = {}
    for i, left in enumerate(keys):
        for right in keys[i+1:]:
            correlation = rank_ic(wide[left], wide[right])
            lookup[(left, right)] = correlation
            if correlation is not None and abs(correlation) > PROTOCOL["maximum_abs_training_correlation"]:
                correlations.append({"left": list(left), "right": list(right), "rank_correlation": correlation})
    eligible = sorted((row for row in results if row["status"] == "eligible_development_factor"),
                      key=lambda row: (-row["folds"][0]["train_rank_ic"], row["key"]))
    pool, chosen = [], []
    for row in eligible:
        key = (row["symbol"], row["factor"])
        redundant = False
        for old in chosen:
            correlation = 1.0 if old == key else lookup.get(tuple(sorted((key, old))))
            if correlation is not None and abs(correlation) > PROTOCOL["maximum_abs_training_correlation"]:
                redundant = True
                break
        if redundant or len(pool) >= PROTOCOL["maximum_pool_size"]:
            row["status"] = "redundant_development_factor" if redundant else "pool_limit"
        else:
            pool.append(row["key"])
            chosen.append(key)
    return pool, correlations
