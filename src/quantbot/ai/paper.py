"""固定 Alpaca 模拟账户；复用 StockBot 客户端与 PABot 订单恢复门禁。"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from quantbot.integrations.alpaca import AlpacaTradingClient
from quantbot.trading.adapters import broker_account_snapshot_from_payload
from quantbot.trading.domain import OrderIntent
from quantbot.trading.risk import BrokerAccountRiskGate

from .domain import AccountState
from .admission import require_broker_paper_admission
from .journal import AIJournal, runtime_lock
from .market import EASTERN
from .models import DemoProvider
from .risk import build_plan
from .transport import JsonTransport, ProviderError


PAPER_URL = "https://paper-api.alpaca.markets"
TERMINAL = {"filled", "canceled", "expired", "rejected"}


def _field_number(payload, name):
    value = float(payload[name])
    if not Decimal(str(value)).is_finite():
        raise ValueError(f"券商 {name} 必须为有限数值")
    return value


def _price(value):
    quantum = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return str(Decimal(str(value)).quantize(quantum, rounding=ROUND_DOWN))


class AlpacaPaperClient(AlpacaTradingClient):
    def __init__(self, environ=None, transport=None):
        env = os.environ if environ is None else environ
        key, secret = env.get("ALPACA_API_KEY", ""), env.get("ALPACA_SECRET_KEY", "")
        if not key or not secret:
            raise ValueError("缺少 Alpaca 模拟账户密钥 ALPACA_API_KEY / ALPACA_SECRET_KEY")
        super().__init__(key, secret, transport=transport or JsonTransport(), base_url=PAPER_URL)

    def get(self, path, params=None):
        return self.transport.get_json(PAPER_URL + path, headers=self._headers(), params=params)

    def clock(self):
        return self.get("/v2/clock")

    def account_state(self, symbols) -> tuple[AccountState, str]:
        payload = self.fetch_account()
        broker = broker_account_snapshot_from_payload(payload, "alpaca", datetime.now().astimezone())
        positions = {}
        for position in self.get("/v2/positions"):
            symbol = str(position["symbol"]).replace(".", "-")
            quantity = _field_number(position, "qty")
            if symbol not in symbols or quantity < 0 or not quantity.is_integer():
                raise ValueError("模拟账户含股票池外资产、空头或碎股，停止交易")
            positions[symbol] = int(quantity)
        identifier = str(payload.get("id", "")).strip()
        if not identifier:
            raise ValueError("券商账户缺少稳定身份")
        account = AccountState(_field_number(payload, "cash"), broker.equity, positions, broker)
        return account, sha256(f"alpaca:paper:{identifier}".encode()).hexdigest()

    def query(self, client_order_id):
        return self.get(
            "/v2/orders:by_client_order_id", {"client_order_id": client_order_id, "nested": "true"}
        )

    def open_orders(self):
        orders = self.get("/v2/orders", {"status": "open", "nested": "true", "limit": 500})
        if len(orders) >= 500:
            raise ValueError("未完成订单数量触及查询上限，停止交易")
        return orders

    def submit_intent(self, intent, plan):
        payload = {
            "symbol": intent.symbol.replace("-", "."),
            "qty": str(int(intent.quantity)),
            "side": intent.side,
            "client_order_id": intent.client_order_id,
            "type": "limit" if intent.side == "buy" else "market",
            "time_in_force": "day",
        }
        if intent.side == "buy":
            limit, stop = plan.limit_prices[intent.symbol], plan.stops[intent.symbol]
            if limit - stop < 0.02:
                raise ValueError("入场限价与止损价距离不足")
            payload.update(
                {
                    "type": "limit",
                    "limit_price": _price(limit),
                    "order_class": "oto",
                    "stop_loss": {"stop_price": _price(stop)},
                    "time_in_force": "gtc",
                }
            )
        return self.transport.post_json(PAPER_URL + "/v2/orders", headers=self._headers(), payload=payload)

    def cancel_and_wait(self, order_id):
        self.transport.delete_json(PAPER_URL + f"/v2/orders/{order_id}", headers=self._headers())
        for _ in range(20):
            response = self.get(f"/v2/orders/{order_id}")
            status = response.get("status")
            if status in TERMINAL:
                if status == "filled":
                    raise RuntimeError("保护性止损已成交，需重新核对持仓，未提交卖单")
                return
            time.sleep(0.25)
        raise RuntimeError("止损取消结果未确认，停止提交卖单")

    def protect(self, symbol, quantity, stop, client_id):
        return self.transport.post_json(
            PAPER_URL + "/v2/orders",
            headers=self._headers(),
            payload={
                "symbol": symbol.replace("-", "."),
                "qty": str(quantity),
                "side": "sell",
                "type": "stop",
                "time_in_force": "gtc",
                "stop_price": _price(stop),
                "client_order_id": client_id,
            },
        )


def _safe_order(order):
    fields = (
        "id",
        "client_order_id",
        "symbol",
        "side",
        "type",
        "status",
        "qty",
        "filled_qty",
        "filled_avg_price",
        "stop_price",
    )
    return {
        **{k: order[k] for k in fields if k in order},
        "legs": [_safe_order(leg) for leg in (order.get("legs") or [])],
    }


def _record_response(audit, run_id, intent, response, state, config):
    status = str(response.get("status", "unknown"))
    quantity = float(response.get("filled_qty", 0))
    if quantity < 0 or quantity > intent.quantity or not quantity.is_integer():
        raise RuntimeError("券商成交数量与整数股订单不一致，需人工对账")
    if response.get("client_order_id") != intent.client_order_id:
        raise RuntimeError("券商返回了错误的 client_order_id")
    normalized = status if status in TERMINAL else "partial_fill" if quantity else "submitted"
    event = audit.order(
        run_id,
        intent,
        normalized,
        broker_id=response.get("id"),
        metadata={"broker_order": _safe_order(response)},
    )
    if status in TERMINAL:
        if quantity:
            price = _field_number(response, "filled_avg_price")
            audit.journal.append_fill(
                idempotency_key=f"paper:{response['id']}:{int(quantity)}",
                run_id=run_id,
                order_id=event,
                symbol=intent.symbol,
                side=intent.side,
                quantity=int(quantity),
                price=price,
                metadata={"source": "broker_cumulative_terminal_fill"},
            )
        if intent.side != "sell" or not state.positions.get(intent.symbol, 0):
            audit.commit_order(
                run_id,
                intent,
                event,
                {"cash": state.cash, "equity": state.equity, "positions": dict(state.positions)},
            )
        return event
    return None


def _protection_ids(audit):
    # Read this task's own append-only ledger; no historical account files are imported.
    import sqlite3

    with sqlite3.connect(audit.journal.path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        rows = connection.execute("SELECT metadata_json FROM orders").fetchall()
    ids = set()
    for (encoded,) in rows:
        metadata = json.loads(encoded)
        order = metadata.get("broker_order", metadata.get("broker_state", {}).get("protection", {}))
        if order.get("type") == "stop" and order.get("id"):
            ids.add(order["id"])
        for leg in order.get("legs", []):
            if leg.get("type") == "stop" and leg.get("id"):
                ids.add(leg["id"])
    return ids


def _restore_protection(client, audit, context, run_id, intent, terminal, state, stop, day):
    """A lost stop-submission response gets queried by its stable ID, never reposted."""
    import sqlite3

    client_id = "qp-" + sha256(intent.client_order_id.encode()).hexdigest()[:32]
    with sqlite3.connect(audit.journal.path.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        attempted = any(
            json.loads(row[0]).get("protection_client_order_id") == client_id
            for row in connection.execute("SELECT metadata_json FROM runs")
        )
    try:
        replacement = client.query(client_id)
    except ProviderError as error:
        if error.status != 404 or attempted:
            raise RuntimeError("保护性止损提交结果未知，未重复提交") from None
        audit.run(
            context, "protection_submitting", day=day, metadata={"protection_client_order_id": client_id}
        )
        replacement = client.protect(intent.symbol, state.positions[intent.symbol], stop, client_id)
    if (
        replacement.get("client_order_id") != client_id
        or replacement.get("side") != "sell"
        or replacement.get("type") != "stop"
        or replacement.get("status") in TERMINAL
        or float(replacement.get("qty", 0)) < state.positions[intent.symbol]
    ):
        raise RuntimeError("残余持仓保护性止损未确认，需核对券商状态")
    audit.commit_order(
        run_id,
        intent,
        terminal,
        {
            "cash": state.cash,
            "equity": state.equity,
            "positions": dict(state.positions),
            "protection": _safe_order(replacement),
        },
    )


def _flatten(orders):
    for order in orders:
        yield order
        yield from _flatten(order.get("legs") or [])


def run_paper(market, config, provider, *, confirm: bool, client=None, admission=None):
    if not confirm:
        raise ValueError("提交券商模拟单需要 --confirm-paper；先用 decide 查看计划")
    if isinstance(provider, DemoProvider):
        raise ValueError("演示规则不能连接券商")
    require_broker_paper_admission(config, provider, admission)
    client = client or AlpacaPaperClient()
    clock = client.clock()
    if clock.get("is_open") is not True:
        return {"status": "market_closed", "submitted_orders": 0, "real_model_calls": 0}
    now = datetime.fromisoformat(str(clock["timestamp"]).replace("Z", "+00:00"))
    if now.tzinfo is None:
        raise ValueError("券商时间必须包含时区")
    day = now.astimezone(EASTERN).date()
    sessions = client.fetch_calendar(day - timedelta(days=14), day)
    previous = max((session for session in sessions if session < day), default=None)
    if previous is None:
        raise ValueError("无法确认前一交易日")
    context = market.context(previous)
    account, fingerprint = client.account_state(context.features)
    directory = Path(config["execution"]["runtime_dir"]) / f"alpaca-paper-{fingerprint[:16]}"
    with (
        runtime_lock(directory),
        AIJournal(directory / "journal.sqlite", config, "alpaca_paper", fingerprint) as audit,
    ):
        # Recover all older ambiguous submissions before invoking the model or planning new risk.
        for pending in audit.journal.list_pending_order_recoveries():
            intent = OrderIntent(
                pending.client_order_id, pending.symbol, pending.side, pending.quantity, context.as_of
            )
            try:
                response = client.query(pending.client_order_id)
            except ProviderError:
                return {"status": "recovery_hold", "reason": "order_status_unknown", "submitted_orders": 0}
            current, _ = client.account_state(context.features)
            terminal = _record_response(audit, pending.run_id, intent, response, current, config)
            if not terminal:
                return {"status": "recovery_hold", "reason": "order_still_working", "submitted_orders": 0}
            # An exit can leave a partial holding: restore a broker-native stop before admitting new risk.
            if intent.side == "sell" and current.positions.get(intent.symbol, 0):
                open_protections = [
                    o
                    for o in _flatten(client.open_orders())
                    if o.get("id") in _protection_ids(audit)
                    and o.get("symbol", "").replace(".", "-") == intent.symbol
                ]
                if not open_protections:
                    stop = context.features[intent.symbol]["suggested_stop"]
                    _restore_protection(
                        client, audit, context, pending.run_id, intent, terminal, current, stop, day
                    )
                else:
                    audit.commit_order(
                        pending.run_id,
                        intent,
                        terminal,
                        {
                            "cash": current.cash,
                            "equity": current.equity,
                            "positions": dict(current.positions),
                            "confirmed_protections": [_safe_order(o) for o in open_protections],
                        },
                    )
        account, _ = client.account_state(context.features)
        known = _protection_ids(audit)
        protections = {}
        for order in _flatten(client.open_orders()):
            if order.get("status") in TERMINAL:
                continue
            symbol = str(order.get("symbol", "")).replace(".", "-")
            if order.get("id") not in known or order.get("type") != "stop" or order.get("side") != "sell":
                return {
                    "status": "recovery_hold",
                    "reason": "unmanaged_or_working_order",
                    "submitted_orders": 0,
                }
            protections.setdefault(symbol, []).append(order)
        for symbol, quantity in account.positions.items():
            covered = sum(
                float(order["qty"]) - float(order.get("filled_qty", 0))
                for order in protections.get(symbol, [])
            )
            if covered < quantity:
                return {
                    "status": "recovery_hold",
                    "reason": "position_without_managed_stop",
                    "submitted_orders": 0,
                }
        if audit.existing_runs(day):
            previous_status = audit.existing_runs(day)[-1]["status"]
            if previous_status in {"started", "model_error_hold"}:
                return {
                    "status": "recovery_hold",
                    "reason": "prior_model_run_incomplete",
                    "submitted_orders": 0,
                }
            return {
                "status": "already_processed",
                "previous_status": previous_status,
                "submitted_orders": 0,
                "real_model_calls": 0,
            }
        audit.run(context, "started", day=day)
        try:
            decision = provider.decide(context, account, config)
        except (ProviderError, ValueError):
            audit.run(context, "model_error_hold", day=day)
            return {
                "status": "recovery_hold",
                "reason": "model_response_unavailable_or_invalid",
                "submitted_orders": 0,
            }
        refreshed, _ = client.account_state(context.features)
        if refreshed.positions != account.positions or abs(refreshed.cash - account.cash) > 0.01:
            audit.run(
                context, "recovery_hold", day=day, metadata={"reason": "account_changed_during_decision"}
            )
            return {
                "status": "recovery_hold",
                "reason": "account_changed_during_decision",
                "submitted_orders": 0,
            }
        if client.clock().get("is_open") is not True:
            audit.run(context, "recovery_hold", day=day, metadata={"reason": "market_closed_during_decision"})
            return {
                "status": "recovery_hold",
                "reason": "market_closed_during_decision",
                "submitted_orders": 0,
            }
        account_stops = {
            symbol: min(float(o["stop_price"]) for o in orders) for symbol, orders in protections.items()
        }
        account = AccountState(
            refreshed.cash, refreshed.equity, refreshed.positions, refreshed.broker, account_stops
        )
        plan = build_plan(context, account, decision, config, identity=fingerprint)
        run_id = audit.plan(context, plan, day=day)
        submitted = 0
        for intent in plan.intents:
            refreshed, _ = client.account_state(context.features)
            gate = BrokerAccountRiskGate().evaluate(
                refreshed.broker, intent.quantity * plan.limit_prices[intent.symbol], side=intent.side
            )
            if not gate.allowed or (
                intent.side == "buy"
                and intent.quantity * plan.limit_prices[intent.symbol]
                > max(0, refreshed.cash - refreshed.equity * config["risk"]["cash_buffer"])
            ):
                audit.run(context, "recovery_hold", day=day, metadata={"reason": "pre_submit_account_risk"})
                return {
                    "status": "recovery_hold",
                    "reason": "pre_submit_account_risk",
                    "submitted_orders": submitted,
                }
            # Persist the ambiguous state BEFORE the network side effect. Never blindly retry a POST.
            audit.order(run_id, intent, "submitted", metadata={"submission_state": "request_pending"})
            try:
                if intent.side == "sell":
                    for protection in protections.get(intent.symbol, []):
                        client.cancel_and_wait(protection["id"])
                    current, _ = client.account_state(context.features)
                    if current.positions.get(intent.symbol, 0) != account.positions.get(intent.symbol, 0):
                        return {
                            "status": "recovery_hold",
                            "reason": "position_changed_during_cancel",
                            "submitted_orders": submitted,
                        }
                response = client.submit_intent(intent, plan)
                submitted += 1
                current, _ = client.account_state(context.features)
                terminal = _record_response(audit, run_id, intent, response, current, config)
                if intent.side == "sell" and terminal and current.positions.get(intent.symbol, 0):
                    _restore_protection(
                        client,
                        audit,
                        context,
                        run_id,
                        intent,
                        terminal,
                        current,
                        plan.stops[intent.symbol],
                        day,
                    )
            except (ProviderError, RuntimeError, ValueError):
                audit.run(
                    context, "recovery_hold", day=day, metadata={"client_order_id": intent.client_order_id}
                )
                return {
                    "status": "recovery_hold",
                    "reason": "submission_or_protection_status_unknown",
                    "submitted_orders": submitted,
                    "submission_uncertain": True,
                    "journal": str(audit.journal.path),
                }
            # An unfilled exit must complete and be reconciled before submitting additional orders.
            if intent.side == "sell" and not terminal:
                break
        audit.run(context, "submitted", day=day, metadata={"submitted_orders": submitted})
        return {
            "status": "submitted",
            "submitted_orders": submitted,
            "plan": plan.payload(),
            "journal": str(audit.journal.path),
            "real_model_calls": int(decision.provider == "openai_compatible"),
        }
