"""策略数据契约：StrategyContext + BaseStrategy 抽象基类."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from quantbot.common.contracts.order import Order, OrderSide, OrderType
from quantbot.common.contracts.position import Position

if TYPE_CHECKING:
    from quantbot.common.data.models import OHLCVBar


@dataclass
class StrategyContext:
    """策略运行时上下文，由回测引擎注入."""

    symbol: str
    timeframe: str
    initial_cash: float = 1_000_000.0
    cash: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    orders: list[Order] = field(default_factory=list)
    params: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cash == 0.0:
            self.cash = self.initial_cash

    def buy(
        self,
        symbol: str,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        comment: str = "",
    ) -> Order:
        """创建买入订单."""
        order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=order_type,
            quantity=quantity,
            price=price,
            comment=comment,
        )
        self.orders.append(order)
        return order

    def sell(
        self,
        symbol: str,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: float | None = None,
        comment: str = "",
    ) -> Order:
        """创建卖出订单."""
        order = Order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=order_type,
            quantity=quantity,
            price=price,
            comment=comment,
        )
        self.orders.append(order)
        return order

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol)

    def portfolio_value(self, current_prices: dict[str, float]) -> float:
        """计算组合总价值."""
        equity = sum(
            pos.quantity * current_prices.get(pos.symbol, pos.avg_price)
            for pos in self.positions.values()
        )
        return self.cash + equity


class BaseStrategy(ABC):
    """量化策略抽象基类.

    子类必须实现：
        - on_bar(bar, context): 每根 K 线到达时的处理逻辑

    可选覆盖：
        - on_init(context): 策略初始化
        - on_stop(context): 策略停止时的清仓/汇报逻辑
    """

    # 策略元信息（子类覆盖）
    name: str = "BaseStrategy"
    description: str = ""
    version: str = "1.0.0"
    author: str = ""

    # 默认参数（子类覆盖）
    default_params: dict[str, object] = {}

    def on_init(self, context: StrategyContext) -> None:  # noqa: B027
        """策略初始化（可选覆盖）."""

    @abstractmethod
    def on_bar(self, bar: OHLCVBar, context: StrategyContext) -> None:
        """处理新 K 线（子类必须实现）."""

    def on_stop(self, context: StrategyContext) -> None:  # noqa: B027
        """策略停止时执行（可选覆盖）."""

    def get_param(self, context: StrategyContext, key: str, default: object = None) -> object:
        """获取策略参数（优先从 context.params 读取，否则使用 default_params）."""
        return context.params.get(key, self.default_params.get(key, default))
