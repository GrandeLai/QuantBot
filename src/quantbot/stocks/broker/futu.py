"""Futu OpenAPI trading provider."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

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
from quantbot.common.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class FutuProviderAvailability:
    """Availability snapshot for Futu provider startup."""

    configured: bool
    reason: str | None


class FutuTradingProvider:
    """Futu provider with explicit availability reporting."""

    def __init__(
        self,
        *,
        sdk: Any | None = None,
        quote_context: Any | None = None,
        trade_context: Any | None = None,
        settings: Settings | Any | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._sdk = sdk
        self._sdk_import_error: str | None = None
        self._quote_context = quote_context
        self._trade_context = trade_context
        self._acc_id: int | None = None
        self._trd_env = None

        if self._sdk is None:
            try:
                import futu as futu_sdk  # type: ignore
            except Exception as exc:  # pragma: no cover - depends on local environment
                self._sdk_import_error = str(exc)
            else:  # pragma: no cover - depends on local environment
                self._sdk = futu_sdk

        self._availability = self._resolve_availability()

    def get_status(self) -> TradingProviderStatus:
        """Return provider status and capability boundaries."""
        notes = [
            "Futu provider 已接入 unified trading 主线。",
            "若本机未安装 futu SDK、未配置 OpenD 地址，或 OpenD 未启动，交易接口会返回结构化不可用错误。",
        ]
        if getattr(self._settings, "futu_market", ""):
            notes.append(f"当前市场偏好：{self._settings.futu_market}")
        if self._availability.configured:
            notes.append("当前阶段已打通行情、账户、持仓、委托、成交与下单基础能力。")
        return TradingProviderStatus(
            provider=TradingProviderKind.FUTU,
            mode=TradingMode.PAPER,
            configured=self._availability.configured,
            reason=self._availability.reason,
            capabilities=TradingCapability(
                supported_markets=[TradingMarket.US, TradingMarket.HK],
                supported_asset_types=[
                    TradingAssetType.STOCK,
                    TradingAssetType.ETF,
                    TradingAssetType.WARRANT,
                    TradingAssetType.OPTION,
                ],
                supported_order_types=[TradingOrderType.MARKET, TradingOrderType.LIMIT],
                supports_us_short_selling=False,
                supports_otc=False,
                supports_us_prepost=False,
                supports_options=True,
                notes=notes,
            ),
        )

    def search_securities(self, query: str, *, limit: int = 20) -> list[TradingSecurity]:
        """Search tradable securities using the local catalog first."""
        matches = find_supported_securities(query, limit=limit)
        market = self._preferred_market()
        if market == TradingMarket.UNKNOWN:
            return matches
        return [item for item in matches if item.market == market][:limit]

    def get_quotes(self, symbols: list[str]) -> list[TradingQuote]:
        """Fetch quote snapshots via Futu market snapshot API."""
        self._ensure_ready("行情查询")
        if not symbols:
            return []
        data = self._call(
            self._quote_context.get_market_snapshot,  # type: ignore[union-attr]
            [self._to_futu_symbol(symbol) for symbol in symbols],
            action="行情查询",
            error_code="futu_quote_failed",
        )
        rows = self._rows(data)
        return [self._map_quote(row) for row in rows]

    def get_account_overview(self) -> TradingAccountOverview:
        """Fetch account overview for the selected simulated account."""
        self._ensure_ready("账户查询")
        data = self._call(
            self._trade_context.accinfo_query,  # type: ignore[union-attr]
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="账户查询",
            error_code="futu_account_failed",
        )
        row = self._first_row(data)
        available_cash = self._float(row, "available_funds")
        total_assets = self._float(row, "total_assets")
        market_value = self._float(row, "market_val")
        total_pnl = self._float(row, "unrealized_pl")
        return TradingAccountOverview(
            provider=TradingProviderKind.FUTU,
            mode=TradingMode.PAPER,
            currency=str(row.get("currency", "USD")),
            total_assets=round(total_assets, 2),
            available_cash=round(available_cash, 2),
            withdrawable_cash=round(self._float(row, "avl_withdrawal_cash", default=available_cash), 2),
            buying_power=round(self._float(row, "power", default=available_cash), 2),
            positions_market_value=round(market_value, 2),
            today_pnl=round(self._float(row, "realized_pl"), 2),
            today_pnl_pct=0.0,
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl / (total_assets - total_pnl), 6) if total_assets > total_pnl else 0.0,
            cash_infos=[
                TradingCashInfo(
                    currency=str(row.get("currency", "USD")),
                    available_cash=round(available_cash, 2),
                    withdraw_cash=round(self._float(row, "avl_withdrawal_cash", default=available_cash), 2),
                    frozen_cash=round(self._float(row, "frozen_cash"), 2),
                )
            ],
            updated_at=self._now(),
        )

    def get_positions(self) -> list[TradingPosition]:
        """Fetch current positions."""
        self._ensure_ready("持仓查询")
        data = self._call(
            self._trade_context.position_list_query,  # type: ignore[union-attr]
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="持仓查询",
            error_code="futu_positions_failed",
        )
        return [self._map_position(row) for row in self._rows(data)]

    def get_today_orders(self) -> list[TradingOrder]:
        """Fetch today's orders."""
        self._ensure_ready("当日委托查询")
        data = self._call(
            self._trade_context.order_list_query,  # type: ignore[union-attr]
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="当日委托查询",
            error_code="futu_today_orders_failed",
        )
        return [self._map_order(row) for row in self._rows(data)]

    def get_history_orders(self) -> list[TradingOrder]:
        """Fetch historical orders."""
        self._ensure_ready("历史委托查询")
        data = self._call(
            self._trade_context.history_order_list_query,  # type: ignore[union-attr]
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="历史委托查询",
            error_code="futu_history_orders_failed",
        )
        return [self._map_order(row) for row in self._rows(data)]

    def get_order_detail(self, order_id: str) -> TradingOrder:
        """Fetch one order detail via the order list query."""
        for order in self.get_today_orders():
            if order.order_id == order_id:
                return order
        for order in self.get_history_orders():
            if order.order_id == order_id:
                return order
        raise TradingProviderError("订单不存在", code="order_not_found", status_code=404)

    def estimate_order(self, request: TradingOrderEstimateRequest) -> TradingOrderEstimate:
        """Estimate max buy/sell quantity."""
        self._ensure_ready("下单估算")
        quote = self.get_quotes([request.symbol])
        reference_price = request.submitted_price or (quote[0].last_price if quote else 0.0)
        data = self._call(
            self._trade_context.acctradinginfo_query,  # type: ignore[union-attr]
            order_type=self._sdk_order_type(request.order_type),
            code=self._to_futu_symbol(request.symbol),
            price=reference_price,
            trd_side=self._sdk_order_side(request.side),
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="下单估算",
            error_code="futu_estimate_failed",
        )
        row = self._first_row(data)
        return TradingOrderEstimate(
            symbol=request.symbol,
            side=request.side,
            order_type=request.order_type,
            reference_price=reference_price,
            cash_max_qty=int(self._float(row, "max_cash_buy")),
            sell_max_qty=int(self._float(row, "max_position_sell")),
        )

    def submit_order(self, request: TradingOrderRequest) -> TradingSubmitResult:
        """Submit an order via Futu place_order."""
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    def cancel_order(self, order_id: str) -> TradingCancelResult:
        """Cancel an order via Futu modify_order."""
        raise RuntimeError("QuantBot 统一交易入口未准入此旧券商写入路径；使用受风控的模拟执行入口")

    def get_today_executions(self) -> list[TradingExecution]:
        """Fetch today's executions."""
        self._ensure_ready("当日成交查询")
        data = self._call(
            self._trade_context.deal_list_query,  # type: ignore[union-attr]
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="当日成交查询",
            error_code="futu_today_executions_failed",
        )
        return [self._map_execution(row) for row in self._rows(data)]

    def get_history_executions(self) -> list[TradingExecution]:
        """Fetch historical executions."""
        self._ensure_ready("历史成交查询")
        history_method = getattr(self._trade_context, "history_deal_list_query", None)  # type: ignore[union-attr]
        if history_method is None:
            return self.get_today_executions()
        data = self._call(
            history_method,
            trd_env=self._trd_env,
            acc_id=self._acc_id,
            action="历史成交查询",
            error_code="futu_history_executions_failed",
        )
        return [self._map_execution(row) for row in self._rows(data)]

    def get_cash_flows(self) -> list[TradingCashFlow]:
        """Return cash-flow records when the SDK supports them, otherwise an empty list."""
        self._ensure_ready("资金流水查询")
        for attr_name in ("history_cash_list_query", "cash_flow_query"):
            method = getattr(self._trade_context, attr_name, None)  # type: ignore[union-attr]
            if method is None:
                continue
            data = self._call(
                method,
                trd_env=self._trd_env,
                acc_id=self._acc_id,
                action="资金流水查询",
                error_code="futu_cash_flow_failed",
            )
            return [self._map_cash_flow(row) for row in self._rows(data)]
        return []

    def _resolve_availability(self) -> FutuProviderAvailability:
        if self._sdk is None:
            return FutuProviderAvailability(
                configured=False,
                reason=f"Futu provider unavailable: futu SDK not installed ({self._sdk_import_error})",
            )
        if not getattr(self._settings, "futu_host", "") or not getattr(self._settings, "futu_port", 0):
            return FutuProviderAvailability(
                configured=False,
                reason="Futu provider unavailable: QUANTBOT_FUTU_HOST / QUANTBOT_FUTU_PORT 未配置",
            )
        if self._quote_context is None or self._trade_context is None:
            try:
                self._quote_context, self._trade_context = self._build_contexts()
            except Exception as exc:
                return FutuProviderAvailability(configured=False, reason=f"Futu provider unavailable: {exc}")
        try:
            self._trd_env = self._sdk.TrdEnv.SIMULATE
        except Exception as exc:
            return FutuProviderAvailability(configured=False, reason=f"Futu provider unavailable: missing TrdEnv ({exc})")
        try:
            self._acc_id = self._select_account_id()
        except Exception as exc:
            return FutuProviderAvailability(configured=False, reason=f"Futu provider unavailable: {exc}")
        return FutuProviderAvailability(configured=True, reason=None)

    def _build_contexts(self) -> tuple[Any, Any]:
        quote_context = self._sdk.OpenQuoteContext(  # type: ignore[union-attr]
            host=self._settings.futu_host,
            port=self._settings.futu_port,
        )
        trade_context = self._sdk.OpenSecTradeContext(  # type: ignore[union-attr]
            filter_trdmarket=self._sdk.TrdMarket[self._settings.futu_market.upper()],  # type: ignore[union-attr]
            host=self._settings.futu_host,
            port=self._settings.futu_port,
        )
        return quote_context, trade_context

    def _select_account_id(self) -> int:
        data = self._call(
            self._trade_context.get_acc_list,  # type: ignore[union-attr]
            action="账户查询",
            error_code="futu_account_list_failed",
        )
        rows = self._rows(data)
        for row in rows:
            if str(row.get("trd_env", "")).upper() == str(self._trd_env):
                return int(row.get("acc_id", 0))
        if rows:
            return int(rows[0].get("acc_id", 0))
        raise RuntimeError("Futu provider unavailable: no trading account found")

    def _ensure_ready(self, action: str) -> None:
        if not self._availability.configured:
            raise TradingProviderError(
                f"{action}失败: {self._availability.reason}",
                code="futu_unavailable",
                status_code=503,
            )

    def _call(self, func, *args: Any, action: str, error_code: str, **kwargs: Any) -> Any:
        try:
            ret, data = func(*args, **kwargs)
        except Exception as exc:
            raise TradingProviderError(f"Futu {action}失败: {exc}", code=error_code, status_code=502) from exc
        ret_ok = getattr(self._sdk, "RET_OK", 0)
        if ret != ret_ok:
            raise TradingProviderError(f"Futu {action}失败: {data}", code=error_code, status_code=502)
        return data

    def _rows(self, data: Any) -> list[dict[str, Any]]:
        if data is None:
            return []
        if hasattr(data, "to_dict"):
            try:
                return [dict(item) for item in data.to_dict(orient="records")]
            except TypeError:
                pass
        if isinstance(data, Mapping):
            return [dict(data)]
        if isinstance(data, Iterable) and not isinstance(data, (str, bytes)):
            rows: list[dict[str, Any]] = []
            for item in data:
                if isinstance(item, Mapping):
                    rows.append(dict(item))
                else:
                    rows.append(dict(vars(item)))
            return rows
        return [dict(vars(data))]

    def _first_row(self, data: Any) -> dict[str, Any]:
        rows = self._rows(data)
        if not rows:
            raise TradingProviderError("Futu 返回为空", code="futu_empty_result", status_code=502)
        return rows[0]

    def _map_quote(self, row: Mapping[str, Any]) -> TradingQuote:
        symbol = self._from_futu_symbol(str(row.get("code", "")))
        security = find_security_by_symbol(symbol) or TradingSecurity(
            symbol=symbol,
            name=str(row.get("stock_name", symbol)),
            market=self._market_from_symbol(symbol),
            currency="USD" if symbol.endswith(".US") else "HKD",
            asset_type=TradingAssetType.STOCK,
            lot_size=int(row.get("lot_size", 1) or 1),
        )
        last_price = self._float(row, "last_price")
        prev_close = self._float(row, "prev_close_price", default=last_price)
        restrictions: list[str] = []
        if row.get("suspension") is True:
            restrictions.append("当前标的已停牌")
        return TradingQuote(
            symbol=symbol,
            name=str(row.get("stock_name", security.name)),
            market=security.market,
            currency=security.currency,
            asset_type=security.asset_type,
            last_price=round(last_price, 4),
            prev_close=round(prev_close, 4),
            change=round(last_price - prev_close, 4),
            change_pct=round((last_price - prev_close) / prev_close, 6) if prev_close else 0.0,
            trade_session=TradingSessionStatus.REGULAR,
            trade_status="normal",
            tradeable=len(restrictions) == 0,
            restrictions=restrictions,
        )

    def _map_position(self, row: Mapping[str, Any]) -> TradingPosition:
        symbol = self._from_futu_symbol(str(row.get("code", "")))
        security = find_security_by_symbol(symbol) or TradingSecurity(
            symbol=symbol,
            name=str(row.get("stock_name", symbol)),
            market=self._market_from_symbol(symbol),
            currency=str(row.get("currency", "USD")),
            asset_type=TradingAssetType.STOCK,
        )
        quantity = int(self._float(row, "qty"))
        return TradingPosition(
            symbol=symbol,
            name=str(row.get("stock_name", security.name)),
            market=security.market,
            currency=str(row.get("currency", security.currency)),
            asset_type=security.asset_type,
            quantity=quantity,
            available_quantity=int(self._float(row, "can_sell_qty", default=quantity)),
            cost_price=self._float(row, "cost_price"),
            last_price=self._float(row, "nominal_price"),
            market_value=self._float(row, "market_val"),
            unrealized_pnl=self._float(row, "pl_val"),
            unrealized_pnl_pct=self._float(row, "pl_ratio"),
            day_pnl=self._float(row, "today_pl_val"),
            day_pnl_pct=0.0,
        )

    def _map_order(self, row: Mapping[str, Any]) -> TradingOrder:
        symbol = self._from_futu_symbol(str(row.get("code", "")))
        security = find_security_by_symbol(symbol)
        return TradingOrder(
            order_id=str(row.get("order_id", "")),
            symbol=symbol,
            name=str(row.get("stock_name", security.name if security else symbol)),
            market=security.market if security else self._market_from_symbol(symbol),
            currency=str(row.get("currency", security.currency if security else "USD")),
            asset_type=security.asset_type if security else TradingAssetType.STOCK,
            side=self._map_order_side(row.get("trd_side")),
            order_type=self._map_order_type(row.get("order_type")),
            status=self._map_order_status(row.get("order_status")),
            quantity=int(self._float(row, "qty")),
            executed_quantity=int(self._float(row, "dealt_qty")),
            submitted_price=self._nullable_float(row.get("price")),
            executed_price=self._nullable_float(row.get("dealt_avg_price")),
            submitted_at=self._to_iso(row.get("create_time")),
            updated_at=self._to_iso(row.get("updated_time", row.get("create_time"))),
            message=str(row.get("last_err_msg", "")) or None,
        )

    def _map_execution(self, row: Mapping[str, Any]) -> TradingExecution:
        symbol = self._from_futu_symbol(str(row.get("code", "")))
        security = find_security_by_symbol(symbol)
        return TradingExecution(
            execution_id=str(row.get("deal_id", "")),
            order_id=str(row.get("order_id", "")),
            symbol=symbol,
            name=str(row.get("stock_name", security.name if security else symbol)),
            market=security.market if security else self._market_from_symbol(symbol),
            currency=str(row.get("currency", security.currency if security else "USD")),
            asset_type=security.asset_type if security else TradingAssetType.STOCK,
            side=self._map_order_side(row.get("trd_side")),
            price=self._float(row, "price"),
            quantity=int(self._float(row, "qty")),
            executed_at=self._to_iso(row.get("create_time")),
        )

    def _map_cash_flow(self, row: Mapping[str, Any]) -> TradingCashFlow:
        amount = self._float(row, "amount", default=self._float(row, "cash_flow"))
        return TradingCashFlow(
            cash_flow_id=str(row.get("serial_no", row.get("cash_flow_id", ""))),
            currency=str(row.get("currency", "USD")),
            amount=amount,
            balance=self._float(row, "balance"),
            business_type=str(row.get("business_type", "")),
            direction="credit" if amount >= 0 else "debit",
            description=str(row.get("remark", row.get("description", "cash flow"))),
            symbol=self._from_futu_symbol(str(row["code"])) if row.get("code") else None,
            occurred_at=self._to_iso(row.get("create_time", row.get("business_time"))),
        )

    def _sdk_order_side(self, side: TradingOrderSide) -> Any:
        return self._sdk.TrdSide.BUY if side == TradingOrderSide.BUY else self._sdk.TrdSide.SELL  # type: ignore[union-attr]

    def _sdk_order_type(self, order_type: TradingOrderType) -> Any:
        return self._sdk.OrderType.NORMAL if order_type == TradingOrderType.LIMIT else self._sdk.OrderType.MARKET  # type: ignore[union-attr]

    def _sdk_modify_order_cancel(self) -> Any:
        return self._sdk.ModifyOrderOp.CANCEL  # type: ignore[union-attr]

    def _map_order_side(self, raw: Any) -> TradingOrderSide:
        text = str(raw).lower()
        return TradingOrderSide.SELL if "sell" in text else TradingOrderSide.BUY

    def _map_order_type(self, raw: Any) -> TradingOrderType:
        text = str(raw).lower()
        return TradingOrderType.LIMIT if "normal" in text or "limit" in text or "lo" in text else TradingOrderType.MARKET

    def _map_order_status(self, raw: Any) -> TradingOrderStatus:
        text = str(raw).lower()
        if "partial" in text and "fill" in text:
            return TradingOrderStatus.PARTIAL_FILLED
        if "filled" in text or text == "fill_all":
            return TradingOrderStatus.FILLED
        if "cancel" in text:
            return TradingOrderStatus.CANCELED
        if "reject" in text or "failed" in text:
            return TradingOrderStatus.REJECTED
        if "expire" in text or "disabled" in text:
            return TradingOrderStatus.EXPIRED
        if "submit" in text or "submitted" in text or "wait" in text:
            return TradingOrderStatus.SUBMITTED
        return TradingOrderStatus.UNKNOWN

    def _to_futu_symbol(self, symbol: str) -> str:
        if symbol.endswith(".US"):
            return f"US.{symbol.removesuffix('.US')}"
        if symbol.endswith(".HK"):
            return f"HK.{symbol.removesuffix('.HK').zfill(5)}"
        return symbol

    def _from_futu_symbol(self, symbol: str) -> str:
        if symbol.startswith("US."):
            return f"{symbol.removeprefix('US.')}.US"
        if symbol.startswith("HK."):
            return f"{symbol.removeprefix('HK.').lstrip('0') or '0'}.HK"
        return symbol

    def _preferred_market(self) -> TradingMarket:
        market = str(getattr(self._settings, "futu_market", "")).upper()
        if market == "US":
            return TradingMarket.US
        if market == "HK":
            return TradingMarket.HK
        return TradingMarket.UNKNOWN

    def _market_from_symbol(self, symbol: str) -> TradingMarket:
        if symbol.endswith(".US"):
            return TradingMarket.US
        if symbol.endswith(".HK"):
            return TradingMarket.HK
        return self._preferred_market()

    def _float(self, row: Mapping[str, Any], key: str, *, default: float = 0.0) -> float:
        value = row.get(key, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _nullable_float(self, value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _to_iso(self, value: Any) -> str:
        if value is None or value == "":
            return self._now()
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC).isoformat()
            return value.isoformat()
        return str(value)

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()
