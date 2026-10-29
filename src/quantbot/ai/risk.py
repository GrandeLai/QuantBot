"""模型提议经确定性仓位、现金、ATR 风险预算和券商事实约束生成订单。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from hashlib import sha256
import math
from typing import Mapping

from quantbot.trading.domain import OrderIntent
from quantbot.trading.risk import BrokerAccountRiskGate

from .domain import AccountState, DecisionBatch, MarketContext, validate_decision
from .market import EASTERN


@lru_cache(maxsize=4096)
def _stock_cooldown_end(exit_day: str, bars: int) -> date:
    """仅缓存交易日历，不访问行情；到期日收盘可计划下一交易日入场。"""
    from .feeds import stock_calendar

    start = date.fromisoformat(exit_day)
    span = 366  # 当前配置最多 100 个交易日；极端休市下仍检查实际覆盖。
    while True:
        end = start + timedelta(days=span)
        calendar = stock_calendar(start, end)
        if len(calendar.sessions_in_range(exit_day, str(end))) > bars:
            return calendar.session_offset(exit_day, bars).date()
        span *= 2


def stock_in_cooldown(as_of: datetime, last_exit_bar: Mapping[str, str], symbol: str, bars: int) -> bool:
    if not bars or symbol not in last_exit_bar:
        return False
    exited = datetime.fromisoformat(last_exit_bar[symbol]).astimezone(EASTERN).date()
    return as_of.astimezone(EASTERN).date() < _stock_cooldown_end(exited.isoformat(), bars)


@dataclass(frozen=True)
class TradePlan:
    intents: tuple[OrderIntent, ...]
    stops: dict[str, float]
    limit_prices: dict[str, float]
    adjustments: tuple[str, ...]
    decision: DecisionBatch

    def payload(self) -> dict:
        return {
            "provider": self.decision.provider,
            "model": self.decision.model,
            "response_id": self.decision.response_id,
            "usage": self.decision.usage,
            "allocations": [vars(a) for a in self.decision.allocations],
            "orders": [
                {
                    **vars(i),
                    "created_at": i.created_at.isoformat(),
                    "stop": self.stops.get(i.symbol),
                    "limit_price": self.limit_prices[i.symbol],
                }
                for i in self.intents
            ],
            "adjustments": list(self.adjustments),
        }


def build_plan(
    context: MarketContext,
    account: AccountState,
    decision: DecisionBatch,
    config: dict,
    *,
    identity: str = "simulation",
    entry_cooldown: frozenset[str] = frozenset(),
) -> TradePlan:
    account.payload(context)
    allocations = validate_decision(
        {
            "snapshot_id": context.snapshot_id,
            "as_of": context.as_of.isoformat(),
            "decisions": [vars(a) for a in decision.allocations],
        },
        context,
    )
    risk, execution = config["risk"], config["execution"]
    if account.equity <= 0:
        return TradePlan((), {}, {}, ("nonpositive_equity",), decision)
    limits = {
        s: math.floor(p * (1 + execution["limit_buffer_bps"] / 10000) * (100 if p >= 1 else 10000))
        / (100 if p >= 1 else 10000)
        for s, p in context.prices.items()
    }
    if any(value <= 0 for value in limits.values()):
        raise ValueError("行情价格低于支持的订单精度")
    stops = {s: account.stops.get(s, context.features[s]["suggested_stop"]) for s in context.features}
    adjustments = []
    fixed = {a.symbol for a in allocations if a.confidence < risk["min_confidence"]}
    target_weights = {
        a.symbol: 0 if a.symbol in entry_cooldown else min(a.target_weight, risk["max_weight"])
        for a in allocations if a.symbol not in fixed
    }
    for allocation in allocations:
        if allocation.symbol in fixed:
            adjustments.append(f"{allocation.symbol}:low_confidence_hold")
        elif allocation.symbol in entry_cooldown and allocation.target_weight > 0:
            adjustments.append(f"{allocation.symbol}:entry_cooldown")
        elif allocation.target_weight > risk["max_weight"]:
            adjustments.append(f"{allocation.symbol}:max_weight_clamped")
    fixed_exposure = sum(account.positions.get(s, 0) * context.prices[s] / account.equity for s in fixed)
    gross_budget = max(0, risk["max_gross_exposure"] - fixed_exposure)
    total_weight = sum(target_weights.values())
    if total_weight > gross_budget:
        factor = gross_budget / total_weight
        target_weights = {s: w * factor for s, w in target_weights.items()}
        adjustments.append("gross_exposure_scaled")
    fixed_positions = sum(account.positions.get(s, 0) > 0 for s in fixed)
    ranked = sorted(
        (a for a in allocations if a.symbol not in fixed and target_weights[a.symbol] > 0),
        key=lambda a: (-a.confidence, -context.features[a.symbol]["factor_score"], a.symbol),
    )
    selected = {a.symbol for a in ranked[: max(0, risk["max_positions"] - fixed_positions)]}
    targets = {s: account.positions.get(s, 0) for s in fixed}
    for symbol, weight in target_weights.items():
        current = account.positions.get(symbol, 0)
        current_value = current * context.prices[symbol]
        target_value = weight * account.equity
        if symbol not in selected:
            targets[symbol] = 0
        elif math.isclose(target_value, current_value, rel_tol=1e-12, abs_tol=1e-8):
            targets[symbol] = current
        elif target_value > current_value:
            # The buffered buy limit budgets additional shares, not existing holdings.
            targets[symbol] = current + math.floor((target_value - current_value) / limits[symbol])
        else:
            targets[symbol] = math.floor(target_value / context.prices[symbol])
        if weight > 0 and symbol not in selected:
            adjustments.append(f"{symbol}:max_positions_rejected")
    cash_available = max(
        0, min(account.cash, account.broker.buying_power) - account.equity * risk["cash_buffer"]
    )
    existing_heat = sum(
        min(account.positions.get(s, 0), targets[s]) * max(0, context.prices[s] - stops[s])
        for s in context.features
    )
    heat_available = max(0, account.equity * risk["max_portfolio_heat"] - existing_heat)
    intents = []
    gate = BrokerAccountRiskGate()
    # Reductions come first; their unconfirmed proceeds are never budgeted for purchases.
    priority = sorted(
        targets,
        key=lambda s: (targets[s] >= account.positions.get(s, 0), -context.features[s]["factor_score"], s),
    )
    for symbol in priority:
        current, target = account.positions.get(symbol, 0), targets[symbol]
        if target == current:
            continue
        side = "buy" if target > current else "sell"
        quantity = abs(target - current)
        if side == "buy":
            distance = limits[symbol] - stops[symbol]
            if distance <= 0 or limits[symbol] <= 0 or stops[symbol] >= context.prices[symbol]:
                adjustments.append(f"{symbol}:invalid_stop")
                continue
            quantity = min(
                quantity,
                math.floor(cash_available / limits[symbol]),
                math.floor(account.equity * risk["risk_per_trade"] / distance),
                math.floor(heat_available / distance),
            )
            if quantity <= 0:
                adjustments.append(f"{symbol}:cash_or_risk_budget_rejected")
                continue
        allowed = gate.evaluate(account.broker, quantity * limits[symbol], side=side)
        if not allowed.allowed:
            adjustments.extend(f"{symbol}:{reason}" for reason in allowed.reasons)
            continue
        client_id = (
            "qb-" + sha256(f"{identity}|{context.snapshot_id}|{symbol}|{side}".encode()).hexdigest()[:32]
        )
        intents.append(OrderIntent(client_id, symbol, side, quantity, context.as_of))
        if side == "buy":
            cash_available -= quantity * limits[symbol]
            heat_available -= quantity * (limits[symbol] - stops[symbol])
    return TradePlan(tuple(intents), stops, limits, tuple(adjustments), decision)
