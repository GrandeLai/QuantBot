"""Immutable cross-directory research index, separate from authenticated trading admission."""

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path

from quantbot.evidence import hash_payload
from quantbot.experiment_registry import ExperimentRecord, ExperimentRegistry
from quantbot.factors.conditional_study import check_ref, file_ref, read, save


def _interval(start, end):
    if date.fromisoformat(start) > date.fromisoformat(end):
        raise ValueError("invalid research interval")


def access_interval(access):
    """Read legacy minute-access records without rewriting immutable catalog history."""
    canonical = "start" in access or "end" in access
    legacy = "read_start" in access or "read_end" in access
    try:
        start, end = (access["start"], access["end"]) if canonical else (access["read_start"], access["read_end"])
        if canonical and legacy and (start, end) != (access["read_start"], access["read_end"]):
            raise ValueError("conflicting access interval fields")
    except KeyError as exc:
        raise ValueError("incomplete research access interval") from exc
    _interval(start, end)
    if access["market"] not in {"crypto", "stocks"} or not access["symbols"]:
        raise ValueError("invalid research access scope")
    return access.get("id") or "legacy:" + hash_payload(access), start, end


def trial_registry(trials):
    """Keep existing trial accounting, including failed and exposure-only placeholders."""
    registry = ExperimentRegistry()
    records = [ExperimentRecord(experiment_id=t["id"], strategy_family="ai_" + t["market"],
        kind="candidate_comparison", baseline_components=("imported_or_frozen",),
        candidate_components=(hash_payload(t["definition"]),), added_component=None, parameter_grid={},
        declared_trial_count=1, trial_ids=(t["id"],),
        preregistered_at=datetime.fromisoformat(t["registered_at"])) for t in trials]
    registry.import_known_experiments(records)
    if registry.global_declared_trial_count != len(trials):
        raise ValueError("duplicate trial identity")
    return registry


def validate(catalog):
    if catalog["version"] != 1 or catalog["scope"] != "offline_development_index":
        raise ValueError("not a supported research catalog")
    if hash_payload({k: v for k, v in catalog.items() if k != "digest"}) != catalog["digest"]:
        raise ValueError("catalog digest mismatch")
    if catalog["formal_admission"] or catalog["trading_enabled"]:
        raise ValueError("research catalog cannot authorize trading")
    trial_registry(catalog["trials"])
    if len({d["id"] for d in catalog["datasets"]}) != len(catalog["datasets"]):
        raise ValueError("duplicate dataset identity")
    for data in catalog["datasets"]:
        _interval(data["read_start"], data["read_end"])
        if data["role"] != "exposed_development" or not data["symbols"]:
            raise ValueError("imported research data must remain exposed development")
    for protected in catalog["protected_intervals"]:
        _interval(protected["start"], protected["end"])
    for access in catalog["accesses"]:
        access_interval(access)
    known = {t["id"] for t in catalog["trials"]}
    if set(catalog["outcomes"]) - known:
        raise ValueError("outcome without registered trial")


def load(path):
    """Verify every revision, without rereading protected price files or resolving credentials."""
    catalog = read(path)
    validate(catalog)
    if catalog["parent"] is not None:
        parent = load(check_ref(catalog["parent"]))
        for field in ("trials", "datasets", "protected_intervals", "accesses"):
            if catalog[field][:len(parent[field])] != parent[field]:
                raise ValueError("catalog history was removed or rewritten: " + field)
        if any(catalog["outcomes"].get(k) != v for k, v in parent["outcomes"].items()):
            raise ValueError("historical outcomes were removed or rewritten")
        if catalog["policies"] != parent["policies"]:
            raise ValueError("policy references changed")
    return catalog


def write_revision(catalog, output, *, parent=None):
    updated = deepcopy(catalog)
    updated["parent"] = file_ref(parent) if parent else None
    updated["updated_at"] = datetime.now(UTC).isoformat()
    updated.pop("digest", None)
    updated["digest"] = hash_payload(updated)
    validate(updated)
    if parent:
        old = load(parent)
        for field in ("trials", "datasets", "protected_intervals", "accesses"):
            if updated[field][:len(old[field])] != old[field]:
                raise ValueError("cannot discard historical " + field)
        if any(updated["outcomes"].get(k) != v for k, v in old["outcomes"].items()):
            raise ValueError("cannot change historical outcomes")
        if old["policies"] != updated["policies"]:
            raise ValueError("cannot change policy references")
    save(Path(output), updated)
    return updated


def dataset_record(market, section):
    """Record the complete READ interval (including warmup), not just evaluated dates."""
    path = check_ref(section["data"])
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or section["spec"]["bar"] != "1d":
        raise ValueError("catalog importer expects nonempty daily data")
    dates = []
    for row in rows:
        if market == "crypto":
            opening = datetime.fromisoformat(row["timestamp"])
            if opening.tzinfo is None or row["bar"] != "1d":
                raise ValueError("crypto data requires aware daily opening times")
            dates.append((opening.astimezone(UTC) + timedelta(days=1)).date().isoformat())
        else:
            dates.append(date.fromisoformat(row["date"]).isoformat())
    symbols = sorted({r["symbol"] for r in rows})
    if symbols != sorted(section["spec"]["symbols"]) or max(dates) > section["latest_allowed_close_date"]:
        raise ValueError("dataset exceeds the registered symbols or boundary")
    return {"id": market + ":" + section["data"]["sha256"], "market": market,
            "symbols": symbols, "data": section["data"], "read_start": min(dates), "read_end": max(dates),
            "date_basis": "completed_UTC_bar" if market == "crypto" else "exchange_session_date",
            "rows": len(rows), "role": "exposed_development", "provenance": section["provenance"],
            "actions": section.get("actions"), "independent_evidence": False}


def import_leadership(protocol_path, completed_path, output):
    """Import the cumulative 130-trial record; unknown older research is not invented."""
    protocol, completed = read(protocol_path), read(completed_path)
    if (protocol["id"] != "relative_leadership_factors_v1"
            or file_ref(protocol_path) != read(Path(protocol_path).parent / "protocol-freeze.json")["protocol"]):
        raise ValueError("expected the frozen leadership protocol")
    result = read(check_ref(completed["result"]))
    if result["protocol_reference"] != file_ref(protocol_path):
        raise ValueError("completed results do not match the protocol")
    current = [{"id": r["hypothesis"], "p_value": r["p_value"], "raw_p_value": r["raw_p_value"],
                "kind": "leadership_factor", "status": r["status"]} for r in result["results"]]
    historical = protocol["prior_tests"] + current
    if len(historical) != 130 or result["correction_family_size"] != 130:
        raise ValueError("incomplete historical search family")
    frozen = read(Path(protocol_path).parent / "protocol-freeze.json")
    registered_at = frozen["frozen_at"]
    trials = []
    for row in historical:
        identity = row["id"]
        market = "stocks" if identity.startswith(("stocks:", "prior-strategy:stocks:", "etf_")) else "crypto"
        trials.append({"id": identity, "market": market, "definition": row,
            "registered_at": registered_at, "timestamp_basis": "imported_cumulative_protocol_not_original_trial_time",
            "origin": file_ref(protocol_path)})
    for policy in protocol["preserved_policies"]:
        check_ref(policy)
    datasets = [dataset_record(m, s) for m, s in protocol["markets"].items()]
    catalog = {"version": 1, "scope": "offline_development_index", "parent": None,
        "trials": trials, "datasets": datasets, "outcomes": {r["id"]: r for r in historical}, "accesses": [],
        "policies": protocol["preserved_policies"], "evidence": [file_ref(protocol_path), completed["result"]],
        "protected_intervals": [
            {"market": "crypto", "symbols": ["BTC/USDT", "ETH/USDT"], "start": "2026-07-03",
             "end": "2026-09-01", "reason": "original_sealed_holdout"},
            {"market": "stocks", "symbols": ["*"], "start": "2026-07-01", "end": "9999-12-31",
             "reason": "post_development_prices_unreleased"}],
        "formal_admission": False, "trading_enabled": False,
        "coverage": "Known 130-trial cumulative family only; not a census of all legacy projects or runtime databases."}
    return write_revision(catalog, output)


def check_window(catalog, market, symbols, start, end, *, role):
    _interval(start, end)
    if market not in {"crypto", "stocks"} or not symbols or role not in {"development", "independent"}:
        raise ValueError("invalid market, symbols or requested data role")
    reasons = []
    for interval in catalog["protected_intervals"]:
        if (market == interval["market"] and ("*" in interval["symbols"] or set(symbols) & set(interval["symbols"]))
                and start <= interval["end"] and end >= interval["start"]):
            reasons.append("protected:" + interval["reason"])
    overlaps = [d["id"] for d in catalog["datasets"] if d["market"] == market
                and set(symbols) & set(d["symbols"]) and start <= d["read_end"] and end >= d["read_start"]]
    for access in catalog["accesses"]:
        identity, read_start, read_end = access_interval(access)
        if (access["market"] == market and ("*" in access["symbols"] or set(symbols) & set(access["symbols"]))
                and start <= read_end and end >= read_start):
            overlaps.append(identity)
    if role == "independent" and overlaps:
        reasons.append("previously_exposed_interval_including_warmup")
    return {"allowed_by_index": not reasons, "reasons": reasons, "overlapping_exposures": overlaps,
            "independence_certified": False, "formal_admission": False,
            "boundary": "No overlap in this index does not prove unseen data or grant holdout access."}


def claim_development(catalog_path, output, *, market, symbols, start, end, purpose):
    """Persist exposure before work, so failed runs still count. Does not grant sealed access."""
    catalog = load(catalog_path)
    check = check_window(catalog, market, symbols, start, end, role="development")
    if not check["allowed_by_index"]:
        raise ValueError("data access denied: " + ",".join(check["reasons"]))
    record = {"market": market, "symbols": sorted(symbols), "start": start, "end": end, "purpose": purpose,
              "claimed_at": datetime.now(UTC).isoformat(), "role": "development"}
    record["id"] = hash_payload(record)
    catalog["accesses"].append(record)
    return write_revision(catalog, output, parent=catalog_path)


def reserve_plans(catalog_path, plans_path, output):
    catalog, plans = load(catalog_path), read(plans_path)
    if (plans["stage"] != "hypotheses_only" or plans["model_calls_authorized"] != 0
            or plans["trading_enabled"] or plans["formal_admission"]
            or plans["preserved_policies"] != catalog["policies"]):
        raise ValueError("plans must preserve research-only boundaries and policies")
    for ref in catalog["policies"]:
        check_ref(ref)
    if plans["cost_bps"] != [15, 30]:
        raise ValueError("cost stress cannot be lowered")
    counts = Counter(h["market"] for h in plans["hypotheses"])
    if counts != {"crypto": 2, "stocks": 2}:
        raise ValueError("reserve exactly two hypotheses per market")
    registered = datetime.now(UTC).isoformat()
    for h in plans["hypotheses"]:
        if not all(h.get(k) for k in ("id", "mechanism", "definition", "controls", "failure_conditions", "data_requirements")):
            raise ValueError("incomplete research hypothesis")
        catalog["trials"].append({"id": h["id"], "market": h["market"], "definition": h,
                                  "registered_at": registered, "origin": file_ref(plans_path)})
    return write_revision(catalog, output, parent=catalog_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("import-leadership")
    ingest.add_argument("--protocol", required=True)
    ingest.add_argument("--completed", required=True)
    ingest.add_argument("--output", required=True)
    reserve = commands.add_parser("reserve")
    reserve.add_argument("--catalog", required=True)
    reserve.add_argument("--plans", required=True)
    reserve.add_argument("--output", required=True)
    check = commands.add_parser("check-window")
    claim = commands.add_parser("claim-development")
    for command in (check, claim):
        command.add_argument("--catalog", required=True)
        command.add_argument("--market", choices=("crypto", "stocks"), required=True)
        command.add_argument("--symbols", nargs="+", required=True)
        command.add_argument("--start", required=True)
        command.add_argument("--end", required=True)
    check.add_argument("--role", choices=("development", "independent"), required=True)
    claim.add_argument("--output", required=True)
    claim.add_argument("--purpose", required=True)
    args = parser.parse_args()
    if args.command == "import-leadership":
        result = import_leadership(args.protocol, args.completed, args.output)
    elif args.command == "reserve":
        result = reserve_plans(args.catalog, args.plans, args.output)
    elif args.command == "claim-development":
        result = claim_development(args.catalog, args.output, market=args.market, symbols=args.symbols,
                                   start=args.start, end=args.end, purpose=args.purpose)
    else:
        result = check_window(load(args.catalog), args.market, args.symbols, args.start, args.end, role=args.role)
    if "trials" in result:
        result = {"catalog": str(Path(args.output).resolve()), "trials": len(result["trials"]),
                  "datasets": len(result["datasets"]), "digest": result["digest"], "formal_admission": False}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
