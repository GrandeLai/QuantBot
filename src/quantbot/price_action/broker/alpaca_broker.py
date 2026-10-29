"""Alpaca 券商实现。

Alpaca（https://alpaca.markets）的特点：
    - 同一套 API，通过 ``paper=True/False`` 切换模拟盘与实盘，
      策略代码零改动；
    - 美股零佣金；
    - 模拟盘免费注册即可使用，非常适合先跑 paper trading 验证系统。

密钥通过环境变量注入（见 config.py），
模拟盘与实盘的密钥是两套，注意不要混用。

★ 代码格式差异：
    Yahoo 用连字符表示股票类别（BRK-B），Alpaca 用点号（BRK.B）。
    本模块在下单/查询时自动做双向转换，系统内部统一使用 Yahoo 格式。
"""

from __future__ import annotations

import hashlib
import time
from datetime import date, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, GetOrdersRequest, MarketOrderRequest

from ..config import get_alpaca_keys
from ..data.session import EASTERN, MarketSession
from .base import (
    AccountInfo,
    Broker,
    BrokerCapabilityUnavailable,
    BrokerOrderSnapshot,
    BrokerPosition,
)

# 订单终态集合（不再变化的状态）
_TERMINAL_STATUS = {"filled", "canceled", "expired", "rejected", "done_for_day"}


def to_alpaca_symbol(symbol: str) -> str:
    """Yahoo 格式 → Alpaca 格式（BRK-B → BRK.B）。"""
    return symbol.replace("-", ".")


def to_yahoo_symbol(symbol: str) -> str:
    """Alpaca 格式 → Yahoo 格式（BRK.B → BRK-B）。"""
    return symbol.replace(".", "-")


class AlpacaBroker(Broker):
    """基于 alpaca-py SDK 的券商实现。"""

    def __init__(self, paper: bool = True):
        """
        参数:
            paper: True 连接模拟盘，False 连接实盘。
        """
        key, secret = get_alpaca_keys()
        self.client = TradingClient(key, secret, paper=paper)
        # 行情客户端：免费账户走 IEX 数据，足够做下单前的价格复核
        self.data_client = StockHistoricalDataClient(key, secret)
        self.paper = paper
        mode = "模拟盘 (paper)" if paper else "实盘 (LIVE)"
        print(f"[broker] 已连接 Alpaca {mode}")

    def get_account(self) -> AccountInfo:
        acct = self.client.get_account()
        return AccountInfo(
            equity=float(acct.equity),
            cash=float(acct.cash),
            buying_power=float(acct.buying_power),
        )

    def account_fingerprint(self) -> str:
        """绑定 Alpaca 账户与 paper/live 域，但绝不把账号原文写入账本。"""
        account = self.client.get_account()
        identifier = self._order_field(account, "id") \
            or self._order_field(account, "account_number")
        if identifier is None or not str(identifier).strip():
            raise BrokerCapabilityUnavailable("Alpaca 账户响应缺少稳定账户身份")
        mode = "paper" if self.paper else "live"
        payload = f"alpaca:{mode}:{identifier}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def get_positions(self) -> dict[str, BrokerPosition]:
        """查询持仓，键为 Yahoo 格式代码（系统内部统一格式）。"""
        out: dict[str, BrokerPosition] = {}
        for p in self.client.get_all_positions():
            sym = to_yahoo_symbol(p.symbol)
            out[sym] = BrokerPosition(
                symbol=sym,
                shares=int(float(p.qty)),
                avg_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
            )
        return out

    @staticmethod
    def _order_field(order, name: str):
        """兼容 alpaca-py 模型和 SDK 的原始 dict 响应。"""
        if isinstance(order, dict):
            return order.get(name)
        return getattr(order, name, None)

    @staticmethod
    def _enum_value(value):
        return value.value if hasattr(value, "value") else value

    def _normalize_order(self, order) -> BrokerOrderSnapshot:
        """把 alpaca-py 模型规范化为稳定的公共只读契约。"""
        order_id = self._order_field(order, "id")
        symbol = self._order_field(order, "symbol")
        side = self._enum_value(self._order_field(order, "side"))
        status = self._enum_value(self._order_field(order, "status"))
        quantity = self._order_field(order, "qty")
        filled_quantity = self._order_field(order, "filled_qty")
        filled_avg_price = self._order_field(order, "filled_avg_price")
        if order_id is None or symbol is None or side is None or quantity is None:
            raise RuntimeError("Alpaca 订单响应缺少必要字段")
        try:
            normalized_quantity = int(float(quantity))
            normalized_filled = int(float(filled_quantity or 0))
            normalized_avg = (
                None if filled_avg_price in (None, "") else float(filled_avg_price)
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Alpaca 订单数量或成交价格式非法") from exc
        return BrokerOrderSnapshot(
            order_id=str(order_id),
            client_order_id=(
                str(self._order_field(order, "client_order_id"))
                if self._order_field(order, "client_order_id") not in (None, "")
                else None
            ),
            symbol=to_yahoo_symbol(str(symbol)),
            side=str(side).lower(),
            quantity=normalized_quantity,
            filled_quantity=normalized_filled,
            filled_avg_price=normalized_avg,
            # SDK 新状态不能静默变成终态；缺失状态显式保留为 unknown。
            status=str(status).lower() if status not in (None, "") else "unknown",
        )

    def _validate_recovered_order(
        self,
        order,
        *,
        symbol: str,
        shares: int,
        side: OrderSide,
        client_order_id: str,
    ) -> str:
        """确认 client ID 对应的是同一订单意图，返回 broker order ID。"""
        expected_symbol = to_alpaca_symbol(symbol)
        actual_symbol = self._order_field(order, "symbol")
        actual_side = self._enum_value(self._order_field(order, "side"))
        actual_qty = self._order_field(order, "qty")
        actual_type = self._order_field(order, "order_type") \
            or self._order_field(order, "type")
        actual_type = self._enum_value(actual_type)
        actual_tif = self._enum_value(self._order_field(order, "time_in_force"))
        actual_client_id = self._order_field(order, "client_order_id")
        order_id = self._order_field(order, "id")

        same_intent = (
            actual_symbol == expected_symbol
            and actual_side == side.value
            and actual_qty is not None
            and float(actual_qty) == float(shares)
            and actual_type == OrderType.MARKET.value
            and actual_tif == TimeInForce.DAY.value
            and actual_client_id == client_order_id
            and order_id is not None
        )
        if not same_intent:
            raise RuntimeError(
                f"Alpaca client_order_id {client_order_id!r} 已对应不同订单意图，"
                "拒绝复用"
            )
        return str(order_id)

    def _lookup_by_client_order_id(
        self,
        client_order_id: str,
    ):
        """按 client ID 查订单；只有明确 404 才表示尚不存在。

        网络超时等未知读取结果必须向上抛出，不能继续提交写请求。
        """
        try:
            return self.client.get_order_by_client_id(client_order_id)
        except Exception as exc:  # noqa: BLE001 - SDK 异常类型跨版本变化
            if getattr(exc, "status_code", None) == 404:
                return None
            raise RuntimeError(
                f"无法确认 Alpaca client_order_id {client_order_id!r} 是否已存在"
            ) from exc

    def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        """按 client ID 读取并规范化订单；只有明确 404 返回 ``None``。"""
        if not isinstance(client_order_id, str) or not client_order_id.strip():
            raise ValueError("client_order_id 必须是非空字符串")
        order = self._lookup_by_client_order_id(client_order_id)
        return None if order is None else self._normalize_order(order)

    def list_open_orders(self) -> list[BrokerOrderSnapshot]:
        """读取账户内全部工作订单；任何读取失败都向上抛出。"""
        try:
            orders = self.client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
            )
        except Exception as exc:  # noqa: BLE001 - SDK 异常类型跨版本变化
            raise RuntimeError("无法读取 Alpaca 工作订单") from exc
        return [self._normalize_order(order) for order in orders]

    def _submit_market_order(
        self,
        symbol: str,
        shares: int,
        side: OrderSide,
        client_order_id: str | None,
    ) -> str:
        """提交或恢复一笔幂等市价单，未知写结果后绝不重发。"""
        if client_order_id is not None:
            if not isinstance(client_order_id, str) or not client_order_id.strip():
                raise ValueError("client_order_id 必须是非空字符串")
            existing = self._lookup_by_client_order_id(client_order_id)
            if existing is not None:
                return self._validate_recovered_order(
                    existing,
                    symbol=symbol,
                    shares=shares,
                    side=side,
                    client_order_id=client_order_id,
                )

        request = MarketOrderRequest(
            symbol=to_alpaca_symbol(symbol),
            qty=shares,
            side=side,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        try:
            order = self.client.submit_order(request)
        except Exception as submit_error:  # noqa: BLE001 - 写结果可能已在服务端成功
            if client_order_id is None:
                raise
            # 只读恢复：提交响应丢失或重复 client ID 时查询原订单，绝不重发。
            try:
                recovered = self._lookup_by_client_order_id(client_order_id)
            except Exception as lookup_error:
                raise RuntimeError(
                    f"Alpaca 订单 {client_order_id!r} 提交结果未知，且恢复查询失败；"
                    "禁止自动重试写请求"
                ) from lookup_error
            if recovered is None:
                raise submit_error
            return self._validate_recovered_order(
                recovered,
                symbol=symbol,
                shares=shares,
                side=side,
                client_order_id=client_order_id,
            )
        order_id = self._order_field(order, "id")
        if order_id is not None:
            return str(order_id)
        if client_order_id is not None:
            recovered = self._lookup_by_client_order_id(client_order_id)
            if recovered is not None:
                return self._validate_recovered_order(
                    recovered,
                    symbol=symbol,
                    shares=shares,
                    side=side,
                    client_order_id=client_order_id,
                )
        raise RuntimeError("Alpaca 提交响应缺少 broker order id，订单结果未知")

    def market_buy(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        """市价买入。使用 DAY 有效期：当日未成交自动取消。"""
        order_id = self._submit_market_order(
            symbol,
            shares,
            OrderSide.BUY,
            client_order_id,
        )
        print(f"[broker] 已提交/恢复买单 {symbol} x {shares}，订单号 {order_id}")
        return order_id

    def market_sell(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        """市价卖出。"""
        order_id = self._submit_market_order(
            symbol,
            shares,
            OrderSide.SELL,
            client_order_id,
        )
        print(f"[broker] 已提交/恢复卖单 {symbol} x {shares}，订单号 {order_id}")
        return order_id

    def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
        """轮询订单直至终态或超时，返回是否完全成交。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            order = self.client.get_order_by_id(order_id)
            status = str(order.status.value if hasattr(order.status, "value")
                         else order.status)
            if status == "filled":
                return True
            if status in _TERMINAL_STATUS:
                print(f"[broker] 订单 {order_id} 终态为 {status}（未完全成交）")
                return False
            time.sleep(2)
        print(f"[broker] 订单 {order_id} 等待成交超时（{timeout}s）")
        return False

    def cancel_order(self, order_id: str) -> bool:
        """撤销订单（超时清理用）。撤销失败（可能已成交）返回 False。"""
        try:
            self.client.cancel_order_by_id(order_id)
            return True
        except Exception as e:  # noqa: BLE001 - 已成交/已撤销时会抛错
            print(f"[broker] 撤销订单 {order_id} 失败（可能已成交）: {e}")
            return False

    def order_fill(self, order_id: str) -> tuple[int, float] | None:
        """查询订单实际成交股数与均价（未成交返回 None）。"""
        try:
            order = self.client.get_order_by_id(order_id)
            qty = int(float(order.filled_qty or 0))
            avg = float(order.filled_avg_price or 0)
            if qty > 0 and avg > 0:
                return qty, avg
        except Exception as e:  # noqa: BLE001 - 查询失败按未知处理
            print(f"[broker] 查询订单 {order_id} 成交明细失败: {e}")
        return None

    def get_last_price(self, symbol: str) -> float | None:
        """查询最新成交价（IEX 实时数据）。"""
        try:
            req = StockLatestTradeRequest(
                symbol_or_symbols=to_alpaca_symbol(symbol)
            )
            trades = self.data_client.get_stock_latest_trade(req)
            trade = trades[to_alpaca_symbol(symbol)]
            return float(trade.price)
        except Exception as e:  # noqa: BLE001 - 报价失败不应中断整个流程
            print(f"[broker] 获取 {symbol} 实时价失败: {e}")
            return None

    def is_market_open(self) -> bool:
        return bool(self.client.get_clock().is_open)

    def get_market_session(self, trading_date: str) -> MarketSession:
        day = date.fromisoformat(trading_date)
        calendar = sorted(self.client.get_calendar(GetCalendarRequest(
            start=day - timedelta(days=30), end=day
        )), key=lambda session: session.date)
        if len(calendar) < 2 or calendar[-1].date != day:
            raise RuntimeError(f"{trading_date} 不是可确认的交易会话")
        previous, current = calendar[-2:]
        # alpaca-py 的 Calendar 将接口的美东 HH:MM 解析成无时区 datetime。
        def eastern(value):
            return (value.replace(tzinfo=EASTERN) if value.tzinfo is None
                    else value.astimezone(EASTERN)).isoformat()

        return MarketSession(
            trading_date=trading_date, signal_date=previous.date.isoformat(),
            opens_at=eastern(current.open), closes_at=eastern(current.close),
            source="alpaca:calendar",
        )
