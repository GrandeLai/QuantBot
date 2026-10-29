"""Operational risk controls beyond portfolio weights."""

from .regime import Regime
from .trading import (
    AccountStatePort,
    BrokerAccountRiskDecision,
    BrokerAccountRiskGate,
    BrokerAccountSnapshot,
    StaticAccountStatePort,
)


class BrokerDayTradeProtector:
    """Compatibility-friendly facade over broker-reported day-trade facts."""

    def __init__(self, account_state):
        if isinstance(account_state, BrokerAccountSnapshot):
            account_state = StaticAccountStatePort(account_state)
        if not isinstance(account_state, AccountStatePort):
            raise ValueError(
                "account_state must be BrokerAccountSnapshot or AccountStatePort"
            )
        self.account_state = account_state
        self.risk_gate = BrokerAccountRiskGate()

    def decision(
        self, requested_notional: float, side: str = "buy"
    ) -> BrokerAccountRiskDecision:
        return self.risk_gate.evaluate(
            self.account_state.account_snapshot(),
            requested_notional=requested_notional,
            side=side,
            day_trade=True,
        )

    def can_day_trade(self, requested_notional: float, side: str = "buy") -> bool:
        return self.decision(requested_notional, side=side).allowed


class PDTProtector(BrokerDayTradeProtector):
    """Deprecated name retained for callers; policy comes only from broker facts."""



class LiquidityShockMonitor:
    def __init__(self, max_spread_bps: float = 20, max_adv_participation: float = 0.01):
        self.max_spread_bps = max_spread_bps
        self.max_adv_participation = max_adv_participation

    def is_shocked(self, bid: float, ask: float, order_notional: float, adv_dollars: float) -> bool:
        mid = (bid + ask) / 2
        spread_bps = ((ask - bid) / mid * 10_000) if mid else float("inf")
        participation = order_notional / adv_dollars if adv_dollars else float("inf")
        return spread_bps > self.max_spread_bps or participation > self.max_adv_participation


class RegimeTurnoverLimiter:
    def __init__(
        self,
        bear_max_turnover: float = 0.25,
        normal_max_turnover: float = 1.0,
    ):
        self.bear_max_turnover = bear_max_turnover
        self.normal_max_turnover = normal_max_turnover

    def allow(self, regime: Regime, proposed_turnover: float) -> bool:
        if regime == Regime.R5_CRISIS:
            return False
        if regime == Regime.R4_BEAR:
            return proposed_turnover <= self.bear_max_turnover
        return proposed_turnover <= self.normal_max_turnover
