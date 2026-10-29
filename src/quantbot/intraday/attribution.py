"""Explain frozen fills without rerunning or optimizing the old strategies."""

from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal as D
import gzip
import json

import numpy as np
import pandas as pd

from quantbot.factors.conditional_study import check_ref, file_ref, read, save
from .data import checked_protocol, frozen_policy_snapshots, load_bars
from .research import audit_report, prepare_sessions


def episodes(result):
    """Pair partial fills into flat-to-flat episodes and reconcile realized PnL."""
    audit_report(result)
    opened, closed = {}, []
    for event in result["events"]:
        if event["status"] != "filled":
            continue
        symbol, side = event["symbol"], event["side"]
        quantity = D(event["quantity"])
        if side == "buy" and symbol not in opened:
            opened[symbol] = {
                "symbol": symbol, "entry_order": event["client_order_id"],
                "signal_at": event["created_at"], "entry_at": event["at"],
                "entry_execution_time_basis": event.get("execution_time_basis", "unknown"),
                "quantity": D(0), "bought": D(0), "net_pnl": D(0), "cost": D(0),
                "entry_reference_notional": D(0), "exit_reasons": [], "fill_count": 0,
            }
        row = opened[symbol]
        if side == "buy" and row["entry_order"] != event["client_order_id"]:
            raise ValueError("归因不支持同一仓位混入不同入场订单")
        sign = 1 if side == "buy" else -1
        row["quantity"] += sign * quantity
        row["net_pnl"] -= sign * quantity * D(event["price"])
        row["cost"] += D(event["estimated_cost"])
        row["fill_count"] += 1
        if side == "buy":
            row["bought"] += quantity
            row["entry_reference_notional"] += quantity * D(event["reference_price"])
        else:
            row["exit_reasons"].append(event["reason"])
        if not row["quantity"]:
            row["exit_at"] = event["at"]
            row["exit_execution_time_basis"] = event.get("execution_time_basis", "unknown")
            row["exit_bar_open"] = event.get("bar_open")
            row["gross_contribution"] = row["net_pnl"] + row["cost"]
            row["gross_bps"] = 10000 * row["gross_contribution"] / row["entry_reference_notional"]
            row["round_trip_cost_bps"] = 10000 * row["cost"] / row["entry_reference_notional"]
            row["net_bps"] = 10000 * row["net_pnl"] / row["entry_reference_notional"]
            row["entry_reference"] = row["entry_reference_notional"] / row["bought"]
            row["exit_reasons"] = sorted(set(row["exit_reasons"]))
            closed.append({k: str(v) if isinstance(v, D) else v for k, v in row.items()})
            del opened[symbol]
    if opened or result["account"]["positions"]:
        raise ValueError("残仓不能伪装成已实现盈亏归因")
    net = sum((D(row["net_pnl"]) for row in closed), D(0))
    if abs(net - (D(str(result["account"]["cash"])) - D(str(result["initial_equity"])))) > D("1e-8"):
        raise ValueError("逐笔归因与冻结账户损益不一致")
    return closed


def describe(rows):
    def total(key):
        return float(sum((D(row[key]) for row in rows), D(0)))

    return {
        "episodes": len(rows), "net_pnl": total("net_pnl"), "cost": total("cost"),
        "gross_contribution": total("gross_contribution"),
        "net_winners": sum(D(r["net_pnl"]) > 0 for r in rows),
        "gross_winners": sum(D(r["gross_contribution"]) > 0 for r in rows),
        "gross_winners_erased_by_cost": sum(D(r["gross_contribution"]) > 0 >= D(r["net_pnl"]) for r in rows),
        "exit_reasons": dict(Counter(reason for r in rows for reason in r["exit_reasons"])),
        "median_gross_bps": float(np.median([float(r["gross_bps"]) for r in rows])) if rows else None,
        "median_round_trip_cost_bps": float(np.median([float(r["round_trip_cost_bps"]) for r in rows])) if rows else None,
    }


def explain_signals(rows, prepared, factor):
    """Descriptive completed-bar evidence; not a tradable best-exit counterfactual."""
    signals = {at: values for day in prepared.values() for at, values in day["signals"].items()}
    bars = {at: values for day in prepared.values() for at, values in day["bars"]}
    base_times = {
        (day, symbol): at for day, own in prepared.items()
        for at, values in own["signals"].items() for symbol, v in values.items() if v["base"]
    }
    for row in rows:
        at, symbol = pd.Timestamp(row["signal_at"]), row["symbol"]
        value = signals[at][symbol]
        entry, exit_at = pd.Timestamp(row["entry_at"]), pd.Timestamp(row["exit_at"])
        own_day = next(day for day, own in prepared.items() if own["opening"] <= at < own["closing"])
        cutoff, timing_status = None, "unknown_execution_timing"
        if row.get("entry_execution_time_basis") == "bar_open":
            if row.get("exit_execution_time_basis") == "bar_open":
                cutoff, timing_status = exit_at, "exact_exit"
            elif row.get("exit_execution_time_basis") == "intrabar_time_unknown" and row.get("exit_bar_open"):
                # The event's at is the bar end, not the unknown intrabar fill time.
                cutoff = pd.Timestamp(row["exit_bar_open"])
                timing_status = "bounded_before_intrabar_exit"
        completed = [(t + pd.Timedelta(minutes=5), float(b[symbol]["close"]))
                     for t, b in prepared[own_day]["bars"]
                     if cutoff is not None and entry < t + pd.Timedelta(minutes=5) <= cutoff]
        closes = [close for _, close in completed]
        highest = max(completed, key=lambda item: item[1]) if completed else None
        group = [bars[at - pd.Timedelta(minutes=step)][symbol] for step in (15, 10, 5)]
        high, low = max(float(b["high"]) for b in group), min(float(b["low"]) for b in group)
        breakout_at = base_times.get((own_day, symbol))
        if factor in ("vwap_reclaim", "vwap_trend_reclaim"):
            reference_name, reference = "signal_session_vwap", value["session_vwap"]
            delay = None
        elif factor in ("range_breakdown", "failed_breakdown_reversal"):
            reference_name, reference = "prior_hour_low", value["prior_hour_low"]
            delay = None
        else:
            reference_name, reference = "opening_range_high", value["opening_high"]
            delay = (at - breakout_at).total_seconds() / 60 if breakout_at is not None else None
        row.update(
            relative_volume=value["relative_volume"],
            confirmation_delay_minutes=delay,
            signal_reference=reference_name,
            signal_reference_price=reference,
            reclosed_below_signal_reference=any(c <= reference for c in closes) if closes else None,
            signal_body_bps=(value["close"] / float(group[0]["open"]) - 1) * 10000,
            close_location=(value["close"] - low) / (high - low) if high > low else None,
            opening_range_extension_bps=(value["close"] / value["opening_high"] - 1) * 10000,
            entry_chase_bps=(float(row["entry_reference"]) / value["close"] - 1) * 10000,
            reclosed_below_opening_high=any(c <= value["opening_high"] for c in closes) if closes else None,
            completed_close_evidence_version=1,
            completed_close_count=len(completed) if cutoff is not None else None,
            completed_close_cutoff_at=cutoff.isoformat() if cutoff is not None else None,
            completed_close_timing_status=timing_status,
            max_completed_close_at=highest[0].isoformat() if highest else None,
            max_completed_close_gain_bps=(highest[1] / float(row["entry_reference"]) - 1) * 10000
            if highest else None,
        )


def attribute(protocol_path, study_path, output):
    study = read(study_path)
    started = read(study_path.parent / "started.json")
    frozen = read(check_ref(started["freeze"]))
    snapshots = frozen_policy_snapshots(frozen)
    protocol = (checked_protocol(protocol_path, policy_snapshots=snapshots) if snapshots
                else checked_protocol(protocol_path))
    if frozen["protocol"] != file_ref(protocol_path):
        raise ValueError("归因报告和冻结方案不匹配")
    from .evidence import check_frozen_action_refs, frozen_replay_actions

    check_frozen_action_refs(frozen)
    prepared = {}
    for market, reference in frozen["data"].items():
        if reference and frozen["quality"][market]["complete"]:
            frame, sessions, _ = load_bars(check_ref(reference), protocol, market)
            actions = frozen_replay_actions(frozen, protocol, market, frame, sessions)
            prepared[market] = prepare_sessions(
                frame, sessions, protocol["markets"][market]["symbols"],
                atr_window=protocol["markets"][market]["strategy"]["atr_window"], actions=actions,
            )
    seen, summaries, all_rows = set(), [], []
    for reference in study["artifacts"]:
        with gzip.open(check_ref(reference), "rt", encoding="utf-8") as handle:
            pair = json.load(handle)
        for result in pair.values():
            identity = tuple(result[k] for k in ("market", "factor", "cost_bps", "latency_seconds", "window"))
            if identity in seen:
                continue
            seen.add(identity)
            rows = episodes(result)
            explain_signals(rows, prepared[result["market"]], result.get("recipe", {}).get("seed", result["factor"]))
            keys = {k: result[k] for k in ("market", "factor", "cost_bps", "latency_seconds", "window")}
            for row in rows:
                row.update(keys)
            grouped = defaultdict(list)
            for row in rows:
                grouped[row["symbol"]].append(row)
            summaries.append({**keys, **describe(rows), "by_symbol": {s: describe(rs) for s, rs in grouped.items()},
                "attempts": len(result["attempts"]),
                "accepted_attempts": sum(a["accepted"] for a in result["attempts"]),
                "canceled": dict(Counter(e["reason"] for e in result["events"] if e["status"] == "canceled")),
                "risk_hold": result["risk_hold"],
                "reclosed_below_opening_high": sum(r["reclosed_below_opening_high"] is True for r in rows),
                "reclosed_below_opening_high_unknown": sum(r["reclosed_below_opening_high"] is None for r in rows),
                "reclosed_below_signal_reference": sum(r["reclosed_below_signal_reference"] is True for r in rows),
                "reclosed_below_signal_reference_unknown": sum(r["reclosed_below_signal_reference"] is None for r in rows),
                "median_signal_body_bps": float(np.median([r["signal_body_bps"] for r in rows])) if rows else None,
                "median_entry_chase_bps": float(np.median([r["entry_chase_bps"] for r in rows])) if rows else None,
            })
            all_rows += rows
    report = {
        "created_at": datetime.now(UTC).isoformat(), "protocol": file_ref(protocol_path),
        "study": file_ref(study_path), "freeze": started["freeze"], "unique_replays": len(seen),
        "summaries": summaries, "episodes": all_rows,
        "limitations": ["gross contribution adds recorded costs back; not a zero-cost replay",
            "continuous account cooldown and risk cause path dependence; unmatched trades are not causal increments",
            "completed close extrema are descriptive, not achievable intrabar exits",
            "unknown intrabar exits exclude the exit bar close; missing timing or completed closes remains unknown",
            "previously exposed development windows; no new independent validation"],
        "formal_admission": False, "trading_enabled": False,
    }
    snapshots = frozen_policy_snapshots(frozen)
    if snapshots:
        checked_protocol(protocol_path, policy_snapshots=snapshots)
    else:
        checked_protocol(protocol_path)
    check_frozen_action_refs(frozen)
    save(output, report)
    return report
