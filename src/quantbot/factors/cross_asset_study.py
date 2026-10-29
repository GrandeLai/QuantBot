"""Frozen cross-asset entry filters on refreshed, independently reconciled baselines."""

import argparse
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
import json
import math
from pathlib import Path

from quantbot.ai.development import run as run_development
from quantbot.ai.development_strategies import development_candidates
from quantbot.ai.market import EASTERN
from quantbot.evidence import source_tree_hash
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from quantbot.factors.paired_study import (
    SCREEN as PAIRED_SCREEN, analyze_market, screen_market, validate as validate_previous,
)
from quantbot.factors.time_series import holm_adjust


HYPOTHESES = [
    {"id": "btc_eth_trend_confirmation", "market": "crypto", "horizon_sessions": 20,
     "confirmation_symbol": "ETH/USDT", "momentum_sessions": 20, "ema_span": 20,
     "momentum_strictly_positive": True, "close_above_or_equal_ema": True},
    {"id": "etf_relative_breadth_5", "market": "stocks", "horizon_sessions": 5,
     "lookback_sessions": 5, "minimum_relative_wins": 3,
     "returns": "close_plus_same_day_ordinary_dividend_over_previous_close_minus_one",
     "ties": "not_a_win"},
]
SCREEN = {**PAIRED_SCREEN, "correction_family_size": 128}
REPAIR_CONTRACT = "cancel_entire_pending_plan_if_any_order_affected_by_corporate_action"


def observe_factor(snapshot, context, event):
    """Use only completed closes and dividends whose ex-date has already occurred."""
    if snapshot.market == "crypto":
        values = {name: context.features["ETH/USDT"].get(name) for name in ("close", "ema_20", "momentum")}
        values = {name: float(value) if value is not None and math.isfinite(value) else None
                  for name, value in values.items()}
        valid = all(value is not None for value in values.values())
        return {"valid": valid, "keep": valid and values["momentum"] > 0 and values["close"] >= values["ema_20"],
                "as_of": context.as_of.isoformat(), "symbol": "ETH/USDT", "values": values}
    if snapshot.market != "stocks":
        raise ValueError("cross-asset research only supports the frozen two markets")
    index = bisect_left(snapshot.times, context.as_of)
    if index >= len(snapshot.times) or snapshot.times[index] != context.as_of:
        raise ValueError("factor origin must be an actual snapshot close")
    if index < 5:
        return {"valid": False, "keep": False, "reason": "insufficient_completed_sessions"}
    dates = [at.astimezone(EASTERN).date().isoformat() for at in snapshot.times[index-5:index+1]]
    symbols = (event["incumbent"], event["target"])
    closes = {(row["date"], row["symbol"]): Decimal(str(row["close"])) for row in snapshot.rows
              if row["date"] in dates and row["symbol"] in symbols}
    if any((day, symbol) not in closes for day in dates for symbol in symbols):
        return {"valid": False, "keep": False, "reason": "missing_lookback_close"}
    dividends = {(a["date"], a["symbol"]): Decimal(a["amount_per_share"]) for a in snapshot.actions
                 if a["type"] == "dividend" and a["date"] in dates[1:] and a["symbol"] in symbols}
    daily = []
    for previous, day in zip(dates, dates[1:]):
        a, b = symbols
        a_numerator = closes[day, a] + dividends.get((day, a), Decimal(0))
        b_numerator = closes[day, b] + dividends.get((day, b), Decimal(0))
        won = b_numerator * closes[previous, a] > a_numerator * closes[previous, b]
        daily.append({"date": day, "relative_win": won,
                      "incumbent_total_return": float(a_numerator / closes[previous, a] - 1),
                      "target_total_return": float(b_numerator / closes[previous, b] - 1)})
    wins = sum(row["relative_win"] for row in daily)
    return {"valid": True, "keep": wins >= 3, "as_of": context.as_of.isoformat(),
            "relative_wins": wins, "lookback_sessions": 5, "daily": daily}


def validate(protocol):
    if (protocol["id"] != "cross_asset_entry_factors_v1" or protocol["stage"] != "development_only"
            or protocol["model_calls_authorized"] != 0 or protocol["trading_enabled"]
            or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("cross-asset study requires offline development-only scope")
    if protocol["hypotheses"] != HYPOTHESES or protocol["screening"] != SCREEN:
        raise ValueError("hypotheses or screening differ from the frozen design")
    if protocol["repair_contract"] != REPAIR_CONTRACT or protocol["baseline_refresh"] != {
            "required": True, "runs_per_market": 8, "total_runs": 16, "same_strategies_and_risk": True}:
        raise ValueError("the registered execution repair and full baseline refresh are required")
    check_ref(protocol["explanation"])
    previous = read(check_ref(protocol["previous_protocol"]))
    validate_previous(previous)
    results = read(check_ref(protocol["previous_results"]))
    expected = previous["prior_tests"] + [{"id": r["hypothesis"], "p_value": r["p_value"],
        "raw_p_value": r["raw_p_value"], "kind": "previous_paired_factor"} for r in results["results"]]
    if len(expected) != 126 or protocol["prior_tests"] != expected:
        raise ValueError("prior exposures were omitted or changed")
    for field in ("markets", "preserved_policies", "market_data_boundaries", "strategy_budget"):
        if protocol[field] != previous[field]:
            raise ValueError("original data, risks, policies or budget changed: " + field)


def baseline_plan(protocol, study, source_hash):
    sections = {}
    for market, section in protocol["markets"].items():
        candidate = next(c for c in development_candidates(market) if c.id == section["baseline_candidate"])
        sections[market] = {**deepcopy(section), "output": str(study / "baselines" / market),
                            "candidates_and_controls": [json.loads(json.dumps(asdict(candidate)))]}
    return {"stage": "development_only", "source_hash": source_hash, "model_calls_authorized": 0,
            "preserved_policies": protocol["preserved_policies"], "markets": sections}


def register(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if file_ref(protocol_path) != read(protocol_path.parent / "protocol-freeze.json")["protocol"]:
        raise ValueError("preregistered protocol changed")
    protocol = read(protocol_path)
    validate(protocol)
    output.mkdir(parents=True, exist_ok=False)
    source = source_tree_hash()
    save(output / "baseline-plan.json", baseline_plan(protocol, output, source))
    freeze = {"frozen_at": datetime.now(UTC).isoformat(), "source_hash": source,
              "protocol": file_ref(protocol_path), "baseline_plan": file_ref(output / "baseline-plan.json"),
              "compiled_hypotheses": HYPOTHESES, "compiled_screen": SCREEN,
              "model_calls": 0, "trading_enabled": False}
    save(output / "execution-freeze.json", freeze)
    return freeze


def refreshed_market(protocol, market, study, plan_path):
    report = run_development(plan_path, market)
    section = deepcopy(protocol["markets"][market])
    directory = study / "baselines" / market
    section["baseline_report"] = file_ref(directory / "report.json")
    old_runs = {(r["window"], r["cost_bps"]): r for r in section["baseline_runs"]}
    section["baseline_runs"] = []
    comparisons = []
    for row in report["runs"]:
        path = directory / f"{row['candidate']}__{row['window']}__cost{row['cost_multiplier']}.json"
        section["baseline_runs"].append({"window": row["window"], "cost_bps": row["cost_bps"],
                                         "run": file_ref(path), "journal": row["journal"]})
        before = read(check_ref(old_runs[row["window"], row["cost_bps"]]["run"]))
        after = read(path)
        account_equal = before["equity_curve"] == after["equity_curve"]
        orders_equal = before["orders"] == after["orders"]
        if market == "crypto" and not (account_equal and orders_equal):
            raise ValueError("the stock corporate-action repair unexpectedly changed the crypto baseline")
        comparisons.append({"window": row["window"], "cost_bps": row["cost_bps"],
            "old_run": old_runs[row["window"], row["cost_bps"]]["run"], "new_run": file_ref(path),
            "account_path_equal": account_equal, "orders_equal": orders_equal,
            "old_net_return": before["summary"]["net_liquidation_return_estimate"],
            "new_net_return": row["net_liquidation_return_estimate"],
            "old_cancellations": before["summary"]["cancellations"], "new_cancellations": row["cancellations"],
            "risk_hold": row["risk_hold"], "reconciliation": row["reconciliation"]["status"]})
    save(study / f"{market}-baseline-refreshed.json", {"source_hash": report["source_hash"],
         "completed_at": datetime.now(UTC).isoformat(), "section": section, "comparisons": comparisons})
    inputs = {"markets": {market: section}}
    hypothesis = next(h for h in HYPOTHESES if h["market"] == market)
    return analyze_market(inputs, market, study, hypothesis=hypothesis, filter_rule=observe_factor)


def finalize(rows, prior):
    if [(r["market"], r["hypothesis"]) for r in rows] != [(h["market"], h["id"]) for h in HYPOTHESES] or len(prior) != 126:
        raise ValueError("both new hypotheses and all prior exposures are required")
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
    return {"results": rows, "pool_by_market": pool, "correction_family_size": 128,
        "formal_admission": False, "trading_enabled": False, "holdout_evaluated": False,
        "model_calls": 0, "baseline_refresh_replays": 16, "new_strategy_replays": 0,
        "stage": "development_only", "next_action": "freeze_qualified_strategies" if any(pool.values()) else "stop_empty_pool"}


def run(study):
    study = Path(study).resolve()
    freeze = read(study / "execution-freeze.json")
    if source_tree_hash() != freeze["source_hash"]:
        raise ValueError("source changed after freeze")
    protocol = read(check_ref(freeze["protocol"]))
    validate(protocol)
    plan_path = check_ref(freeze["baseline_plan"])
    if read(plan_path) != baseline_plan(protocol, study, freeze["source_hash"]):
        raise ValueError("baseline refresh no longer matches the registered plan")
    save(study / "started.json", {"started_at": datetime.now(UTC).isoformat(), "source_hash": source_tree_hash()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(refreshed_market, protocol, h["market"], study, plan_path) for h in HYPOTHESES]
            analyses = [f.result() for f in futures]
        save(study / "events-completed.json", {"completed_at": datetime.now(UTC).isoformat(),
             "markets": [file_ref(study / h["market"] / "events.json") for h in HYPOTHESES]})
        rows = [screen_market(analysis, calendars) for analysis, calendars in analyses]
        result = finalize(rows, protocol["prior_tests"])
        validate(protocol)
        check_ref(freeze["protocol"])
        check_ref(freeze["baseline_plan"])
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
