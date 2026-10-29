"""Shared completed-bar matching for research and persistent local accounts.

Arrival is rounded forward to a bar opening; observed volume only constrains
fill feasibility. All orders, including protection, share one capacity budget.
This is an estimated execution model, never evidence of exchange fills.
"""

from datetime import datetime
from decimal import Decimal as D, ROUND_FLOOR

from quantbot.evidence import hash_payload

from .ledger import _fill


EXECUTION_VERSION = "shared_volume_bar_v1"


def floor_quantity(quantity, step):
    return (D(str(quantity)) / step).to_integral_value(rounding=ROUND_FLOOR) * step


class BarMatcher:
    def __init__(self, state, orders, market, model, *, cost_bps, latency_seconds):
        self.state, self.orders = state, orders
        self.market, self.model = market, model
        self.cost_bps, self.latency_seconds = cost_bps, latency_seconds
        self.events = []

    def step_size(self, symbol):
        return D("1") if self.market == "stocks" else D(self.model["quantity_crypto"][symbol])

    def cancel(self, identifier, reason, at):
        order = self.orders.pop(identifier)
        self.events.append({**order, "status": "canceled", "reason": reason, "at": at.isoformat()})

    def execute(self, order, reference, at, *, timing):
        symbol = order["symbol"]
        quantity = floor_quantity(
            min(D(order["remaining"]), self.available[symbol]), self.step_size(symbol)
        )
        if order["side"] == "sell":
            quantity = min(quantity, D(self.state["positions"].get(symbol, "0")))
        if quantity <= 0:
            return
        used = self.capacity[symbol] - self.available[symbol] + quantity
        impact = D(str(self.model["impact_bps_at_participation_cap"])) * used / self.capacity[symbol]
        cost = D(str(self.cost_bps)) + impact
        if order["side"] == "buy" and D(str(reference)) * (1 + cost / 10000) > D(str(order["limit_price"])):
            self.cancel(order["client_order_id"], "open_above_entry_limit", at)
            return
        fill = _fill(self.state, {**order, "quantity": str(quantity)}, reference, at, cost,
                     bar_close=self.close_at)
        if fill["status"] != "filled":
            self.cancel(order["client_order_id"], fill["reason"], at)
            return
        order["remaining"] = str(D(order["remaining"]) - quantity)
        fill.update(
            fill_id="fill-" + hash_payload([order["client_order_id"], self.close_at.isoformat(),
                                           order["remaining"], str(quantity)])[:32],
            remaining=order["remaining"], partial=D(order["remaining"]) > 0,
            latency_seconds=self.latency_seconds, base_cost_bps=self.cost_bps,
            impact_bps=str(impact), bar_open=self.at.isoformat(),
            bar_capacity=str(self.capacity[symbol]), capacity_used_after=str(used),
            execution_time_basis=timing, execution_version=EXECUTION_VERSION,
        )
        if "fee_bps" in self.model:
            scenario = self.model["one_way_cost_bps"].index(self.cost_bps)
            components = {name: D(str(self.model[key][scenario])) for name, key in (
                ("fee", "fee_bps"), ("half_spread", "half_spread_bps"), ("slippage", "slippage_bps")
            )}
        else:
            # Existing local config specifies an aggregate cost, not a measured
            # fee/spread decomposition. Do not invent that missing attribution.
            components = {"estimated_base": D(str(self.cost_bps))}
        components["impact"] = impact
        fill["cost_components"] = {
            name: str(D(str(reference)) * quantity * bps / 10000) for name, bps in components.items()
        }
        self.events.append(fill)
        self.available[symbol] -= quantity
        if D(order["remaining"]) == 0:
            self.orders.pop(order["client_order_id"], None)

    def advance(self, at, close_at, bars, previous_volume):
        self.at, self.close_at = at, close_at
        self.capacity = {
            symbol: floor_quantity(
                min(D(str(previous_volume.get(symbol, 0))), D(str(bar["volume"])))
                * D(str(self.model["max_volume_participation"])), self.step_size(symbol)
            ) for symbol, bar in bars.items()
        }
        self.available = self.capacity.copy()
        for key, order in sorted(list(self.orders.items()), key=lambda pair: pair[1]["side"] == "buy"):
            if order["side"] == "buy" and at >= datetime.fromisoformat(order["expires_at"]):
                self.cancel(key, "entry_expired", at)
                continue
            if at < datetime.fromisoformat(order["eligible_at"]):
                continue
            self.execute(order, bars[order["symbol"]]["open"], at, timing="bar_open")

        for symbol, quantity in list(self.state["positions"].items()):
            bar = bars[symbol]
            opening, stop = D(str(bar["open"])), D(self.state["stops"][symbol])
            target = self.state.get("take_profits", {}).get(symbol)
            target = D(target) if target is not None else None
            stop_touched = D(str(bar["low"])) <= stop
            profit_touched = target is not None and D(str(bar["high"])) >= target
            if opening <= stop:
                reason, reference, fill_at = "protective_stop", opening, at
            elif target is not None and opening >= target:
                reason, reference, fill_at = "take_profit", target, at
            elif stop_touched:
                reason, reference, fill_at = "protective_stop", stop, close_at
            elif profit_touched:
                reason, reference, fill_at = "take_profit", target, close_at
            else:
                continue
            for key, order in list(self.orders.items()):
                if order["symbol"] == symbol:
                    self.cancel(key, reason if order["side"] == "buy"
                                else "protective_exit_replaces_pending", fill_at)
            key = "protect-" + hash_payload([symbol, close_at.isoformat(), reason])[:24]
            order = {
                "client_order_id": key, "symbol": symbol, "side": "sell",
                "quantity": quantity, "remaining": quantity, "reason": reason,
                "created_at": fill_at.isoformat(), "eligible_at": fill_at.isoformat(),
                "stop": str(stop), "both_touched": stop_touched and profit_touched,
            }
            if target is not None:
                order["take_profit"] = str(target)
            self.orders[key] = order
            self.execute(order, reference, fill_at,
                         timing="bar_open" if fill_at == at else "intrabar_time_unknown")
        self.state["pending"] = list(self.orders.values())
        return self.events
