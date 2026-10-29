"""One-opportunity interventions from an audited baseline account; never a portfolio strategy."""

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import math

from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.entry_attribution import _equal
from quantbot.ai.local_execution import (
    account_view, advance, cancel_pending, create_plan, model_account, new_account, queue_plan,
)
from quantbot.ai.reconciliation import reconcile_replay


HYPOTHESES = [
    {"id": "btc_retest_confirmation", "market": "crypto", "horizon_sessions": 20,
     "wait_sessions": 5, "anchor_atr_tolerance": 0.5,
     "confirmation_close_above_open": True, "efficiency_floor": 0.30},
    {"id": "etf_switch_cost_coverage", "market": "stocks", "horizon_sessions": 5,
     "momentum_sessions": 20, "cost_legs": 2, "minimum_coverage": 1.0},
]


def valid_etf(values):
    return values["uptrend"] and values["momentum"] > 0 and not values["channel_exit"]


def switch_coverage(challenger, incumbent, cost_bps):
    """Frozen heuristic, not a fitted expected-return estimate."""
    if cost_bps not in (15, 30):
        raise ValueError("paired research requires 15/30 bps")
    advantage = Decimal(str(challenger["momentum"])) - Decimal(str(incumbent["momentum"]))
    return float(advantage * 5 / 20 / (Decimal(2 * cost_bps) / 10000))


def retest_state(anchor, values, bar, elapsed):
    """Only the current completed candle can confirm or invalidate the fixed anchor."""
    if elapsed > 5:
        return "expired"
    if elapsed < 1:
        return "waiting"
    level, tolerance = anchor["level"], anchor["atr"] * 0.5
    if values["close"] < level - tolerance:
        return "invalidated"
    if (level - tolerance <= float(bar["low"]) <= level + tolerance and values["close"] >= level
            and values["close"] > float(bar["open"]) and values["trend_confirmed_2"]
            and values["efficiency_ratio_10"] >= 0.30):
        return "confirmed"
    return "expired" if elapsed == 5 else "waiting"


def opportunity(market, state, context, plan, cost_bps):
    """Use submitted plans, including orders subsequently cancelled; no future fill selection."""
    buys = [o for o in plan["orders"] if o["side"] == "buy"]
    held = [s for s, q in state["positions"].items() if Decimal(q) > 0]
    if market == "crypto":
        if held or not any(o["symbol"] == "BTC/USDT" for o in buys):
            return None, "no_flat_btc_entry_plan"
        values = context.features["BTC/USDT"]
        if not (values["close"] > values["prior_high_20"] and values["trend_confirmed_2"]
                and values["efficiency_ratio_10"] >= 0.30):
            raise ValueError("baseline BTC plan does not match the frozen breakout")
        return {"target": "BTC/USDT", "anchor": {"level": values["prior_high_20"],
                                                  "atr": values["atr"]}}, None
    if not held:
        return None, "cash_entry" if buys else "flat_no_entry"
    if len(held) != 1:
        # Corporate-action replanning can cancel one leg of the original rotation.
        # The frozen hypothesis only covers origins with one incumbent.
        return None, "multiple_incumbents_outside_scope"
    incumbent = held[0]
    if not valid_etf(context.features[incumbent]):
        return None, "invalid_incumbent_exit"
    alternatives = [o for o in buys if o["symbol"] != incumbent]
    if not alternatives:
        return None, "hold_or_risk_reduction"
    sales = [o for o in plan["orders"] if o["side"] == "sell" and o["symbol"] == incumbent]
    if (len(alternatives) != 1 or len(sales) != 1
            or Decimal(str(sales[0]["quantity"])) != Decimal(state["positions"][incumbent])):
        raise ValueError("discretionary rotation needs a full incumbent sale and one challenger")
    target = alternatives[0]["symbol"]
    if not (valid_etf(context.features[target]) and context.features[target]["previous_rotation_eligible"]):
        raise ValueError("challenger does not satisfy the original two-session eligibility")
    score = switch_coverage(context.features[target], context.features[incumbent], cost_bps)
    return {"incumbent": incumbent, "target": target, "coverage_score": score}, None


def check_account(actual, expected):
    if set(actual) != set(expected):
        raise ValueError("baseline account fields changed")
    for field, value in expected.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            _equal(actual[field], value, field)
        elif actual[field] != value:
            raise ValueError("baseline account changed: " + field)


def parent_checkpoints(snapshot, records, config, spec):
    """Rebuild the original state and yield only pre-plan account checkpoints."""
    state = new_account(config["execution"]["initial_cash"])
    last_account_index = None
    for index, record in enumerate(records):
        payload = record["payload"]
        if record["kind"] == "account":
            context = snapshot.context(datetime.fromisoformat(payload["as_of"]))
            action_count = len(state["corporate_action_events"])
            orders = advance(state, snapshot, context, config, spec)
            check_account(account_view(state), payload["account"])
            if orders != payload["orders"]:
                raise ValueError("baseline orders differ during state reconstruction")
            if state["corporate_action_events"][action_count:] != payload.get("corporate_actions", []):
                raise ValueError("baseline dividend events changed")
            last_account_index = index
        elif record["kind"] == "decision":
            if payload["as_of"] != state["as_of"] or state["pending"]:
                raise ValueError("baseline plan has no clean account checkpoint")
            context = snapshot.context(datetime.fromisoformat(payload["as_of"]))
            yield state, context, payload, last_account_index + 1
            queue_plan(state, deepcopy(payload))
        elif record["kind"] == "decision_error":
            raise ValueError("frozen parent contains a decision error")
        elif record["kind"] == "orders_canceled":
            canceled = cancel_pending(state, "evaluation_end_or_risk_hold")
            if canceled != payload["orders"]:
                raise ValueError("baseline cancellation changed")
        elif record["kind"] == "completed":
            check_account(account_view(state), payload["account"])


def _target_plan(state, context, config, market, target, *, enter=False):
    current = model_account(state, context, market).payload(context)["current_weights"]
    weight = current.get(target, 0) if target else 0
    if enter:
        weight = config["risk"]["max_weight"]
    decision = DecisionBatch(tuple(Allocation(symbol, float(weight if symbol == target else 0), 1.0,
        "冻结成对事件；单次入场、原保护单与执行风控") for symbol in sorted(context.features)),
        "paired_event_probe", "fixed_local_intervention")
    return create_plan(state, context, decision, config, market, "paired_event")


def run_branch(snapshot, checkpoint, contexts, config, spec, original_plan, event, *, candidate,
               retain_original=None):
    """Carry original account state; an explicit filter retains or rejects the origin plan.

    Without an explicit filter, preserve the frozen retest/switch-cost policies.
    """
    market = spec["market"]
    horizon = 20 if market == "crypto" else 5
    if len(contexts) != horizon + 1 or contexts[0].as_of.isoformat() != checkpoint["as_of"]:
        raise ValueError("paired event requires the exact frozen horizon and origin")
    state = deepcopy(checkpoint)
    if state["pending"] or state["risk_latched"]:
        raise ValueError("cannot fork pending or halted parent state")
    initial = account_view(state)
    target = event["target"]
    if retain_original is not None and (not candidate or type(retain_original) is not bool):
        raise ValueError("an explicit origin filter requires a candidate and a boolean")
    fixed_filter = retain_original is not None
    keep = not candidate or (retain_original if fixed_filter else
                            market == "stocks" and event["coverage_score"] >= 1)
    if candidate and market == "stocks" and not keep:
        target = event["incumbent"]
    status = "waiting" if candidate and market == "crypto" and not fixed_filter else (
        "filtered" if fixed_filter and not keep else "immediate")
    attempted = status != "waiting"
    confirmed_at = None
    records, filled = [], []
    for i, context in enumerate(contexts):
        if i:
            action_count = len(state["corporate_action_events"])
            orders = advance(state, snapshot, context, config, spec)
            filled.extend(o for o in orders if o["status"] == "filled")
            records.append({"kind": "account", "payload": {"as_of": context.as_of.isoformat(),
                "account": account_view(state), "orders": orders,
                "corporate_actions": state["corporate_action_events"][action_count:],
                "valuation_prices": dict(context.prices)}})
        if i == horizon or state["risk_latched"]:
            canceled = cancel_pending(state, "evaluation_end_or_risk_hold")
            if canceled:
                records.append({"kind": "orders_canceled", "payload": {"orders": canceled}})
            continue
        if i == 0 and (not candidate or keep and (market == "stocks" or fixed_filter)):
            plan = deepcopy(original_plan)
        else:
            selected, enter = None, False
            if state["positions"].get(target):
                values = context.features[target]
                exiting = values["close"] < values["prior_low_10"] if market == "crypto" else not valid_etf(values)
                selected = None if exiting else target
            elif market == "crypto" and candidate and not attempted:
                if i:
                    bars = snapshot.bars(contexts[i-1].as_of, context.as_of)
                    if len(bars) != 1:
                        raise ValueError("confirmation needs one complete session")
                    status = retest_state(event["anchor"], context.features[target], bars[0][1][target], i)
                    if status == "confirmed":
                        selected, enter, keep, attempted = target, True, True, True
                        confirmed_at = context.as_of.isoformat()
                    elif status != "waiting":
                        attempted = True
            plan = _target_plan(state, context, config, market, selected, enter=enter)
        queue_plan(state, plan)
        records.append({"kind": "decision", "payload": {"as_of": context.as_of.isoformat(),
            "event_phase": status, "plan": deepcopy(plan)}})
    final = account_view(state)
    sell_cost = sum(float(q) * context.prices[s] for s, q in state["positions"].items()) \
        * config["execution"]["cost_bps"] / 10000
    pnl = final["equity"] - initial["equity"] - sell_cost
    if not math.isfinite(pnl):
        raise ValueError("non-finite event contribution")
    return {"records": records, "final_account": final,
            "initial_equity": initial["equity"], "net_pnl": pnl,
            "account_contribution": pnl / config["execution"]["initial_cash"],
            "estimated_final_sell_cost": sell_cost, "remaining_positions": final["positions"],
            "incremental_costs": final["estimated_execution_cost"] - initial["estimated_execution_cost"],
            "factor_kept": keep, "confirmed_at": confirmed_at, "event_phase": status,
            "target_buy_filled": any(o["side"] == "buy" and o["symbol"] == event["target"] for o in filled),
            "risk_hold": state["risk_latched"]}


def audit_branch(parent_records, prefix_end, branch):
    """Independent cash-flow reconciliation includes the genuine pre-fork history."""
    combined = parent_records[:prefix_end] + branch["records"]
    result = reconcile_replay(combined)
    if result["status"] != "reconciled":
        raise ValueError("paired branch failed independent reconciliation")
    return {"status": result["status"], "account_snapshots": result["account_snapshots"],
            "prefix_account_snapshots": sum(r["kind"] == "account" for r in parent_records[:prefix_end]),
            "branch_account_snapshots": sum(r["kind"] == "account" for r in branch["records"]),
            "discrepancies": result["discrepancies"]}
