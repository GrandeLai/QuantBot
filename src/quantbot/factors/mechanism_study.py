"""冻结/运行四机制离线研究：python -m quantbot.factors.mechanism_study。"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import csv
from datetime import UTC, datetime
import gzip
import json
from pathlib import Path

from quantbot.ai.development import cost_config, select_times
from quantbot.ai.feeds import validate_rows
from quantbot.ai.research_catalog import check_window, load, write_revision
from quantbot.evidence import source_tree_hash
from .conditional_study import check_ref, file_ref, read, save
from .mechanism_probes import KINDS, ResearchSnapshot, feature_panel, origins, run_probe, validate_actions
from .mechanism_screen import finalize, screen


METHOD = {
    "version": "mechanism_execution_probes_v1",
    "entry": "one origin-close target, next-bar-open with unchanged limits; no reentry after cancellation/exit",
    "crypto_assets": "BTC and ETH pooled in one account per origin; both included, no winner selection",
    "calendar": "first 20 window sessions embargoed; last 20 excluded as origins; complete horizon inside window",
    "stocks_signal": "gross ordinary-dividend reinvestment index; account pays cash on actual payment date",
    "split_basis": "as-traded OHLC and shares; feature history adjusted only for effective splits",
    "unknown_availability": "explicit ex-post diagnostic only, null timestamps preserved, factor gate fails closed",
    "sector_control": "request equal weights over all nine at candidate gross; original max_positions=5 and factor_score ordering apply",
    "holding": "current weights until frozen exit; original risk caps may trim; never rerotate or top up",
    "horizon": "mark remaining positions, deduct estimated sell costs; never label a hypothetical liquidation as a fill",
    "cash_control": "zero-return idle cash; interest, taxes and borrowing not modeled",
    "statistics": "calendar-origin account returns; no compounding of independently reset probes",
}


def validate_protocol(protocol):
    if (protocol["id"] != METHOD["version"] or protocol["method"] != METHOD
            or protocol["stage"] != "development_only" or protocol["model_calls_authorized"] != 0
            or protocol["trading_enabled"] or protocol["formal_admission"] or protocol["sealed_holdout_evaluated"]):
        raise ValueError("只接受冻结的开发方案，禁止模型/交易/封存留出")
    plans = read(check_ref(protocol["plans"]))
    catalog = load(check_ref(protocol["catalog"]))
    cards = plans["hypotheses"]
    if tuple(c["mechanism"] for c in cards) != KINDS or len(cards) != 4:
        raise ValueError("必须保留四项冻结机制")
    registered = {t["id"]: t["definition"] for t in catalog["trials"]}
    if any(registered.get(c["id"]) != c or c["id"] in catalog["outcomes"] for c in cards):
        raise ValueError("假设发生变化或已经运行，不能覆盖结果")
    if plans["statistics"] != protocol["statistics"] or plans["preserved_policies"] != catalog["policies"]:
        raise ValueError("统计门槛/准出政策改变")
    if set(protocol["markets"]) != {"crypto", "stocks"}:
        raise ValueError("必须同时登记两个市场")
    for item in plans["preserved_policies"]:
        check_ref(item)
    for market, section in protocol["markets"].items():
        frozen = plans["risk_by_market"][market]
        if section["spec"]["source"] != "csv" or section["spec"]["market"] != market \
                or section["spec"].get("sec_context_path"):
            raise ValueError("禁止网络行情和未冻结事件输入")
        if any(section["config"][k] != frozen[k] for k in ("risk", "strategy")) or any(
                section["spec"][k] != frozen[k] for k in ("max_daily_loss", "max_drawdown")):
            raise ValueError("止损、止盈、冷却或熔断设置改变")
        if section["windows"] != plans["development_windows"][market]:
            raise ValueError("开发窗口发生变化")
        if section["config"]["execution"]["cost_bps"] != 15:
            raise ValueError("基础费用必须为 15 bps")
        access = check_window(catalog, market, section["spec"]["symbols"], section["read_start"],
                              section["read_end"], role="development")
        if not access["allowed_by_index"]:
            raise ValueError("数据边界受保护：" + str(access["reasons"]))
        # Hash all input sources before reading bars; a source hash alone is not PIT certification.
        for key in ("data", "actions", "quality"):
            if key in section:
                check_ref(section[key])
        if section["pit_verified"] != read(section["quality"]["path"])["pit_verified"]:
            raise ValueError("PIT 声明与冻结数据质量不一致")
    return plans, catalog


def data_blockers(section, card):
    quality = read(check_ref(section["quality"]))
    return ["invalid_ohlcv:"+symbol for symbol in card["symbols"] if symbol in quality["blocked_symbols"]]


def make_snapshot(section, symbols):
    config, spec = deepcopy(section["config"]), deepcopy(section["spec"])
    config["market"]["symbols"] = spec["symbols"] = list(symbols)
    with check_ref(section["data"]).open(newline="") as handle:
        all_rows = list(csv.DictReader(handle))
    rows = [r for r in all_rows if r["symbol"] in symbols]
    for row in all_rows:
        day = row.get("date", row.get("timestamp", ""))[:10]
        # Crypto read_start/end describe completed closes, not candle-open dates.
        if spec["market"] == "crypto":
            from datetime import timedelta
            day = (datetime.fromisoformat(row["timestamp"]) + timedelta(days=1)).date().isoformat()
        if not section["read_start"] <= day <= section["read_end"]:
            raise ValueError("数据文件包含未声明日期")
    for row in rows:
        for key in ("open", "high", "low", "close", "volume"):
            row[key] = float(row[key])
    actions = [a for a in read(check_ref(section["actions"])) if a["symbol"] in symbols] if "actions" in section else []
    validate_actions(actions, set(symbols))
    snapshot = validate_rows(rows, spec, config, datetime.now(UTC), fresh=False)
    snapshot = ResearchSnapshot(snapshot.market, rows, config, snapshot.source, snapshot.calendar, actions)
    return snapshot, config, spec


def freeze(protocol_path, output):
    protocol = read(protocol_path)
    plans, _ = validate_protocol(protocol)
    schedules = {}
    for card in plans["hypotheses"]:
        section = protocol["markets"][card["market"]]
        blocked = data_blockers(section, card)
        if blocked:
            schedules[card["id"]] = {"data_blocked": blocked}
            continue
        snapshot, _, _ = make_snapshot(section, card["symbols"])
        schedules[card["id"]] = {w["id"]: [at.isoformat() for at in origins(card, snapshot.times, w)]
                                   for w in section["windows"]}
    # Every datum, date grid and executable source is committed before any outcome.
    save(output, {"protocol": file_ref(protocol_path), "source_hash": source_tree_hash(),
                  "origins": schedules, "frozen_at": datetime.now(UTC).isoformat(),
                  "outcomes_seen": False, "model_calls": 0})


def run_market(protocol, plans, frozen, market, output):
    section = protocol["markets"][market]
    results = []
    for card in [c for c in plans["hypotheses"] if c["market"] == market]:
        directory = output / card["id"]
        directory.mkdir()
        blocked = data_blockers(section, card)
        if blocked:
            if frozen["origins"][card["id"]] != {"data_blocked": blocked}:
                raise ValueError("冻结的数据阻断条件改变")
            result = {"id": card["id"], "market": market, "status": "data_blocked", "costs": [],
                      "reasons": blocked, "raw_p_value": 1., "p_value": 1., "qualified": False}
            save(directory / "screen.json", result)
            results.append(result)
            continue
        snapshot, config, spec = make_snapshot(section, card["symbols"])
        panel = feature_panel(snapshot)
        calendars = {w["id"]: [at.isoformat() for at in select_times(snapshot, w)] for w in section["windows"]}
        events = {15: [], 30: []}
        for cost in (15, 30):
            used = cost_config(config, cost//15)
            snap = snapshot.with_config(used)
            for window in section["windows"]:
                name = window["id"]
                starts = [at.isoformat() for at in origins(card, snap.times, window)]
                if starts != frozen["origins"][card["id"]][name]:
                    raise ValueError("冻结原点改变")
                for ordinal, start in enumerate(starts):
                    event = {"window": name, "signal_at": start}
                    for branch in ("candidate", "control"):
                        path = directory / f"{name}-{cost}-{ordinal:03}-{branch}.jsonl"
                        result = run_probe(snap, card, panel, datetime.fromisoformat(start), used, spec,
                                           path, candidate=branch == "candidate")
                        compressed = path.with_suffix(".jsonl.gz")
                        with path.open("rb") as source, gzip.open(compressed, "xb") as dest:
                            dest.write(source.read())
                        result["journal"] = file_ref(compressed)
                        path.unlink()  # Identical durable compressed evidence was written successfully.
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
        raise ValueError("冻结代码或方案已改变")
    protocol = read(protocol_path)
    plans, catalog = validate_protocol(protocol)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(output / "started.json", {"freeze": file_ref(freeze_path), "started_at": datetime.now(UTC).isoformat()})
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run_market, protocol, plans, frozen, m, output) for m in ("crypto", "stocks")]
            results = [r for future in futures for r in future.result()]
        if file_ref(protocol_path) != frozen["protocol"] or source_tree_hash() != frozen["source_hash"]:
            raise ValueError("运行期间代码或协议改变")
        validate_protocol(protocol)
        report = finalize(results, catalog, protocol["statistics"])
        report.update(stage="development_only", model_calls=0, freeze=file_ref(freeze_path),
                      independent_validation=False)
        save(output / "report.json", report)
        for result in report["results"]:
            catalog["outcomes"][result["id"]] = {"id": result["id"], "p_value": result["p_value"],
                "raw_p_value": result["raw_p_value"], "kind": "frozen_mechanism_probe", "qualified": result["qualified"],
                "evidence": file_ref(output / "report.json"), "reasons": result["reasons"]}
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
    else:
        if not args.output:
            parser.error("run requires --output")
        report = run(args.protocol, args.freeze, args.output)
        print(json.dumps({"factor_pool": report["factor_pool"], "family_size": report["family_size"]}))


if __name__ == "__main__":
    main()
