"""Preregistered intraday factor probes, continuous ledgers and daily inference."""

import argparse
from collections import defaultdict, deque
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quantbot.ai.research_catalog import check_window, claim_development, load, write_revision
from quantbot.evidence import source_tree_hash
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from quantbot.factors.entry_screen import _bootstrap
from quantbot.factors.mechanism_screen import finalize
from .data import acquire, checked_protocol, frozen_policy_snapshots, import_minutes, load_bars
from .execution import IntradayAccount
from .strategies import SessionSignals, select_mechanism


def prepare_sessions(frame, sessions, symbols, *, atr_window=14, actions=()):
    """Only completed earlier sessions contribute ATR and same-slot references."""
    by_day = defaultdict(dict)
    day_for_stamp = {
        at: day
        for day, (opening, closing) in sessions.items()
        for at in pd.date_range(opening, closing, freq="5min", inclusive="left")
    }
    for row in frame.itertuples(index=False):
        value = row._asdict()
        at, symbol = value.pop("timestamp"), value.pop("symbol")
        by_day[day_for_stamp[at]].setdefault(at, {})[symbol] = value
    volumes = defaultdict(lambda: deque(maxlen=20))
    ranges = {s: deque(maxlen=atr_window) for s in symbols}
    prior_close = {}
    prepared = {}
    for day, (opening, closing) in sessions.items():
        day_actions = [a for a in actions if a["date"] == day]
        if day_actions:
            from .actions import adjust_history

            adjust_history(day_actions, prior_close, ranges, volumes)
        bars = sorted(by_day[day].items())
        opening_high = dict.fromkeys(symbols)
        opening_low = dict.fromkeys(symbols)
        atr = {s: float(np.mean(ranges[s])) if len(ranges[s]) == atr_window else None for s in symbols}
        signals, breakouts, day_volumes = {}, {}, {}
        mechanisms = {s: SessionSignals() for s in symbols}
        for index in range(2, len(bars), 3):
            at = bars[index][0] + timedelta(minutes=5)
            if index == 5:
                opening_high = {s: max(float(b[s]["high"]) for _, b in bars[:6]) for s in symbols}
                opening_low = {s: min(float(b[s]["low"]) for _, b in bars[:6]) for s in symbols}
            slot = index // 3
            values = {}
            for symbol in symbols:
                group = [b[symbol] for _, b in bars[index - 2 : index + 1]]
                volume = sum(float(b["volume"]) for b in group)
                history = volumes[(symbol, slot)]
                median = float(np.median(history)) if len(history) == 20 else None
                relative = volume / median if median is not None and median > 0 else None
                close = float(group[-1]["close"])
                low = min(float(b["low"]) for b in group)
                base = (
                    symbol not in breakouts
                    and at > opening + timedelta(minutes=30)
                    and at <= closing - timedelta(minutes=135)
                    and close > opening_high[symbol]
                )
                if base:
                    breakouts[symbol] = at
                retest = (
                    symbol in breakouts
                    and breakouts[symbol] < at <= breakouts[symbol] + timedelta(minutes=30)
                    and low <= opening_high[symbol] < close
                )
                values[symbol] = {
                    "base": base,
                    "breakout_next_bar": (
                        symbol in breakouts
                        and at == breakouts[symbol] + timedelta(minutes=15)
                        and at <= closing - timedelta(minutes=135)
                        and close > opening_high[symbol]
                    ),
                    "retest": retest,
                    "relative_volume": relative,
                    "atr": atr[symbol],
                    "close": close,
                    "opening_high": opening_high[symbol],
                    "opening_low": opening_low[symbol],
                    "valid": atr[symbol] is not None and relative is not None,
                    **mechanisms[symbol].update(
                        group,
                        eligible=(opening + timedelta(minutes=60) <= at
                                  <= closing - timedelta(minutes=135)),
                    ),
                }
                day_volumes[(symbol, slot)] = volume
            # Combine only this completed timestamp, after both symbols have updated.
            moves = [value["net_move_30m_bps"] for value in values.values()]
            pair_move = min(moves) if len(moves) == 2 and all(move is not None for move in moves) else None
            for value in values.values():
                value["pair_net_move_30m_bps"] = pair_move
            signals[at] = values
        prepared[day] = {
            "bars": bars,
            "signals": signals,
            "opening": opening,
            "closing": closing,
            "feature_valid": all(v["valid"] for values in signals.values() for v in values.values()),
        }
        if day_actions:
            prepared[day]["actions"] = day_actions
        for symbol in symbols:
            high = max(float(b[symbol]["high"]) for _, b in bars)
            low = min(float(b[symbol]["low"]) for _, b in bars)
            close = float(bars[-1][1][symbol]["close"])
            previous = prior_close.get(symbol, float(bars[0][1][symbol]["open"]))
            ranges[symbol].append(max(high - low, abs(high - previous), abs(low - previous)))
            prior_close[symbol] = close
        for key, value in day_volumes.items():
            volumes[key].append(value)
    return prepared


def evaluation_days(protocol, prepared, window):
    days = [d for d in prepared if window["start"] <= d <= window["end"]]
    # Leave both boundaries out; earlier completed sessions are feature warmup only.
    left, right = protocol["statistics"]["embargo_sessions"], protocol["statistics"]["purge_sessions"]
    if len(days) <= left + right:
        raise ValueError("窗口不足以保留原 purge/embargo")
    return days[left : len(days) - right]


def select_signal(factor, value, symbol, at, pending, stress_round_trip_bps, *, recipe=None):
    """Fixed probes; a failed first retest is consumed, never replaced by a later winner."""
    if recipe is not None:
        from .mining import matches

        return (select_signal(recipe["seed"], value, symbol, at, pending, stress_round_trip_bps)
                and matches(recipe, value, stress_round_trip_bps))
    if factor in ("vwap_reclaim", "vwap_trend_reclaim", "range_breakdown", "failed_breakdown_reversal"):
        return select_mechanism(factor, value, stress_round_trip_bps)
    if factor == "breakout_next_bar":
        return value["breakout_next_bar"]
    if factor not in ("control", "volume_confirmation", "retest_confirmation",
                      "volume_cost_room", "retest_persistence"):
        raise ValueError("未知日内因子")
    if value["base"]:
        if factor in ("retest_confirmation", "retest_persistence"):
            pending[symbol] = None
        elif factor == "control":
            return True
        elif factor == "volume_confirmation":
            return value["relative_volume"] >= 1.5
        else:
            width = value["opening_high"] - value["opening_low"]
            return (value["relative_volume"] >= 1.5
                    and width / value["opening_high"] * 10000 >= stress_round_trip_bps
                    and value["close"] - value["opening_high"] <= 0.5 * width)
    if symbol not in pending:
        return False
    waiting = pending[symbol]
    if waiting is not None:
        # Check exactly the next completed 15-minute bar, including when it is not itself a retest.
        if at >= waiting["at"] + timedelta(minutes=15):
            del pending[symbol]
            return (at == waiting["at"] + timedelta(minutes=15)
                    and value["close"] > value["opening_high"]
                    and value["close"] >= waiting["close"])
        return False
    if value["retest"]:
        if factor == "retest_confirmation":
            return True
        pending[symbol] = {"at": at, "close": value["close"]}
    return False


def replay(protocol, market, prepared, window, factor, cost, latency, *, recipe=None):
    days = evaluation_days(protocol, prepared, window)
    account = IntradayAccount(
        market,
        protocol["markets"][market],
        protocol["execution_model"],
        cost_bps=cost,
        latency_seconds=latency,
        sessions=prepared,
    )
    first_index = list(prepared).index(days[0])
    if first_index:
        previous = prepared[list(prepared)[first_index - 1]]["bars"][-1][1]
        from decimal import Decimal

        account.previous_volume = {s: Decimal(b["volume"]) for s, b in previous.items()}
    for day in days:
        own = prepared[day]
        pending_retest = {}
        consumed = set()
        for at, bars in own["bars"]:
            account.advance(at, bars, day=day, session_close=own["closing"])
            close_at = at + timedelta(minutes=5)
            features = own["signals"].get(close_at, {})
            for symbol, value in features.items():
                if close_at > own["closing"] - timedelta(minutes=135) or not value["valid"]:
                    continue
                selected = select_signal(
                    factor, value, symbol, close_at, pending_retest,
                    2 * (max(protocol["execution_model"]["one_way_cost_bps"])
                         + protocol["execution_model"]["impact_bps_at_participation_cap"]),
                    recipe=recipe,
                )
                if selected and symbol not in consumed:
                    consumed.add(symbol)
                    account.enter(
                        symbol,
                        close_at,
                        day,
                        value["close"],
                        value["atr"],
                        identity=f"{window['id']}:{factor}:{cost}:{latency}",
                    )
    result = account.report()
    result.update(
        window=window["id"],
        market=market,
        factor=factor,
        cost_bps=cost,
        latency_seconds=latency,
        feature_coverage=sum(prepared[d]["feature_valid"] for d in days) / len(days),
        evaluated_start=days[0],
        evaluated_end=days[-1],
    )
    if recipe is not None:
        result["recipe"] = recipe
    return result


def audit_report(result):
    """Rebuild cash/quantity/cost from persisted fills, independently of engine state."""
    from decimal import Decimal as D

    cash = D(str(result["initial_equity"]))
    positions = {}
    costs = D(0)
    filled = set()
    consumed = defaultdict(lambda: D(0))
    for event in result["events"]:
        if event["status"] != "filled":
            continue
        identity = (event["client_order_id"], event["at"], event["remaining"])
        if identity in filled:
            raise ValueError("重复成交证据")
        filled.add(identity)
        symbol = event["symbol"]
        quantity = D(event["quantity"])
        price = D(event["price"])
        key = (event["bar_open"], symbol)
        consumed[key] += quantity
        if consumed[key] > D(event["bar_capacity"]) or consumed[key] != D(event["capacity_used_after"]):
            raise ValueError("成交超过共享分钟量能预算")
        sign = 1 if event["side"] == "buy" else -1
        cash -= sign * quantity * price
        positions[symbol] = positions.get(symbol, D(0)) + sign * quantity
        if positions[symbol] < 0 or cash < 0:
            raise ValueError("回放账目出现透支或超卖")
        if not positions[symbol]:
            positions.pop(symbol)
        costs += D(event["estimated_cost"])
        if abs(sum((D(v) for v in event["cost_components"].values()), D(0)) - D(event["estimated_cost"])) > D(
            "0.00000001"
        ):
            raise ValueError("费用分项和成交总成本不一致")
    state = result["account"]
    if (
        abs(cash - D(str(state["cash"]))) > D("0.00000001")
        or positions != {s: D(q) for s, q in state["positions"].items()}
        or abs(costs - D(str(state["estimated_execution_cost"]))) > D("0.00000001")
    ):
        raise ValueError("落盘成交重算与账户不一致")
    return {"status": "reconciled", "fills": len(filled), "cash": str(cash), "costs": str(costs)}


def screen_scenario(card, pairs, statistics, *, cost, latency):
    grids = []
    windows = []
    reasons = []
    for candidate, control in pairs:
        if [r["day"] for r in candidate["daily_curve"]] != [r["day"] for r in control["daily_curve"]]:
            raise ValueError("候选和对照交易日不配对")
        a = np.array([candidate["initial_equity"]] + [r["equity"] for r in candidate["daily_curve"]])
        b = np.array([control["initial_equity"]] + [r["equity"] for r in control["daily_curve"]])
        own = a[1:] / a[:-1] - 1
        base = b[1:] / b[:-1] - 1
        grids.append(np.column_stack((np.ones(len(own)), np.ones(len(own)), own, own - base)))
        row = {
            "window": candidate["window"],
            "sessions": len(own),
            "net_return": candidate["net_return"],
            "control_net_return": control["net_return"],
            "increment": candidate["net_return"] - control["net_return"],
            "entry_orders_filled": candidate["entry_orders_filled"],
            "partial_fills": candidate["partial_fills"],
            "feature_coverage": candidate["feature_coverage"],
            "candidate_risk_hold": candidate["risk_hold"],
            "control_risk_hold": control["risk_hold"],
            "jointly_positive": candidate["net_return"] > 0
            and candidate["net_return"] > control["net_return"],
        }
        for branch in (candidate, control):
            audit_report(branch)
        if candidate["risk_hold"] or candidate["session_violations"] or control["session_violations"]:
            reasons.append(candidate["window"] + ":risk_hold_or_session_residual")
        # A normally stopped control keeps its cash curve in the paired comparison;
        # its loss limit is not a failure of the candidate being evaluated.
        if control["risk_hold"] not in (None, "daily_loss_limit", "drawdown_limit"):
            reasons.append(candidate["window"] + ":control_abnormal_risk_hold")
        if row["entry_orders_filled"] < statistics["minimum_filled_per_window"]:
            reasons.append(candidate["window"] + ":insufficient_independent_entry_orders")
        if row["feature_coverage"] < statistics["minimum_feature_coverage"]:
            reasons.append(candidate["window"] + ":insufficient_feature_coverage")
        windows.append(row)
    boot = _bootstrap(grids)
    net = boot["net_contribution_per_kept_event"]
    delta = boot["increment_per_baseline_event"]
    if sum(w["entry_orders_filled"] for w in windows) < statistics["minimum_total_filled"]:
        reasons.append("insufficient_total_entries")
    if sum(w["jointly_positive"] for w in windows) < statistics["minimum_positive_windows"]:
        reasons.append("insufficient_positive_windows")
    for name, metric in (("net", net), ("increment", delta)):
        if metric["observed"] <= 0:
            reasons.append("nonpositive_" + name)
        if metric["ci_low"] is None or metric["ci_low"] <= 0:
            reasons.append(name + "_confidence_interval_not_positive")
    return {
        "cost_bps": cost,
        "latency_seconds": latency,
        "windows": windows,
        "net_daily_return": net,
        "increment_daily_return": delta,
        "p_value": boot["p_value"],
        "reasons": sorted(set(reasons)),
        "unit": "continuous account arithmetic daily return; windows have separate initial accounts",
    }


def freeze(protocol_path, data_manifest_path, output, *, policy_snapshots=None):
    snapshot_binding = {}
    if policy_snapshots is not None:
        snapshot_binding = {"policy_snapshots_input": file_ref(policy_snapshots),
                            "policy_snapshots": read(policy_snapshots)}
    snapshots = frozen_policy_snapshots(snapshot_binding)
    protocol = (checked_protocol(protocol_path, policy_snapshots=snapshots) if snapshots
                else checked_protocol(protocol_path))
    prior = load(check_ref(protocol["prior_catalog"]))
    manifest = read(data_manifest_path)
    if manifest["protocol"] != file_ref(protocol_path):
        raise ValueError("采集数据未绑定当前方案")
    historical = protocol["statistics"]["historical_trials"]
    if len(prior["trials"]) != historical or len(prior["outcomes"]) != historical:
        raise ValueError(f"累计研究账本必须保留此前{historical}项结果")
    read_end = (datetime.fromisoformat(protocol["range"]["end_exclusive"]) - timedelta(days=1)).date().isoformat() if any(
        manifest[m]["data"] is not None for m in ("crypto", "stocks")) else None
    for market in ("crypto", "stocks"):
        section = manifest[market]
        if section["data"] is None:
            continue
        access = check_window(prior, market, protocol["markets"][market]["symbols"],
                              protocol["range"]["start"], read_end, role="development")
        if not access["allowed_by_index"]:
            raise ValueError("数据窗口被累计研究账本阻断: " + ",".join(access["reasons"]))
        if section.get("evidence") is not None:
            from .evidence import inspect_evidence

            metadata = inspect_evidence(section["evidence"], market, protocol["markets"][market]["symbols"],
                                        protocol["range"]["start"], read_end, protocol["execution_model"])
            check_ref(metadata["components"]["corporate_actions"])
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    quality, references, action_bindings = {}, {}, {}
    catalog_ref = protocol["prior_catalog"]
    catalog = deepcopy(prior)
    for market in ("crypto", "stocks"):
        section = manifest[market]
        if section["data"] is not None:
            # Record exposure before prices (including a failed grid or action check).
            accessed_path = output / ("catalog-accessed-" + market + ".json")
            catalog = claim_development(check_ref(catalog_ref), accessed_path, market=market,
                symbols=protocol["markets"][market]["symbols"], start=protocol["range"]["start"],
                end=read_end, purpose="intraday_execution_freeze")
            catalog_ref = file_ref(accessed_path)
            path = check_ref(section["data"])
            frame, sessions, quality[market] = load_bars(path, protocol, market)
            references[market] = file_ref(path)
            from .evidence import replay_action_binding

            action_bindings[market] = replay_action_binding(protocol, market, section, frame, sessions)
        else:
            quality[market] = {"complete": False, "pit_verified": False, "reasons": section["reasons"]}
            references[market] = None
            action_bindings[market] = None
    catalog["trials"] += [
        {"id": card["id"], "market": card["market"], "definition": card,
         "registered_at": protocol["frozen_at"]}
        for card in protocol["factors"]
    ]
    for market, ref in references.items():
        if ref:
            catalog["datasets"].append({
                "id": protocol.get("study_id", protocol.get("id", "dual_market_intraday_v1")) + "_" + market,
                "market": market, "symbols": protocol["markets"][market]["symbols"],
                "read_start": protocol["range"]["start"], "read_end": read_end,
                "role": "exposed_development", "data": ref})
    write_revision(catalog, output / "catalog-frozen.json", parent=check_ref(catalog_ref))
    frozen = {
        "protocol": file_ref(protocol_path),
        "data_manifest": file_ref(data_manifest_path),
        "data": references,
        "corporate_actions": action_bindings,
        "quality": quality,
        "catalog": file_ref(output / "catalog-frozen.json"),
        "source_hash": source_tree_hash(),
        "frozen_at": datetime.now(UTC).isoformat(),
        "before_factor_results": True,
        **snapshot_binding,
    }
    save(output / "execution-freeze.json", frozen)
    return frozen


def run(protocol_path, freeze_path, output):
    frozen = read(freeze_path)
    snapshots = frozen_policy_snapshots(frozen)
    protocol = (checked_protocol(protocol_path, policy_snapshots=snapshots) if snapshots
                else checked_protocol(protocol_path))
    if frozen["protocol"] != file_ref(protocol_path) or frozen["source_hash"] != source_tree_hash():
        raise ValueError("冻结代码或方案改变")
    check_ref(frozen["data_manifest"])
    from .evidence import check_frozen_action_refs

    check_frozen_action_refs(frozen)
    catalog = load(check_ref(frozen["catalog"]))
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    save(
        output / "started.json",
        {"freeze": file_ref(freeze_path), "started_at": datetime.now(UTC).isoformat()},
    )
    results = []
    counts = {"paired_account_audits": 0, "unique_replays": 0, "snapshots": 0, "fills": 0, "partial_fills": 0}
    artifacts = []
    try:
        for market in ("crypto", "stocks"):
            cards = [c for c in protocol["factors"] if c["market"] == market]
            if not frozen["quality"][market]["complete"]:
                for card in cards:
                    results.append(
                        {
                            "id": card["id"],
                            "market": market,
                            "status": "blocked_data",
                            "scenarios": [],
                            "reasons": ["missing_complete_frozen_minute_data"],
                            "raw_p_value": 1.0,
                            "p_value": 1.0,
                            "qualified": False,
                        }
                    )
                    save(output / (card["id"] + "-screen.json"), results[-1])
                continue
            frame, sessions, quality = load_bars(check_ref(frozen["data"][market]), protocol, market)
            if quality != frozen["quality"][market]:
                raise ValueError("冻结数据质量不一致")
            from .evidence import frozen_replay_actions

            actions = frozen_replay_actions(frozen, protocol, market, frame, sessions)
            prepared = prepare_sessions(
                frame,
                sessions,
                protocol["markets"][market]["symbols"],
                atr_window=protocol["markets"][market]["strategy"]["atr_window"], actions=actions,
            )
            del frame
            controls = {}
            control_factors = [c.get("control", "control") for c in cards]
            if protocol.get("original_control_required"):
                control_factors.append("control")
            for control_factor in dict.fromkeys(control_factors):
                for cost in protocol["execution_model"]["one_way_cost_bps"]:
                    for latency in protocol["execution_model"]["latency_seconds"]:
                        for window in protocol["windows"]:
                            controls[(control_factor, cost, latency, window["id"])] = replay(
                                protocol, market, prepared, window, control_factor, cost, latency
                            )
                            counts["unique_replays"] += 1
            for card in cards:
                scenarios = []
                for cost in protocol["execution_model"]["one_way_cost_bps"]:
                    for latency in protocol["execution_model"]["latency_seconds"]:
                        pairs, original_pairs = [], []
                        for window in protocol["windows"]:
                            candidate = replay(
                                protocol, market, prepared, window, card["factor"], cost, latency,
                                recipe=card.get("recipe"),
                            )
                            counts["unique_replays"] += 1
                            control = controls[(card.get("control", "control"), cost, latency, window["id"])]
                            pairs.append((candidate, control))
                            branches = {"candidate": candidate, "control": control}
                            if protocol.get("original_control_required"):
                                original = controls[("control", cost, latency, window["id"])]
                                original_pairs.append((candidate, original))
                                branches["original_control"] = original
                            name = f"{card['id']}-{cost}-{latency}-{window['id']}.json.gz"
                            for result in branches.values():
                                verification = audit_report(result)
                                counts["paired_account_audits"] += 1
                                counts["snapshots"] += result["reconciliation"]["account_snapshots"]
                                counts["fills"] += verification["fills"]
                                counts["partial_fills"] += result["partial_fills"]
                            path = output / name
                            with gzip.open(path, "xt", encoding="utf-8") as handle:
                                json.dump(
                                    branches,
                                    handle,
                                    ensure_ascii=False,
                                    allow_nan=False,
                                )
                            artifacts.append(file_ref(path))
                        scenario = screen_scenario(card, pairs, protocol["statistics"], cost=cost, latency=latency)
                        scenario["control_factor"] = card.get("control", "control")
                        if original_pairs:
                            original_screen = screen_scenario(
                                card, original_pairs, protocol["statistics"], cost=cost, latency=latency
                            )
                            scenario["original_control"] = original_screen
                            scenario["p_value"] = max(scenario["p_value"], original_screen["p_value"])
                            scenario["reasons"] += ["original_control:" + r for r in original_screen["reasons"]]
                        scenarios.append(scenario)
                        print(
                            json.dumps(
                                {
                                    "market": market,
                                    "factor": card["factor"],
                                    "cost": cost,
                                    "latency": latency,
                                    "windows": 3,
                                }
                            ),
                            flush=True,
                        )
                reasons = ["missing_point_in_time_inputs", "uncalibrated_execution_costs"]
                reasons += [
                    f"{s['cost_bps']}:{s['latency_seconds']}:{r}" for s in scenarios for r in s["reasons"]
                ]
                raw = max(s["p_value"] for s in scenarios)
                results.append(
                    {
                        "id": card["id"],
                        "market": market,
                        "status": "completed_diagnostic_only",
                        "scenarios": scenarios,
                        "reasons": reasons,
                        "raw_p_value": raw,
                        "p_value": 1.0 if reasons else raw,
                        "qualified": False,
                    }
                )
                save(output / (card["id"] + "-screen.json"), results[-1])
        from .evidence import check_frozen_action_refs

        check_frozen_action_refs(frozen)
        if source_tree_hash() != frozen["source_hash"]:
            raise ValueError("运行期间源码改变")
        snapshots = frozen_policy_snapshots(frozen)
        if snapshots:
            checked_protocol(protocol_path, policy_snapshots=snapshots)
        else:
            checked_protocol(protocol_path)
        report = finalize(results, catalog, protocol["statistics"])
        report.update(
            data_quality=frozen["quality"],
            verification=counts,
            artifacts=artifacts,
            execution_completed=all(r["status"] != "blocked_data" for r in results),
            available_market_replays_completed=True,
            model_calls=0,
            broker_calls=0,
            boundary="Research probes only. Final-version bars and estimated costs cannot certify PIT or OKX execution.",
        )
        if protocol["id"] == "dual_market_intraday_mining_v1":
            from .mining import write_mining_summary

            write_mining_summary(protocol, report, output)
        save(output / "report.json", report)
        completed = deepcopy(catalog)
        for result in results:
            completed["outcomes"][result["id"]] = {
                "p_value": result["p_value"],
                "qualified": False,
                "status": result["status"],
                "reasons": result["reasons"],
                "evidence": file_ref(output / "report.json"),
            }
        write_revision(completed, output / "catalog-completed.json", parent=check_ref(frozen["catalog"]))
        return report
    except Exception as exc:
        save(
            output / "failure.json",
            {
                "error": str(exc),
                "type": type(exc).__name__,
                "partial_verification": counts,
                "partial_results": results,
                "artifacts": artifacts,
                "formal_admission": False,
                "trading_enabled": False,
            },
        )
        raise


def main(argv=None, *, stdout=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(
        "acquire", "freeze", "run", "check-data", "import-data", "attribute", "mine-plan", "mine",
        "diagnose", "check-evidence", "validate-prepare", "validate-run", "validate-assess",
    ))
    parser.add_argument("--protocol")
    parser.add_argument("--output")
    parser.add_argument("--data-manifest")
    parser.add_argument("--catalog", help="check-evidence / mine-plan / mine：承接已有完成研究的曝光账本")
    parser.add_argument("--freeze")
    parser.add_argument("--policy-snapshots", help="freeze / check-evidence：显式历史政策快照映射 JSON")
    parser.add_argument("--data")
    parser.add_argument("--market", choices=("crypto", "stocks"))
    parser.add_argument("--study")
    parser.add_argument("--attribution", help="已冻结的逐笔归因报告")
    parser.add_argument("--request", help="独立验证候选与窗口请求 JSON")
    parser.add_argument("--result", help="独立验证结果 JSON")
    parser.add_argument("--search-space", help="有限候选语法 JSON；省略则生成双市场共36项")
    args = parser.parse_args(argv)
    if args.policy_snapshots and args.command not in ("freeze", "check-evidence"):
        parser.error("--policy-snapshots 只适用于 freeze / check-evidence；其余入口使用冻结映射")
    if args.catalog and args.command not in ("check-evidence", "mine-plan", "mine"):
        parser.error("--catalog 只适用于 check-evidence / mine-plan / mine；独立验证使用 request.catalog")
    if args.command not in ("diagnose", "validate-run", "validate-assess") and not args.protocol:
        parser.error("此命令需要 --protocol")
    if args.command == "diagnose" and args.attribution and args.output:
        from .diagnostics import diagnose

        report = diagnose(Path(args.attribution), Path(args.output))
        result = {"output": args.output, "trading_enabled": False,
                  "replays": len(report["replays"]), "market_coverage": report["market_coverage"]}
    elif args.command == "check-evidence" and args.data_manifest and args.output:
        from .evidence import evidence_check

        result = evidence_check(args.protocol, args.data_manifest, args.output,
                                policy_snapshots_path=args.policy_snapshots, catalog_path=args.catalog)
    elif args.command == "validate-prepare" and args.study and args.request and args.output:
        from .validation import prepare

        result = prepare(Path(args.protocol), Path(args.study), Path(args.request), Path(args.output))
    elif args.command == "validate-run" and args.freeze and args.output:
        from .validation import run as validate_run

        result = validate_run(Path(args.freeze), Path(args.output))
    elif args.command == "validate-assess" and args.freeze and args.result:
        from .validation import assess

        result = assess(Path(args.freeze), Path(args.result))
    elif args.command in ("mine-plan", "mine") and args.study and args.data_manifest and args.output:
        from .mining import plan

        result = plan(args.protocol, args.study, args.data_manifest, args.output,
                      space=read(args.search_space) if args.search_space else None, catalog_path=args.catalog)
        if args.command == "mine":
            root = Path(args.output)
            mapping = result["policy_snapshots"]
            freeze(result["protocol"]["path"], result["data_manifest"]["path"], root / "execution",
                   policy_snapshots=mapping["path"] if mapping else None)
            report = run(root / "PROTOCOL.json", root / "execution/execution-freeze.json", root / "study")
            result.update(factor_pool=report["factor_pool"], verification=report["verification"],
                          execution_completed=report["execution_completed"], trading_enabled=False,
                          summary=str(root / "study/mining-summary.json"))
    elif args.command == "acquire" and args.output:
        result = acquire(args.protocol, args.output)
    elif args.command == "attribute" and args.study and args.output:
        from .attribution import attribute

        report = attribute(Path(args.protocol), Path(args.study), Path(args.output))
        result = {"unique_replays": report["unique_replays"], "output": args.output}
    elif args.command == "import-data" and args.data_manifest and args.data and args.market and args.output:
        result = import_minutes(args.protocol, args.data_manifest, args.data, args.market, args.output)
    elif args.command == "freeze" and args.data_manifest and args.output:
        result = freeze(args.protocol, args.data_manifest, args.output, policy_snapshots=args.policy_snapshots)
    elif args.command == "run" and args.freeze and args.output:
        result = run(args.protocol, args.freeze, args.output)
        result = {
            k: result[k]
            for k in ("factor_pool", "family_size", "execution_completed", "verification", "trading_enabled")
        }
    elif args.command == "check-data" and args.data and args.market:
        _, _, result = load_bars(args.data, checked_protocol(args.protocol), args.market)
    else:
        parser.error("命令所需参数不完整")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
