"""Reconciled economic diagnostics of existing runs; no new targets or simulated fills."""

from collections import Counter
from datetime import UTC
from decimal import Decimal as D
from statistics import mean, median

from .entry_attribution import _equal, _load, _time, reconstruct_entries
from .market import EASTERN


HORIZONS = (5, 10)


def _day(value, market):
    return _time(value).astimezone(EASTERN if market == "stocks" else UTC).date().isoformat()


def _post_exit(accounts, index, symbol, market):
    """Start at the exit session CLOSE, excluding unknown exit-day intrabar ordering."""
    result = {}
    for horizon in HORIZONS:
        if index + horizon >= len(accounts):
            result[str(horizon)] = {"status": "right_censored", "total_return": None}
            continue
        growth = D(1)
        for previous, current in zip(accounts[index:index+horizon], accounts[index+1:index+horizon+1]):
            dividend = sum((D(str(e["action"]["amount_per_share"]))
                            for e in current.get("corporate_actions", [])
                            if e["kind"] == "dividend_entitlement" and e["action"]["symbol"] == symbol), D(0))
            # Account events only include dividends earned by this account. The caller supplies
            # issuer actions below, including ex-dates after the position has been sold.
            if "diagnostic_dividends" in current:
                dividend = current["diagnostic_dividends"].get(symbol, D(0))
            growth *= (D(str(current["valuation_prices"][symbol])) + dividend) / D(
                str(previous["valuation_prices"][symbol]))
        result[str(horizon)] = {"status": "observed", "total_return": float(growth - 1),
                               "end": accounts[index+horizon]["as_of"]}
    return {"start": accounts[index]["as_of"], "basis": "exit_session_close_total_return",
            "hypothetical_price_diagnostic_not_trade_pnl": True, "horizons": result}


def episode_paths(records, episodes, cost_bps, market):
    """Observe cash-flow PnL at fills and held closes; intraday MFE/MAE are not identifiable."""
    accounts = [dict(r["payload"]) for r in records if r["kind"] == "account"]
    dividends = {}
    for action in records[0]["payload"].get("corporate_actions", []):
        key = (action["date"], action["symbol"])
        dividends[key] = dividends.get(key, D(0)) + D(str(action["amount_per_share"]))
    for row in accounts:
        day = _day(row["as_of"], market)
        row["diagnostic_dividends"] = {s: value for (d, s), value in dividends.items() if d == day}
    by_entry = {e["entry_order_id"]: e for e in episodes}
    active, states = {}, []
    rate = D(str(cost_bps)) / 10000

    def observe(state, price, index, phase):
        net = state["cash_flow"] + state["dividends"] + state["quantity"] * price * (1 - rate)
        state["observations"].append({"as_of": accounts[index]["as_of"], "phase": phase,
                                      "net_liquidation_pnl": float(net),
                                      "sessions_after_entry": index - state["entry_index"]})

    for index, row in enumerate(accounts):
        timeline = [(o["at"], 1, "fill", o) for o in row["orders"] if o["status"] == "filled"]
        timeline += [(e["at"], 0, "dividend", e) for e in row.get("corporate_actions", [])
                     if e["kind"] == "dividend_entitlement"]
        timeline.sort(key=lambda e: (_time(e[0]), e[1]))
        for _, _, kind, event in timeline:
            if kind == "dividend":
                if D(str(event["amount"])):
                    active[event["action"]["symbol"]]["dividends"] += D(str(event["amount"]))
                continue
            symbol, quantity = event["symbol"], D(str(event["quantity"]))
            if event["side"] == "buy" and symbol not in active:
                state = {"episode": by_entry[event["client_order_id"]], "entry_index": index,
                         "cash_flow": D(0), "dividends": D(0), "quantity": D(0),
                         "observations": [], "intrabar_exit_time_unknown": False}
                active[symbol] = state
                states.append(state)
            state = active[symbol]
            sign = 1 if event["side"] == "buy" else -1
            state["quantity"] += sign * quantity
            state["cash_flow"] -= sign * quantity * D(str(event["price"]))
            observe(state, D(str(event["reference_price"])), index, "fill")
            if event.get("execution_time_basis") == "intrabar_time_unknown":
                state["intrabar_exit_time_unknown"] = True
            if not state["quantity"]:
                _equal(state["cash_flow"] + state["dividends"], state["episode"]["net_pnl"], "economic episode")
                state["post_exit"] = _post_exit(accounts, index, symbol, market)
                del active[symbol]
        for symbol, state in active.items():
            observe(state, D(str(row["valuation_prices"][symbol])), index, "held_close")
    if len(states) != len(episodes):
        raise ValueError("economic paths do not cover every reconciled episode")
    output = []
    for state in states:
        episode, observations = state["episode"], state["observations"]
        pnl = [row["net_liquidation_pnl"] for row in observations]
        first = next((row for row in observations if row["net_liquidation_pnl"] > 0), None)
        closes = [row for row in observations if row["phase"] == "held_close"]
        if episode["exit_at"] is None:
            _equal(pnl[-1], episode["net_pnl"] - episode["estimated_final_sell_cost"], "open economic episode")
        notional = episode["entry_reference_notional"]
        output.append({**episode, "observed_mfe_net_fraction": max(0, max(pnl)) / notional,
            "observed_mae_net_fraction": min(0, min(pnl)) / notional,
            "first_positive_observation": first, "held_close_count": len(closes),
            "negative_held_close_count": sum(row["net_liquidation_pnl"] < 0 for row in closes),
            "intrabar_exit_time_unknown": state["intrabar_exit_time_unknown"],
            "observations": observations, "post_exit": state.get("post_exit"),
            "excursion_basis": "observed_fills_and_held_closes_with_estimated_liquidation_cost",
            "intraday_extrema_verified": False})
    return output


def decision_funnel(records):
    """Disjoint reasons for flat decision closes, plus actual target-to-plan sizing."""
    snapshots, accounts, decisions = {}, {}, {}
    outcomes, cancellations, adjustments = {}, Counter(), Counter()
    for record in records:
        data, kind = record["payload"], record["kind"]
        if kind == "account":
            accounts[data["as_of"]] = data
        elif kind == "decision_started":
            snapshots[data["as_of"]] = data["market"]
        elif kind == "decision":
            decisions[data["as_of"]] = data
        if kind in {"account", "orders_canceled"}:
            for order in data["orders"]:
                if order["status"] in {"filled", "canceled"}:
                    identity = order["client_order_id"]
                    if identity in outcomes:
                        raise ValueError("duplicate terminal order in economic attribution")
                    outcomes[identity] = order
                if order["status"] == "canceled":
                    cancellations[order.get("reason", "unspecified")] += 1
    flat = Counter()
    sizing, plans = [], []
    for at, decision in decisions.items():
        row = accounts[at]
        account = row["account"]
        buys = [o for o in decision["orders"] if o["side"] == "buy"]
        targets = {a["symbol"]: a["target_weight"] for a in decision["allocations"]}
        adjustments.update(decision["adjustments"])
        if not account["positions"]:
            category = ("buy_plan_submitted" if buys else "zero_target_from_strategy"
                        if not any(targets.values()) else "positive_target_without_buy_plan")
            flat[category] += 1
        for order in buys:
            symbol = order["symbol"]
            price = snapshots[at]["features"][symbol]["close"]
            current = float(account["positions"].get(symbol, 0)) * price
            requested = max(0, targets[symbol] * account["equity"] - current)
            planned = float(order["quantity"]) * price
            terminal = outcomes.get(order["client_order_id"])
            if terminal is None:
                raise ValueError("submitted buy has no terminal outcome")
            sizing.append({"signal_at": at, "symbol": symbol, "requested_notional": requested,
                "planned_notional_at_signal": planned, "planned_fraction_of_requested":
                planned / requested if requested else None,
                "terminal_status": terminal["status"], "reason": terminal.get("reason"),
                "adjustments": decision["adjustments"],
                "limit": order["limit_price"], "stop_at_signal": order["stop"]})
        sales = [o for o in decision["orders"] if o["side"] == "sell"]
        if buys and sales:
            if any(outcomes[o["client_order_id"]]["status"] == "filled" for o in sales):
                failed = [outcomes[o["client_order_id"]] for o in buys
                          if outcomes[o["client_order_id"]]["status"] == "canceled"]
                if failed:
                    plans.append({"signal_at": at, "failed_buys": failed,
                                  "reason": "sale_filled_but_replacement_buy_canceled"})
    ratios = [row["planned_fraction_of_requested"] for row in sizing if row["requested_notional"] > 0]
    return {"decisions": len(decisions), "flat_decision_closes": sum(flat.values()),
            "flat_close_partition": dict(flat), "adjustment_counts": dict(adjustments),
            "cancellation_counts": dict(cancellations), "buy_plans": len(sizing),
            "filled_buy_plans": sum(row["terminal_status"] == "filled" for row in sizing),
            "median_planned_fraction_of_requested": median(ratios) if ratios else None,
            "sizing": sizing, "sale_filled_replacement_failed": plans,
            "limitation": "Strategy zero targets include internal filters/cooldowns; logs cannot identify their causal split. "
                          "Sizing differences include risk, cash and quantity rounding; this is not a causal risk ablation."}


def analyze_run(run_path, journal_meta, market):
    reconstructed = reconstruct_entries(run_path, journal_meta, market)
    document, records, _ = _load(run_path, journal_meta)
    summary = document["summary"]
    accounts = [r["payload"] for r in records if r["kind"] == "account"]
    exposures = []
    for row in accounts:
        a = row["account"]
        invested = sum(float(q) * row["valuation_prices"][s] for s, q in a["positions"].items())
        _equal(invested, a["equity"] - a["cash"] - a["dividend_receivable"], "daily invested capital")
        exposures.append(invested / a["equity"])
    initial = reconstructed["initial_equity"]
    net = summary["final_equity"] - initial
    costs, final_cost = summary["costs"], summary["estimated_final_sell_cost"]
    episodes = episode_paths(records, reconstructed["events"] + reconstructed["open_episodes"],
                             summary["cost_bps"], market)
    years = (_time(accounts[-1]["as_of"]) - _time(accounts[0]["as_of"])).total_seconds() / (365.25 * 86400)
    net_return = summary["net_liquidation_return_estimate"]
    realized = [e for e in episodes if e["exit_at"] is not None]
    by_reason = {}
    for reason in sorted({e["exit_reasons"][-1] for e in realized}):
        rows = [e for e in realized if e["exit_reasons"][-1] == reason]
        by_reason[reason] = {"episodes": len(rows), "net_pnl": sum(e["net_pnl"] for e in rows),
                            "median_holding_sessions": median(e["holding_bars"] for e in rows),
                            "post_exit": {}}
        for horizon in HORIZONS:
            values = [e["post_exit"]["horizons"][str(horizon)]["total_return"] for e in rows
                      if e["post_exit"]["horizons"][str(horizon)]["status"] == "observed"]
            by_reason[reason]["post_exit"][str(horizon)] = {
                "observed": len(values), "right_censored": len(rows) - len(values),
                "mean_total_return": mean(values) if values else None,
                "positive": sum(v > 0 for v in values)}
    return {"market": market, "source": reconstructed["source"], "accounting": reconstructed["accounting"],
        "cost_bps": summary["cost_bps"], "bars": len(accounts),
        "start": accounts[0]["as_of"], "end": accounts[-1]["as_of"],
        "economics": {"initial_equity": initial, "gross_same_fills_pnl_including_dividends": net + costs,
            "estimated_execution_costs": costs, "net_mark_to_market_pnl": net,
            "estimated_final_sell_cost": final_cost, "net_liquidation_pnl_estimate": net - final_cost,
            "net_liquidation_return_estimate": net_return,
            "cagr_estimate": (1 + net_return) ** (1 / years) - 1 if years > 0 and net_return > -1 else None,
            "gross_addback_is_not_a_zero_cost_strategy": True},
        "capital": {"active_close_count": sum(v > 0 for v in exposures),
            "active_close_fraction": mean(v > 0 for v in exposures), "mean_close_exposure": mean(exposures),
            "mean_exposure_when_active": mean(v for v in exposures if v > 0) if any(exposures) else 0,
            "maximum_close_exposure": max(exposures), "basis": "end_of_session_not_intraday_time_weighted"},
        "funnel": decision_funnel(records), "episodes": episodes, "exit_summary": by_reason,
        "boundary": {"stage": "descriptive_existing_development", "new_strategy_replays": 0,
            "formal_admission": False, "trading_enabled": False, "model_calls": 0,
            "intraday_extrema_verified": False, "sealed_holdout_evaluated": False,
            "inference": "Post-exit returns and observed excursions are descriptive, correlated observations; "
                         "they do not prove a different stop/target would improve returns."}}
