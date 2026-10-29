"""持续任务和历史评估共用的本地账户：次根开盘、显式成本、原子持久化。

纯本地撮合，不包含券商接口。股票订单经既有整数风控，加密订单经既有 Decimal 风控。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from quantbot.crypto.ai import CryptoAccount, spot_plan
from quantbot.crypto.ai import INTERVALS
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.evidence import hash_payload
from quantbot.trading.domain import BrokerAccountSnapshot, MarginStatus

from .bar_execution import BarMatcher, EXECUTION_VERSION
from .domain import AccountState
from .corporate_actions import opening_actions, pay_dividends
from .market import EASTERN
from .risk import build_plan, stock_in_cooldown
from .ledger import (
    new_account as new_account,
    account_view as account_view,
    _fill as _fill,
    risk_reason as risk_reason,
    cancel_pending as cancel_pending,
)


def matching_model(config, spec):
    """Explicit execution assumptions; they are not calibrated venue evidence."""
    execution = config["execution"]
    seconds = int(INTERVALS[spec.get("bar", "1d")].total_seconds())
    steps = {i.symbol: str(i.min_size) for i in MockExchange().list_instruments("spot")} if spec["market"] == "crypto" else {}
    return {
        "version": EXECUTION_VERSION,
        "quantity_crypto": steps,
        "one_way_cost_bps": [execution["cost_bps"]],
        "latency_seconds": [execution.get("latency_seconds", 0)],
        "max_volume_participation": execution.get("max_volume_participation", 0.01),
        "impact_bps_at_participation_cap": execution.get("impact_bps_at_participation_cap", 0),
        "entry_ttl_seconds": seconds * execution.get("entry_ttl_bars", 3),
    }


def advance(state, snapshot, context, config, spec):
    events = []
    previous = datetime.fromisoformat(state["as_of"]) if state["as_of"] else context.as_of
    if previous > context.as_of:
        raise ValueError("行情早于账户已处理时间")
    if state["as_of"] and previous not in snapshot.times:
        raise ValueError("行情未覆盖上次处理时间，无法对账和恢复止损")
    model = matching_model(config, spec)
    latency = model["latency_seconds"][0]
    volumes = state.get("execution_previous_volume")
    if volumes is None:
        # Seed only from a bar already complete at the previous decision time.
        prior_bars = snapshot.bars(previous - timedelta(microseconds=1), previous)
        volumes = {s: str(bar["volume"]) for s, bar in prior_bars[-1][1].items()} if prior_bars else {}
    for order in state["pending"]:
        order.setdefault("created_at", previous.isoformat())
        created = datetime.fromisoformat(order["created_at"])
        order.setdefault("remaining", str(order["quantity"]))
        order.setdefault("eligible_at", (created + timedelta(seconds=latency)).isoformat())
        if order["side"] == "buy":
            order.setdefault("expires_at", (created + timedelta(seconds=model["entry_ttl_seconds"])).isoformat())
    for at, bars in snapshot.bars(previous, context.as_of):
        opening_at = (
            snapshot.calendar.session_open(str(at.astimezone(EASTERN).date())).to_pydatetime()
            if spec["market"] == "stocks" else at - INTERVALS[spec["bar"]]
        )
        zone = EASTERN if spec["market"] == "stocks" else UTC
        day = at.astimezone(zone).date().isoformat()
        if state["day"] != day:
            state["day"], state["day_open_equity"] = day, str(account_view(state)["equity"])
        action_count = len(state["corporate_action_events"])
        events += opening_actions(state, snapshot.actions, day, opening_at)
        for action_event in state["corporate_action_events"][action_count:]:
            if action_event["kind"] == "split":
                action = action_event["action"]
                symbol = action["symbol"]
                if symbol in volumes:
                    volumes[symbol] = str(D(volumes[symbol]) * D(action["ratio"]))
        state["prices"] = {s: str(bar["open"]) for s, bar in bars.items()}
        state["risk_latched"] = state["risk_latched"] or risk_reason(state, spec)
        if state["risk_latched"]:
            kept = []
            for order in state["pending"]:
                if order["side"] == "buy":
                    events.append({**order, "status": "canceled", "reason": state["risk_latched"],
                                   "at": opening_at.isoformat()})
                else:
                    kept.append(order)
            state["pending"] = kept
        orders = {o["client_order_id"]: o for o in state["pending"]}
        matcher = BarMatcher(state, orders, spec["market"], model,
                             cost_bps=config["execution"]["cost_bps"], latency_seconds=latency)
        events += matcher.advance(opening_at, at, bars, volumes)
        pay_dividends(state, day, at)
        state["prices"] = {s: str(b["close"]) for s, b in bars.items()}
        state["peak"] = str(max(D(state["peak"]), D(str(account_view(state)["equity"]))))
        state["risk_latched"] = state["risk_latched"] or risk_reason(state, spec)
        volumes = {s: str(b["volume"]) for s, b in bars.items()}
    state["execution_previous_volume"] = volumes
    state["execution_version"] = EXECUTION_VERSION
    state["prices"] = {s: str(p) for s, p in context.prices.items()}
    if state["day"] is None:
        zone = EASTERN if spec["market"] == "stocks" else UTC
        state["day"] = context.as_of.astimezone(zone).date().isoformat()
    state["as_of"] = context.as_of.isoformat()
    return events


def model_account(state, context, market):
    if market == "crypto":
        return CryptoAccount(
            D(state["cash"]),
            {s: D(q) for s, q in state["positions"].items()},
            {s: D(p) for s, p in state["stops"].items()},
            take_profits={s: D(p) for s, p in state.get("take_profits", {}).items()},
            last_exit_bar=dict(state.get("last_exit_bar", {})),
        )
    view = account_view(state)
    return AccountState(
        view["cash"],
        view["equity"],
        {s: int(D(q)) for s, q in state["positions"].items()},
        BrokerAccountSnapshot(
            "local_paper", context.as_of, view["equity"], view["cash"], False, False, MarginStatus.SIMULATED
        ),
        {s: float(p) for s, p in state["stops"].items()},
        dividend_receivable=view["dividend_receivable"],
        last_exit_bar=dict(state.get("last_exit_bar", {})),
    )


def create_plan(state, context, decision, config, market, identity):
    account = model_account(state, context, market)
    if market == "stocks":
        cooldown = config["strategy"].get("stock_cooldown_bars", 0)
        cooling = frozenset(symbol for symbol in account.last_exit_bar
                            if not account.positions.get(symbol, 0)
                            and stock_in_cooldown(context.as_of, account.last_exit_bar, symbol, cooldown))
        plan = build_plan(context, account, decision, config, identity=identity,
                          entry_cooldown=cooling).payload()
        for order in plan["orders"]:
            if order["side"] == "buy" and config["risk"].get("stock_stop_loss_pct"):
                order.update(stop_loss_pct=config["risk"]["stock_stop_loss_pct"],
                             take_profit_pct=config["risk"]["stock_take_profit_pct"])
        return plan
    instruments = {i.symbol: i for i in MockExchange().list_instruments("spot")}
    plan = spot_plan(context, account, decision, config, instruments)
    for order in plan["orders"]:
        order["limit_price"] = str(
            D(order["price"]) * (1 + D(str(config["execution"]["limit_buffer_bps"])) / 10000)
        )
        if order["side"] == "buy" and config["risk"].get("crypto_stop_loss_pct"):
            order.update(stop_loss_pct=config["risk"]["crypto_stop_loss_pct"],
                         take_profit_pct=config["risk"]["crypto_take_profit_pct"])
    return plan


def queue_plan(state, plan):
    if state["pending"]:
        raise ValueError("仍有未处理订单，禁止覆盖")
    state["pending"] = [{**order, "created_at": state["as_of"]} for order in plan["orders"]]
    return [{**o, "status": "pending", "reason": "next_bar_open"} for o in state["pending"]]


def queue_exit(state):
    events = cancel_pending(state, "exit_requested")
    state["pending"] = [
        {
            "client_order_id": "exit-" + hash_payload([s, state["as_of"]])[:24],
            "symbol": s,
            "side": "sell",
            "quantity": q,
            "reason": "manual_exit",
        }
        for s, q in state["positions"].items()
    ]
    return events + [{**o, "status": "pending"} for o in state["pending"]]
