"""Broker-fact pre-trade risk decisions shared by all runtimes."""

from dataclasses import dataclass
import math
from typing import Tuple

from .domain import BrokerAccountSnapshot, MarginStatus


@dataclass(frozen=True)
class BrokerAccountRiskDecision:
    allowed: bool
    reasons: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.allowed, bool):
            raise ValueError("allowed must be a boolean")
        reasons = tuple(self.reasons)
        if any(not isinstance(reason, str) or not reason for reason in reasons):
            raise ValueError("reasons must contain non-empty strings")
        if self.allowed == bool(reasons):
            raise ValueError("allowed must be true exactly when reasons are empty")
        object.__setattr__(self, "reasons", reasons)


class BrokerAccountRiskGate:
    """Evaluates only facts reported by the broker; it infers no PDT rules."""

    def evaluate(
        self,
        snapshot: BrokerAccountSnapshot,
        requested_notional: float,
        side: str = "buy",
        day_trade: bool = False,
    ) -> BrokerAccountRiskDecision:
        if not isinstance(snapshot, BrokerAccountSnapshot):
            raise ValueError("snapshot must be a BrokerAccountSnapshot")
        if isinstance(requested_notional, bool):
            raise ValueError("requested_notional must be finite and positive")
        try:
            notional = float(requested_notional)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("requested_notional must be finite and positive")
        if not math.isfinite(notional) or notional <= 0.0:
            raise ValueError("requested_notional must be finite and positive")
        if side not in ("buy", "sell", "short"):
            raise ValueError("side must be buy, sell, or short")
        if not isinstance(day_trade, bool):
            raise ValueError("day_trade must be a boolean")

        reasons = []
        if snapshot.trading_blocked:
            reasons.append("account_trading_blocked")
        if snapshot.restrictions:
            reasons.append("account_restricted")
        if snapshot.margin_status == MarginStatus.UNKNOWN:
            reasons.append("margin_status_unknown")
        if side == "short" and not snapshot.shorting_enabled:
            reasons.append("shorting_disabled")

        consumes_buying_power = side in ("buy", "short")
        if consumes_buying_power and notional > snapshot.buying_power:
            reasons.append("insufficient_buying_power")
        if consumes_buying_power and day_trade:
            available = snapshot.day_trade_buying_power
            if available is None:
                reasons.append("day_trade_buying_power_unavailable")
            elif notional > available:
                reasons.append("insufficient_day_trade_buying_power")
        return BrokerAccountRiskDecision(not reasons, tuple(reasons))
