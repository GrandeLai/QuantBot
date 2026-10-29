"""已有股票持仓的现金约束期权覆盖回测；所有成交来自历史双边报价。"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal as D

from .greeks import BlackScholes
from .overlay_data import amount, fields, integer


STRATEGIES = ("hold", "covered_call", "protective_put", "collar")


@dataclass(frozen=True)
class OverlayPolicy:
    min_dte: int = 14
    target_dte: int = 30
    max_dte: int = 45
    roll_dte: int = 3
    call_moneyness: float = 1.05
    put_moneyness: float = 0.95
    coverage_fraction: float = 1.0
    max_premium_fraction: float = 0.05
    max_spread_fraction: float = 0.3
    min_open_interest: int = 10
    max_quote_participation: float = 0.1
    commission_per_contract: float = 0.65
    exercise_fee_per_contract: float = 0.0
    exercise_threshold: float = 0.01
    opening_window_seconds: int = 60
    decision_delay_seconds: int = 900
    max_quote_age_seconds: int = 300
    risk_free_rate: float = 0.03
    liquidate_at_end: bool = False

    def __post_init__(self):
        for key in ("call_moneyness", "put_moneyness", "coverage_fraction", "max_premium_fraction",
                    "max_spread_fraction", "max_quote_participation", "commission_per_contract",
                    "exercise_fee_per_contract", "exercise_threshold", "risk_free_rate"):
            if type(getattr(self, key)) not in {int, float}:
                raise ValueError(f"{key} 必须为数值")
        for key in ("min_dte", "target_dte", "max_dte", "roll_dte"):
            integer(getattr(self, key), key, 0, 730)
        if not 1 <= self.min_dte <= self.target_dte <= self.max_dte or self.roll_dte >= self.min_dte:
            raise ValueError("要求 1 <= min_dte <= target_dte <= max_dte，且 roll_dte < min_dte")
        for key in ("coverage_fraction", "max_premium_fraction", "max_spread_fraction", "max_quote_participation", "risk_free_rate"):
            amount(getattr(self, key), key, maximum="1")
        if self.max_quote_participation <= 0:
            raise ValueError("报价参与率必须大于 0")
        if not D(1) <= amount(self.call_moneyness, "call_moneyness") <= D(2) or not D("0.1") <= amount(self.put_moneyness, "put_moneyness") <= D(1):
            raise ValueError("看涨行权价比例须为 1..2，看跌为 0.1..1")
        for key in ("commission_per_contract", "exercise_fee_per_contract", "exercise_threshold"):
            amount(getattr(self, key), key, maximum="100")
        if self.exercise_threshold <= 0:
            raise ValueError("到期行权门槛必须为正")
        integer(self.min_open_interest, "min_open_interest")
        integer(self.opening_window_seconds, "opening_window_seconds", 0, 300)
        integer(self.decision_delay_seconds, "decision_delay_seconds", 0, 3600)
        integer(self.max_quote_age_seconds, "max_quote_age_seconds", 0, 3600)
        if type(self.liquidate_at_end) is not bool:
            raise ValueError("liquidate_at_end 必须为布尔值")


class OverlayAccount:
    def __init__(self, data, portfolio):
        fields(portfolio, "cash positions", "初始持仓")
        if not isinstance(portfolio["positions"], dict) or not portfolio["positions"]:
            raise ValueError("需要导入已有股票持仓")
        self.cash = amount(portfolio["cash"], "cash")
        self.stocks = {}
        for symbol, qty in portfolio["positions"].items():
            if symbol not in data.symbols:
                raise ValueError("持仓缺少对应历史股价")
            self.stocks[symbol] = integer(qty, "股票数量", 1)
        self.basis = {s: D(str(data.bars[data.days[0], s].close)) for s in self.stocks}
        self.initial_equity = self.cash + sum(self.basis[s] * q for s, q in self.stocks.items())
        self.options = {}
        self.unsettled = []
        self.receivables = []
        self.stock_realized = D(0)
        self.option_cashflow = D(0)
        self.dividend_income = D(0)
        self.fees = D(0)
        self.opening_premium_received = D(0)
        self.opening_premium_paid = D(0)

    @property
    def buying_power(self):
        return self.cash - sum((p["amount"] for p in self.unsettled), D(0))

    def cash_trade(self, net, day, data):
        if net < 0 and -net > self.buying_power:
            raise ValueError("已结算资金不足")
        self.cash += net
        if net > 0:
            self.unsettled.append({"date": data.settlement_day(day), "amount": net})

    def settle(self, day):
        self.unsettled = [p for p in self.unsettled if p["date"] > day]
        paid = [p for p in self.receivables if p["date"] <= day]
        self.cash += sum((p["amount"] for p in paid), D(0))
        self.receivables = [p for p in self.receivables if p["date"] > day]
        return paid

    def view(self, data, day, policy):
        stock_value = sum((D(str(data.bars[day, s].close)) * q for s, q in self.stocks.items()), D(0))
        stock_pnl = self.stock_realized + sum(((D(str(data.bars[day, s].close)) - self.basis[s]) * q for s, q in self.stocks.items()), D(0))
        option_value = D(0)
        for cid, qty in self.options.items():
            q = data.quote(cid, day, "close", policy)
            if q is None:
                raise ValueError(f"持有合约 {cid} 在 {day} 缺少可得的收盘报价，不能用理论价格填补")
            option_value += (q.bid if qty > 0 else q.ask) * qty * data.contracts[cid].multiplier
        receivable = sum((p["amount"] for p in self.receivables), D(0))
        equity = self.cash + receivable + stock_value + option_value
        components = {"stock_pnl": stock_pnl, "option_pnl": self.option_cashflow + option_value,
                      "dividend_income": self.dividend_income, "fees": -self.fees}
        if equity - self.initial_equity != sum(components.values()):
            raise RuntimeError("期权账本权益与现金流归因不一致")
        return {"date": day.isoformat(), "equity": float(equity), "cash": float(self.cash),
                "buying_power": float(self.buying_power), "unsettled_credits": float(self.cash - self.buying_power),
                "dividend_receivable": float(receivable), "stock_value": float(stock_value),
                "option_liquidation_value": float(option_value), "stock_positions": dict(self.stocks),
                "option_positions": dict(self.options), **{k: float(v) for k, v in components.items()}}


def liquid(quote, policy):
    mid = (quote.ask + quote.bid) / 2
    return mid > 0 and (quote.ask - quote.bid) / mid <= D(str(policy.max_spread_fraction))


def select_overlay(data, state, day, symbol, strategy, policy):
    if strategy == "hold":
        return {}
    count = int(D(state.stocks[symbol]) * D(str(policy.coverage_fraction)) / 100)
    if count == 0:
        return {}
    spot = D(str(data.bars[day, symbol].close))
    choices = {}
    cutoff = data.at(day, "close") + timedelta(seconds=policy.decision_delay_seconds)
    for cid in data.quotes.get((day, "close"), {}):
        contract = data.contracts[cid]
        dte = (contract.last_trade_date - day).days
        if contract.symbol != symbol or not policy.min_dte <= dte <= policy.max_dte or contract.available_at > cutoff:
            continue
        quote = data.quote(cid, day, "close", policy)
        if quote is None or quote.open_interest < policy.min_open_interest or not liquid(quote, policy):
            continue
        if (contract.kind == "call" and contract.strike < spot) or (contract.kind == "put" and contract.strike > spot):
            continue
        choices.setdefault(contract.last_trade_date, {"call": [], "put": []})[contract.kind].append(contract)
    kinds = ("call", "put") if strategy == "collar" else ("call",) if strategy == "covered_call" else ("put",)
    expiries = [expiry for expiry, kinds_map in choices.items() if all(kinds_map[k] for k in kinds)]
    if not expiries:
        return None
    expiry = min(expiries, key=lambda e: (abs((e - day).days - policy.target_dte), e))
    selected = {}
    for kind in kinds:
        target = spot * D(str(policy.call_moneyness if kind == "call" else policy.put_moneyness))
        contract = min(choices[expiry][kind], key=lambda c: (abs(c.strike - target), c.id))
        selected[contract.id] = -count if kind == "call" else count
    return selected


def plan_overlay(data, state, day, strategy, policy):
    plans, decisions = [], []
    for symbol in state.stocks:
        owned = {cid: q for cid, q in state.options.items() if data.contracts[cid].symbol == symbol}
        overcovered = any(abs(q) * data.contracts[cid].multiplier > state.stocks[symbol] for cid, q in owned.items())
        rolling = any((data.contracts[cid].last_trade_date - day).days <= policy.roll_dte for cid in owned)
        if owned and not rolling and not overcovered:
            continue
        selected = {} if overcovered else select_overlay(data, state, day, symbol, strategy, policy)
        if selected is None:
            decisions.append({"date": str(day), "symbol": symbol, "reason": "no_eligible_chain", "planned": False})
        elif selected != owned:
            plan = {"decision_at": (data.at(day, "close") + timedelta(seconds=policy.decision_delay_seconds)).isoformat(),
                    "symbol": symbol, "from": owned, "target": selected, "reason": "reduce_uncovered_put" if overcovered else "roll" if owned else "open"}
            plans.append(plan)
            decisions.append({**plan, "planned": True})
    return plans, decisions


def execute_plan(data, state, plan, day, phase, policy, events, premium_budget):
    current = {cid: q for cid, q in state.options.items() if data.contracts[cid].symbol == plan["symbol"]}
    rejection = None
    if current != plan["from"]:
        rejection = "position_changed_since_decision"
    target = plan["target"]
    for kind in ("call", "put"):
        relevant = [(data.contracts[cid], q) for cid, q in target.items() if data.contracts[cid].kind == kind]
        if any((q >= 0 if kind == "call" else q <= 0) for _, q in relevant) or sum(abs(q) * c.multiplier for c, q in relevant) > state.stocks[plan["symbol"]]:
            rejection = "uncovered_or_overhedged_position"
    legs, gross, fees, opening_debit = [], D(0), D(0), D(0)
    for cid in sorted(set(current) | set(target)):
        delta = target.get(cid, 0) - current.get(cid, 0)
        if not delta:
            continue
        contract, quote = data.contracts[cid], data.quote(cid, day, phase, policy)
        if quote is None or not liquid(quote, policy):
            rejection = "missing_or_wide_quote"
            break
        size = quote.ask_size if delta > 0 else quote.bid_size
        if abs(delta) > int(D(size) * D(str(policy.max_quote_participation))):
            rejection = "insufficient_displayed_size"
            break
        price = quote.ask if delta > 0 else quote.bid
        fee = D(str(policy.commission_per_contract)) * abs(delta)
        cashflow = -D(delta) * price * contract.multiplier
        gross += cashflow
        fees += fee
        opening = abs(target.get(cid, 0)) > abs(current.get(cid, 0))
        if opening and delta > 0:
            opening_debit += -cashflow
        legs.append({"type": "option_trade", "contract_id": cid, "quantity": abs(delta),
                     "action": ("buy" if delta > 0 else "sell") + ("_to_open" if opening else "_to_close"),
                     "price": str(price), "multiplier": contract.multiplier, "cashflow": str(cashflow), "fee": str(fee),
                     "quote_at": quote.quote_at.isoformat(), "available_at": quote.available_at.isoformat()})
    if opening_debit > premium_budget:
        rejection = "premium_budget"
    if gross - fees < -state.buying_power:
        rejection = "settled_cash_limit"
    if rejection:
        events.append({"type": "plan_rejected", "date": str(day), "reason": rejection, "plan": plan})
        return False
    if not legs:
        return True
    at = max(leg["available_at"] for leg in legs)
    # 多腿采用一次净额包成交；只要任一腿失败，就整包拒绝，不制造裸露空头。
    state.cash_trade(gross - fees, day, data)
    state.option_cashflow += gross
    state.fees += fees
    for cid in current:
        state.options.pop(cid)
    state.options.update(target)
    for leg in legs:
        if leg["action"] == "sell_to_open":
            state.opening_premium_received += D(leg["cashflow"])
        elif leg["action"] == "buy_to_open":
            state.opening_premium_paid -= D(leg["cashflow"])
        events.append({**leg, "at": at, "decision_at": plan["decision_at"], "reason": plan["reason"]})
    return True


def intrinsic(contract, spot):
    return max(D(0), spot - contract.strike if contract.kind == "call" else contract.strike - spot)


def deliver_stock(data, state, cid, day, policy, events, reason):
    contract, qty = data.contracts[cid], abs(state.options[cid])
    shares = qty * contract.multiplier
    if shares > state.stocks[contract.symbol]:
        raise ValueError("实物行权/指派缺少可交付股票；拒绝创建股票空头")
    fee = D(str(policy.exercise_fee_per_contract)) * qty
    proceeds = contract.strike * shares
    state.cash_trade(proceeds - fee, day, data)
    state.fees += fee
    state.stock_realized += (contract.strike - state.basis[contract.symbol]) * shares
    state.stocks[contract.symbol] -= shares
    state.options.pop(cid)
    events.append({"type": reason, "at": data.at(day, "close").isoformat(), "contract_id": cid,
                   "contracts": qty, "shares": shares, "strike": str(contract.strike),
                   "stock_proceeds": str(proceeds), "fee": str(fee), "settlement_date": str(data.settlement_day(day))})


def expiry_and_assignment(data, state, day, policy, scenario, events):
    for cid, qty in list(state.options.items()):
        contract = data.contracts[cid]
        spot = D(str(data.bars[day, contract.symbol].close))
        intrinsic_value = intrinsic(contract, spot)
        if day == contract.last_trade_date:
            if intrinsic_value >= D(str(policy.exercise_threshold)):
                if qty > 0 and qty * contract.multiplier > state.stocks[contract.symbol]:
                    # 不足股票的长看跌只平仓，不能隐式变成卖空股票。
                    owned = {key: q for key, q in state.options.items() if data.contracts[key].symbol == contract.symbol}
                    plan = {"decision_at": data.at(day, "open").isoformat(), "symbol": contract.symbol,
                            "from": owned, "target": {key: q for key, q in owned.items() if key != cid}, "reason": "expiry_delivery_shortfall"}
                    if not execute_plan(data, state, plan, day, "close", policy, events, D(0)):
                        raise ValueError("到期长看跌缺少可交付股票，且无法按历史报价平仓")
                else:
                    deliver_stock(data, state, cid, day, policy, events, "expiration_assignment" if qty < 0 else "put_exercise")
            else:
                state.options.pop(cid)
                events.append({"type": "expired", "contract_id": cid, "date": str(day), "contracts": abs(qty)})
        elif scenario == "ex_dividend_stress" and qty < 0 and intrinsic_value > 0:
            upcoming = [d for d in data.dividends if d["symbol"] == contract.symbol
                        and d["ex_date"] == data.settlement_day(day) and d["available_at"] <= data.at(day, "close")]
            if upcoming:
                quote = data.quote(cid, day, "close", policy)
                if quote is None:
                    raise ValueError("提前指派情景缺少可得的报价")
                if sum(d["amount"] for d in upcoming) > max(D(0), quote.ask - intrinsic_value):
                    deliver_stock(data, state, cid, day, policy, events, "early_assignment_scenario")


def option_greeks(data, cid, day, policy):
    contract, quote = data.contracts[cid], data.quote(cid, day, "close", policy)
    if quote is None or quote.iv is None or contract.last_trade_date <= day:
        return None
    return asdict(BlackScholes(S=data.bars[day, contract.symbol].close, K=float(contract.strike),
                              T=(contract.last_trade_date - day).days / 365, r=policy.risk_free_rate,
                              sigma=quote.iv, option_type=contract.kind).compute())


def greek_report(data, state, day, previous_day, previous_positions, option_pnl_change, policy):
    exposure = {symbol: {"delta": float(shares), "gamma": 0.0, "theta": 0.0, "vega": 0.0} for symbol, shares in state.stocks.items()}
    complete, stable = True, 0
    estimated = {key: 0.0 for key in ("delta", "gamma", "theta", "vega")}
    for cid, qty in state.options.items():
        g = option_greeks(data, cid, day, policy)
        if g is None:
            complete = False
        else:
            symbol = data.contracts[cid].symbol
            for key in exposure[symbol]:
                exposure[symbol][key] += g[key] * qty * data.contracts[cid].multiplier
        if previous_day and previous_positions.get(cid) == qty:
            before = option_greeks(data, cid, previous_day, policy)
            if before is None or g is None:
                continue
            contract = data.contracts[cid]
            ds = data.bars[day, contract.symbol].close - data.bars[previous_day, contract.symbol].close
            iv_change = data.quote(cid, day, "close", policy).iv - data.quote(cid, previous_day, "close", policy).iv
            factor = qty * contract.multiplier
            estimated["delta"] += before["delta"] * ds * factor
            estimated["gamma"] += 0.5 * before["gamma"] * ds * ds * factor
            estimated["theta"] += before["theta"] * (day - previous_day).days * factor
            estimated["vega"] += before["vega"] * iv_change * 100 * factor
            stable += abs(qty)
    dollar_delta = sum(v["delta"] * data.bars[day, s].close for s, v in exposure.items())
    return {"exposure_by_symbol": exposure if complete else None, "dollar_delta": dollar_delta if complete else None,
            "complete": complete, "approximate_pnl": estimated,
            "residual_to_option_pnl": option_pnl_change - sum(estimated.values()), "stable_contract_intervals": stable,
            "basis": "欧式 Black-Scholes 近似；仅归因前后持仓相同且 IV 完整的期权，交易/指派/价差等进入残差"}


def replay_overlay(data, portfolio, strategy, policy, scenario="expiration_only"):
    if strategy not in STRATEGIES or scenario not in {"expiration_only", "ex_dividend_stress"}:
        raise ValueError("未知覆盖策略或指派情景")
    state = OverlayAccount(data, portfolio)
    curve, events, decisions, errors = [], [], [], []
    pending, previous_options = [], {}
    peak = state.initial_equity
    try:
        for index, day in enumerate(data.days):
            cutoff = data.at(day, "close") + timedelta(seconds=policy.decision_delay_seconds)
            if any(data.bar_availability[day, s] > cutoff for s in state.stocks):
                raise ValueError(f"{day} 股价晚于决策时点发布")
            if index:
                for dividend in data.dividends:
                    if dividend["ex_date"] == day:
                        entitlement = dividend["amount"] * state.stocks.get(dividend["symbol"], 0)
                        state.receivables.append({"date": dividend["pay_date"], "amount": entitlement})
                        state.dividend_income += entitlement
                        events.append({"type": "dividend_entitlement", "date": str(day), "dividend_id": dividend["id"], "amount": str(entitlement)})
                for payment in state.settle(day):
                    events.append({"type": "dividend_paid", "date": str(day), "amount": str(payment["amount"])})
                budget = D(str(curve[-1]["equity"])) * D(str(policy.max_premium_fraction))
                for plan in pending:
                    before = state.opening_premium_paid
                    execute_plan(data, state, plan, day, "open", policy, events, budget)
                    budget -= state.opening_premium_paid - before
                expiry_and_assignment(data, state, day, policy, scenario, events)
            if day == data.days[-1] and policy.liquidate_at_end:
                for symbol in state.stocks:
                    owned = {cid: q for cid, q in state.options.items() if data.contracts[cid].symbol == symbol}
                    if owned and not execute_plan(data, state, {"decision_at": data.at(day, "open").isoformat(), "symbol": symbol,
                        "from": owned, "target": {}, "reason": "predeclared_terminal_close"}, day, "close", policy, events, D(0)):
                        raise ValueError("终点预定平仓未完成，保留实际持仓")
            view = state.view(data, day, policy)
            peak = max(peak, D(str(view["equity"])))
            view["drawdown_fraction"] = float(D(str(view["equity"])) / peak - 1)
            view["greeks"] = greek_report(data, state, day, data.days[index - 1] if index else None,
                                           previous_options, view["option_pnl"] - (curve[-1]["option_pnl"] if curve else 0), policy)
            curve.append(view)
            previous_options = dict(state.options)
            if day != data.days[-1]:
                pending, daily_decisions = plan_overlay(data, state, day, strategy, policy)
                decisions.extend(daily_decisions)
    except (ValueError, ArithmeticError) as exc:
        errors.append({"date": str(day), "error": str(exc)})
    complete = len(curve) == len(data.days) and not errors
    last = curve[-1] if curve else None
    return {"strategy": strategy, "assignment_scenario": scenario, "execution_completed": complete,
            "research_conclusion": "not_assessed", "initial_equity": float(state.initial_equity),
            "final_equity": last["equity"] if complete else None,
            "return_fraction": last["equity"] / float(state.initial_equity) - 1 if complete else None,
            "max_drawdown_fraction": min((v["drawdown_fraction"] for v in curve), default=None),
            "option_trades": sum(e["type"] == "option_trade" for e in events),
            "assignments": sum(e["type"] in {"early_assignment_scenario", "expiration_assignment"} for e in events),
            "opening_premium_received": float(state.opening_premium_received), "opening_premium_paid": float(state.opening_premium_paid),
            "fees": float(state.fees), "equity_curve": curve, "events": events, "decisions": decisions, "errors": errors,
            "ending_positions": {"stocks": deepcopy(state.stocks), "options": deepcopy(state.options)},
            "accounting_basis": "历史 bid/ask 的净额多腿包；期权按可平仓侧估值；正交易现金 T+1 可用；分红除息确认、支付日入账"}
