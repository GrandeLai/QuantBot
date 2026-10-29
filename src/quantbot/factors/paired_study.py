"""Frozen BTC retest / ETF switch-cost paired research, with no trading or model calls."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
import gzip
import json
from pathlib import Path

import numpy as np

from quantbot.ai.development import select_times, validate_reconstructed_dividends
from quantbot.ai.entry_attribution import _load, reconstruct_entries
from quantbot.ai.feeds import fetch_snapshot
from quantbot.evidence import hash_payload, source_tree_hash
from quantbot.factors.conditional_study import (
    check_ref, file_ref, read, save, validate_protocol as validate_previous,
)
from quantbot.factors.entry_screen import _bootstrap
from quantbot.factors.paired_events import HYPOTHESES, audit_branch, opportunity, parent_checkpoints, run_branch
from quantbot.factors.time_series import holm_adjust


SCREEN = {"cost_bps": [15, 30], "minimum_feature_coverage": 0.95,
          "minimum_kept_per_window": 5, "minimum_total_kept": 30,
          "minimum_total_rejected": 10, "minimum_kept_filled": 30,
          "minimum_positive_windows": 2, "bootstrap_replicates": 4999,
          "block_sessions": 20, "seed": 1729, "familywise_alpha": 0.05,
          "correction_family_size": 126}


def validate(protocol):
    if (protocol["id"] != "paired_retest_switch_v1" or protocol["stage"] != "development_only"
            or protocol["model_calls_authorized"] != 0 or protocol["trading_enabled"]
            or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("paired study requires offline development-only scope")
    if protocol["hypotheses"] != HYPOTHESES or protocol["screening"] != SCREEN:
        raise ValueError("paired hypotheses or screening differ from the frozen design")
    if protocol["strategy_budget"] != {"maximum_candidates": 2, "runs_per_candidate": 8,
            "maximum_replays": 16, "require_screen_pass": True}:
        raise ValueError("strategy budget changed")
    check_ref(protocol["explanation"])
    previous = read(check_ref(protocol["previous_protocol"]))
    validate_previous(previous)
    results = read(check_ref(protocol["previous_results"]))
    expected_prior = previous["prior_tests"] + [{"id": row["key"], "p_value": row["p_value"],
        "raw_p_value": row["raw_p_value"], "kind": "previous_conditional_factor"} for row in results["results"]]
    if len(expected_prior) != 124 or protocol["prior_tests"] != expected_prior:
        raise ValueError("previous tests missing or altered")
    for field in ("markets", "preserved_policies", "market_data_boundaries"):
        if protocol[field] != previous[field]:
            raise ValueError("baseline inputs, risk or sealed boundaries changed: " + field)


def register(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if file_ref(protocol_path) != read(protocol_path.parent / "protocol-freeze.json")["protocol"]:
        raise ValueError("preregistered protocol changed")
    protocol = read(protocol_path)
    validate(protocol)
    output.mkdir(parents=True, exist_ok=False)
    freeze = {"frozen_at": datetime.now(UTC).isoformat(), "source_hash": source_tree_hash(),
              "protocol": file_ref(protocol_path), "model_calls": 0, "trading_enabled": False,
              "compiled_hypotheses": HYPOTHESES, "compiled_screen": SCREEN}
    save(output / "execution-freeze.json", freeze)
    return freeze


def analyze_market(protocol, market, output, *, hypothesis=None, filter_rule=None):
    if (hypothesis is None) != (filter_rule is None):
        raise ValueError("a new event filter requires its frozen hypothesis")
    section = protocol["markets"][market]
    directory = output / market
    directory.mkdir(exist_ok=False)
    snapshot = fetch_snapshot(section["spec"], section["config"], datetime.now(UTC), directory, fresh=False)
    if snapshot.times[-1].date().isoformat() > section["latest_allowed_close_date"]:
        raise ValueError("snapshot exceeds frozen development boundary")
    if section.get("actions"):
        snapshot.actions = read(check_ref(section["actions"]))
        validate_reconstructed_dividends(snapshot.actions, set(section["spec"]["symbols"]))
    snapshot.provenance = section["provenance"]
    calendars = {w["id"]: select_times(snapshot, w) for w in section["windows"]}
    if [at for grid in calendars.values() for at in grid] != select_times(snapshot, section["continuous_window"]):
        raise ValueError("development windows no longer cover the frozen interval")
    hypothesis = hypothesis or next(h for h in HYPOTHESES if h["market"] == market)
    horizon = hypothesis["horizon_sessions"]
    summaries, parents = [], []
    baseline_report = read(check_ref(section["baseline_report"]))
    for descriptor in section["baseline_runs"]:
        if descriptor["window"] == "continuous":
            continue
        check_ref(descriptor["run"])
        rebuilt = reconstruct_entries(Path(descriptor["run"]["path"]), descriptor["journal"], market)
        document, records, source = _load(descriptor["run"]["path"], descriptor["journal"])
        summary = document["summary"]
        matches = [r for r in baseline_report["runs"] if r["candidate"] == section["baseline_candidate"]
                   and r["window"] == descriptor["window"] and r["cost_bps"] == descriptor["cost_bps"]]
        if matches != [{**summary, "journal": descriptor["journal"]}] \
                or rebuilt["source"]["code_hash"] != baseline_report["source_hash"]:
            raise ValueError("baseline identity/source evidence mismatch")
        config, spec = records[0]["payload"]["config"], records[0]["payload"]["spec"]
        grid = calendars[descriptor["window"]]
        indexes = {at: i for i, at in enumerate(grid)}
        if records[0]["payload"]["window"] != [at.isoformat() for at in grid]:
            raise ValueError("parent journal window mismatch")
        counts, exclusions, event_count = Counter(), [], 0
        for state, context, plan, prefix_end in parent_checkpoints(snapshot, records, config, spec):
            event, reason = opportunity(market, state, context, plan, descriptor["cost_bps"])
            if event is None:
                counts[reason] += 1
                continue
            counts["opportunities_before_boundary"] += 1
            origin = indexes[context.as_of]
            if origin + horizon >= len(grid):
                counts["insufficient_forward_boundary"] += 1
                exclusions.append({"as_of": context.as_of.isoformat(), "event": event,
                                   "reason": "insufficient_forward_boundary"})
                continue
            selected = [snapshot.context(at) for at in grid[origin:origin+horizon+1]]
            observation = filter_rule(snapshot, context, event) if filter_rule else None
            control = run_branch(snapshot, state, selected, config, spec, plan, event, candidate=False)
            candidate = run_branch(snapshot, state, selected, config, spec, plan, event, candidate=True,
                                   retain_original=observation["keep"] if observation else None)
            for branch in (control, candidate):
                branch["reconciliation"] = audit_branch(records, prefix_end, branch)
            event_count += 1
            identity = f"{descriptor['window']}__cost{descriptor['cost_bps']}__event{event_count:04d}"
            contribution = candidate["account_contribution"]
            row = {"id": identity, "window": descriptor["window"], "market": market,
                "hypothesis": hypothesis["id"], "cost_bps": descriptor["cost_bps"],
                "signal_at": context.as_of.isoformat(), "end_at": selected[-1].as_of.isoformat(),
                "session_index": origin, "event": event,
                "factor_valid": observation["valid"] if observation else True,
                "kept": candidate["factor_kept"], "kept_filled": candidate["factor_kept"] and candidate["target_buy_filled"],
                "candidate_contribution": contribution, "baseline_contribution": control["account_contribution"],
                "incremental_contribution": contribution-control["account_contribution"],
                "candidate_net_pnl": candidate["net_pnl"], "baseline_net_pnl": control["net_pnl"],
                "candidate_confirmed_at": candidate["confirmed_at"],
                "candidate_event_phase": candidate["event_phase"],
                "risk_hold": candidate["risk_hold"] or control["risk_hold"],
                "candidate_open": bool(candidate["remaining_positions"]),
                "baseline_open": bool(control["remaining_positions"]),
                "parent_prefix_end": prefix_end, "parent": descriptor,
                "checkpoint_hash": hash_payload(state)}
            if observation is not None:
                row["factor_observation"] = observation
            payload = {"summary": row, "checkpoint": deepcopy(state), "control": control, "candidate": candidate,
                       "source": source, "parent_prefix_end": prefix_end}
            path = directory / (identity + ".json.gz")
            with gzip.open(path, "xt", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, allow_nan=False)
            summaries.append({**row, "artifact": file_ref(path)})
        parents.append({"window": descriptor["window"], "cost_bps": descriptor["cost_bps"],
                        "reconstructed_accounts": rebuilt["accounting"]["account_snapshots"],
                        "opportunity_counts": dict(counts), "paired_events": event_count,
                        "boundary_exclusions": exclusions, "parent": descriptor,
                        "accounting": rebuilt["accounting"]})
        print(json.dumps({"market": market, "window": descriptor["window"], "cost_bps": descriptor["cost_bps"],
                          "paired_events": event_count, "boundary_excluded": len(exclusions)}), flush=True)
    result = {"market": market, "hypothesis": hypothesis["id"], "events": summaries,
              "parents": parents, "formal_admission": False, "model_calls": 0,
              "scope": "paired_local_interventions_not_portfolio_returns"}
    save(directory / "events.json", result)
    return result, calendars


def screen_market(analysis, calendars):
    costs = []
    for cost in SCREEN["cost_bps"]:
        folds, grids = [], []
        for window, times in calendars.items():
            events = [e for e in analysis["events"] if e["cost_bps"] == cost and e["window"] == window]
            reasons = []
            valid = sum(e["factor_valid"] for e in events)
            kept = sum(e["kept"] for e in events)
            coverage = valid/len(events) if events else 0
            if coverage < SCREEN["minimum_feature_coverage"]:
                reasons.append("insufficient_feature_coverage")
            if kept < SCREEN["minimum_kept_per_window"]:
                reasons.append("insufficient_kept_events")
            net = sum(e["candidate_contribution"] for e in events)
            delta = sum(e["incremental_contribution"] for e in events)
            fold = {"window": window, "opportunities": len(events), "kept": kept,
                "rejected": len(events)-kept, "kept_filled": sum(e["kept_filled"] for e in events),
                "feature_coverage": coverage, "candidate_contribution": net,
                "baseline_contribution": sum(e["baseline_contribution"] for e in events),
                "incremental_contribution": delta, "jointly_positive": net > 0 and delta > 0,
                "risk_holds": sum(bool(e["risk_hold"]) for e in events),
                "candidate_open": sum(e["candidate_open"] for e in events),
                "baseline_open": sum(e["baseline_open"] for e in events), "reasons": reasons}
            folds.append(fold)
            grid = np.zeros((len(times), 4))
            for e in events:
                if times[e["session_index"]].isoformat() != e["signal_at"]:
                    raise ValueError("event timestamp and calendar index differ")
                # Both means use all original opportunities, including skipped entries.
                grid[e["session_index"]] += (1, 1, e["candidate_contribution"], e["incremental_contribution"])
            grids.append(grid)
        total_fields = ("opportunities", "kept", "rejected", "kept_filled", "candidate_contribution",
                        "baseline_contribution", "incremental_contribution", "risk_holds", "candidate_open", "baseline_open")
        totals = {key: sum(f[key] for f in folds) for key in total_fields}
        reasons = [f"{f['window']}:{reason}" for f in folds for reason in f["reasons"]]
        for key, threshold in (("kept", 30), ("rejected", 10), ("kept_filled", 30)):
            if totals[key] < threshold:
                reasons.append("insufficient_total_" + key)
        insufficient = bool(reasons)
        positive_windows = sum(f["jointly_positive"] for f in folds)
        if positive_windows < 2:
            reasons.append("fewer_than_two_jointly_positive_windows")
        for field in ("candidate_contribution", "incremental_contribution"):
            if totals[field] <= 0:
                reasons.append("nonpositive_mean_window_" + field)
        if totals["risk_holds"]:
            reasons.append("branch_risk_hold")
        bootstrap = {"status": "insufficient_evidence", "p_value": 1.0}
        if not insufficient:
            bootstrap = _bootstrap(grids)
            bootstrap["net_contribution_per_opportunity"] = bootstrap.pop("net_contribution_per_kept_event")
            bootstrap["increment_per_opportunity"] = bootstrap.pop("increment_per_baseline_event")
            for metric in ("net_contribution_per_opportunity", "increment_per_opportunity"):
                low = bootstrap[metric]["ci_low"]
                if low is None or low <= 0:
                    reasons.append(metric + "_uncertain")
        costs.append({"cost_bps": cost, "folds": folds, "totals": totals,
            "jointly_positive_windows": positive_windows, "bootstrap": bootstrap,
            "raw_p_value": bootstrap["p_value"], "p_value": 1.0 if reasons else bootstrap["p_value"],
            "reasons": reasons, "status": "insufficient_evidence" if insufficient else
                ("rejected" if reasons else "pending_multiplicity")})
    reasons = [f"cost_{c['cost_bps']}:{r}" for c in costs for r in c["reasons"]]
    return {"market": analysis["market"], "hypothesis": analysis["hypothesis"], "costs": costs,
        "raw_p_value": max(c["raw_p_value"] for c in costs), "p_value": max(c["p_value"] for c in costs),
        "reasons": reasons, "status": "insufficient_evidence" if any(c["status"] == "insufficient_evidence" for c in costs)
            else ("rejected" if reasons else "pending_multiplicity")}


def finalize(rows, prior):
    if [(r["market"], r["hypothesis"]) for r in rows] != [(h["market"], h["id"]) for h in HYPOTHESES] or len(prior) != 124:
        raise ValueError("both hypotheses and all prior exposures are required")
    adjusted = holm_adjust([r["p_value"] for r in rows] + [r["p_value"] for r in prior])[:2]
    pool = {"crypto": [], "stocks": []}
    for row, p in zip(rows, adjusted, strict=True):
        row["holm_p_value"] = p
        if p > .05:
            row["reasons"].append("multiple_testing_not_passed")
        if not row["reasons"]:
            row["status"] = "eligible_development_factor"
            pool[row["market"]].append(row["hypothesis"])
        elif row["status"] != "insufficient_evidence":
            row["status"] = "rejected"
    return {"results": rows, "pool_by_market": pool, "correction_family_size": 126,
        "formal_admission": False, "trading_enabled": False, "holdout_evaluated": False,
        "model_calls": 0, "new_strategy_replays": 0, "stage": "development_only",
        "next_action": "freeze_qualified_strategies" if any(pool.values()) else "stop_empty_pool"}


def run(study):
    study = Path(study).resolve()
    freeze = read(study / "execution-freeze.json")
    if source_tree_hash() != freeze["source_hash"]:
        raise ValueError("source changed after freeze")
    protocol = read(check_ref(freeze["protocol"]))
    validate(protocol)
    save(study / "started.json", {"started_at": datetime.now(UTC).isoformat(), "source_hash": source_tree_hash()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(analyze_market, protocol, h["market"], study) for h in HYPOTHESES]
            analyses = [f.result() for f in futures]
        save(study / "events-completed.json", {"completed_at": datetime.now(UTC).isoformat(),
             "markets": [file_ref(study / h["market"] / "events.json") for h in HYPOTHESES]})
        rows = [screen_market(analysis, calendars) for analysis, calendars in analyses]
        result = finalize(rows, protocol["prior_tests"])
        validate(protocol)
        check_ref(freeze["protocol"])
        if source_tree_hash() != freeze["source_hash"]:
            raise ValueError("source changed during execution")
        result.update(source_hash=freeze["source_hash"], protocol_reference=freeze["protocol"])
        save(study / "result.json", result)
        save(study / "completed.json", {"completed_at": datetime.now(UTC).isoformat(),
                                       "result": file_ref(study / "result.json")})
        return result
    except BaseException as exc:
        save(study / "failed.json", {"type": type(exc).__name__, "message": str(exc),
                                   "formal_admission": False, "trading_enabled": False})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--protocol", required=True)
    freeze.add_argument("--output", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--study", required=True)
    args = parser.parse_args()
    if args.command == "freeze":
        result = register(args.protocol, args.output)
    else:
        result = run(args.study)
        result = {k: result[k] for k in ("pool_by_market", "next_action", "formal_admission")}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
