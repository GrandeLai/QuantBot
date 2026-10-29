"""Shared Decimal cash, position, protective-price and risk ledger.

Costs are booked in execution prices on both sides; realized PnL includes entry
cost through average cost. No strategy, data adapter or broker is invoked here.
"""

from decimal import Decimal as D

def new_account(cash):
    return {
        "cash": str(cash),
        "positions": {},
        "stops": {},
        "take_profits": {},
        "last_exit_bar": {},
        "average_cost": {},
        "pending": [],
        "as_of": None,
        "prices": {},
        "realized_pnl": "0",
        "costs": "0",
        "peak": str(cash),
        "day": None,
        "day_open_equity": str(cash),
        "risk_latched": None,
        "dividend_income": "0",
        "dividend_receivables": {},
        "corporate_action_events": [],
    }


def account_view(state):
    value = sum((D(q) * D(str(state["prices"][s])) for s, q in state["positions"].items()), D(0))
    basis = sum((D(q) * D(state["average_cost"][s]) for s, q in state["positions"].items()), D(0))
    receivable = sum((D(item["amount"]) for item in state.get("dividend_receivables", {}).values()), D(0))
    equity = D(state["cash"]) + value + receivable
    return {
        "cash": float(state["cash"]),
        "equity": float(equity),
        "positions": dict(state["positions"]),
        "stops": dict(state["stops"]),
        "take_profits": dict(state.get("take_profits", {})),
        "last_exit_bar": dict(state.get("last_exit_bar", {})),
        "pending_orders": [dict(order) for order in state["pending"]],
        "realized_pnl": float(state["realized_pnl"]),
        "unrealized_pnl": float(value - basis),
        "estimated_execution_cost": float(state["costs"]),
        "dividend_receivable": float(receivable),
        "dividend_income": float(state.get("dividend_income", "0")),
        "drawdown_fraction": float(equity / D(state["peak"]) - 1),
        "pnl_basis": "local_simulation_with_estimated_costs",
    }


def _fill(state, order, reference, at, cost_bps, *, bar_close):
    symbol, side, qty = order["symbol"], order["side"], D(str(order["quantity"]))
    reference = D(str(reference))
    cost = reference * qty * D(str(cost_bps)) / 10000
    price = reference * (1 + D(str(cost_bps)) / 10000 * (1 if side == "buy" else -1))
    owned = D(state["positions"].get(symbol, "0"))
    cash = D(state["cash"])
    if side == "buy":
        if price * qty > cash:
            return {**order, "status": "canceled", "reason": "insufficient_cash", "at": at.isoformat()}
        stop = max(D(state["stops"].get(symbol, "0")), D(str(order["stop"])))
        if order.get("stop_loss_pct"):
            stop = max(stop, reference * (1 - D(str(order["stop_loss_pct"]))))
            if reference <= stop:
                return {**order, "status": "canceled", "reason": "open_at_or_below_stop", "at": at.isoformat()}
        if order.get("take_profit_pct"):
            target = reference * (1 + D(str(order["take_profit_pct"])))
            target = min(target, D(state.get("take_profits", {}).get(symbol, str(target))))
            state.setdefault("take_profits", {})[symbol] = str(target)
            order = {**order, "take_profit": str(target), "stop": str(stop)}
        basis = D(state["average_cost"].get(symbol, "0")) * owned + price * qty
        state["positions"][symbol] = str(owned + qty)
        state["average_cost"][symbol] = str(basis / (owned + qty))
        state["stops"][symbol] = str(stop)
        state["cash"] = str(cash - price * qty)
    else:
        if qty > owned:
            raise ValueError("模拟账本卖出数量超过持仓")
        state["realized_pnl"] = str(
            D(state["realized_pnl"]) + qty * (price - D(state["average_cost"][symbol]))
        )
        state["cash"] = str(cash + price * qty)
        if qty == owned:
            for key in ("positions", "stops", "average_cost"):
                state[key].pop(symbol)
            state.setdefault("take_profits", {}).pop(symbol, None)
            state.setdefault("last_exit_bar", {})[symbol] = bar_close.isoformat()
        else:
            state["positions"][symbol] = str(owned - qty)
    state["costs"] = str(D(state["costs"]) + cost)
    return {
        **order,
        "status": "filled",
        "at": at.isoformat(),
        "price": str(price),
        "reference_price": str(reference),
        "estimated_cost": str(cost),
    }


def risk_reason(state, spec):
    equity = D(str(account_view(state)["equity"]))
    if equity <= 0:
        return "nonpositive_equity"
    if equity <= D(state["day_open_equity"]) * (1 - D(str(spec["max_daily_loss"]))):
        return "daily_loss_limit"
    if equity <= D(state["peak"]) * (1 - D(str(spec["max_drawdown"]))):
        return "drawdown_limit"
    return None


def cancel_pending(state, reason, *, keep_exits=False):
    events = [{**o, "status": "canceled", "reason": reason} for o in state["pending"]
              if not keep_exits or o["side"] != "sell"]
    state["pending"] = [o for o in state["pending"] if keep_exits and o["side"] == "sell"]
    return events


