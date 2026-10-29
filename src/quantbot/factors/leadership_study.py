"""Frozen relative-leadership filters using the reconciled post-repair baselines."""

import argparse
from bisect import bisect_left
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path

from quantbot.ai.market import EASTERN
from quantbot.evidence import source_tree_hash
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from quantbot.factors.cross_asset_study import SCREEN as PREVIOUS_SCREEN, validate as validate_previous
from quantbot.factors.paired_study import analyze_market, screen_market
from quantbot.factors.time_series import holm_adjust


HYPOTHESES = [
    {"id": "btc_relative_momentum_leader", "market": "crypto", "horizon_sessions": 20,
     "momentum_sessions": 20, "leader": "BTC/USDT", "comparison": "ETH/USDT", "operator": "strictly_greater"},
    {"id": "etf_atr_normalized_momentum", "market": "stocks", "horizon_sessions": 5,
     "lookback_sessions": 20, "atr_sessions": 14,
     "returns": "compounded_close_plus_ex_date_ordinary_dividend",
     "score": "total_return_divided_by_atr_over_close", "operator": "challenger_strictly_greater_than_incumbent"},
]
SCREEN = {**PREVIOUS_SCREEN, "correction_family_size": 130}


def observe_factor(snapshot, context, event):
    """The filter only sees completed closes, current ATR and already occurred ex-dates."""
    if snapshot.market == "crypto":
        values = {s: context.features[s].get("momentum") for s in ("BTC/USDT", "ETH/USDT")}
        values = {s: float(v) if v is not None and math.isfinite(v) else None for s, v in values.items()}
        valid = all(v is not None for v in values.values())
        return {"valid": valid, "keep": valid and values["BTC/USDT"] > values["ETH/USDT"],
                "as_of": context.as_of.isoformat(), "momentum": values}
    if snapshot.market != "stocks":
        raise ValueError("leadership research only supports the frozen two markets")
    index = bisect_left(snapshot.times, context.as_of)
    if index >= len(snapshot.times) or snapshot.times[index] != context.as_of:
        raise ValueError("factor origin must be an actual snapshot close")
    if index < 20:
        return {"valid": False, "keep": False, "reason": "insufficient_completed_sessions"}
    dates = [t.astimezone(EASTERN).date().isoformat() for t in snapshot.times[index-20:index+1]]
    symbols = (event["incumbent"], event["target"])
    closes = {(r["date"], r["symbol"]): Decimal(str(r["close"])) for r in snapshot.rows
              if r["date"] in dates and r["symbol"] in symbols}
    atrs = {s: context.features[s].get("atr") for s in symbols}
    if (any((d, s) not in closes or not closes[d, s].is_finite() or closes[d, s] <= 0
            for d in dates for s in symbols)
            or any(v is None or not math.isfinite(v) or v <= 0 for v in atrs.values())):
        return {"valid": False, "keep": False, "reason": "missing_or_invalid_lookback_or_atr"}
    dividends = {}
    for action in snapshot.actions:
        key = (action["date"], action["symbol"])
        if action["type"] == "dividend" and key[0] in dates[1:] and key[1] in symbols:
            dividends[key] = dividends.get(key, Decimal(0)) + Decimal(action["amount_per_share"])
    scores, values = {}, {}
    for symbol in symbols:
        growth = Decimal(1)
        for previous, day in zip(dates, dates[1:]):
            growth *= (closes[day, symbol] + dividends.get((day, symbol), Decimal(0))) / closes[previous, symbol]
        relative_atr = Decimal(str(atrs[symbol])) / closes[dates[-1], symbol]
        scores[symbol] = (growth - 1) / relative_atr
        values[symbol] = {"total_return_20": float(growth - 1), "atr_fraction": float(relative_atr),
                          "score": float(scores[symbol])}
    return {"valid": True, "keep": scores[event["target"]] > scores[event["incumbent"]],
            "as_of": context.as_of.isoformat(), "lookback_start": dates[0], "sessions": 20, "values": values}


def validate_baseline_code(manifest, source_root, expected_hash):
    """Cached baseline replay is allowed only with byte-identical pre-existing source."""
    digest = hashlib.sha256()
    for relative, expected in sorted(manifest["files"].items()):
        data = (source_root / relative).read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("baseline execution source changed: " + relative)
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    if digest.hexdigest() != manifest["source_hash"] or manifest["source_hash"] != expected_hash:
        raise ValueError("baseline source manifest does not reproduce its source hash")


def validate(protocol):
    if (protocol["id"] != "relative_leadership_factors_v1" or protocol["stage"] != "development_only"
            or protocol["model_calls_authorized"] != 0 or protocol["trading_enabled"]
            or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("leadership study requires offline development-only scope")
    if protocol["hypotheses"] != HYPOTHESES or protocol["screening"] != SCREEN:
        raise ValueError("hypotheses or screening differ from the frozen design")
    check_ref(protocol["explanation"])
    check_ref(protocol["attribution"])
    previous = read(check_ref(protocol["previous_protocol"]))
    validate_previous(previous)
    results = read(check_ref(protocol["previous_results"]))
    if read(check_ref(protocol["previous_completed"]))["result"] != protocol["previous_results"]:
        raise ValueError("previous research has no matching terminal result")
    expected = previous["prior_tests"] + [{"id": r["hypothesis"], "p_value": r["p_value"],
        "raw_p_value": r["raw_p_value"], "kind": "previous_cross_asset_factor"} for r in results["results"]]
    if len(expected) != 128 or protocol["prior_tests"] != expected:
        raise ValueError("prior exposures were omitted or changed")
    for field in ("preserved_policies", "market_data_boundaries", "strategy_budget"):
        if protocol[field] != previous[field]:
            raise ValueError("original risks, policies or budget changed: " + field)
    if set(protocol["markets"]) != {"crypto", "stocks"}:
        raise ValueError("both markets are required")
    for market, section in protocol["markets"].items():
        refreshed = read(check_ref(protocol["baseline_refreshes"][market]))
        if section != refreshed["section"] or refreshed["source_hash"] != results["source_hash"]:
            raise ValueError("post-repair baseline identity changed")
        check_ref(section["baseline_report"])
        for run in section["baseline_runs"]:
            check_ref(run["run"])
            check_ref(run["journal"])
    validate_baseline_code(read(check_ref(protocol["baseline_source_manifest"])),
                           Path(__file__).resolve().parents[1], results["source_hash"])


def register(protocol_path, output):
    protocol_path, output = Path(protocol_path).resolve(), Path(output).resolve()
    if file_ref(protocol_path) != read(protocol_path.parent / "protocol-freeze.json")["protocol"]:
        raise ValueError("preregistered protocol changed")
    protocol = read(protocol_path)
    validate(protocol)
    output.mkdir(parents=True, exist_ok=False)
    freeze = {"frozen_at": datetime.now(UTC).isoformat(), "source_hash": source_tree_hash(),
              "protocol": file_ref(protocol_path), "compiled_hypotheses": HYPOTHESES,
              "compiled_screen": SCREEN, "baseline_replays_reused": 16,
              "model_calls": 0, "trading_enabled": False}
    save(output / "execution-freeze.json", freeze)
    return freeze


def finalize(rows, prior):
    if [(r["market"], r["hypothesis"]) for r in rows] != [(h["market"], h["id"]) for h in HYPOTHESES] or len(prior) != 128:
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
    return {"results": rows, "pool_by_market": pool, "correction_family_size": 130,
            "formal_admission": False, "trading_enabled": False, "holdout_evaluated": False,
            "model_calls": 0, "baseline_replays_reused": 16, "new_baseline_replays": 0,
            "new_strategy_replays": 0, "stage": "development_only",
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
            futures = [executor.submit(analyze_market, protocol, h["market"], study,
                        hypothesis=h, filter_rule=observe_factor) for h in HYPOTHESES]
            analyses = [f.result() for f in futures]
        save(study / "events-completed.json", {"completed_at": datetime.now(UTC).isoformat(),
             "markets": [file_ref(study / h["market"] / "events.json") for h in HYPOTHESES]})
        result = finalize([screen_market(a, grid) for a, grid in analyses], protocol["prior_tests"])
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
