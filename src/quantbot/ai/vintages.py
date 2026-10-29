"""带发布时间的原始 OHLCV 版本输入。时间约束可验证，供应方声明不等于第三方认证。"""

from datetime import UTC, date, datetime

from quantbot.crypto.ai import INTERVALS
from quantbot.trading.domain import Bar

from .market import FIELDS


def instant(value):
    if not isinstance(value, str):
        raise ValueError("发布时间必须为带时区的 ISO 时间")
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("发布时间必须带时区")
    return at.astimezone(UTC)


def load_vintages(payload, spec, config, now, *, fresh=False):
    from .feeds import stock_calendar, validate_rows

    fields = {"schema_version", "market", "provider", "captured_at", "data_kind", "price_basis", "rows", "actions"}
    if not isinstance(payload, dict) or set(payload) != fields or type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("历史版本文件须包含 schema_version=1 及约定的完整字段")
    if payload["market"] != spec["market"] or payload["price_basis"] != "raw":
        raise ValueError("历史版本市场必须与任务一致，且只接受原始未复权价格 raw")
    if not isinstance(payload["provider"], str) or not 1 <= len(payload["provider"].strip()) <= 200:
        raise ValueError("历史版本必须声明数据供应方 provider")
    if not isinstance(payload["data_kind"], str) or payload["data_kind"] not in {"synthetic", "historical_unverified"}:
        raise ValueError("历史版本 data_kind 必须为 synthetic 或 historical_unverified")
    captured = instant(payload["captured_at"])
    if captured > now:
        raise ValueError("采集时间不能在未来")
    records = payload["rows"]
    if not isinstance(records, list) or not 1 <= len(records) <= 100_000:
        raise ValueError("历史版本须包含 1..100000 条记录")
    row_fields = set(FIELDS if spec["market"] == "stocks" else ("timestamp", *FIELDS[1:], "bar"))
    if any(not isinstance(r, dict) or set(r) != row_fields | {"available_at"} for r in records):
        raise ValueError("每条历史版本必须包含标准 OHLCV 列和 available_at")
    calendar = None
    if spec["market"] == "stocks":
        if any(not isinstance(r["date"], str) for r in records):
            raise ValueError("历史版本 date 必须为日期字符串")
        dates = [date.fromisoformat(r["date"]).isoformat() for r in records]
        calendar = stock_calendar(min(dates), max(dates))
    grouped = {}
    for record in records:
        row = {key: record[key] for key in row_fields}
        if row["symbol"] not in spec["symbols"]:
            raise ValueError("历史版本包含任务以外的标的")
        if spec["market"] == "stocks":
            key = row["date"]
            if not calendar.is_session(key):
                raise ValueError("历史版本包含非交易日")
            close = calendar.session_close(key).to_pydatetime()
        else:
            if row["bar"] != spec["bar"]:
                raise ValueError("历史版本周期与任务不一致")
            start = instant(row["timestamp"])
            row["timestamp"] = key = start.isoformat()
            close = start + INTERVALS[spec["bar"]]
        available = instant(record["available_at"])
        if not close <= available <= captured:
            raise ValueError("行情发布时间必须介于该根收盘和采集时间之间")
        bar = Bar(row["symbol"], close, **{k: row[k] for k in FIELDS[2:]})
        row.update({k: getattr(bar, k) for k in FIELDS[2:]})
        row["available_at"] = available.isoformat()
        grouped.setdefault((key, row["symbol"]), []).append((available, close, row))
    rows, vintages = [], []
    for versions in grouped.values():
        versions.sort(key=lambda value: value[0])
        if len({v[0] for v in versions}) != len(versions):
            raise ValueError("同一根行情的多个版本不能具有相同发布时间")
        if versions[0][0] != versions[0][1]:
            raise ValueError("当前收盘决策要求首版行情在收盘时可得；迟发首版需另行定义延迟执行模型")
        rows.append({k: v for k, v in versions[0][2].items() if k != "available_at"})
        vintages.extend(v[2] for v in versions)
    actions = payload["actions"]
    if not isinstance(actions, list) or len(actions) > 100_000:
        raise ValueError("公司行动 actions 必须为列表")
    if spec["market"] == "crypto" and actions:
        raise ValueError("加密历史版本暂不支持资产行动")
    from .corporate_actions import normalize_actions

    actions = normalize_actions(actions, spec["symbols"], captured)
    rows.sort(key=lambda r: (r.get("date", r.get("timestamp")), r["symbol"]))
    vintages.sort(key=lambda r: (r["available_at"], r.get("date", r.get("timestamp")), r["symbol"]))
    snapshot = validate_rows(rows, spec, config, now, fresh=fresh, actions=actions)
    snapshot.vintages = vintages
    snapshot.provenance = {k: v for k, v in payload.items() if k not in {"rows", "actions"}}
    return snapshot
