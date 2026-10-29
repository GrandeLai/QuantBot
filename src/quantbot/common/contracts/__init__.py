"""Pure data contracts shared across QuantBot products.

These contracts are intentionally behavior-light (dataclasses + light helpers)
so both stock-assistant and quant-assistant(-py) can depend on them without
implying coupling to either side's business logic.
"""

from quantbot.common.contracts.agent_advice import AdviceCard, AdviceEvidence, AdviceType
from quantbot.common.contracts.order import Order, OrderSide, OrderType
from quantbot.common.contracts.position import Position
from quantbot.common.contracts.risk import RiskCheckResult, RiskConfig
from quantbot.common.contracts.strategy import BaseStrategy, StrategyContext
from quantbot.common.contracts.trade import TradeRecord
from quantbot.common.contracts.validation import TimeSeriesValidationConfig, ValidationWindow

__all__ = [
    "AdviceCard",
    "AdviceEvidence",
    "AdviceType",
    "BaseStrategy",
    "Order",
    "OrderSide",
    "OrderType",
    "Position",
    "RiskCheckResult",
    "RiskConfig",
    "StrategyContext",
    "TimeSeriesValidationConfig",
    "TradeRecord",
    "ValidationWindow",
]
