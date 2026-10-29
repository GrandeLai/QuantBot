"""冻结并验证一次除息后重规划，同时复核加密信号漏斗；全程离线。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime
import gzip
import json
from pathlib import Path

from quantbot.ai.development import cost_config, select_times
from quantbot.ai.research_catalog import check_window, load, write_revision
from quantbot.evidence import source_tree_hash
from .conditional_study import check_ref, file_ref, read, save
from .mechanism_probes import feature_panel, origins, run_probe
from .mechanism_screen import finalize, screen
from .mechanism_study import data_blockers, make_snapshot
from .signal_diagnostics import signal_funnel


VERSION = "mechanism_v2_stocks_dividend_replan"
METHOD = {
    "trigger": "actual entire first-entry plan canceled by ordinary dividend; first execution session only",
    "decision": "recompute all five assets at cancellation-day close; require no positions or previous exit",
    "retry_limit": 1,
    "fill": "next session open via unchanged shared limits and independent risk controls",
    "control": "identical five-asset eligibility, fees, origins and horizon; no retry",
    "horizon": "original origin plus 20 sessions; never extend after cancellation",
    "crypto": "frozen-condition daily/grid counts only; no new return test or threshold optimization",
    "evidence": "ex-post diagnostic; unverified publication/revision evidence cannot authorize factor admission",
}


def research_card(base):
    card = deepcopy(base)
    card.update(id=VERSION, controls=["cash", METHOD["control"]], readiness="ex_post_diagnostic_only")
    card["definition"]["replan"] = METHOD
    return card


def validate_protocol(protocol):
    if (protocol["id"] != VERSION or protocol["method"] != METHOD or protocol["stage"] != "development_only"
            or protocol["model_calls_authorized"] != 0 or protocol["trading_enabled"]
            or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("重规划方案越过冻结研究边界")
    prior = read(check_ref(protocol["prior_protocol"]))
    plans = read(check_ref(prior["plans"]))
    catalog = load(check_ref(protocol["catalog"]))
    card = research_card(plans["hypotheses"][2])
    if protocol["card"] != card:
        raise ValueError("不得更改原信号、标的、期限或重试规则")
    pending = [t for t in catalog["trials"] if t["id"] not in catalog["outcomes"]]
    if len(pending) != 1 or pending[0]["id"] != VERSION or pending[0]["definition"] != card:
        raise ValueError("须登记唯一重规划试验，保留全部历史结果")
    stats = {**prior["statistics"], "historical_trials": len(catalog["outcomes"]),
             "reserved_new_trials": 1, "minimum_family_size": len(catalog["trials"])}
    if protocol["statistics"] != stats or len(catalog["outcomes"]) < 134:
        raise ValueError("统计门槛或累计家族改变")
    if plans["preserved_policies"] != catalog["policies"]:
        raise ValueError("准出政策改变")
    for ref in catalog["policies"]:
        check_ref(ref)
    # This experiment isolates execution; neither data values nor original config may change.
    if protocol["markets"] != prior["markets"]:
        raise ValueError("重规划不能同时更换行情、风险、费用或窗口")
    for market, section in protocol["markets"].items():
        access = check_window(catalog, market, section["spec"]["symbols"], section["read_start"],
                              section["read_end"], role="development")
        if not access["allowed_by_index"] or section["spec"]["source"] != "csv":
            raise ValueError("不允许读取该数据区间或网络行情")
        for field in ("data", "actions", "quality"):
            if field in section:
                check_ref(section[field])
        if section["pit_verified"] != read(section["quality"]["path"])["pit_verified"]:
            raise ValueError("时点证据声明不一致")
    for ref in protocol["diagnostic_evidence"]:
        check_ref(ref)
    return plans, catalog


def schedules(protocol):
    section, card = protocol["markets"]["stocks"], protocol["card"]
    if data_blockers(section, card):
        raise ValueError("重规划资产有行情质量阻断")
    snapshot, _, _ = make_snapshot(section, card["symbols"])
    return {w["id"]: [at.isoformat() for at in origins(card, snapshot.times, w)] for w in section["windows"]}


def freeze(protocol_path, output):
    protocol = read(protocol_path)
    validate_protocol(protocol)
    save(output, {"protocol": file_ref(protocol_path), "source_hash": source_tree_hash(),
                  "origins": schedules(protocol), "frozen_at": datetime.now(UTC).isoformat(),
                  "new_outcomes_seen": False})


def run_stocks(protocol, frozen, output):
    section, card = protocol["markets"]["stocks"], protocol["card"]
    snapshot, config, spec = make_snapshot(section, card["symbols"])
    panel = feature_panel(snapshot)
    calendars = {w["id"]: [at.isoformat() for at in select_times(snapshot, w)] for w in section["windows"]}
    if schedules(protocol) != frozen["origins"]:
        raise ValueError("原点改变")
    events = {15: [], 30: []}
    directory = output / "stocks"
    directory.mkdir()
    for cost in events:
        used = cost_config(config, cost//15)
        snap = snapshot.with_config(used)
        for name, starts in frozen["origins"].items():
            for ordinal, start in enumerate(starts):
                event = {"window": name, "signal_at": start}
                for branch in ("candidate", "control"):
                    path = directory / f"{name}-{cost}-{ordinal:03}-{branch}.jsonl"
                    result = run_probe(snap, card, panel, datetime.fromisoformat(start), used, spec, path,
                                       candidate=True, replan=branch == "candidate")
                    compressed = path.with_suffix(".jsonl.gz")
                    with path.open("rb") as src, gzip.open(compressed, "xb") as dest:
                        dest.write(src.read())
                    result["journal"] = file_ref(compressed)
                    path.unlink()
                    event[branch] = result
                events[cost].append(event)
            print(json.dumps({"cost": cost, "window": name, "origins": len(starts)}), flush=True)
    save(directory / "events.json", events)
    return screen(card, events, calendars, protocol["statistics"], pit_verified=section["pit_verified"])


def run_crypto(protocol, plans, output):
    card, section = plans["hypotheses"][1], protocol["markets"]["crypto"]
    snapshot, _, _ = make_snapshot(section, card["symbols"])
    result = signal_funnel(snapshot, card, section["windows"])
    save(output / "crypto-diagnostic.json", result)
    return result


def run(protocol_path, freeze_path, output):
    frozen = read(freeze_path)
    if file_ref(protocol_path) != frozen["protocol"] or source_tree_hash() != frozen["source_hash"]:
        raise ValueError("冻结代码或方案改变")
    protocol = read(protocol_path)
    plans, catalog = validate_protocol(protocol)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(output / "started.json", {"freeze": file_ref(freeze_path), "started_at": datetime.now(UTC).isoformat()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            crypto = executor.submit(run_crypto, protocol, plans, output)
            stocks = executor.submit(run_stocks, protocol, frozen, output)
            crypto.result()
            result = stocks.result()
        if source_tree_hash() != frozen["source_hash"] or file_ref(protocol_path) != frozen["protocol"]:
            raise ValueError("运行期间代码或方案改变")
        validate_protocol(protocol)
        report = finalize([result], catalog, protocol["statistics"])
        report.update(stage="development_only", model_calls=0, independent_validation=False,
                      freeze=file_ref(freeze_path), crypto_diagnostic=file_ref(output / "crypto-diagnostic.json"))
        save(output / "report.json", report)
        catalog["outcomes"][VERSION] = {"id": VERSION, "p_value": result["p_value"],
            "raw_p_value": result["raw_p_value"], "qualified": result["qualified"],
            "kind": "bounded_dividend_replan", "reasons": result["reasons"],
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
