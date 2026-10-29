"""券商统一抽象接口。

实盘 runner 只依赖本接口，不依赖具体券商 SDK，
更换券商时只需新增一个实现类。
"""

from __future__ import annotations

from ..data.session import MarketSession

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass


# 只有这些状态可以证明订单不会再产生新的券商侧成交。未知状态必须按
# 工作订单处理，不能因为 SDK 新增了枚举值就误判为安全。
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "expired", "rejected"})


class BrokerCapabilityUnavailable(RuntimeError):
    """券商适配器无法提供生产安全所需的只读能力。"""


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    """券商侧订单的规范化只读快照。

    ``status`` 保留券商状态的小写文本；只有 :data:`TERMINAL_ORDER_STATUSES`
    中的值会被视为终态，其余（包括未来新增的未知状态）全部视为工作中。
    """

    order_id: str
    client_order_id: str | None
    symbol: str
    side: str
    quantity: int
    filled_quantity: int
    filled_avg_price: float | None
    status: str

    @property
    def is_terminal(self) -> bool:
        return self.status.lower() in TERMINAL_ORDER_STATUSES

    @property
    def is_open(self) -> bool:
        return not self.is_terminal

    @property
    def fill(self) -> tuple[int, float] | None:
        if self.filled_quantity <= 0 or self.filled_avg_price is None:
            return None
        if self.filled_avg_price <= 0:
            return None
        return self.filled_quantity, self.filled_avg_price


@dataclass
class BrokerPosition:
    """券商侧的一笔持仓快照。"""
    symbol: str
    shares: int
    avg_price: float
    market_value: float


@dataclass
class AccountInfo:
    """账户快照。"""
    equity: float          # 总权益
    cash: float            # 可用现金
    buying_power: float    # 购买力


class Broker(ABC):
    """券商操作抽象基类。"""

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """查询账户权益与现金。"""

    @abstractmethod
    def get_positions(self) -> dict[str, BrokerPosition]:
        """查询当前全部持仓，返回 {代码: 持仓}。"""

    def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        """按调用方幂等键读取订单。

        只有券商明确确认订单不存在时才可返回 ``None``；网络、鉴权、限流或
        未实现能力必须抛异常。默认实现故意 fail-closed，同时避免一次性破坏
        研究代码中的旧测试替身。
        """
        raise BrokerCapabilityUnavailable(
            f"{type(self).__name__} 不支持按 client_order_id 查询订单"
        )

    def list_open_orders(self) -> list[BrokerOrderSnapshot]:
        """返回账户内全部工作订单；读取失败必须抛异常，不能返回空列表。"""
        raise BrokerCapabilityUnavailable(
            f"{type(self).__name__} 不支持查询工作订单"
        )

    def account_fingerprint(self) -> str:
        """返回不暴露账号原文的稳定账户 SHA-256 身份。"""
        raise BrokerCapabilityUnavailable(
            f"{type(self).__name__} 不支持生成匿名账户指纹"
        )

    def get_market_session(self, trading_date: str) -> MarketSession:
        """读取指定交易日及前一会话；假日、未知或不支持时必须抛错。"""
        raise BrokerCapabilityUnavailable(f"{type(self).__name__} 不支持权威交易日历")

    @abstractmethod
    def market_buy(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        """市价买入，返回券商订单 ID。

        ``client_order_id`` 是调用方生成的确定性幂等键。券商支持时应将
        它原样传递给下游；省略时保持原有下单行为。
        """

    @abstractmethod
    def market_sell(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        """市价卖出，返回券商订单 ID；幂等语义同 ``market_buy``。"""

    @abstractmethod
    def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
        """轮询等待订单终态，返回是否完全成交。

        用于"先卖后买"时确认卖单成交、资金到账，
        避免买单因购买力不足被拒。
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤销订单，返回是否成功提交撤销请求。

        用于 wait_order 超时后的清理：超时 ≠ 拒单，订单可能仍在
        市场上存活——不撤单就继续后续操作会造成双倍建仓。
        """

    @abstractmethod
    def order_fill(self, order_id: str) -> tuple[int, float] | None:
        """查询订单实际成交结果：(成交股数, 成交均价)。

        未成交 / 查询失败返回 None。
        用于把真实成交价回填到本地状态（entry_price / init_risk），
        使实盘的金字塔触发、风险计算与回测假设保持一致。
        """

    @abstractmethod
    def get_last_price(self, symbol: str) -> float | None:
        """查询最新成交价（实时报价）；不可用时返回 None。

        用于下单前的隔夜跳空保护：开盘价已跌破止损的信号直接放弃。
        """

    @abstractmethod
    def is_market_open(self) -> bool:
        """当前是否处于交易时段。"""


class DryRunBroker(Broker):
    """演习模式券商：不真正下单，只把动作打印出来。

    用于在没有 API 密钥（或不想真实成交）的情况下
    验证实盘 runner 的完整流程。
    """

    def __init__(self, equity: float = 100000.0):
        self._equity = equity

    def get_account(self) -> AccountInfo:
        return AccountInfo(equity=self._equity, cash=self._equity, buying_power=self._equity)

    def get_positions(self) -> dict[str, BrokerPosition]:
        return {}

    def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        # 演习模式没有券商侧副作用，明确不存在任何远端订单。
        return None

    def list_open_orders(self) -> list[BrokerOrderSnapshot]:
        return []

    def account_fingerprint(self) -> str:
        return hashlib.sha256(b"quantbot.price_action:dry-run:v1").hexdigest()

    def market_buy(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        print(f"[dry-run] 买入 {symbol} x {shares}（未真实下单）")
        return client_order_id or "dry-run"

    def market_sell(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        print(f"[dry-run] 卖出 {symbol} x {shares}（未真实下单）")
        return client_order_id or "dry-run"

    def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
        return True  # 演习模式视为立即成交

    def cancel_order(self, order_id: str) -> bool:
        return True  # 演习模式视为撤销成功

    def order_fill(self, order_id: str) -> tuple[int, float] | None:
        return None  # 演习模式无成交明细，调用方回退到报价

    def get_last_price(self, symbol: str) -> float | None:
        return None  # 演习模式无实时报价，调用方回退到昨收价

    def is_market_open(self) -> bool:
        return True
