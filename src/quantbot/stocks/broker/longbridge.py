"""Longbridge sandbox Provider."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
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
from quantbot.common.security.keystore import load_api_key


class LongbridgeTradingProvider:
    """基于 Longbridge 官方 OpenAPI 的 sandbox provider."""

    def __init__(self) -> None:
        self._settings = get_settings()
        self._config = self._build_config()
        self._quote_context, self._trade_context, self._sdk = self._build_contexts()

    def get_status(self) -> TradingProviderStatus:
        return TradingProviderStatus(
            provider=TradingProviderKind.LONGBRIDGE,
            mode=TradingMode.PAPER,
            configured=True,
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
                    "底层交易能力基于 Longbridge sandbox。",
                    "Longbridge sandbox 支持港股 / 美股股票、ETF、港股轮证。",
                    "Longbridge sandbox 支持美股股票做空，但当前 UI 未开放完整做空流程。",
                    "Longbridge sandbox 暂不支持美股 OTC、盘前盘后交易、期权交易。",
                ],
            ),
        )

    def search_securities(self, query: str, *, limit: int = 20) -> list[TradingSecurity]:
        catalog_matches = find_supported_securities(query, limit=limit)
        if catalog_matches:
            return catalog_matches

        normalized = query.strip().upper()
        if "." not in normalized:
            return []
        try:
            info_items = self._quote_context.static_info([normalized])
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 标的查询失败: {exc}", code="security_search_failed") from exc
        return [self._map_security_info(item) for item in info_items][:limit]

    def get_quotes(self, symbols: list[str]) -> list[TradingQuote]:
        if not symbols:
            return []
        try:
            static_infos = {item.symbol: item for item in self._quote_context.static_info(symbols)}
            quotes = {item.symbol: item for item in self._quote_context.quote(symbols)}
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 行情查询失败: {exc}", code="quote_failed") from exc

        results: list[TradingQuote] = []
        for symbol in symbols:
            quote = quotes.get(symbol)
            info = static_infos.get(symbol)
            if quote is None:
                continue
            results.append(self._map_quote(symbol=symbol, quote=quote, info=info))
        return results

    def get_account_overview(self) -> TradingAccountOverview:
        balances = self._trade_context.account_balance()
        positions = self.get_positions()
        positions_market_value = sum(item.market_value for item in positions)
        total_pnl = sum(item.unrealized_pnl for item in positions)
        today_pnl = sum(item.day_pnl for item in positions)
        default_currency = "USD"

        cash_infos: list[TradingCashInfo] = []
        available_cash = 0.0
        withdrawable_cash = 0.0
        buying_power = 0.0
        total_assets = 0.0
        for item in balances:
            cash_infos.append(
                TradingCashInfo(
                    currency=str(getattr(item, "currency", default_currency)),
                    available_cash=self._to_float(getattr(item, "available_cash", 0.0)),
                    withdraw_cash=self._to_float(getattr(item, "withdraw_cash", 0.0)),
                    frozen_cash=self._to_float(getattr(item, "frozen_cash", 0.0)),
                    settling_cash=self._to_float(getattr(item, "settling_cash", 0.0)),
                )
            )
            available_cash += self._to_float(getattr(item, "available_cash", 0.0))
            withdrawable_cash += self._to_float(getattr(item, "withdraw_cash", 0.0))
            buying_power += self._to_float(getattr(item, "max_finance_amount", 0.0)) or self._to_float(
                getattr(item, "available_cash", 0.0)
            )
            total_assets += self._to_float(getattr(item, "net_assets", 0.0))
            default_currency = str(getattr(item, "currency", default_currency))

        cost_basis = sum(item.cost_price * item.quantity for item in positions)
        return TradingAccountOverview(
            provider=TradingProviderKind.LONGBRIDGE,
            mode=TradingMode.PAPER,
            currency=default_currency,
            total_assets=round(total_assets or (available_cash + positions_market_value), 2),
            available_cash=round(available_cash, 2),
            withdrawable_cash=round(withdrawable_cash, 2),
            buying_power=round(buying_power or available_cash, 2),
            positions_market_value=round(positions_market_value, 2),
            today_pnl=round(today_pnl, 2),
            today_pnl_pct=round(today_pnl / (total_assets - today_pnl), 6) if total_assets > today_pnl else 0.0,
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl / cost_basis, 6) if cost_basis > 0 else 0.0,
            cash_infos=cash_infos,
            updated_at=self._now(),
        )

    def get_positions(self) -> list[TradingPosition]:
        try:
            raw_positions = self._trade_context.stock_positions()
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 持仓查询失败: {exc}", code="positions_failed") from exc

        symbols = [str(getattr(item, "symbol", "")) for item in raw_positions if getattr(item, "symbol", None)]
        quotes = {item.symbol: item for item in self.get_quotes(symbols)} if symbols else {}

        positions: list[TradingPosition] = []
        for item in raw_positions:
            symbol = str(getattr(item, "symbol", ""))
            security = find_security_by_symbol(symbol) or TradingSecurity(
                symbol=symbol,
                name=str(
                    getattr(item, "symbol_name", None)
                    or getattr(item, "stock_name", None)
                    or symbol
                ),
                market=self._market_from_symbol(symbol),
                currency=str(getattr(item, "currency", "USD")),
                asset_type=TradingAssetType.STOCK,
            )
            quote = quotes.get(symbol)
            last_price = quote.last_price if quote is not None else self._to_float(getattr(item, "market_price", 0.0))
            prev_close = quote.prev_close if quote is not None else last_price
            quantity = int(self._to_float(getattr(item, "quantity", 0.0)))
            available_quantity = int(self._to_float(getattr(item, "available_quantity", quantity)))
            cost_price = self._to_float(getattr(item, "cost_price", 0.0))
            market_value = last_price * quantity
            unrealized_pnl = (last_price - cost_price) * quantity
            day_pnl = (last_price - prev_close) * quantity
            cost_basis = cost_price * quantity
            day_basis = prev_close * quantity if prev_close > 0 else 0.0
            positions.append(
                TradingPosition(
                    symbol=symbol,
                    name=security.name,
                    market=security.market,
                    currency=security.currency,
                    asset_type=security.asset_type,
                    quantity=quantity,
                    available_quantity=available_quantity,
                    cost_price=round(cost_price, 4),
                    last_price=round(last_price, 4),
                    market_value=round(market_value, 2),
                    unrealized_pnl=round(unrealized_pnl, 2),
                    unrealized_pnl_pct=round(unrealized_pnl / cost_basis, 6) if cost_basis > 0 else 0.0,
                    day_pnl=round(day_pnl, 2),
                    day_pnl_pct=round(day_pnl / day_basis, 6) if day_basis > 0 else 0.0,
                )
            )
        return positions

    def get_today_orders(self) -> list[TradingOrder]:
        try:
            items = self._trade_context.today_orders()
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 当日委托查询失败: {exc}", code="today_orders_failed") from exc
        return [self._map_order(item) for item in items]

    def get_history_orders(self) -> list[TradingOrder]:
        try:
            items = self._trade_context.history_orders()
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 历史委托查询失败: {exc}", code="history_orders_failed") from exc
        return [self._map_order(item) for item in items]

    def get_order_detail(self, order_id: str) -> TradingOrder:
        try:
            detail = self._trade_context.order_detail(order_id)
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 订单详情查询失败: {exc}", code="order_detail_failed") from exc
        return self._map_order(detail)

    def estimate_order(self, request: TradingOrderEstimateRequest) -> TradingOrderEstimate:
        quote = self.get_quotes([request.symbol])
        if not quote:
            raise TradingProviderError("无法获取标的行情", code="quote_failed")
        reference_price = quote[0].last_price

        try:
            estimate = self._trade_context.estimate_max_purchase_quantity(
                symbol=request.symbol,
                order_type=self._sdk_order_type(request.order_type),
                side=self._sdk_order_side(request.side),
                price=request.submitted_price or reference_price,
            )
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 最大可买数量查询失败: {exc}", code="estimate_failed") from exc

        return TradingOrderEstimate(
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            reference_price=reference_price,
            cash_max_qty=int(self._to_float(getattr(estimate, "cash_max_qty", 0.0))),
            sell_max_qty=int(self._to_float(getattr(estimate, "margin_max_qty", 0.0))),
        )

    def submit_order(self, request: TradingOrderRequest) -> TradingSubmitResult:
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    def cancel_order(self, order_id: str) -> TradingCancelResult:
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    def get_today_executions(self) -> list[TradingExecution]:
        try:
            items = self._trade_context.today_executions()
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 当日成交查询失败: {exc}", code="today_executions_failed") from exc
        return [self._map_execution(item) for item in items]

    def get_history_executions(self) -> list[TradingExecution]:
        try:
            items = self._trade_context.history_executions()
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 历史成交查询失败: {exc}", code="history_executions_failed") from exc
        return [self._map_execution(item) for item in items]

    def get_cash_flows(self) -> list[TradingCashFlow]:
        try:
            end_at = datetime.now(tz=UTC)
            start_at = end_at - timedelta(days=30)
            items = self._trade_context.cash_flow(start_at=start_at, end_at=end_at, page=1, size=100)
        except Exception as exc:
            raise TradingProviderError(f"Longbridge 资金流水查询失败: {exc}", code="cash_flow_failed") from exc
        return [self._map_cash_flow(item) for item in items]

    def _build_config(self):
        app_key = self._settings.longbridge_app_key or load_api_key("longbridge_app_key") or os.getenv(
            "LONGBRIDGE_APP_KEY",
            "",
        )
        app_secret = self._settings.longbridge_app_secret or load_api_key(
            "longbridge_app_secret",
        ) or os.getenv("LONGBRIDGE_APP_SECRET", "")
        access_token = self._settings.longbridge_access_token or load_api_key(
            "longbridge_access_token",
        ) or os.getenv("LONGBRIDGE_ACCESS_TOKEN", "")

        if not (app_key and app_secret and access_token):
            raise RuntimeError("Longbridge 配置缺失，请设置 App Key / App Secret / Access Token")

        os.environ.setdefault("LONGBRIDGE_APP_KEY", app_key)
        os.environ.setdefault("LONGBRIDGE_APP_SECRET", app_secret)
        os.environ.setdefault("LONGBRIDGE_ACCESS_TOKEN", access_token)
        if self._settings.longbridge_region:
            os.environ.setdefault("LONGBRIDGE_REGION", self._settings.longbridge_region)

        try:
            from longbridge.openapi import Config
        except ImportError as exc:
            raise RuntimeError("未安装 longbridge Python SDK，请先执行依赖安装") from exc

        return Config.from_apikey(app_key, app_secret, access_token)

    def _build_contexts(self):
        try:
            import longbridge.openapi as sdk
        except ImportError as exc:
            raise RuntimeError("未安装 longbridge Python SDK") from exc

        quote_context = sdk.QuoteContext(self._config)
        trade_context = sdk.TradeContext(self._config)
        return quote_context, trade_context, sdk

    def _map_security_info(self, item) -> TradingSecurity:
        symbol = str(getattr(item, "symbol", ""))
        catalog = find_security_by_symbol(symbol)
        name = str(
            getattr(item, "name_cn", None)
            or getattr(item, "name_en", None)
            or getattr(item, "name", None)
            or (catalog.name if catalog is not None else symbol)
        )
        market = catalog.market if catalog is not None else self._market_from_symbol(symbol)
        asset_type = catalog.asset_type if catalog is not None else TradingAssetType.STOCK
        return TradingSecurity(
            symbol=symbol,
            name=name,
            market=market,
            currency=str(getattr(item, "currency", catalog.currency if catalog is not None else "USD")),
            asset_type=asset_type,
            lot_size=int(self._to_float(getattr(item, "lot_size", catalog.lot_size if catalog is not None else 1))),
            shortable=bool(catalog.shortable) if catalog is not None else market == TradingMarket.US,
        )

    def _map_quote(self, *, symbol: str, quote, info) -> TradingQuote:
        security = self._map_security_info(info) if info is not None else (
            find_security_by_symbol(symbol)
            or TradingSecurity(
                symbol=symbol,
                name=symbol,
                market=self._market_from_symbol(symbol),
                currency="USD",
                asset_type=TradingAssetType.STOCK,
            )
        )
        last_price = self._to_float(getattr(quote, "last_done", getattr(quote, "last_price", 0.0)))
        prev_close = self._to_float(getattr(quote, "prev_close", last_price))
        change = last_price - prev_close
        restrictions: list[str] = []
        if security.asset_type == TradingAssetType.OPTION:
            restrictions.append("Longbridge sandbox 暂不支持期权交易")
        if security.asset_type == TradingAssetType.OTC:
            restrictions.append("Longbridge sandbox 暂不支持美股 OTC")
        session = self._trade_session_for_market(security.market)
        if session in {TradingSessionStatus.PRE_MARKET, TradingSessionStatus.POST_MARKET}:
            restrictions.append("Longbridge sandbox 暂不支持盘前盘后交易")

        return TradingQuote(
            symbol=symbol,
            name=security.name,
            market=security.market,
            currency=security.currency,
            asset_type=security.asset_type,
            last_price=round(last_price, 4),
            prev_close=round(prev_close, 4),
            change=round(change, 4),
            change_pct=round(change / prev_close, 6) if prev_close else 0.0,
            trade_session=session,
            trade_status=str(getattr(quote, "trade_status", "normal")).lower(),
            tradeable=len(restrictions) == 0,
            restrictions=restrictions,
        )

    def _map_order(self, item) -> TradingOrder:
        symbol = str(getattr(item, "symbol", ""))
        security = find_security_by_symbol(symbol)
        status = self._map_order_status(getattr(item, "status", None))
        return TradingOrder(
            order_id=str(getattr(item, "order_id", "")),
            symbol=symbol,
            name=str(
                getattr(item, "stock_name", None)
                or getattr(item, "symbol_name", None)
                or (security.name if security is not None else symbol)
            ),
            market=security.market if security is not None else self._market_from_symbol(symbol),
            currency=str(getattr(item, "currency", security.currency if security is not None else "USD")),
            asset_type=security.asset_type if security is not None else TradingAssetType.STOCK,
            side=self._map_order_side(getattr(item, "side", None)),
            order_type=self._map_order_type(getattr(item, "order_type", None)),
            status=status,
            quantity=int(self._to_float(getattr(item, "quantity", 0.0))),
            executed_quantity=int(self._to_float(getattr(item, "executed_quantity", 0.0))),
            submitted_price=self._nullable_float(getattr(item, "submitted_price", None)),
            trigger_price=self._nullable_float(getattr(item, "trigger_price", None)),
            executed_price=self._nullable_float(
                getattr(item, "executed_price", getattr(item, "last_done", None)),
            ),
            submitted_at=self._to_iso(getattr(item, "submitted_at", None)),
            updated_at=self._to_iso(getattr(item, "updated_at", None)),
            message=str(getattr(item, "msg", None) or getattr(item, "reject_reason", "") or "") or None,
        )

    def _map_execution(self, item) -> TradingExecution:
        symbol = str(getattr(item, "symbol", ""))
        security = find_security_by_symbol(symbol)
        return TradingExecution(
            execution_id=str(getattr(item, "execution_id", getattr(item, "trade_id", ""))),
            order_id=str(getattr(item, "order_id", "")),
            symbol=symbol,
            name=str(
                getattr(item, "stock_name", None)
                or getattr(item, "symbol_name", None)
                or (security.name if security is not None else symbol)
            ),
            market=security.market if security is not None else self._market_from_symbol(symbol),
            currency=str(getattr(item, "currency", security.currency if security is not None else "USD")),
            asset_type=security.asset_type if security is not None else TradingAssetType.STOCK,
            side=self._map_order_side(getattr(item, "side", None)),
            price=self._to_float(getattr(item, "price", getattr(item, "executed_price", 0.0))),
            quantity=int(self._to_float(getattr(item, "quantity", 0.0))),
            executed_at=self._to_iso(getattr(item, "trade_done_at", getattr(item, "executed_at", None))),
        )

    def _map_cash_flow(self, item) -> TradingCashFlow:
        raw_amount = self._to_float(getattr(item, "cash_flow", getattr(item, "amount", 0.0)))
        return TradingCashFlow(
            cash_flow_id=str(getattr(item, "cash_flow_id", getattr(item, "serial_no", ""))),
            currency=str(getattr(item, "currency", "USD")),
            amount=raw_amount,
            balance=self._to_float(getattr(item, "balance", 0.0)),
            business_type=str(getattr(item, "business_type", "")),
            direction="credit" if raw_amount >= 0 else "debit",
            description=str(
                getattr(item, "transaction_flow_name", None)
                or getattr(item, "description", None)
                or "cash flow"
            ),
            symbol=str(getattr(item, "symbol", "")) or None,
            occurred_at=self._to_iso(getattr(item, "business_time", getattr(item, "occurred_at", None))),
        )

    def _sdk_order_side(self, side: TradingOrderSide):
        enum_cls = self._sdk.OrderSide
        return self._enum_member(enum_cls, "Buy" if side == TradingOrderSide.BUY else "Sell")

    def _sdk_order_type(self, order_type: TradingOrderType):
        enum_cls = self._sdk.OrderType
        name = "MO" if order_type == TradingOrderType.MARKET else "LO"
        return self._enum_member(enum_cls, name)

    def _map_order_side(self, raw) -> TradingOrderSide:
        text = str(raw).lower()
        return TradingOrderSide.SELL if "sell" in text else TradingOrderSide.BUY

    def _map_order_type(self, raw) -> TradingOrderType:
        text = str(raw).lower()
        return TradingOrderType.LIMIT if "lo" in text or "limit" in text else TradingOrderType.MARKET

    def _map_order_status(self, raw) -> TradingOrderStatus:
        text = str(raw).lower()
        if "partial" in text and "fill" in text:
            return TradingOrderStatus.PARTIAL_FILLED
        if "filled" in text or "fill" in text:
            return TradingOrderStatus.FILLED
        if "cancel" in text or "withdraw" in text:
            return TradingOrderStatus.CANCELED
        if "reject" in text:
            return TradingOrderStatus.REJECTED
        if "expire" in text or "invalid" in text:
            return TradingOrderStatus.EXPIRED
        if "submit" in text or "new" in text or "wait" in text:
            return TradingOrderStatus.SUBMITTED
        return TradingOrderStatus.UNKNOWN

    def _enum_member(self, enum_cls, *names: str):
        for name in names:
            if hasattr(enum_cls, name):
                return getattr(enum_cls, name)
            upper = name.upper()
            if hasattr(enum_cls, upper):
                return getattr(enum_cls, upper)
        raise RuntimeError(f"Longbridge SDK enum member not found: {enum_cls.__name__} {names}")

    def _market_from_symbol(self, symbol: str) -> TradingMarket:
        if symbol.endswith(".US"):
            return TradingMarket.US
        if symbol.endswith(".HK"):
            return TradingMarket.HK
        return TradingMarket.UNKNOWN

    def _trade_session_for_market(self, market: TradingMarket) -> TradingSessionStatus:
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

    def _to_float(self, value) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _nullable_float(self, value) -> float | None:
        if value is None:
            return None
        parsed = self._to_float(value)
        return parsed

    def _to_iso(self, value) -> str:
        if value is None:
            return self._now()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.isoformat()
        return str(value)

    def _now(self) -> str:
        return datetime.now(tz=UTC).isoformat()
