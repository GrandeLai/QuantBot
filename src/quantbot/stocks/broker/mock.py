"""Mock 交易 Provider.

仅用于：
- 本地无 Longbridge 凭证 / SDK 时的 UI 验证
- 集成测试与演示兜底

注意：它不是产品主路径。主目标始终是 broker sandbox/testnet。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from quantbot.stocks.broker.catalog import find_security_by_symbol, find_supported_securities
from quantbot.stocks.broker.types import (
    TradingAccountOverview,
    TradingAssetType,
    TradingCancelResult,
    TradingCapability,
    TradingCashFlow,
    TradingCashInfo,
    TradingExecution,
    TradingMarket,
    TradingMode,
    TradingOrder,
    TradingOrderEstimate,
    TradingOrderEstimateRequest,
    TradingOrderRequest,
    TradingOrderSide,
    TradingOrderStatus,
    TradingOrderType,
    TradingPosition,
    TradingProviderError,
    TradingProviderKind,
    TradingProviderStatus,
    TradingQuote,
    TradingSecurity,
    TradingSessionStatus,
    TradingSubmitResult,
)
from quantbot.common.config import get_settings


class MockTradingProvider:
    """面向 Longbridge 返回结构设计的 mock provider."""

    def __init__(
        self,
        *,
        fallback_reason: str | None = None,
        using_fallback: bool = False,
    ) -> None:
        self._fallback_reason = fallback_reason
        self._using_fallback = using_fallback
        self._cash = 250_000.0
        self._currency = "USD"
        self._quote_seed: dict[str, tuple[float, float]] = {
            "AAPL.US": (192.84, 190.10),
            "TSLA.US": (176.22, 179.05),
            "NVDA.US": (122.71, 120.91),
            "MSFT.US": (423.66, 419.34),
            "SPY.US": (517.50, 514.92),
            "QQQ.US": (441.28, 438.33),
            "700.HK": (311.40, 308.80),
            "9988.HK": (74.95, 73.25),
            "2800.HK": (18.62, 18.48),
        }
        self._positions: dict[str, dict[str, Any]] = {}
        self._orders: list[TradingOrder] = []
        self._executions: list[TradingExecution] = []
        self._cash_flows: list[TradingCashFlow] = []
        self._pending_partial_fills: dict[str, dict[str, float]] = {}
        self._order_seq = 1
        self._execution_seq = 1
        self._cash_flow_seq = 1
        self._force_session = get_settings().trading_mock_force_session

    def get_status(self) -> TradingProviderStatus:
        return TradingProviderStatus(
            provider=TradingProviderKind.MOCK,
            mode=TradingMode.PAPER,
            configured=True,
            using_mock_fallback=self._using_fallback,
            reason=self._fallback_reason,
            capabilities=TradingCapability(
                supported_markets=[TradingMarket.US, TradingMarket.HK],
                supported_asset_types=[
                    TradingAssetType.STOCK,
                    TradingAssetType.ETF,
                    TradingAssetType.WARRANT,
                ],
                supported_order_types=[
                    TradingOrderType.MARKET,
                    TradingOrderType.LIMIT,
                ],
                notes=[
                    "Mock provider 仅用于本地兜底，正式安全执行目标为 broker sandbox/testnet。",
                    "Broker sandbox/testnet 不支持美股 OTC、盘前盘后交易、期权交易。",
                    "美股做空能力取决于 broker sandbox/testnet，当前 UI 未开放独立融券做空流程。",
                ],
            ),
        )

    def search_securities(self, query: str, *, limit: int = 20) -> list[TradingSecurity]:
        return find_supported_securities(query, limit=limit)

    def get_quotes(self, symbols: list[str]) -> list[TradingQuote]:
        items: list[TradingQuote] = []
        for symbol in symbols:
            security = self._require_supported_security(symbol)
            last_price, prev_close = self._quote_seed.get(symbol, (100.0, 99.0))
            change = last_price - prev_close
            change_pct = change / prev_close if prev_close else 0.0
            items.append(
                TradingQuote(
                    symbol=security.symbol,
                    name=security.name,
                    market=security.market,
                    currency=security.currency,
                    asset_type=security.asset_type,
                    last_price=round(last_price, 4),
                    prev_close=round(prev_close, 4),
                    change=round(change, 4),
                    change_pct=round(change_pct, 6),
                    trade_session=self._trade_session_for_market(security.market),
                    trade_status="normal",
                    tradeable=security.tradeable,
                    restrictions=list(security.restrictions),
                )
            )
        return items

    def get_account_overview(self) -> TradingAccountOverview:
        positions = self.get_positions()
        positions_market_value = sum(item.market_value for item in positions)
        total_cost = sum(item.cost_price * item.quantity for item in positions)
        total_pnl = sum(item.unrealized_pnl for item in positions)
        today_pnl = sum(item.day_pnl for item in positions)
        total_assets = self._cash + positions_market_value

        return TradingAccountOverview(
            provider=TradingProviderKind.MOCK,
            mode=TradingMode.PAPER,
            currency=self._currency,
            total_assets=round(total_assets, 2),
            available_cash=round(self._cash, 2),
            withdrawable_cash=round(self._cash, 2),
            buying_power=round(self._cash, 2),
            positions_market_value=round(positions_market_value, 2),
            today_pnl=round(today_pnl, 2),
            today_pnl_pct=round(today_pnl / (total_assets - today_pnl), 6) if total_assets > today_pnl else 0.0,
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl / total_cost, 6) if total_cost > 0 else 0.0,
            cash_infos=[
                TradingCashInfo(
                    currency=self._currency,
                    available_cash=round(self._cash, 2),
                    withdraw_cash=round(self._cash, 2),
                )
            ],
            updated_at=self._now(),
            warnings=[
                "当前为 mock fallback，返回结构已对齐 Longbridge 交易接口，但成交与资产变化仅用于本地验证。",
            ],
        )

    def get_positions(self) -> list[TradingPosition]:
        items: list[TradingPosition] = []
        for symbol, raw in self._positions.items():
            security = self._require_supported_security(symbol)
            quote = self.get_quotes([symbol])[0]
            quantity = int(raw["quantity"])
            available_quantity = int(raw["available_quantity"])
            cost_price = float(raw["cost_price"])
            market_value = quote.last_price * quantity
            unrealized_pnl = (quote.last_price - cost_price) * quantity
            cost_basis = cost_price * quantity
            day_pnl = (quote.last_price - quote.prev_close) * quantity
            day_base = quote.prev_close * quantity if quote.prev_close > 0 else 0.0
            items.append(
                TradingPosition(
                    symbol=symbol,
                    name=security.name,
                    market=security.market,
                    currency=security.currency,
                    asset_type=security.asset_type,
                    quantity=quantity,
                    available_quantity=available_quantity,
                    cost_price=round(cost_price, 4),
                    last_price=quote.last_price,
                    market_value=round(market_value, 2),
                    unrealized_pnl=round(unrealized_pnl, 2),
                    unrealized_pnl_pct=round(unrealized_pnl / cost_basis, 6) if cost_basis > 0 else 0.0,
                    day_pnl=round(day_pnl, 2),
                    day_pnl_pct=round(day_pnl / day_base, 6) if day_base > 0 else 0.0,
                )
            )
        return sorted(items, key=lambda item: item.market_value, reverse=True)

    def get_today_orders(self) -> list[TradingOrder]:
        self._advance_partial_orders()
        return list(reversed(self._orders))

    def get_history_orders(self) -> list[TradingOrder]:
        self._advance_partial_orders()
        return list(reversed(self._orders))

    def get_order_detail(self, order_id: str) -> TradingOrder:
        self._advance_partial_orders(target_order_id=order_id)
        order = next((item for item in self._orders if item.order_id == order_id), None)
        if order is None:
            raise TradingProviderError("订单不存在", code="order_not_found", status_code=404)
        return order

    def estimate_order(self, request: TradingOrderEstimateRequest) -> TradingOrderEstimate:
        security = self._validate_security_for_trade(request.symbol)
        quote = self.get_quotes([request.symbol])[0]
        reference_price = quote.last_price if request.order_type == TradingOrderType.MARKET else float(
            request.submitted_price or quote.last_price
        )
        cash_max_qty = int(self._cash // max(reference_price, 0.01))
        if security.lot_size > 1:
            cash_max_qty = (cash_max_qty // security.lot_size) * security.lot_size
        position = self._positions.get(request.symbol)
        sell_max_qty = int(position["available_quantity"]) if position is not None else 0
        reason = None
        if request.side == TradingOrderSide.SELL and sell_max_qty <= 0:
            reason = "当前未持有可卖数量；broker sandbox/testnet 支持能力取决于券商，当前 UI 未开放做空下单流程。"
        return TradingOrderEstimate(
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            reference_price=round(reference_price, 4),
            cash_max_qty=max(cash_max_qty, 0),
            sell_max_qty=max(sell_max_qty, 0),
            reason=reason,
        )

    def submit_order(self, request: TradingOrderRequest) -> TradingSubmitResult:
        security = self._validate_security_for_trade(request.symbol)
        quote = self.get_quotes([request.symbol])[0]
        reference_price = quote.last_price

        if request.order_type == TradingOrderType.LIMIT and request.submitted_price is None:
            raise TradingProviderError("限价单必须填写限价", code="invalid_price")

        if security.lot_size > 1 and request.quantity % security.lot_size != 0:
            raise TradingProviderError(
                f"{security.symbol} 每手 {security.lot_size} 股，请按整手数量下单",
                code="invalid_lot_size",
            )

        if request.side == TradingOrderSide.SELL:
            position = self._positions.get(request.symbol)
            available_quantity = int(position["available_quantity"]) if position is not None else 0
            if available_quantity < request.quantity:
                raise TradingProviderError(
                    "可卖数量不足。当前版本未开放美股卖空流程，请仅卖出已有持仓。",
                    code="insufficient_position",
                )

        order_id = f"MOCK-{self._order_seq:06d}"
        self._order_seq += 1
        submitted_at = self._now()
        order = TradingOrder(
            order_id=order_id,
            symbol=security.symbol,
            name=security.name,
            market=security.market,
            currency=security.currency,
            asset_type=security.asset_type,
            side=request.side,
            order_type=request.order_type,
            status=TradingOrderStatus.SUBMITTED,
            quantity=request.quantity,
            executed_quantity=0,
            submitted_price=request.submitted_price,
            trigger_price=request.trigger_price,
            submitted_at=submitted_at,
            updated_at=submitted_at,
        )
        self._orders.append(order)

        fill_price = reference_price if request.order_type == TradingOrderType.MARKET else (request.submitted_price or 0.0)
        should_fill = request.order_type == TradingOrderType.MARKET or (
            request.side == TradingOrderSide.BUY and fill_price >= reference_price
        ) or (
            request.side == TradingOrderSide.SELL and fill_price <= reference_price
        )

        if should_fill:
            partial_qty = self._partial_fill_quantity(order)
            if partial_qty < order.quantity:
                self._apply_fill(order, fill_price, partial_qty, final=False)
                self._pending_partial_fills[order.order_id] = {
                    "remaining_quantity": float(order.quantity - partial_qty),
                    "price": fill_price,
                }
            else:
                self._fill_order(order, fill_price)

        return TradingSubmitResult(
            provider=TradingProviderKind.MOCK,
            order_id=order.order_id,
            status=order.status,
            message="mock provider 已受理订单",
        )

    def cancel_order(self, order_id: str) -> TradingCancelResult:
        order = self.get_order_detail(order_id)
        if order.status not in {TradingOrderStatus.SUBMITTED, TradingOrderStatus.PENDING_SUBMIT}:
            raise TradingProviderError("当前订单状态不支持撤单", code="order_not_cancelable")
        order.status = TradingOrderStatus.CANCELED
        order.updated_at = self._now()
        order.message = "mock provider 已撤单"
        return TradingCancelResult(
            provider=TradingProviderKind.MOCK,
            order_id=order_id,
            status=order.status,
            message="撤单成功",
        )

    def get_today_executions(self) -> list[TradingExecution]:
        return list(reversed(self._executions))

    def get_history_executions(self) -> list[TradingExecution]:
        return list(reversed(self._executions))

    def get_cash_flows(self) -> list[TradingCashFlow]:
        return list(reversed(self._cash_flows))

    def _fill_order(self, order: TradingOrder, price: float) -> None:
        self._apply_fill(order, price, order.quantity - order.executed_quantity, final=True)

    def _apply_fill(self, order: TradingOrder, price: float, quantity: int, *, final: bool) -> None:
        order.executed_quantity += quantity
        order.executed_price = round(price, 4)
        order.updated_at = self._now()
        order.status = TradingOrderStatus.FILLED if final else TradingOrderStatus.PARTIAL_FILLED
        order.message = "mock provider 已成交" if final else "mock provider 部分成交"

        cash_delta = price * quantity
        existing = self._positions.get(order.symbol)

        if order.side == TradingOrderSide.BUY:
            if cash_delta > self._cash:
                order.status = TradingOrderStatus.REJECTED
                order.message = "资金不足"
                return
            self._cash -= cash_delta
            if existing is None:
                self._positions[order.symbol] = {
                    "quantity": quantity,
                    "available_quantity": quantity,
                    "cost_price": price,
                }
            else:
                current_qty = int(existing["quantity"])
                total_qty = current_qty + quantity
                average_cost = (
                    float(existing["cost_price"]) * current_qty + price * quantity
                ) / total_qty
                existing["quantity"] = total_qty
                existing["available_quantity"] = total_qty
                existing["cost_price"] = average_cost
            amount = -cash_delta
            direction = "debit"
            description = f"买入 {order.symbol}"
        else:
            if existing is None:
                order.status = TradingOrderStatus.REJECTED
                order.message = "持仓不足"
                return
            current_qty = int(existing["quantity"])
            next_qty = current_qty - quantity
            self._cash += cash_delta
            if next_qty <= 0:
                self._positions.pop(order.symbol, None)
            else:
                existing["quantity"] = next_qty
                existing["available_quantity"] = next_qty
            amount = cash_delta
            direction = "credit"
            description = f"卖出 {order.symbol}"

        execution_id = f"EXEC-{self._execution_seq:06d}"
        self._execution_seq += 1
        security = self._require_supported_security(order.symbol)
        self._executions.append(
            TradingExecution(
                execution_id=execution_id,
                order_id=order.order_id,
                symbol=order.symbol,
                name=security.name,
                market=security.market,
                currency=security.currency,
                asset_type=security.asset_type,
                side=order.side,
                price=round(price, 4),
                quantity=quantity,
                executed_at=self._now(),
            )
        )

        self._cash_flows.append(
            TradingCashFlow(
                cash_flow_id=f"FLOW-{self._cash_flow_seq:06d}",
                currency=security.currency,
                amount=round(amount, 2),
                balance=round(self._cash, 2),
                business_type="trade",
                direction=direction,
                description=description,
                symbol=order.symbol,
                occurred_at=self._now(),
            )
        )
        self._cash_flow_seq += 1

    def _advance_partial_orders(self, target_order_id: str | None = None) -> None:
        if not self._pending_partial_fills:
            return
        order_ids = [target_order_id] if target_order_id else list(self._pending_partial_fills.keys())
        for order_id in order_ids:
            state = self._pending_partial_fills.get(order_id)
            if state is None:
                continue
            order = next((item for item in self._orders if item.order_id == order_id), None)
            if order is None:
                self._pending_partial_fills.pop(order_id, None)
                continue
            self._apply_fill(order, float(state["price"]), int(state["remaining_quantity"]), final=True)
            self._pending_partial_fills.pop(order_id, None)

    def _partial_fill_quantity(self, order: TradingOrder) -> int:
        if order.order_type == TradingOrderType.MARKET and order.quantity >= 100:
            return max(order.quantity // 2, 1)
        return order.quantity

    def _validate_security_for_trade(self, symbol: str) -> TradingSecurity:
        security = self._require_supported_security(symbol)
        if security.asset_type in {TradingAssetType.OPTION, TradingAssetType.OTC}:
            raise TradingProviderError("当前 broker sandbox/testnet 不支持该资产类型", code="unsupported_asset")

        session = self._trade_session_for_market(security.market)
        if session in {
            TradingSessionStatus.PRE_MARKET,
            TradingSessionStatus.POST_MARKET,
            TradingSessionStatus.CLOSED,
            TradingSessionStatus.MIDDAY_BREAK,
        }:
            raise TradingProviderError(
                "当前不在 broker sandbox/testnet 支持的常规交易时段内",
                code="unsupported_session",
            )
        return security

    def _require_supported_security(self, symbol: str) -> TradingSecurity:
        security = find_security_by_symbol(symbol)
        if security is None:
            raise TradingProviderError("未找到可交易标的，请从搜索结果中选择", code="security_not_found", status_code=404)
        return security

    def _trade_session_for_market(self, market: TradingMarket) -> TradingSessionStatus:
        if self._force_session == "regular":
            return TradingSessionStatus.REGULAR

        now_utc = datetime.now(tz=UTC)
        if market == TradingMarket.US:
            local = now_utc.astimezone(ZoneInfo("America/New_York"))
            if local.weekday() >= 5:
                return TradingSessionStatus.CLOSED
            current_minutes = local.hour * 60 + local.minute
            if 570 <= current_minutes < 960:
                return TradingSessionStatus.REGULAR
            if 240 <= current_minutes < 570:
                return TradingSessionStatus.PRE_MARKET
            if 960 <= current_minutes < 1200:
                return TradingSessionStatus.POST_MARKET
            return TradingSessionStatus.CLOSED

        if market == TradingMarket.HK:
            local = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))
            if local.weekday() >= 5:
                return TradingSessionStatus.CLOSED
            current_minutes = local.hour * 60 + local.minute
            if 570 <= current_minutes < 720 or 780 <= current_minutes < 960:
                return TradingSessionStatus.REGULAR
            if 720 <= current_minutes < 780:
                return TradingSessionStatus.MIDDAY_BREAK
            return TradingSessionStatus.CLOSED

        return TradingSessionStatus.UNKNOWN

    def _now(self) -> str:
        return datetime.now(tz=UTC).isoformat()
