"""只读核验已保存的本地回测账目；独立从成交现金流重建，不调用执行器或模型。"""

from decimal import Decimal, InvalidOperation
from datetime import date, datetime


def _amount(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"账目核验的 {field} 必须为有限数值")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"账目核验的 {field} 必须为有限数值") from None
    if not result.is_finite():
        raise ValueError(f"账目核验的 {field} 必须为有限数值")
    return result


def _instant(value):
    if not isinstance(value, str):
        raise ValueError("账目核验的时间必须为带时区 ISO 时间")
    try:
        at = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("账目核验的时间必须为带时区 ISO 时间") from None
    if at.tzinfo is None:
        raise ValueError("账目核验的时间必须为带时区 ISO 时间")
    return at


def reconcile_replay(records):
    """records 来自 inspect_replay 的结构检查；金额再按外部证据检查。

    成交价格已包含估计成本，不能再次从现金扣费。已实现损益使用平均成本，
    与当前本地账户契约一致；不验证成交真实性、市场容量或原始价格来源。
    """
    accounts = [record["payload"] for record in records if record["kind"] == "account"]
    missing = sum("valuation_prices" not in row for row in accounts)
    boundary = "仅核对已保存记录的现金流与估值一致性；部分记录通过不代表运行完整，也不认证行情或实际成交。"
    if not accounts or missing:
        return {"status": "unavailable", "account_snapshots": len(accounts), "missing_valuation_snapshots": missing,
                "reason": "没有账户快照" if not accounts else "旧日志缺少逐时点估值价格，请重新回测", "boundary": boundary}
    metadata = records[0]["payload"]
    try:
        initial_cash = _amount(metadata["config"]["execution"]["initial_cash"], "initial_cash")
        market = metadata["spec"]["market"]
    except (KeyError, TypeError):
        raise ValueError("账目核验缺少初始资金或市场身份") from None
    if initial_cash <= 0 or market not in {"stocks", "crypto"}:
        raise ValueError("账目核验的初始资金或市场身份无效")
    cash, peak, realized, costs = initial_cash, initial_cash, Decimal(0), Decimal(0)
    positions, basis, seen_fills, differences = {}, {}, set(), []
    order_remaining = {}
    receivables, seen_actions, dividend_income = {}, set(), Decimal(0)
    declared = metadata.get("corporate_actions", [])
    if not isinstance(declared, list) or any(not isinstance(a, dict) for a in declared):
        raise ValueError("账目核验的公司行动来源结构无效")
    action_inputs = {}
    for action in declared:
        if "id" not in action:
            continue  # 旧格式的行动标记没有可执行的会计条款。
        if any(not isinstance(action.get(key), str) or not action[key] for key in ("id", "symbol", "date", "type")):
            raise ValueError("账目核验的公司行动来源身份无效")
        if action["id"] in action_inputs or action["type"] not in {"split", "dividend"}:
            raise ValueError("账目核验的公司行动来源重复或类型无效")
        date.fromisoformat(action["date"])
        if action["type"] == "split":
            if _amount(action.get("ratio"), "split_ratio") <= 0:
                raise ValueError("账目核验的拆股比例无效")
        else:
            if (action.get("currency") != "USD" or action.get("distribution") != "ordinary_cash"
                    or not isinstance(action.get("pay_date"), str)):
                raise ValueError("账目核验的分红来源条款无效")
            if date.fromisoformat(action["pay_date"]) < date.fromisoformat(action["date"]):
                raise ValueError("账目核验的分红支付日早于除息日")
            if _amount(action.get("amount_per_share"), "dividend_per_share") <= 0:
                raise ValueError("账目核验的每股分红无效")
        action_inputs[action["id"]] = action

    def compare(at, field, expected, actual):
        # 报告的数值字段经过浮点序列化；按金额大小允许浮点表示误差。
        tolerance = max(Decimal("0.00000001"), abs(expected) * Decimal("1e-15"))
        if abs(expected - actual) > tolerance:
            differences.append({"as_of": at, "field": field, "expected": str(expected), "reported": str(actual)})

    def check_account(at, account, prices):
        nonlocal peak
        if not isinstance(account, dict) or not isinstance(prices, dict) or not isinstance(account.get("positions"), dict):
            raise ValueError("账目核验的账户、持仓或估值价格结构无效")
        reported_positions = {symbol: _amount(quantity, "position_quantity") for symbol, quantity in account["positions"].items()}
        if reported_positions != positions:
            differences.append({"as_of": at, "field": "positions", "expected": {s: str(q) for s, q in positions.items()},
                                "reported": {s: str(q) for s, q in reported_positions.items()}})
        value = Decimal(0)
        for symbol, quantity in positions.items():
            if symbol not in prices:
                raise ValueError(f"账目核验缺少 {symbol} 的估值价格")
            price = _amount(prices[symbol], "valuation_price")
            if price <= 0:
                raise ValueError("账目核验的估值价格必须大于零")
            value += quantity * price
        receivable = sum((item["amount"] for item in receivables.values()), Decimal(0))
        equity = cash + value + receivable
        peak = max(peak, equity)
        expected = {"cash": cash, "equity": equity, "realized_pnl": realized,
                    "unrealized_pnl": value - sum(basis.values(), Decimal(0)),
                    "estimated_execution_cost": costs, "drawdown_fraction": equity / peak - 1,
                    "dividend_income": dividend_income, "dividend_receivable": receivable}
        for field, amount in expected.items():
            if field not in account:
                if "corporate_actions" not in metadata and field in {"dividend_income", "dividend_receivable"}:
                    continue  # 旧格式没有公司行动账目；不能要求事后补写历史证据。
                raise ValueError(f"账目核验缺少账户字段 {field}")
            compare(at, field, amount, _amount(account[field], field))
        return {**{field: str(amount) for field, amount in expected.items()},
                "positions": {symbol: str(quantity) for symbol, quantity in positions.items()}}

    previous_as_of = None
    for row in accounts:
        if not isinstance(row.get("orders"), list) or not isinstance(row.get("as_of"), str):
            raise ValueError("账目核验缺少逐时点订单或时间")
        closing_at = _instant(row["as_of"])
        if previous_as_of is not None and closing_at <= _instant(previous_as_of):
            raise ValueError("账目核验的账户时间必须递增")
        actions = row.get("corporate_actions", [])
        if not isinstance(actions, list) or any(not isinstance(event, dict) for event in actions):
            raise ValueError("账目核验的公司行动事件无效")
        if any(not isinstance(event.get("action"), dict) or not isinstance(event["action"].get("id"), str)
               for event in actions):
            raise ValueError("账目核验的公司行动身份无效")
        if previous_as_of is not None:
            required = {identity for identity, action in action_inputs.items()
                        if previous_as_of[:10] < action["date"] <= row["as_of"][:10]}
            observed = {event.get("action", {}).get("id") for event in actions
                        if event.get("kind") in {"split", "dividend_entitlement"}}
            if required != observed:
                raise ValueError("账目核验的公司行动生效记录缺失或多余")
        events = []
        for order in row["orders"]:
            if not isinstance(order, dict) or order.get("status") not in {"filled", "canceled"}:
                raise ValueError("账目核验的订单状态无效")
            if order["status"] != "filled":
                continue
            events.append((order.get("at", row["as_of"]), 1, "fill", order))
        for event in actions:
            if not isinstance(event.get("at"), str) or event.get("kind") not in {"split", "dividend_entitlement", "dividend_payment"}:
                raise ValueError("账目核验的公司行动类型或时间无效")
            events.append((event["at"], 2 if event["kind"] == "dividend_payment" else 0, event["kind"], event))
        if actions:
            # 开盘先登记权益，之后成交，支付日收盘才入账；不能把所有行动挪到所有成交之前。
            try:
                events.sort(key=lambda item: (_instant(item[0]), item[1]))
            except (TypeError, ValueError):
                raise ValueError("账目核验的事件时间无法排序") from None
        for event_at, _, kind, order in events:
            timestamp = _instant(event_at)
            # 连续加密 K 线的下一次开盘与前一根收盘为同一时刻。
            if timestamp > closing_at or previous_as_of is not None and timestamp < _instant(previous_as_of):
                raise ValueError("账目核验的事件时间不在本次账户区间内")
            if kind != "fill":
                action = order.get("action")
                if not isinstance(action, dict) or not isinstance(action.get("id"), str):
                    raise ValueError("账目核验的公司行动身份无效")
                identity, symbol = action["id"], action.get("symbol")
                if market != "stocks" or action_inputs.get(identity) != action or (identity, kind) in seen_actions:
                    raise ValueError("账目核验的公司行动与来源不一致或重复")
                if (kind == "split") != (action["type"] == "split"):
                    raise ValueError("账目核验的公司行动类型与来源不一致")
                if kind != "dividend_payment" and event_at[:10] != action["date"]:
                    raise ValueError("账目核验的公司行动生效时间不一致")
                seen_actions.add((identity, kind))
                owned = positions.get(symbol, Decimal(0))
                if kind == "split":
                    ratio = _amount(action["ratio"], "split_ratio")
                    changed = owned * ratio
                    if ratio <= 0 or changed != changed.to_integral_value():
                        raise ValueError("账目核验发现无效拆股比例或碎股")
                    compare(event_at, "split_quantity_before:" + identity, owned, _amount(order.get("quantity_before"), "quantity_before"))
                    compare(event_at, "split_quantity_after:" + identity, changed, _amount(order.get("quantity_after"), "quantity_after"))
                    if owned:
                        positions[symbol] = changed  # 总成本不变，每股平均成本随股数改变。
                elif kind == "dividend_entitlement":
                    per_share = _amount(action["amount_per_share"], "dividend_per_share")
                    if per_share <= 0:
                        raise ValueError("账目核验的每股分红无效")
                    amount = owned * per_share
                    compare(event_at, "dividend_quantity:" + identity, owned, _amount(order.get("quantity"), "quantity"))
                    compare(event_at, "dividend_amount:" + identity, amount, _amount(order.get("amount"), "amount"))
                    dividend_income += amount
                    if owned:
                        receivables[identity] = {"amount": amount, "pay_date": action["pay_date"]}
                else:
                    if identity not in receivables or receivables[identity]["pay_date"] > event_at[:10]:
                        raise ValueError("账目核验发现未取得权益或提前支付的分红")
                    amount = receivables.pop(identity)["amount"]
                    compare(event_at, "dividend_payment:" + identity, amount, _amount(order.get("amount"), "amount"))
                    cash += amount
                continue
            try:
                identity, symbol, side = order["client_order_id"], order["symbol"], order["side"]
                quantity, price = _amount(order["quantity"], "fill_quantity"), _amount(order["price"], "fill_price")
                reference, cost = _amount(order["reference_price"], "reference_price"), _amount(order["estimated_cost"], "estimated_cost")
            except KeyError:
                raise ValueError("账目核验的成交记录字段不完整") from None
            if not isinstance(identity, str) or not identity or not isinstance(symbol, str) or not symbol:
                raise ValueError("账目核验的成交身份或标的无效")
            fill_identity = order.get("fill_id", identity)
            if not isinstance(fill_identity, str) or not fill_identity:
                raise ValueError("账目核验的分笔成交身份无效")
            if fill_identity in seen_fills:
                raise ValueError("账目核验发现重复成交身份")
            seen_fills.add(fill_identity)
            if "fill_id" in order:
                remaining = _amount(order.get("remaining"), "remaining")
                if remaining < 0 or order.get("partial") is not (remaining > 0):
                    raise ValueError("账目核验的分笔成交余量无效")
                if identity in order_remaining and quantity + remaining != order_remaining[identity]:
                    raise ValueError("账目核验的分笔成交余量不连续")
                order_remaining[identity] = remaining
            if quantity <= 0 or price <= 0 or reference <= 0 or cost < 0 or side not in {"buy", "sell"}:
                raise ValueError("账目核验的成交数量、价格、费用或方向无效")
            if market == "stocks" and quantity != quantity.to_integral_value():
                raise ValueError("账目核验发现非整数美股成交")
            price_cost = (price - reference if side == "buy" else reference - price) * quantity
            compare(row["as_of"], "fill_cost:" + identity, price_cost, cost)
            owned, book = positions.get(symbol, Decimal(0)), basis.get(symbol, Decimal(0))
            notional = price * quantity
            if side == "buy":
                cash -= notional
                positions[symbol], basis[symbol] = owned + quantity, book + notional
            else:
                if quantity > owned:
                    raise ValueError("账目核验发现卖出超过持仓")
                allocated_basis = book * quantity / owned
                realized += notional - allocated_basis
                cash += notional
                if quantity == owned:
                    positions.pop(symbol)
                    basis.pop(symbol)
                else:
                    positions[symbol], basis[symbol] = owned - quantity, book - allocated_basis
            if cash < 0:
                raise ValueError("账目核验发现现金透支")
            costs += cost
        if any(item["pay_date"] <= row["as_of"][:10] for item in receivables.values()):
            raise ValueError("账目核验发现已到支付日但缺少入账事件的分红")
        closing = check_account(row["as_of"], row.get("account"), row["valuation_prices"])
        previous_as_of = row["as_of"]
    terminal = records[-1]
    if terminal["kind"] == "completed":
        closing = check_account(accounts[-1]["as_of"], terminal["payload"]["account"], accounts[-1]["valuation_prices"])
    return {"status": "mismatch" if differences else "reconciled", "account_snapshots": len(accounts),
            "fills": len(seen_fills), "discrepancies": differences, "closing_balances": closing, "boundary": boundary}
