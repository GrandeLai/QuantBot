"""Read-only opportunity/cost diagnostics from frozen, reconciled replay evidence."""

from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal as D
import gzip
import json
from pathlib import Path
from statistics import median

from quantbot.evidence import hash_payload, source_tree_hash
from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from .attribution import describe, episodes


IDENTITY = ("market", "factor", "cost_bps", "latency_seconds", "window")
# Fixed descriptive buckets, not an optimization grid or tradable regime classifier.
CONTEXT_FIELDS = ("relative_volume", "signal_body_bps", "entry_chase_bps")
TIMING_FIELDS = ("entry_execution_time_basis", "exit_execution_time_basis", "exit_bar_open")


def _identity(row):
    return tuple(row[k] for k in IDENTITY)


def _episode_key(row):
    return row["symbol"], row["signal_at"]


def _index(rows, key, label):
    result = {}
    for row in rows:
        own = key(row)
        if own in result:
            raise ValueError("重复" + label)
        result[own] = row
    return result


def _equal_number(actual, expected, label):
    a, b = D(str(actual)), D(str(expected))
    if not a.is_finite() or not b.is_finite() or abs(a - b) > D("1e-8"):
        raise ValueError("归因与成交工件不一致：" + label)


def _completed_close_context(row, prior):
    """Keep ledger evidence usable without endorsing legacy ambiguous extrema."""
    version = prior.get("completed_close_evidence_version")
    if version is None:
        known = (row["entry_execution_time_basis"] == "bar_open"
                 and row["exit_execution_time_basis"] == "bar_open")
        row["completed_close_evidence_status"] = (
            "legacy_exact_exit" if known else "legacy_execution_timing_ambiguous")
        row["max_completed_close_gain_bps"] = prior.get("max_completed_close_gain_bps") if known else None
    else:
        if version != 1:
            raise ValueError("不支持的已完成收盘证据版本")
        cutoff, status = None, "unknown_execution_timing"
        if row["entry_execution_time_basis"] == "bar_open":
            if row["exit_execution_time_basis"] == "bar_open":
                cutoff, status = row["exit_at"], "exact_exit"
            elif row["exit_execution_time_basis"] == "intrabar_time_unknown" and row["exit_bar_open"]:
                cutoff, status = row["exit_bar_open"], "bounded_before_intrabar_exit"
        cutoff = datetime.fromisoformat(cutoff) if cutoff else None
        recorded_cutoff = prior["completed_close_cutoff_at"]
        recorded_cutoff = datetime.fromisoformat(recorded_cutoff) if recorded_cutoff else None
        if prior["completed_close_timing_status"] != status or recorded_cutoff != cutoff:
            raise ValueError("已完成收盘边界与成交时间依据不一致")
        count, gain, maximum_at = (prior[key] for key in (
            "completed_close_count", "max_completed_close_gain_bps", "max_completed_close_at"))
        if cutoff is None:
            valid = count is None and gain is None and maximum_at is None
        elif isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            valid = (gain is None and maximum_at is None) if count == 0 else (
                gain is not None and maximum_at is not None
                and datetime.fromisoformat(row["entry_at"]) < datetime.fromisoformat(maximum_at) <= cutoff)
        else:
            valid = False
        if not valid:
            raise ValueError("已完成收盘数量或极值时间与边界不一致")
        row.update({key: prior[key] for key in (
            "completed_close_evidence_version", "completed_close_count", "completed_close_cutoff_at",
            "max_completed_close_at", "completed_close_timing_status", "max_completed_close_gain_bps")})
        row["completed_close_evidence_status"] = status
    gain = row["max_completed_close_gain_bps"]
    if gain is not None and not D(str(gain)).is_finite():
        raise ValueError("已完成收盘极值必须为有限值")


def _verified_episodes(result, recorded):
    rebuilt = episodes(result)  # Audits fills, shared capacity, costs, cash and residual positions.
    old = _index(recorded, lambda r: r["entry_order"], "归因持仓周期")
    if len(old) != len(rebuilt):
        raise ValueError("归因持仓周期数量与成交工件不一致")
    numeric = {"quantity", "bought", "net_pnl", "cost", "entry_reference_notional",
               "gross_contribution", "gross_bps", "round_trip_cost_bps", "net_bps", "entry_reference"}
    for row in rebuilt:
        prior = old.get(row["entry_order"])
        if prior is None:
            raise ValueError("归因缺少成交持仓周期")
        for field, value in row.items():
            # Old attribution schemas omit these; derive them from frozen fills.
            if field in TIMING_FIELDS and field not in prior:
                continue
            if field in numeric:
                _equal_number(prior[field], value, field)
            elif prior[field] != value:
                raise ValueError("归因与成交工件不一致：" + field)
        # Descriptive context is bound to the attribution hash; no price data is reopened.
        row.update({key: prior[key] for key in CONTEXT_FIELDS if key in prior})
        _completed_close_context(row, prior)
    return rebuilt


def _metrics(rows):
    result = describe(rows)
    reference = sum((D(row["entry_reference_notional"]) for row in rows), D(0))
    gross = sum((D(row["gross_contribution"]) for row in rows), D(0))
    cost = sum((D(row["cost"]) for row in rows), D(0))
    holding = [(datetime.fromisoformat(r["exit_at"]) - datetime.fromisoformat(r["entry_at"])).total_seconds() / 60
               for r in rows]
    hindsight = [r["max_completed_close_gain_bps"] for r in rows
                 if r.get("max_completed_close_gain_bps") is not None]
    coverage = {"below_recorded_cost": 0, "reached_recorded_cost": 0, "unknown": 0}
    unknown = Counter()
    for row in rows:
        if D(row["net_pnl"]) >= 0:
            continue
        gain = row.get("max_completed_close_gain_bps")
        if row["fill_count"] != 2:
            reason = "multiple_fills"
        elif row["completed_close_evidence_status"] in (
                "legacy_execution_timing_ambiguous", "unknown_execution_timing"):
            reason = "execution_timing_ambiguous"
        elif gain is None:
            reason = "no_completed_close_evidence"
        else:
            reason = None
        if reason:
            coverage["unknown"] += 1
            unknown[reason] += 1
        else:
            label = "reached_recorded_cost" if D(str(gain)) >= D(row["round_trip_cost_bps"]) else "below_recorded_cost"
            coverage[label] += 1
    result.update(
        entry_reference_notional=float(reference),
        fill_count=sum(r["fill_count"] for r in rows),
        net_losers=sum(D(r["net_pnl"]) < 0 for r in rows),
        net_flat=sum(D(r["net_pnl"]) == 0 for r in rows),
        weighted_recorded_round_trip_cost_bps=float(10000 * cost / reference) if reference else None,
        break_even_round_trip_cost_bps=float(10000 * gross / reference) if reference else None,
        break_even_recorded_cost_multiplier=float(gross / cost) if cost else None,
        cost_to_positive_gross_ratio=float(cost / gross) if gross > 0 else None,
        positive_gross_available=gross > 0,
        median_holding_minutes=median(holding) if holding else None,
        median_hindsight_completed_close_gain_bps=median(hindsight) if hindsight else None,
        completed_close_evidence_status=dict(Counter(r["completed_close_evidence_status"] for r in rows)),
        losing_completed_close_cost_coverage={**coverage, "unknown_reasons": dict(unknown)},
    )
    return result


def _groups(rows):
    grouped = defaultdict(lambda: defaultdict(list))
    missing = Counter()
    for row in rows:
        grouped["symbol"][row["symbol"]].append(row)
        at = datetime.fromisoformat(row["signal_at"]).astimezone(UTC)
        start = at.hour // 6 * 6
        grouped["signal_utc_six_hour_bucket"][f"{start:02d}-{start + 6:02d}"].append(row)
        for field in CONTEXT_FIELDS:
            value = row.get(field)
            if value is None:
                label = "unavailable"
                missing[field] += 1
            elif field == "relative_volume":
                label = "below_1" if value < 1 else "1_to_below_1.5" if value < 1.5 else "at_least_1.5"
            else:
                label = "positive" if value > 0 else "nonpositive"
            grouped[field][label].append(row)
    return {name: {label: _metrics(values) for label, values in groups.items()}
            for name, groups in grouped.items()}, dict(missing)


def _paired(candidate, control):
    own = _index(candidate, _episode_key, "候选信号时点")
    base = _index(control, _episode_key, "对照信号时点")
    common = sorted(own.keys() & base.keys())
    only_own = [own[key] for key in sorted(own.keys() - base.keys())]
    only_base = [base[key] for key in sorted(base.keys() - own.keys())]
    shared_delta = sum((D(own[key]["net_pnl"]) - D(base[key]["net_pnl"]) for key in common), D(0))
    delta = (sum((D(r["net_pnl"]) for r in candidate), D(0))
             - sum((D(r["net_pnl"]) for r in control), D(0)))
    decomposition = (shared_delta + sum((D(r["net_pnl"]) for r in only_own), D(0))
                     - sum((D(r["net_pnl"]) for r in only_base), D(0)))
    _equal_number(delta, decomposition, "配对损益分解")
    return {
        "matching_key": ["symbol", "signal_at"],
        "matched_episodes": len(common),
        "matched_net_pnl_difference": float(shared_delta),
        "candidate_only": _metrics(only_own),
        "control_only": _metrics(only_base),
        "control_only_profitable": _metrics([r for r in only_base if D(r["net_pnl"]) > 0]),
        "control_only_losing": _metrics([r for r in only_base if D(r["net_pnl"]) < 0]),
        "unmatched_control_opportunities": [
            {key: row[key] for key in ("symbol", "signal_at", "entry_at", "exit_at", "net_pnl", "cost")}
            for row in only_base
        ],
        "observed_account_net_pnl_difference": float(delta),
        "causal_increment": None,
        "interpretation": "same-signal pairing only; unmatched control opportunities may reflect filtering, "
                          "delay, cooldown, sizing or risk path dependence; not proven causal effects",
    }


def diagnose(attribution_path, output):
    """Audit frozen evidence and describe costs; never rerun, select or admit a strategy."""
    attribution_path, output = Path(attribution_path), Path(output)
    source_hash = source_tree_hash()
    inputs = [file_ref(attribution_path)]
    attribution = read(check_ref(inputs[0]))
    study_path = check_ref(attribution["study"])
    study = read(study_path)
    frozen = read(check_ref(attribution["freeze"]))
    protocol = read(check_ref(attribution["protocol"]))
    started_ref = file_ref(study_path.parent / "started.json")
    started = read(started_ref["path"])
    inputs.extend([attribution["study"], attribution["freeze"], attribution["protocol"], started_ref])
    if started["freeze"] != attribution["freeze"] or frozen["protocol"] != attribution["protocol"]:
        raise ValueError("归因、研究和冻结方案引用不一致")
    if attribution.get("sealed_holdout_evaluated") or study.get("sealed_holdout_evaluated"):
        raise ValueError("机会成本诊断只接受开发窗口工件")
    windows = {row["id"] for row in protocol["windows"]}
    cards = {(row["market"], row["factor"]): row for row in protocol["factors"]}
    summaries = _index(attribution["summaries"], _identity, "归因回放汇总")
    recorded = defaultdict(list)
    for row in attribution["episodes"]:
        recorded[_identity(row)].append(row)
    replays, digests, pairs = {}, {}, []
    for reference in study["artifacts"]:
        with gzip.open(check_ref(reference), "rt", encoding="utf-8") as handle:
            pair = json.load(handle)
        inputs.append(reference)
        candidate = pair["candidate"]
        card = cards[(candidate["market"], candidate["factor"])]
        if protocol.get("original_control_required") and "original_control" not in pair:
            raise ValueError("冻结方案要求原对照工件")
        if pair["control"]["factor"] != card["control"]:
            raise ValueError("成交工件未使用冻结对照")
        if "original_control" in pair and pair["original_control"]["factor"] != "control":
            raise ValueError("成交工件原对照身份错误")
        scope = tuple(candidate[k] for k in IDENTITY if k != "factor")
        for result in pair.values():
            identity = _identity(result)
            if (result["window"] not in windows
                    or tuple(result[k] for k in IDENTITY if k != "factor") != scope):
                raise ValueError("候选与对照窗口、成本或延迟不一致")
            # Identical controls recur in many pair files; reject conflicting duplicates.
            digest = hash_payload(result)
            if identity in digests:
                if digests[identity] != digest:
                    raise ValueError("同一回放身份对应不同成交证据")
                continue
            digests[identity] = digest
            rows = _verified_episodes(result, recorded[identity])
            summary = summaries[identity]
            for key, value in describe(rows).items():
                if isinstance(value, (float, int)):
                    _equal_number(summary[key], value, key)
                elif summary[key] != value:
                    raise ValueError("归因汇总与成交工件不一致：" + key)
            net = D(str(result["account"]["cash"])) - D(str(result["initial_equity"]))
            _equal_number(result["net_return"], net / D(str(result["initial_equity"])), "账户收益率")
            groups, missing = _groups(rows)
            components = defaultdict(lambda: D(0))
            for event in result["events"]:
                if event["status"] == "filled":
                    for name, amount in event["cost_components"].items():
                        components[name] += D(amount)
            replays[identity] = {
                **dict(zip(IDENTITY, identity)), "artifact": reference, "replay_digest": digest,
                "candidate_id": cards[(identity[0], identity[1])]["id"]
                if (identity[0], identity[1]) in cards else None,
                "initial_equity": result["initial_equity"], "net_return": result["net_return"],
                "risk_hold": result["risk_hold"], "attempts": len(result["attempts"]),
                "accepted_attempts": sum(a["accepted"] for a in result["attempts"]),
                "metrics": _metrics(rows), "descriptive_groups": groups,
                "execution_cost_components": {name: float(amount) for name, amount in components.items()},
                "missing_context_episodes": missing, "rows": rows,
            }
        pairs.append((card, {role: _identity(result) for role, result in pair.items()}, reference))
    if set(replays) != set(summaries) or set(recorded) - set(replays):
        raise ValueError("归因与研究回放覆盖范围不一致")
    if len(replays) != attribution["unique_replays"]:
        raise ValueError("归因回放数量与工件不一致")
    comparisons, seen_pairs = [], set()
    for card, branches, reference in pairs:
        own = replays[branches["candidate"]]
        for role in ("control", "original_control"):
            if role not in branches:
                continue
            identity = (branches["candidate"], branches[role], role)
            if identity in seen_pairs:
                raise ValueError("重复候选对照场景")
            seen_pairs.add(identity)
            base = replays[branches[role]]
            if own["initial_equity"] != base["initial_equity"]:
                raise ValueError("候选与对照初始资金不一致")
            comparisons.append({
                "candidate_id": card["id"], **{k: own[k] for k in IDENTITY},
                "control_role": role, "control_factor": base["factor"], "artifact": reference,
                **_paired(own["rows"], base["rows"]),
            })
    grouped = defaultdict(list)
    for identity, result in replays.items():
        grouped[identity[:-1]].append(result)
    scenarios = []
    for identity, values in grouped.items():
        rows = [row for value in values for row in value["rows"]]
        initial = sum(D(str(value["initial_equity"])) for value in values)
        metrics = _metrics(rows)
        groups, missing = _groups(rows)
        scenarios.append({
            **dict(zip(IDENTITY[:-1], identity)), "windows": [v["window"] for v in values],
            "candidate_id": values[0]["candidate_id"],
            "positive_windows": sum(v["net_return"] > 0 for v in values), "metrics": metrics,
            "descriptive_groups": groups, "missing_context_episodes": missing,
            "initial_equity_sum": float(initial),
            "pooled_separate_account_return": float(D(str(metrics["net_pnl"])) / initial),
            "aggregation": "sum of cash PnL across separate initial accounts; not compounded return",
        })
    market_coverage = {}
    for market in protocol["markets"]:
        own = [r for r in replays.values() if r["market"] == market]
        blocked = [r for r in study["results"] if r["market"] == market and r["status"] == "blocked_data"]
        market_coverage[market] = {
            "unique_replays": len(own), "status": "diagnostic_only" if own else "blocked_data",
            "blocked_candidates": [{"id": r["id"], "reasons": r["reasons"]} for r in blocked],
            "recorded_data_quality": frozen["quality"][market],
            "regime_classification": "unavailable; fixed signal context is not an independently validated regime",
        }
    for result in replays.values():
        del result["rows"]
    report = {
        "schema": "intraday_opportunity_cost_diagnostics_v1", "created_at": datetime.now(UTC).isoformat(),
        "attribution": inputs[0], "study": attribution["study"], "freeze": attribution["freeze"],
        "protocol": attribution["protocol"],
        "source_identity": {"historical_execution_source_hash": frozen["source_hash"],
                            "analyzer_source_hash": source_hash,
                            "historical_source_reexecuted": False},
        "verification": {"unique_replays": len(replays), "artifact_references": len(study["artifacts"]),
                         "flat_to_flat_episodes": sum(r["metrics"]["episodes"] for r in replays.values()),
                         "ledger_status": "reconciled", "raw_market_data_read": False},
        "market_coverage": market_coverage, "replays": list(replays.values()),
        "scenarios": scenarios, "comparisons": comparisons,
        "definitions": {
            "gross_contribution": "net cash PnL plus recorded execution costs; not a zero-cost replay",
            "break_even_round_trip_cost_bps": "10000 * gross contribution / entry reference notional; "
                                               "negative values mean no nonnegative cost is affordable",
            "break_even_recorded_cost_multiplier": "gross / recorded cost with fills held fixed; not an executable fee quote",
            "cost_to_positive_gross_ratio": "recorded cost / aggregate gross contribution; null when gross <= 0",
            "descriptive_groups": "fixed before this diagnostic: symbol, UTC six-hour bucket, relative volume "
                                  "below 1 / 1 to below 1.5 / at least 1.5, and signal-body/entry-chase sign",
            "losing_completed_close_cost_coverage": "single-entry/single-exit net losers only: compare "
                "observed completed-close gain with final recorded round-trip cost; equal means reached, "
                "not profitable; ambiguous timing, multiple fills and absent observations stay unknown",
        },
        "limitations": [
            "previously exposed development evidence only; no independent profitability validation",
            "continuous-account cooldown, risk and sizing cause path dependence; pair differences are not causal alpha",
            "hindsight completed-close extrema are descriptive and are not achievable exit returns",
            "legacy intrabar exits have no verified holding-period close extrema; their metrics are withheld",
            "completed-close sampling cannot prove an intrabar opportunity never existed; final recorded "
            "cost is not the cost of an alternative exit, and extrema do not establish event order",
            "alternatives, cost/latency scenarios and overlapping episodes must not be summed into a portfolio return",
            "historical market data and contextual features are hash-bound to prior artifacts, not reverified from prices",
            "actual broker fees, model/data/infrastructure cost and live execution remain unverified",
        ],
        "qualified": False, "formal_admission": False, "trading_enabled": False,
        "sealed_holdout_evaluated": False, "model_calls": 0, "broker_calls": 0,
    }
    # Freeze the inputs throughout the read-only analysis, including repeated artifact references.
    for reference in {r["path"]: r for r in inputs}.values():
        check_ref(reference)
    if source_tree_hash() != source_hash:
        raise ValueError("诊断过程中分析器源码发生变化")
    save(output, report)
    return report
