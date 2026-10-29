"""冻结并运行四项双市场入场筛选，全程离线且保留累计失败。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import gzip
import json
from pathlib import Path

from quantbot.ai.development import cost_config, select_times
from quantbot.ai.research_catalog import check_window, load, write_revision
from quantbot.evidence import source_tree_hash
from .conditional_study import check_ref, file_ref, read, save
from .mechanism_probes import run_probe
from .mechanism_screen import finalize, screen
from .mechanism_study import data_blockers, make_snapshot
from .screening_probes import provider_for, research_cards, screening_origins, screening_panel


VERSION = "dual_market_entry_screen_v1"
METHOD = {
    "crypto": "union of daily oversold onsets; same symbols per paired origin; original 10-session horizon",
    "stocks": "same five-asset month-end origins and bounded dividend replan on both branches; horizon 20",
    "weights": "filtered allocations remain cash; no redistribution to surviving assets",
    "recovery": "at most 3 subsequent closes, first recovery -2 < z20 < 0 and rising close; next-open attempt once per onset symbol",
    "cost_buffer": "fixed stress-cost hurdle: tr63 * 20/63 > 1.2 percent at both fee levels; historical-drift proxy, not expected profit",
    "selection": "four fixed hypotheses; no grid or per-asset winner selection; unchanged gate and risk",
    "data": "reuse frozen public development snapshots; XLF and PIT gaps retained; no holdout or network data",
}


def validate_protocol(protocol):
    if (protocol["id"] != VERSION or protocol["method"] != METHOD or protocol["stage"] != "development_only"
            or protocol["model_calls_authorized"] != 0 or protocol["trading_enabled"]
            or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("筛选方案越过冻结研究边界")
    prior = read(check_ref(protocol["prior_protocol"]))
    first = read(check_ref(prior["prior_protocol"]))
    plans = read(check_ref(first["plans"]))
    cards = research_cards(plans["hypotheses"][1], prior["card"])
    previous = load(check_ref(protocol["prior_catalog"]))
    catalog = load(check_ref(protocol["catalog"]))
    pending = [t for t in catalog["trials"] if t["id"] not in catalog["outcomes"]]
    if (protocol["cards"] != cards or [t["definition"] for t in pending] != cards
            or [t["id"] for t in pending] != [c["id"] for c in cards]):
        raise ValueError("须完整登记四项固定假设，不得改变或删除失败")
    if (catalog["outcomes"] != previous["outcomes"] or len(previous["outcomes"]) < 135
            or catalog["trials"][:-4] != previous["trials"]
            or catalog["protected_intervals"] != previous["protected_intervals"]
            or catalog["policies"] != previous["policies"]):
        raise ValueError("累计历史、保护区间或准出规则改变")
    stats = {**prior["statistics"], "historical_trials": len(previous["outcomes"]),
             "reserved_new_trials": 4, "minimum_family_size": len(catalog["trials"])}
    if protocol["statistics"] != stats or protocol["markets"] != prior["markets"]:
        raise ValueError("不得更改数据、风险、费用、窗口或统计门槛")
    for reference in catalog["policies"] + protocol["diagnostic_evidence"]:
        check_ref(reference)
    for market, section in protocol["markets"].items():
        access = check_window(catalog, market, section["spec"]["symbols"], section["read_start"],
                              section["read_end"], role="development")
        if not access["allowed_by_index"] or section["spec"]["source"] != "csv":
            raise ValueError("不允许读取该数据区间或网络行情")
        for key in ("data", "actions", "quality"):
            if key in section:
                check_ref(section[key])
        if section["pit_verified"] != read(section["quality"]["path"])["pit_verified"]:
            raise ValueError("PIT 声明与质量证据不一致")
    return catalog


def schedules(protocol):
    result = {}
    for market in ("crypto", "stocks"):
        cards = [c for c in protocol["cards"] if c["market"] == market]
        section = protocol["markets"][market]
        if any(data_blockers(section, card) for card in cards):
            raise ValueError("筛选标的含未修复的行情质量问题")
        snapshot, _, _ = make_snapshot(section, cards[0]["symbols"])
        panel = screening_panel(snapshot)
        for card in cards:
            result[card["id"]] = {w["id"]: [at.isoformat() for at in screening_origins(card, snapshot, panel, w)]
                                   for w in section["windows"]}
    return result


def freeze(protocol_path, output):
    protocol = read(protocol_path)
    validate_protocol(protocol)
    save(output, {"protocol": file_ref(protocol_path), "source_hash": source_tree_hash(),
                  "origins": schedules(protocol), "frozen_at": datetime.now(UTC).isoformat(),
                  "new_outcomes_seen": False})


def run_market(protocol, frozen, market, output):
    section = protocol["markets"][market]
    cards = [c for c in protocol["cards"] if c["market"] == market]
    snapshot, config, spec = make_snapshot(section, cards[0]["symbols"])
    panel = screening_panel(snapshot)
    calendars = {w["id"]: [at.isoformat() for at in select_times(snapshot, w)] for w in section["windows"]}
    results = []
    for card in cards:
        directory = output / card["id"]
        directory.mkdir()
        events = {15: [], 30: []}
        for cost in events:
            used = cost_config(config, cost//15)
            snap = snapshot.with_config(used)
            for window in section["windows"]:
                name = window["id"]
                starts = [at.isoformat() for at in screening_origins(card, snapshot, panel, window)]
                if starts != frozen["origins"][card["id"]][name]:
                    raise ValueError("冻结原点改变")
                for ordinal, start in enumerate(starts):
                    event = {"window": name, "signal_at": start}
                    for branch in ("candidate", "control"):
                        candidate = branch == "candidate"
                        provider = provider_for(card, panel, start, used, snap.actions or [], candidate=candidate)
                        path = directory / f"{name}-{cost}-{ordinal:03}-{branch}.jsonl"
                        result = run_probe(snap, card, panel, datetime.fromisoformat(start), used, spec, path,
                                           candidate=candidate, provider=provider)
                        result["entry_attempts"] = getattr(provider, "entry_attempts", [])
                        compressed = path.with_suffix(".jsonl.gz")
                        with path.open("rb") as source, gzip.open(compressed, "xb") as dest:
                            dest.write(source.read())
                        result["journal"] = file_ref(compressed)
                        path.unlink()
                        event[branch] = result
                    events[cost].append(event)
                print(json.dumps({"hypothesis": card["id"], "cost": cost, "window": name,
                                  "origins_completed": len(starts)}), flush=True)
        save(directory / "events.json", events)
        result = screen(card, events, calendars, protocol["statistics"], pit_verified=section["pit_verified"])
        save(directory / "screen.json", result)
        results.append(result)
    return results


def run(protocol_path, freeze_path, output):
    frozen = read(freeze_path)
    if file_ref(protocol_path) != frozen["protocol"] or source_tree_hash() != frozen["source_hash"]:
        raise ValueError("冻结代码或方案改变")
    protocol = read(protocol_path)
    catalog = validate_protocol(protocol)
    if schedules(protocol) != frozen["origins"]:
        raise ValueError("冻结原点改变")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(output / "started.json", {"freeze": file_ref(freeze_path), "started_at": datetime.now(UTC).isoformat()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_market, protocol, frozen, m, output) for m in ("crypto", "stocks")]
            results = [r for future in futures for r in future.result()]
        if file_ref(protocol_path) != frozen["protocol"] or source_tree_hash() != frozen["source_hash"]:
            raise ValueError("运行期间代码或方案改变")
        validate_protocol(protocol)
        report = finalize(results, catalog, protocol["statistics"])
        report.update(stage="development_only", model_calls=0, independent_validation=False,
                      freeze=file_ref(freeze_path))
        save(output / "report.json", report)
        for result in results:
            catalog["outcomes"][result["id"]] = {"id": result["id"], "p_value": result["p_value"],
                "raw_p_value": result["raw_p_value"], "qualified": result["qualified"],
                "kind": "frozen_entry_screen", "reasons": result["reasons"],
                "evidence": file_ref(output / "report.json")}
        write_revision(catalog, output / "catalog-completed.json", parent=protocol["catalog"]["path"])
        save(output / "completed.json", {"report": file_ref(output / "report.json"),
            "catalog": file_ref(output / "catalog-completed.json"), "completed_at": datetime.now(UTC).isoformat()})
        return report
    except BaseException as exc:
        save(output / "failed.json", {"error_type": type(exc).__name__, "error": str(exc)})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("freeze", "run"))
    parser.add_argument("--protocol", required=True)
    parser.add_argument("--freeze", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.command == "freeze":
        freeze(args.protocol, args.freeze)
    elif args.output:
        report = run(args.protocol, args.freeze, args.output)
        print(json.dumps({"factor_pool": report["factor_pool"], "family_size": report["family_size"]}))
    else:
        parser.error("run requires --output")


if __name__ == "__main__":
    main()
