"""持仓期权回测的历史输入：原始股价、合约规格、带可得时点的报价及分红。"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from quantbot.ai.feeds import stock_calendar
from quantbot.ai.market import FIELDS
from quantbot.ai.vintages import instant
from quantbot.trading.domain import Bar


def amount(value, name, *, maximum="1000000000000"):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"{name} 必须为非负有限数值")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"{name} 必须为非负有限数值") from None
    if not result.is_finite() or not 0 <= result <= Decimal(maximum):
        raise ValueError(f"{name} 超出有效范围")
    return result


def integer(value, name, minimum=0, maximum=10000000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须为 {minimum}..{maximum} 的整数")
    return value


def fields(value, expected, name):
    if not isinstance(value, dict) or set(value) != set(expected.split()):
        raise ValueError(f"{name} 字段必须为 {expected}")


@dataclass(frozen=True)
class Contract:
    id: str
    symbol: str
    kind: str
    strike: Decimal
    expiry: date
    last_trade_date: date
    multiplier: int
    available_at: datetime


@dataclass(frozen=True)
class Quote:
    contract_id: str
    day: date
    phase: str
    bid: Decimal
    ask: Decimal
    bid_size: int
    ask_size: int
    open_interest: int
    iv: float | None
    quote_at: datetime
    available_at: datetime


class OverlayData:
    def __init__(self, payload):
        fields(payload, "schema_version source data_kind captured_at price_basis bars contracts quotes dividends actions", "期权数据包")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("期权数据包 schema_version 必须为 1")
        if payload["price_basis"] != "raw" or payload["data_kind"] not in {"synthetic", "historical_unverified"}:
            raise ValueError("只接受 raw 价格及 synthetic/historical_unverified 数据声明")
        if not isinstance(payload["source"], str) or not 1 <= len(payload["source"].strip()) <= 200:
            raise ValueError("必须声明期权历史数据来源")
        captured = instant(payload["captured_at"])
        if captured > datetime.now(UTC):
            raise ValueError("采集时间不能在未来")
        for key, maximum in (("bars", 20000), ("contracts", 20000), ("quotes", 100000), ("dividends", 10000), ("actions", 10000)):
            if not isinstance(payload[key], list) or len(payload[key]) > maximum:
                raise ValueError(f"{key} 必须为不超过 {maximum} 项的列表")
        if not payload["bars"]:
            raise ValueError("需要完整历史股价")
        if payload["actions"]:
            raise ValueError("拆股、并购、退市及调整后合约尚需专用会计；当前数据包 actions 必须为空")
        for row in payload["bars"]:
            fields(row, " ".join(FIELDS) + " available_at", "股价")
        start, end = min(r["date"] for r in payload["bars"]), max(r["date"] for r in payload["bars"])
        self.calendar = stock_calendar(start, end)
        self.days = [s.date() for s in self.calendar.sessions_in_range(start, end)]
        if not 3 <= len(self.days) <= 1000:
            raise ValueError("期权回测需要 3..1000 个交易日")
        self.bars = {}
        self.bar_availability = {}
        for row in payload["bars"]:
            day = date.fromisoformat(row["date"])
            if day not in self.days or not isinstance(row["symbol"], str) or not row["symbol"].strip():
                raise ValueError("股价日期或标的无效")
            key = (day, row["symbol"])
            if key in self.bars:
                raise ValueError("同一标的同日股价重复")
            available = instant(row["available_at"])
            if not self.at(day, "close") <= available <= captured:
                raise ValueError("完整股价的可得时点须介于收盘和采集时间之间")
            self.bars[key] = Bar(row["symbol"], self.at(day, "close"), **{k: row[k] for k in FIELDS[2:]})
            self.bar_availability[key] = available
        self.symbols = sorted({s for _, s in self.bars})
        if len(self.symbols) > 20 or set(self.bars) != {(d, s) for d in self.days for s in self.symbols}:
            raise ValueError("每个标的都必须覆盖相同完整交易日，最多 20 个标的")
        self.contracts = {}
        for row in payload["contracts"]:
            fields(row, "id symbol kind strike expiry last_trade_date multiplier deliverable_shares exercise_style settlement currency available_at", "合约")
            if (not isinstance(row["id"], str) or not 1 <= len(row["id"]) <= 120 or row["id"] in self.contracts
                    or row["symbol"] not in self.symbols or row["kind"] not in {"call", "put"}):
                raise ValueError("合约身份必须唯一且标的/类型有效")
            if (integer(row["multiplier"], "multiplier", 1) != 100
                    or integer(row["deliverable_shares"], "deliverable_shares", 1) != 100
                    or row["exercise_style"] != "american" or row["settlement"] != "physical" or row["currency"] != "USD"):
                raise ValueError("当前支持每份交付 100 股的 USD 美式实物期权；调整后合约不能按标准合约回测")
            strike = amount(row["strike"], "strike")
            if strike <= 0:
                raise ValueError("strike 必须为正")
            expiry, last = date.fromisoformat(row["expiry"]), date.fromisoformat(row["last_trade_date"])
            if not last <= expiry <= last + timedelta(days=3):
                raise ValueError("合约到期日和最后交易日不一致")
            # 允许窗口结束后到期；独立日历仍验证最后交易日。
            if not stock_calendar(last, expiry).is_session(str(last)):
                raise ValueError("last_trade_date 必须为交易日")
            available = instant(row["available_at"])
            if available > captured:
                raise ValueError("合约信息尚未可得")
            self.contracts[row["id"]] = Contract(row["id"], row["symbol"], row["kind"], strike, expiry, last, row["multiplier"], available)
        self.quotes = {}
        seen = set()
        for row in payload["quotes"]:
            fields(row, "contract_id date phase bid ask bid_size ask_size open_interest iv quote_at available_at", "期权报价")
            cid, day, phase = row["contract_id"], date.fromisoformat(row["date"]), row["phase"]
            if cid not in self.contracts or day not in self.days or phase not in {"open", "close"}:
                raise ValueError("报价引用未知合约、日期或阶段")
            contract = self.contracts[cid]
            observed, available = instant(row["quote_at"]), instant(row["available_at"])
            opening, closing = self.at(day, "open"), self.at(day, "close")
            if not opening <= observed <= closing or not max(observed, contract.available_at) <= available <= captured or day > contract.last_trade_date:
                raise ValueError("报价时间、发布时间或合约生命周期无效")
            if (cid, day, phase, available) in seen:
                raise ValueError("同一报价存在重复的可得时点")
            seen.add((cid, day, phase, available))
            bid, ask = amount(row["bid"], "bid"), amount(row["ask"], "ask")
            if bid > ask:
                raise ValueError("报价 bid 不得高于 ask")
            iv = None if row["iv"] is None else float(amount(row["iv"], "iv", maximum="10"))
            if iv == 0:
                raise ValueError("缺失 IV 请使用 null，不能使用 0")
            quote = Quote(cid, day, phase, bid, ask, integer(row["bid_size"], "bid_size"),
                          integer(row["ask_size"], "ask_size"), integer(row["open_interest"], "open_interest"),
                          iv, observed, available)
            self.quotes.setdefault((day, phase), {}).setdefault(cid, []).append(quote)
        self.dividends = []
        seen_dividends = set()
        for row in payload["dividends"]:
            fields(row, "id symbol ex_date pay_date amount available_at", "分红")
            if not isinstance(row["id"], str) or not row["id"] or row["id"] in seen_dividends or row["symbol"] not in self.symbols:
                raise ValueError("分红身份重复或标的无效")
            ex_date, pay_date = date.fromisoformat(row["ex_date"]), date.fromisoformat(row["pay_date"])
            if ex_date not in self.days or pay_date < ex_date:
                raise ValueError("分红除息日必须在数据窗口内，支付日不得早于除息日")
            available = instant(row["available_at"])
            if available > min(captured, self.at(ex_date, "open")):
                raise ValueError("分红声明在除息开盘前不可得")
            self.dividends.append({**row, "ex_date": ex_date, "pay_date": pay_date,
                                   "amount": amount(row["amount"], "dividend"), "available_at": available})
            seen_dividends.add(row["id"])

    def at(self, day, phase):
        method = self.calendar.session_open if phase == "open" else self.calendar.session_close
        return method(str(day)).to_pydatetime()

    def settlement_day(self, day):
        return self.calendar.next_session(str(day)).date()

    def quote(self, cid, day, phase, policy):
        clock = self.at(day, phase)
        cutoff = clock + timedelta(seconds=policy.opening_window_seconds if phase == "open" else policy.decision_delay_seconds)
        values = [q for q in self.quotes.get((day, phase), {}).get(cid, ()) if q.available_at <= cutoff
                  and (clock <= q.quote_at <= cutoff if phase == "open" else q.quote_at <= clock)
                  and abs((q.quote_at - clock).total_seconds()) <= policy.max_quote_age_seconds]
        return max(values, key=lambda q: (q.quote_at, q.available_at)) if values else None
