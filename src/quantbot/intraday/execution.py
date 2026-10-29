"""One Decimal ledger and volume budget for stock/spot intraday research.

Reuses the existing cash, protective-price and cost accounting. This is a bar
approximation, not an order-book simulator or an exchange execution adapter.
"""

from datetime import timedelta
from decimal import Decimal as D

from quantbot.ai.ledger import account_view, new_account, risk_reason
from quantbot.ai.bar_execution import BarMatcher, floor_quantity as floor_quantity
from quantbot.evidence import hash_payload


class IntradayAccount:
    def __init__(self, market, section, model, *, cost_bps, latency_seconds, sessions):
        if market not in ("crypto", "stocks") or cost_bps not in model["one_way_cost_bps"]:
            raise ValueError("unknown market or unfrozen cost scenario")
        if latency_seconds not in model["latency_seconds"]:
            raise ValueError("unfrozen latency scenario")
        self.market, self.section, self.model = market, section, model
        self.state = new_account(section["execution"]["initial_cash"])
        self.initial_cash = D(self.state["cash"])
        self.cost_bps, self.latency_seconds = cost_bps, latency_seconds
        self.sessions = {day: i for i, day in enumerate(sessions)}
        self.actions = {day: value.get("actions", []) for day, value in sessions.items()
                        if isinstance(value, dict)} if isinstance(sessions, dict) else {}
        self.action_events = []
        self.orders, self.events, self.curve = {}, [], []
        self.attempts = []
        self.previous_volume, self.entry_at = {}, {}
        self.session_violations = []
        self.snapshot_count = 0
        self._cash_check, self._positions_check = self.initial_cash, {}

    def step_size(self, symbol):
        return D("1") if self.market == "stocks" else D(self.model["quantity_crypto"][symbol])

    def cooling(self, symbol, at, day):
        last = self.state["last_exit_bar"].get(symbol)
        if last is None:
            return False
        from datetime import datetime

        prior = datetime.fromisoformat(last)
        key = "stock_cooldown_bars" if self.market == "stocks" else "crypto_cooldown_bars"
        bars = self.section["strategy"][key]
        if self.market == "crypto":
            return at - prior < timedelta(days=bars)
        from quantbot.ai.market import EASTERN

        prior_day = prior.astimezone(EASTERN).date().isoformat()
        return self.sessions[day] - self.sessions[prior_day] < bars

    def _cancel(self, identifier, reason, at):
        order = self.orders.pop(identifier)
        self.events.append({**order, "status": "canceled", "reason": reason, "at": at.isoformat()})

    def _cancel_buys(self, symbol, reason, at):
        for key, order in list(self.orders.items()):
            if order["side"] == "buy" and (symbol is None or order["symbol"] == symbol):
                self._cancel(key, reason, at)

    def _reject_entry(self, symbol, at, reason, context):
        self.attempts.append({
            "symbol": symbol, "at": at.isoformat(), "accepted": False,
            "rejection": {"reason": reason, "context": context},
        })
        return False

    def enter(self, symbol, at, day, reference, atr, *, identity):
        if self.state["risk_latched"]:
            return self._reject_entry(symbol, at, "risk_latched", {
                "risk_latched": self.state["risk_latched"],
                "as_of": self.state["as_of"], "day": self.state["day"],
                "cash": self.state["cash"], "positions": dict(self.state["positions"]),
                "prices": dict(self.state["prices"]), "peak": self.state["peak"],
                "day_open_equity": self.state["day_open_equity"],
                "max_daily_loss": self.section["max_daily_loss"],
                "max_drawdown": self.section["max_drawdown"],
            })
        if self.cooling(symbol, at, day):
            key = "stock_cooldown_bars" if self.market == "stocks" else "crypto_cooldown_bars"
            return self._reject_entry(symbol, at, "cooldown", {
                "last_exit_bar": self.state["last_exit_bar"][symbol], "day": day,
                "market": self.market, "cooldown_bars": self.section["strategy"][key],
                "cooldown_unit": "sessions" if self.market == "stocks" else "calendar_days",
            })
        owned = D(self.state["positions"].get(symbol, "0"))
        if owned > 0:
            return self._reject_entry(symbol, at, "position_exists", {"quantity": str(owned)})
        pending = next((o for o in self.orders.values() if o["symbol"] == symbol), None)
        if pending is not None:
            return self._reject_entry(symbol, at, "pending_order_exists", {
                "client_order_id": pending["client_order_id"], "side": pending["side"],
                "remaining": pending["remaining"],
            })
        risk = self.section["risk"]
        reference, atr = D(str(reference)), D(str(atr))
        equity = D(str(account_view(self.state)["equity"]))
        market_prefix = "stock" if self.market == "stocks" else "crypto"
        stop_pct = D(str(risk[market_prefix + "_stop_loss_pct"]))
        take_pct = D(str(risk[market_prefix + "_take_profit_pct"]))
        stop = max(
            reference * (1 - stop_pct), reference - atr * D(str(self.section["strategy"]["atr_stop_mult"]))
        )
        limit = reference * (1 + D(str(self.section["execution"]["limit_buffer_bps"])) / 10000)
        if stop <= 0 or stop >= reference:
            return self._reject_entry(symbol, at, "invalid_stop", {
                "reference": str(reference), "atr": str(atr), "stop": str(stop), "limit": str(limit),
            })
        buys = [o for o in self.orders.values() if o["side"] == "buy"]
        reserve = sum((D(o["remaining"]) * D(o["limit_price"]) for o in buys), D(0))
        reserved_risk = sum((D(o["remaining"]) * (D(o["limit_price"]) - D(o["stop"])) for o in buys), D(0))
        open_value = sum(
            (D(q) * D(self.state["prices"][s]) for s, q in self.state["positions"].items()), D(0)
        )
        open_risk = sum(
            (
                D(q) * max(D(0), D(self.state["prices"][s]) - D(self.state["stops"][s]))
                for s, q in self.state["positions"].items()
            ),
            D(0),
        )
        if len(self.state["positions"]) + len(buys) >= risk["max_positions"]:
            return self._reject_entry(symbol, at, "max_positions", {
                "positions_count": len(self.state["positions"]), "pending_buys_count": len(buys),
                "max_positions": risk["max_positions"],
            })
        cash = D(self.state["cash"]) - reserve - equity * D(str(risk["cash_buffer"]))
        gross = equity * D(str(risk["max_gross_exposure"])) - open_value - reserve
        heat = equity * D(str(risk["max_portfolio_heat"])) - open_risk - reserved_risk
        quantity_limits = {
            "cash": cash / limit,
            "gross_exposure": gross / limit,
            "max_weight": equity * D(str(risk["max_weight"])) / limit,
            "risk_per_trade": equity * D(str(risk["risk_per_trade"])) / (limit - stop),
            "portfolio_heat": heat / (limit - stop),
        }
        quantity_limit = min(quantity_limits.values())
        step = self.step_size(symbol)
        quantity = floor_quantity(quantity_limit, step)
        if quantity <= 0:
            return self._reject_entry(symbol, at, "nonpositive_quantity", {
                "quantity_limits": {name: str(value) for name, value in quantity_limits.items()},
                "quantity_step": str(step), "quantity": str(quantity),
                "equity": str(equity), "cash_available": str(cash),
                "gross_exposure_available": str(gross), "portfolio_heat_available": str(heat),
                "cash_reserve": str(reserve), "reserved_risk": str(reserved_risk),
                "open_value": str(open_value), "open_risk": str(open_risk),
                "limit": str(limit), "stop": str(stop),
            })
        key = "intraday-" + hash_payload([identity, symbol, at.isoformat()])[:24]
        self.orders[key] = {
            "client_order_id": key,
            "symbol": symbol,
            "side": "buy",
            "quantity": str(quantity),
            "remaining": str(quantity),
            "stop": str(stop),
            "limit_price": str(limit),
            "stop_loss_pct": str(stop_pct),
            "take_profit_pct": str(take_pct),
            "created_at": at.isoformat(),
            "eligible_at": (at + timedelta(seconds=self.latency_seconds)).isoformat(),
            "expires_at": (at + timedelta(seconds=self.model["entry_ttl_seconds"])).isoformat(),
            "reason": "research_entry",
        }
        self.events.append({**self.orders[key], "status": "accepted", "at": at.isoformat()})
        self.attempts.append({"symbol": symbol, "at": at.isoformat(), "accepted": True})
        return True

    def exit(self, symbol, at, reason):
        self._cancel_buys(symbol, reason, at)
        owned = D(self.state["positions"].get(symbol, "0"))
        if owned <= 0 or any(o["symbol"] == symbol and o["side"] == "sell" for o in self.orders.values()):
            return
        key = "exit-" + hash_payload([symbol, at.isoformat(), reason])[:24]
        self.orders[key] = {
            "client_order_id": key,
            "symbol": symbol,
            "side": "sell",
            "quantity": str(owned),
            "remaining": str(owned),
            "created_at": at.isoformat(),
            "eligible_at": (at + timedelta(seconds=self.latency_seconds)).isoformat(),
            "reason": reason,
        }
        self.events.append({**self.orders[key], "status": "accepted", "at": at.isoformat()})

    def advance(self, at, bars, *, day, session_close):
        if self.state["day"] != day and self.actions.get(day):
            from .actions import open_actions

            self.action_events.extend(open_actions(
                self.actions[day], self.state, self.orders, self.previous_volume, at))
        self.bar_open = at
        close_at = at + timedelta(minutes=5)
        self.state["prices"] = {s: str(b["open"]) for s, b in bars.items()}
        if self.state["day"] != day:
            self.state["day"], self.state["day_open_equity"] = day, str(account_view(self.state)["equity"])
        self.state["risk_latched"] = self.state["risk_latched"] or risk_reason(self.state, self.section)
        if self.state["risk_latched"]:
            self._cancel_buys(None, self.state["risk_latched"], at)
        matcher = BarMatcher(self.state, self.orders, self.market, self.model,
                             cost_bps=self.cost_bps, latency_seconds=self.latency_seconds)
        events = matcher.advance(at, close_at, bars, self.previous_volume)
        self.events.extend(events)
        for fill in events:
            if fill["status"] != "filled":
                continue
            symbol, quantity = fill["symbol"], D(fill["quantity"])
            if fill["side"] == "buy":
                from datetime import datetime

                self.entry_at.setdefault(symbol, datetime.fromisoformat(fill["at"]))
                self._cash_check -= quantity * D(fill["price"])
                self._positions_check[symbol] = self._positions_check.get(symbol, D(0)) + quantity
            else:
                self._cash_check += quantity * D(fill["price"])
                self._positions_check[symbol] -= quantity
                if not self._positions_check[symbol]:
                    self._positions_check.pop(symbol)
                    self.entry_at.pop(symbol, None)
        for symbol in self.state["positions"]:
            if close_at >= session_close - timedelta(minutes=15):
                self.exit(symbol, close_at, "session_flatten")
            elif close_at - self.entry_at[symbol] >= timedelta(minutes=120):
                self.exit(symbol, close_at, "holding_limit")
        self.state["prices"] = {s: str(b["close"]) for s, b in bars.items()}
        self.previous_volume = {s: D(str(b["volume"])) for s, b in bars.items()}
        self.state["as_of"] = close_at.isoformat()
        self.state["pending"] = list(self.orders.values())
        view = account_view(self.state)
        self.state["peak"] = str(max(D(self.state["peak"]), D(str(view["equity"]))))
        self.state["risk_latched"] = self.state["risk_latched"] or risk_reason(self.state, self.section)
        if self.state["risk_latched"]:
            self._cancel_buys(None, self.state["risk_latched"], close_at)
        self.state["pending"] = list(self.orders.values())
        self.reconcile()
        self.snapshot_count += 1
        if close_at == session_close:
            self._cancel_buys(None, "session_end", close_at)
            self.state["pending"] = list(self.orders.values())
            if self.state["positions"] or self.orders:
                self.session_violations.append(
                    {
                        "day": day,
                        "reason": "unliquidated_session_end",
                        "positions": dict(self.state["positions"]),
                    }
                )
            self.curve.append({"day": day, "as_of": close_at.isoformat(), **account_view(self.state)})

    def reconcile(self):
        if abs(self._cash_check - D(self.state["cash"])) > D("0.00000001"):
            raise ValueError("独立重算现金不一致")
        if self._positions_check != {s: D(q) for s, q in self.state["positions"].items()}:
            raise ValueError("独立重算持仓不一致")
        if D(self.state["cash"]) < 0 or any(q <= 0 for q in self._positions_check.values()):
            raise ValueError("负现金或非法持仓")
        for symbol, quantity in self._positions_check.items():
            if quantity != floor_quantity(quantity, self.step_size(symbol)):
                raise ValueError("成交精度不符合市场约束")
        return {
            "status": "reconciled",
            "account_snapshots": self.snapshot_count,
            "cash": str(self._cash_check),
            "positions": {s: str(q) for s, q in self._positions_check.items()},
        }

    def report(self):
        self.state["pending"] = list(self.orders.values())
        fills = [o for o in self.events if o["status"] == "filled"]
        return {
            **({"corporate_actions": self.action_events} if self.action_events else {}),
            "account": account_view(self.state),
            "daily_curve": self.curve,
            "events": self.events,
            "attempts": self.attempts,
            "buy_fills": sum(o["side"] == "buy" for o in fills),
            "partial_fills": sum(o["partial"] for o in fills),
            "entry_orders_filled": len({o["client_order_id"] for o in fills if o["side"] == "buy"}),
            "risk_hold": self.state["risk_latched"],
            "session_violations": self.session_violations,
            "reconciliation": self.reconcile(),
            "initial_equity": float(self.initial_cash),
            "net_return": account_view(self.state)["equity"] / float(self.initial_cash) - 1,
        }
