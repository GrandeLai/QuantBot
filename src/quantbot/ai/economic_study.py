"""Freeze and run descriptive dual-market attribution on existing reconciled journals."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
from pathlib import Path

from quantbot.evidence import source_tree_hash
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from quantbot.factors.leadership_study import validate as validate_prior

from .economic_attribution import HORIZONS, analyze_run
from .research_catalog import check_window, load as load_catalog


METHOD = {"version": 1, "post_exit_sessions": list(HORIZONS), "cost_bps": [15, 30],
          "basis": "existing_reconciled_runs_only", "new_hypothesis_tests": 0,
          "intraday_extrema": "not_identifiable_from_close_and_fill_observations"}


def validate(protocol):
    if (protocol["id"] != "dual_market_economic_attribution_v1" or protocol["method"] != METHOD
            or protocol["model_calls_authorized"] != 0 or protocol["trading_enabled"]
            or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("economic study requires the frozen offline descriptive method")
    prior = read(check_ref(protocol["prior_protocol"]))
    validate_prior(prior)
    catalog = load_catalog(check_ref(protocol["catalog"]))
    if catalog["policies"] != prior["preserved_policies"]:
        raise ValueError("research policy references changed")
    for market, section in prior["markets"].items():
        check = check_window(catalog, market, section["spec"]["symbols"],
            section["continuous_window"]["start"], section["continuous_window"]["end"], role="development")
        if not check["allowed_by_index"]:
            raise ValueError("catalog denied the requested baseline interval")
    return prior


def register(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    protocol = read(protocol_path)
    validate(protocol)
    output.mkdir(parents=True, exist_ok=False)
    freeze = {"protocol": file_ref(protocol_path), "source_hash": source_tree_hash(),
              "frozen_at": datetime.now(UTC).isoformat(), "method": METHOD}
    save(output / "execution-freeze.json", freeze)
    return freeze


def analyze_market(prior, market, output):
    section = prior["markets"][market]
    directory = output / market
    directory.mkdir(exist_ok=False)
    rows = []
    for descriptor in section["baseline_runs"]:
        path = check_ref(descriptor["run"])
        result = analyze_run(path, descriptor["journal"], market)
        if (result["end"][:10] > section["latest_allowed_close_date"]
                or result["cost_bps"] != descriptor["cost_bps"]):
            raise ValueError("baseline data boundary or cost differs from the protocol")
        result.update(window=descriptor["window"], baseline_candidate=section["baseline_candidate"])
        result_path = directory / f"{descriptor['window']}__{descriptor['cost_bps']}bps.json"
        save(result_path, result)
        rows.append({k: result[k] for k in ("window", "cost_bps", "bars", "economics", "capital", "exit_summary")})
        rows[-1].update(report=file_ref(result_path), closed_episodes=sum(e["exit_at"] is not None for e in result["episodes"]),
                       reconciled=result["accounting"]["status"] == "reconciled")
    return {"market": market, "runs": rows}


def run(study):
    study = Path(study).resolve()
    freeze = read(study / "execution-freeze.json")
    if freeze["source_hash"] != source_tree_hash():
        raise ValueError("source changed since attribution freeze")
    protocol = read(check_ref(freeze["protocol"]))
    prior = validate(protocol)
    save(study / "started.json", {"started_at": datetime.now(UTC).isoformat()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(analyze_market, prior, market, study) for market in ("crypto", "stocks")]
            markets = [f.result() for f in futures]
        validate(protocol)
        check_ref(freeze["protocol"])
        if freeze["source_hash"] != source_tree_hash():
            raise ValueError("source changed during attribution")
        result = {"markets": markets, "method": METHOD, "source_hash": freeze["source_hash"],
                  "baseline_reports_reused": 16, "account_snapshots_reconciled":
                  sum(row["bars"] for market in markets for row in market["runs"]),
                  "new_strategy_replays": 0, "model_calls": 0, "formal_admission": False,
                  "trading_enabled": False, "sealed_holdout_evaluated": False,
                  "status": "descriptive_attribution_complete"}
        save(study / "result.json", result)
        save(study / "completed.json", {"completed_at": datetime.now(UTC).isoformat(),
                                       "result": file_ref(study / "result.json")})
        return result
    except BaseException as exc:
        save(study / "failed.json", {"error": type(exc).__name__, "message": str(exc),
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
    result = register(args.protocol, args.output) if args.command == "freeze" else run(args.study)
    if args.command == "run":
        result = {k: result[k] for k in ("status", "baseline_reports_reused", "account_snapshots_reconciled")}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
