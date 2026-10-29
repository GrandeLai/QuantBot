"""事件研究的时点约束和历史行情，保留修订身份而不倒写历史。"""

from datetime import UTC, date, datetime, timedelta
import json

from quantbot.ai.feeds import stock_calendar
from quantbot.ai.market import FIELDS
from quantbot.ai.vintages import instant
from quantbot.trading.domain import Bar

from .archive import ACCESSION, EASTERN, SEC, cik_id, parse_submission, quarters


def exact(value, keys, name):
    if not isinstance(value, dict) or set(value) != set(keys.split()):
        raise ValueError(f"{name} 字段不符合契约：{keys}")


def load_event_bundle(bundle):
    exact(bundle, "schema_version data_kind source captured_at coverage filings", "事件数据")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1 or bundle["data_kind"] not in {"synthetic", "historical_unverified"}:
        raise ValueError("事件数据版本或来源类型不支持")
    if not isinstance(bundle["source"], str) or not 1 <= len(bundle["source"]) <= 300:
        raise ValueError("事件数据需声明来源")
    captured = instant(bundle["captured_at"])
    if captured > datetime.now(UTC):
        raise ValueError("事件采集时间在未来")
    coverage = bundle["coverage"]
    exact(coverage, "start end ciks quarters complete universe_basis", "事件覆盖范围")
    if coverage["complete"] is not True:
        raise ValueError("事件采集未完整完成，请先处理 capture.json 的失败记录")
    if coverage["quarters"] != quarters(coverage["start"], coverage["end"]):
        raise ValueError("季度列表未覆盖整个日期区间")
    if not isinstance(coverage["ciks"], list) or not 1 <= len(coverage["ciks"]) <= 20:
        raise ValueError("事件覆盖需明确 1..20 个 CIK")
    ciks = {cik_id(c) for c in coverage["ciks"]}
    if coverage["universe_basis"] != "explicit_ciks_not_historical_population":
        raise ValueError("事件数据需声明显式公司池，不能假设完整历史股票池")
    if not isinstance(bundle["filings"], list) or len(bundle["filings"]) > 1000:
        raise ValueError("一次事件研究最多接受 1000 个文件版本")
    versions, duplicate_count = {}, 0
    for record in bundle["filings"]:
        exact(record, "cik company form filing_date accession source_url accepted_at accepted_raw accepted_timezone first_seen_at public_available_at availability_evidence report_date change_date post_acceptance_changed primary_document text items items_basis amends_accession content_hash raw_submission", "事件文件")
        if not isinstance(record, dict) or not isinstance(record.get("raw_submission"), str) or len(record["raw_submission"].encode()) > 8_000_000:
            raise ValueError("每个事件需保留不超过 8 MB 的原始文件")
        identity = {key: record[key] for key in ("cik", "company", "form", "filing_date", "accession", "source_url")}
        if (any(not isinstance(v, str) for v in identity.values()) or identity["cik"] not in ciks or ACCESSION.fullmatch(identity["accession"]) is None
                or identity["source_url"] != f"{SEC}edgar/data/{int(identity['cik'])}/{identity['accession']}.txt"):
            raise ValueError("事件身份或 SEC 来源地址不一致")
        if not coverage["start"] <= identity["filing_date"] <= coverage["end"]:
            raise ValueError("事件不在声明的索引区间内")
        parsed = parse_submission(record["raw_submission"], identity, record["first_seen_at"])
        if set(record) != set(parsed):
            raise ValueError("事件字段与归档契约不一致")
        declared_fields = {"public_available_at", "availability_evidence", "amends_accession"}
        if any(record[k] != parsed[k] for k in parsed if k not in declared_fields):
            raise ValueError("事件解析字段或正文哈希与原始文件不一致")
        if instant(record["first_seen_at"]) > captured:
            raise ValueError("首次取得时间晚于数据包采集时间")
        public = record["public_available_at"]
        if public is not None:
            earliest = instant(record["accepted_at"])
            if record["change_date"]:
                earliest = max(earliest, datetime.combine(date.fromisoformat(record["change_date"]), datetime.min.time(), EASTERN).astimezone(UTC))
            if not earliest <= instant(public) <= instant(record["first_seen_at"]):
                raise ValueError("声明的公开时间必须介于受理与首次取得之间")
            if not isinstance(record["availability_evidence"], str) or not 1 <= len(record["availability_evidence"].strip()) <= 1000:
                raise ValueError("声明公开时间需附证据来源")
        elif record["availability_evidence"] is not None:
            raise ValueError("公开时间为空时不能附一个未应用的证据声明")
        if record["amends_accession"] is not None and (not isinstance(record["amends_accession"], str) or ACCESSION.fullmatch(record["amends_accession"]) is None):
            raise ValueError("修订关联 accession 无效")
        key = (record["accession"], record["content_hash"])
        if key in versions:
            previous = versions[key]
            if any(previous[k] != record[k] for k in record if k != "first_seen_at"):
                raise ValueError("同一文件版本存在相互冲突的声明")
            duplicate_count += 1
            if instant(previous["first_seen_at"]) <= instant(record["first_seen_at"]):
                continue
        versions[key] = record
    by_accession = {}
    for record in versions.values():
        by_accession.setdefault(record["accession"], []).append(record)
    for records in by_accession.values():
        records.sort(key=lambda r: (instant(r["first_seen_at"]), r["content_hash"]))
        if len({r["first_seen_at"] for r in records}) != len(records):
            raise ValueError("不同正文版本的首次取得时间相同，无法确定版本顺序")
        if len({(r["cik"], r["accepted_at"], r["form"]) for r in records}) != 1:
            raise ValueError("同一 accession 的身份或受理时间被修订，需人工核验")
        declared = [instant(r["public_available_at"]) for r in records if r["public_available_at"]]
        if any(later <= earlier for earlier, later in zip(declared, declared[1:])):
            raise ValueError("同一申报的公开修订时间必须递增")
        for record in records:
            amended = record["amends_accession"]
            if amended is not None:
                original = by_accession.get(amended, [None])[0]
                if (record["form"] != "8-K/A" or original is None or original["cik"] != record["cik"]
                        or instant(original["accepted_at"]) >= instant(record["accepted_at"])):
                    raise ValueError("修订关联需指向同一公司已受理的原文件")
    return by_accession, duplicate_count


def available_at(record, mode, delay_seconds, *, first_version=True):
    if mode == "observed":
        return instant(record["first_seen_at"])
    if mode == "declared":
        return instant(record["public_available_at"]) if record["public_available_at"] else None
    if mode != "acceptance_proxy":
        raise ValueError("未知事件可得时间模式")
    if not first_version or record["post_acceptance_changed"]:
        return instant(record["first_seen_at"])
    # 受理后的公开延迟只是声明假设；跨到下一 filing date 的记录不倒灌到前一日。
    filed_midnight = datetime.combine(date.fromisoformat(record["filing_date"]), datetime.min.time(), EASTERN)
    return max(instant(record["accepted_at"]) + timedelta(seconds=delay_seconds), filed_midnight.astimezone(UTC))


def visible_events(versions, as_of, *, mode="observed", delay_seconds=900):
    cutoff = instant(as_of) if isinstance(as_of, str) else as_of
    visible = []
    for records in versions.values():
        selected = None
        for index, record in enumerate(records):
            at = available_at(record, mode, delay_seconds, first_version=index == 0)
            if at is not None and at <= cutoff:
                selected = {**record, "effective_available_at": at.isoformat(), "availability_mode": mode}
        if selected is not None:
            visible.append(selected)
    return sorted(visible, key=lambda r: (r["effective_available_at"], r["accession"]))


class EventPrices:
    def __init__(self, payload):
        exact(payload, "schema_version data_kind source captured_at price_basis bars dividends actions", "事件行情")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or payload["data_kind"] not in {"synthetic", "historical_unverified"} or payload["price_basis"] != "raw":
            raise ValueError("事件研究只接受声明来源的原始价格")
        if not isinstance(payload["source"], str) or not 1 <= len(payload["source"]) <= 300:
            raise ValueError("事件行情需声明来源")
        captured = instant(payload["captured_at"])
        if captured > datetime.now(UTC):
            raise ValueError("行情采集时间在未来")
        if payload["actions"] != []:
            raise ValueError("公司行动会计未支持，事件区间不能包含拆股、并购或退市调整")
        if not isinstance(payload["bars"], list) or not 3 <= len(payload["bars"]) <= 30000:
            raise ValueError("需要 3..30000 行历史行情")
        for row in payload["bars"]:
            exact(row, " ".join(FIELDS) + " available_at", "事件股价")
        first, last = min(r["date"] for r in payload["bars"]), max(r["date"] for r in payload["bars"])
        self.calendar = stock_calendar(first, last)
        self.days = [x.date() for x in self.calendar.sessions_in_range(first, last)]
        if not 3 <= len(self.days) <= 1500:
            raise ValueError("事件行情窗口需为 3..1500 个交易日")
        self.opens = [self.calendar.session_open(str(day)).to_pydatetime() for day in self.days]
        self.calendar_opens = [at.to_pydatetime() for at in self.calendar.schedule["open"]]
        self.bars = {}
        for row in payload["bars"]:
            day = date.fromisoformat(row["date"])
            symbol = row["symbol"]
            if not isinstance(symbol, str) or not symbol.strip() or day not in self.days or (day, symbol) in self.bars:
                raise ValueError("事件行情标的、交易日或重复行无效")
            close = self.calendar.session_close(str(day)).to_pydatetime()
            if not close <= instant(row["available_at"]) <= captured:
                raise ValueError("事件行情可得时间无效")
            self.bars[day, symbol] = Bar(symbol, close, **{k: row[k] for k in FIELDS[2:]})
        self.symbols = {s for _, s in self.bars}
        if len(self.symbols) > 21 or set(self.bars) != {(d, s) for d in self.days for s in self.symbols}:
            raise ValueError("事件行情须覆盖相同完整交易日，最多 20 个标的和 1 个基准")
        if not isinstance(payload["dividends"], list) or len(payload["dividends"]) > 10000:
            raise ValueError("分红数组无效")
        self.dividends = {}
        for row in payload["dividends"]:
            exact(row, "symbol ex_date amount", "事件分红")
            key = (date.fromisoformat(row["ex_date"]), row["symbol"])
            value = row["amount"]
            if key not in self.bars or key in self.dividends or type(value) not in {int, float} or not 0 <= value <= 1000000:
                raise ValueError("事件分红必须唯一且数额有效")
            self.dividends[key] = value


def validate_bundle_size(*values):
    try:
        encoded = json.dumps(values, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"事件研究输入不可序列化：{exc}") from None
    if len(encoded.encode()) > 64_000_000:
        raise ValueError("事件研究输入总计超过 64 MB")
