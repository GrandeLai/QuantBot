"""主回测的已声明公司行动：整股拆并股、普通美元现金分红。"""

from datetime import date
from decimal import Decimal, InvalidOperation

from quantbot.evidence import hash_payload


EXIT_INTENTS = {"protective_stop", "take_profit", "manual_exit", "session_flatten", "holding_limit"}


def positive_amount(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"公司行动 {name} 必须为正有限数值")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"公司行动 {name} 必须为正有限数值") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"公司行动 {name} 必须为正有限数值")
    return amount


def supported(action):
    return (action.get("type") == "split" and "ratio" in action
            or action.get("type") == "dividend" and action.get("distribution") == "ordinary_cash")


def normalize_actions(actions, symbols, captured):
    from .feeds import stock_calendar
    from .vintages import instant

    base = {"symbol", "date", "type", "available_at"}
    normalized, identities, sessions = [], set(), set()
    for action in actions:
        if not isinstance(action, dict) or not base <= set(action):
            raise ValueError("公司行动须包含 symbol/date/type/available_at")
        if any(not isinstance(action[key], str) for key in ("symbol", "date", "type")):
            raise ValueError("公司行动标的、日期和类型必须为字符串")
        if action["symbol"] not in symbols or action["type"] not in {"split", "dividend", "merger", "delisting", "symbol_change"}:
            raise ValueError("公司行动标的或类型无效")
        day = date.fromisoformat(action["date"]).isoformat()
        available = instant(action["available_at"])
        if available > captured:
            raise ValueError("公司行动发布时间不能晚于采集时间")
        record = {**action, "date": day, "available_at": available.isoformat()}
        if set(action) != base:
            required = (base | {"id", "ratio"} if action["type"] == "split" else
                        base | {"id", "amount_per_share", "pay_date", "currency", "distribution"})
            if action["type"] not in {"split", "dividend"} or set(action) != required:
                raise ValueError("公司行动详情字段不符合拆股或普通现金分红契约")
            identity = action["id"]
            if not isinstance(identity, str) or not 1 <= len(identity.strip()) <= 120 or identity in identities:
                raise ValueError("公司行动 id 必须为 1..120 字符且不能重复或修订")
            identities.add(identity)
            if action["type"] == "split":
                ratio = positive_amount(action["ratio"], "ratio")
                if ratio == 1:
                    raise ValueError("拆股 ratio 不能等于 1")
                record["ratio"] = str(ratio)
            else:
                if action["currency"] != "USD" or action["distribution"] != "ordinary_cash":
                    raise ValueError("仅支持 USD 普通现金分红，特殊分红和资本返还不适用")
                record["amount_per_share"] = str(positive_amount(action["amount_per_share"], "amount_per_share"))
                if not isinstance(action["pay_date"], str):
                    raise ValueError("分红 pay_date 必须为日期字符串")
                record["pay_date"] = date.fromisoformat(action["pay_date"]).isoformat()
                if record["pay_date"] < day:
                    raise ValueError("普通现金分红 pay_date 不能早于除息日")
        key = (action["symbol"], day)
        if key in sessions:
            raise ValueError("同标的同日多个公司行动的顺序尚不支持")
        sessions.add(key)
        normalized.append(record)
    if normalized:
        days = [a["date"] for a in normalized]
        calendar = stock_calendar(min(days), max(days))
        for action in normalized:
            if not calendar.is_session(action["date"]):
                raise ValueError("公司行动生效日期必须为交易日")
            if supported(action) and instant(action["available_at"]) > calendar.session_open(action["date"]).to_pydatetime():
                raise ValueError("公司行动详情须在生效日开盘前可得；迟发或更正尚不支持")
    return sorted(normalized, key=lambda a: (a["date"], a["symbol"]))


def check_action_window(actions, start, end):
    if any(start <= a["date"] <= end and not supported(a) for a in actions or []):
        raise ValueError("区间包含缺少详情或尚不支持的公司行动，不能继续回测")


def opening_actions(state, actions, day, at):
    """先记除权/除息权益，再处理当日买卖；旧目标计划撤回后重新决策。"""
    canceled = []
    for action in actions or []:
        if action["date"] != day:
            continue
        if not supported(action):
            raise ValueError("缺少详情或尚不支持的公司行动，不能继续回测")
        symbol, identity = action["symbol"], action["id"]
        quantity = Decimal(state["positions"].get(symbol, "0"))
        if action["type"] == "split":
            ratio = Decimal(action["ratio"])
            changed = quantity * ratio
            if changed != changed.to_integral_value():
                raise ValueError("拆并股产生碎股，缺少发行人碎股处理条款，拒绝虚构现金替代")
            if quantity:
                state["positions"][symbol] = str(changed)
                state["average_cost"][symbol] = str(Decimal(state["average_cost"][symbol]) / ratio)
                state["stops"][symbol] = str(Decimal(state["stops"][symbol]) / ratio)
                if symbol in state.get("take_profits", {}):
                    state["take_profits"][symbol] = str(Decimal(state["take_profits"][symbol]) / ratio)
            state["prices"][symbol] = str(Decimal(state["prices"][symbol]) / ratio)
            event = {"kind": "split", "quantity_before": str(quantity), "quantity_after": str(changed)}
        else:
            per_share = Decimal(action["amount_per_share"])
            if per_share >= Decimal(state["prices"][symbol]) * Decimal("0.25"):
                raise ValueError("分红达到前收盘价的 25%，特殊除息规则尚不支持")
            if quantity and Decimal(state["stops"][symbol]) <= per_share:
                raise ValueError("除息后保护止损不再为正数，需重新定义风险阈值")
            amount = quantity * per_share
            if quantity:
                state["stops"][symbol] = str(Decimal(state["stops"][symbol]) - per_share)
                if symbol in state.get("take_profits", {}):
                    state["take_profits"][symbol] = str(Decimal(state["take_profits"][symbol]) - per_share)
                state.setdefault("dividend_receivables", {})[identity] = {
                    "amount": str(amount), "symbol": symbol, "pay_date": action["pay_date"], "action": dict(action)}
                state["dividend_income"] = str(Decimal(state.get("dividend_income", "0")) + amount)
            event = {"kind": "dividend_entitlement", "quantity": str(quantity), "amount": str(amount)}
        state.setdefault("corporate_action_events", []).append({**event, "action": dict(action), "at": at.isoformat()})
        pending = state["pending"]
        exits = [o for o in pending if o["side"] == "sell" and o.get("reason") in EXIT_INTENTS]
        targets = [o for o in pending if o not in exits]
        if any(order["symbol"] == symbol for order in targets):
            # queue_plan admits one pending target plan. Revoke all its legs before
            # execution when any leg is stale; protective orders remain separate.
            for order in targets:
                reason = "corporate_action_replan" if order["symbol"] == symbol else "corporate_action_plan_replan"
                canceled.append({**order, "status": "canceled", "reason": reason,
                                 "action_id": identity, "at": at.isoformat()})
            targets = []
        # An already-triggered exit is an execution obligation, not a stale
        # target allocation. Preserve it even if the ex-date price recovers.
        adjusted_exits = []
        for order in exits:
            if order["symbol"] != symbol:
                adjusted_exits.append(order)
                continue
            canceled.append({**order, "status": "canceled", "reason": "corporate_action_exit_replaced",
                             "action_id": identity, "at": at.isoformat()})
            remaining = state["positions"].get(symbol, "0")
            if Decimal(remaining) > 0:
                replacement = {
                    **order, "client_order_id": "action-exit-" + hash_payload([order["client_order_id"], identity])[:24],
                    "replaces_client_order_id": order["client_order_id"], "quantity": remaining,
                    "remaining": remaining, "stop": state["stops"][symbol],
                }
                if symbol in state.get("take_profits", {}):
                    replacement["take_profit"] = state["take_profits"][symbol]
                adjusted_exits.append(replacement)
        state["pending"] = targets + adjusted_exits
    return canceled


def pay_dividends(state, day, at):
    # 只有日期而无实际入账时间，保守地到支付日（或之后首个交易日）收盘才可用于下一次决策。
    for identity, item in list(state.get("dividend_receivables", {}).items()):
        if item["pay_date"] > day:
            continue
        state["cash"] = str(Decimal(state["cash"]) + Decimal(item["amount"]))
        state["dividend_receivables"].pop(identity)
        state["corporate_action_events"].append({"kind": "dividend_payment", "action": item["action"],
            "amount": item["amount"], "at": at.isoformat(), "cash_availability_basis": "pay_date_or_next_session_close"})
