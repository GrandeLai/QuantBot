"""AI 决策契约只表达目标持仓，不开放工具、任意代码或订单参数。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import math
from typing import Mapping, Protocol

from quantbot.trading.domain import BrokerAccountSnapshot


def strict_json(text: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"JSON 包含重复字段：{key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"JSON 不允许 {value}")

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid_constant)


@dataclass(frozen=True)
class MarketContext:
    snapshot_id: str
    as_of: datetime
    features: Mapping[str, dict]
    regime: str
    benchmark: str
    sec_events: dict | None = None

    @property
    def prices(self) -> dict[str, float]:
        return {symbol: values["close"] for symbol, values in self.features.items()}

    def payload(self) -> dict:
        payload = {
            "snapshot_id": self.snapshot_id,
            "as_of": self.as_of.isoformat(),
            "regime": self.regime,
            "benchmark": self.benchmark,
            "features": dict(self.features),
        }
        if self.sec_events is not None:
            payload["sec_events"] = self.sec_events
        return payload


@dataclass(frozen=True)
class AccountState:
    cash: float
    equity: float
    positions: Mapping[str, int]
    broker: BrokerAccountSnapshot
    stops: Mapping[str, float] = field(default_factory=dict)
    dividend_receivable: float = 0.0
    last_exit_bar: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if any(isinstance(v, bool) or not math.isfinite(v) or v < 0 for v in (self.cash, self.equity, self.dividend_receivable)):
            raise ValueError("账户现金与权益必须为非负有限数值")
        if any(type(q) is not int or q < 0 for q in self.positions.values()):
            raise ValueError("主交易链路只接受整数股多头持仓")
        if not isinstance(self.broker, BrokerAccountSnapshot):
            raise ValueError("必须提供真实或明确模拟的券商账户事实")

    def payload(self, context: MarketContext) -> dict:
        if set(self.positions) - set(context.features):
            raise ValueError("账户持有股票池以外的资产，使用专用模拟账户或补齐行情")
        return {
            "cash": self.cash,
            "equity": self.equity,
            "dividend_receivable": self.dividend_receivable,
            "positions": dict(self.positions),
            "current_weights": {
                s: q * context.prices[s] / self.equity if self.equity else 0
                for s, q in self.positions.items()
            },
        }


@dataclass(frozen=True)
class Allocation:
    symbol: str
    target_weight: float
    confidence: float
    rationale: str


@dataclass(frozen=True)
class DecisionBatch:
    allocations: tuple[Allocation, ...]
    provider: str
    model: str
    response_id: str = ""
    usage: dict = field(default_factory=dict)


class AllocationAccount(Protocol):
    """Common model input; execution quantities remain market-specific."""

    def payload(self, context: MarketContext) -> dict: ...


class DecisionProvider(Protocol):
    def decide(self, context: MarketContext, account: AllocationAccount, config: dict) -> DecisionBatch: ...


def validate_decision(payload: dict, context: MarketContext) -> tuple[Allocation, ...]:
    if not isinstance(payload, dict) or set(payload) != {"snapshot_id", "as_of", "decisions"}:
        raise ValueError("模型返回必须只包含 snapshot_id、as_of、decisions")
    if payload["snapshot_id"] != context.snapshot_id:
        raise ValueError("模型决策引用了错误的行情快照 snapshot_id")
    if payload["as_of"] != context.as_of.isoformat():
        raise ValueError("模型决策引用了错误的行情时间 as_of")
    decisions = payload["decisions"]
    if not isinstance(decisions, list) or len(decisions) != len(context.features):
        raise ValueError("模型必须对股票池每个标的返回一个目标持仓")
    allocations = []
    seen = set()
    for item in decisions:
        if not isinstance(item, dict) or set(item) != {"symbol", "target_weight", "confidence", "rationale"}:
            raise ValueError("模型决策字段不符合契约")
        symbol = item["symbol"]
        if not isinstance(symbol, str) or symbol not in context.features or symbol in seen:
            raise ValueError("模型返回了未知或重复股票代码")
        seen.add(symbol)
        for key in ("target_weight", "confidence"):
            value = item[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"模型 {key} 必须为有限数值")
            if not 0 <= value <= 1:
                raise ValueError(f"模型 {key} 必须在 0..1 之间")
        if not isinstance(item["rationale"], str) or not 1 <= len(item["rationale"].strip()) <= 2000:
            raise ValueError("模型必须提供不超过 2000 字符的决策理由")
        allocations.append(
            Allocation(
                symbol, float(item["target_weight"]), float(item["confidence"]), item["rationale"].strip()
            )
        )
    return tuple(allocations)
