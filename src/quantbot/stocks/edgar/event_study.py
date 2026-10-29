"""8-K 事件窗口研究：可得时间之后的交易日、固定窗口与基准超额。"""

from bisect import bisect_left
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
import json
import math
from pathlib import Path
import random
import statistics
from uuid import uuid4

from quantbot.evidence import hash_payload, source_tree_hash

from .archive import EASTERN, cik_id
from .event_data import EventPrices, available_at, exact, load_event_bundle, validate_bundle_size


@dataclass(frozen=True)
class EventPolicy:
    benchmark: str = "SPY"
    horizons: tuple = (1, 5, 20)
    availability_mode: str = "observed"
    assumed_public_delay_seconds: int = 900
    order_latency_seconds: int = 60
    cost_bps_per_side: float = 10
    include_amendments: bool = False
    allow_overlapping: bool = False
    items: tuple = ()
    seed: int = 1729

    def __post_init__(self):
        if not isinstance(self.benchmark, str) or not self.benchmark.strip():
            raise ValueError("需明确事件基准标的")
        if (not isinstance(self.horizons, (tuple, list)) or not 1 <= len(self.horizons) <= 5
                or any(type(v) is not int or not 1 <= v <= 60 for v in self.horizons)
                or list(self.horizons) != sorted(set(self.horizons))):
            raise ValueError("事件窗口须为 1..60 个交易日、升序去重，最多 5 项")
        if self.availability_mode not in {"observed", "declared", "acceptance_proxy"}:
            raise ValueError("需选择 observed / declared / acceptance_proxy 可得时间模式")
        for key, lower, upper in (("assumed_public_delay_seconds", 60, 604800), ("order_latency_seconds", 1, 86400), ("seed", 0, 2**32 - 1)):
            if type(getattr(self, key)) is not int or not lower <= getattr(self, key) <= upper:
                raise ValueError(f"{key} 超出允许范围")
        if type(self.cost_bps_per_side) not in {int, float} or not math.isfinite(self.cost_bps_per_side) or not 0 <= self.cost_bps_per_side <= 500:
            raise ValueError("单边成本须为 0..500 bps")
        if type(self.include_amendments) is not bool or type(self.allow_overlapping) is not bool:
            raise ValueError("修订和重叠策略须为布尔值")
        if not isinstance(self.items, (tuple, list)) or any(not isinstance(i, str) or len(i) != 4 or i[1] != "." or not (i[0] + i[2:]).isdigit() for i in self.items):
            raise ValueError("条目筛选须为如 2.02 的列表")


def identities(rows, symbols):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError("需提供 1..100 条带有效日期的 CIK 与股票映射")
    result = []
    for row in rows:
        exact(row, "cik symbol start end", "历史标的映射")
        item = {**row, "cik": cik_id(row["cik"]), "start": date.fromisoformat(row["start"]), "end": date.fromisoformat(row["end"])}
        if item["symbol"] not in symbols or item["start"] > item["end"]:
            raise ValueError("标的映射日期或行情缺失")
        for before in result:
            if item["cik"] == before["cik"] and max(item["start"], before["start"]) <= min(item["end"], before["end"]):
                raise ValueError("同一 CIK 的历史映射日期重叠")
        result.append(item)
    return result


def window_return(data, symbol, start, end, bps):
    entry = data.bars[data.days[start], symbol].open
    exit_price = data.bars[data.days[end], symbol].close
    dividend = sum(data.dividends.get((day, symbol), 0) for day in data.days[start + 1:end + 1])
    cost = bps / 10000
    return (exit_price * (1 - cost) + dividend) / (entry * (1 + cost)) - 1


def summarize(rows, policy):
    summary = []
    for horizon in policy.horizons:
        values = [r for event in rows for r in event["windows"] if r["sessions"] == horizon]
        clusters = {}
        for row in values:
            clusters.setdefault(row["entry_date"], []).append(row["excess_return"])
        cluster_values = [statistics.mean(clusters[key]) for key in sorted(clusters)]
        interval = None
        if len(cluster_values) >= 5:
            rng = random.Random(policy.seed + horizon)
            draws = sorted(statistics.mean(rng.choices(cluster_values, k=len(cluster_values))) for _ in range(1000))
            interval = [draws[24], draws[974]]
        summary.append({"sessions": horizon, "events": len(values), "entry_date_clusters": len(clusters),
            "mean_return": statistics.mean(r["net_return"] for r in values) if values else None,
            "mean_benchmark_return": statistics.mean(r["benchmark_return"] for r in values) if values else None,
            "mean_excess_return": statistics.mean(r["excess_return"] for r in values) if values else None,
            "median_excess_return": statistics.median(r["excess_return"] for r in values) if values else None,
            "positive_excess_fraction": sum(r["excess_return"] > 0 for r in values) / len(values) if values else None,
            "double_cost_mean_excess": statistics.mean(r["double_cost_excess"] for r in values) if values else None,
            "double_cost_mean_return": statistics.mean(r["double_cost_return"] for r in values) if values else None,
            "equal_date_cluster_mean_excess": statistics.mean(cluster_values) if cluster_values else None,
            "date_cluster_bootstrap_95": interval, "bootstrap_draws": 1000 if interval else 0})
    return summary


def event_study(*, bundle, prices, universe, policy=None, output):
    validate_bundle_size(bundle, prices, universe, policy)
    try:
        policy = EventPolicy(**({} if policy is None else policy))
    except TypeError as exc:
        raise ValueError(f"事件参数字段无效：{exc}") from None
    try:
        versions, duplicates = load_event_bundle(bundle)
        data = EventPrices(prices)
        mapping = identities(universe, data.symbols)
    except (TypeError, KeyError) as exc:
        raise ValueError(f"事件输入字段或类型无效：{exc}") from None
    if policy.benchmark not in data.symbols:
        raise ValueError("事件研究缺少基准行情")
    root = Path(output).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=False)
    code_hash = source_tree_hash()
    plan = {"schema_version": 1, "run_id": uuid4().hex, "registered_at": datetime.now(UTC).isoformat(),
            "policy": asdict(policy), "universe": universe, "code_hash": code_hash,
            "events_hash": hash_payload(bundle), "prices_hash": hash_payload(prices)}
    inputs = {"plan": plan, "plan_hash": hash_payload(plan), "event_input": bundle, "price_input": prices}
    (root / "input.json").write_text(json.dumps(inputs, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    candidates, skipped, groups = [], [], {}
    for accession, records in versions.items():
        record = records[0]
        at = available_at(record, policy.availability_mode, policy.assumed_public_delay_seconds)
        reason = None
        if record["form"] == "8-K/A" and not policy.include_amendments:
            reason = "amendment_excluded"
        elif at is None:
            reason = "public_availability_unknown"
        elif policy.items and not set(policy.items).intersection(record["items"]):
            reason = "item_filter"
        if reason:
            skipped.append({"accession": accession, "reason": reason})
            continue
        local_day = at.astimezone(EASTERN).date()
        symbol = next((r["symbol"] for r in mapping if r["cik"] == record["cik"] and r["start"] <= local_day <= r["end"]), None)
        if symbol is None:
            skipped.append({"accession": accession, "reason": "no_historical_symbol_mapping"})
            continue
        ready = at + timedelta(seconds=policy.order_latency_seconds)
        next_open = bisect_left(data.calendar_opens, ready)
        entry = bisect_left(data.opens, ready)
        # 先用完整日历定位下一开盘，再要求行情覆盖该次开盘，不能把缺失期间挪到窗口首日。
        if next_open >= len(data.calendar_opens) or entry >= len(data.days) or data.opens[entry] != data.calendar_opens[next_open]:
            skipped.append({"accession": accession, "reason": "outside_price_window", "available_at": at.isoformat()})
            continue
        candidates.append((entry, at, accession, record, symbol))
    for entry, at, accession, record, symbol in sorted(candidates, key=lambda row: row[:3]):
        key = (symbol, entry)
        if key in groups:
            groups[key]["accessions"].append(accession)
            groups[key]["items"] = sorted(set(groups[key]["items"]) | set(record["items"]))
            continue
        groups[key] = {"symbol": symbol, "cik": record["cik"], "entry_index": entry, "entry_date": str(data.days[entry]),
                       "entry_at": data.opens[entry].isoformat(), "available_at": at.isoformat(),
                       "accepted_at": record["accepted_at"], "first_seen_at": record["first_seen_at"],
                       "source_url": record["source_url"], "availability_mode": policy.availability_mode,
                       "accessions": [accession], "items": list(record["items"]), "windows": []}
    rows, last_end = [], {}
    for event in groups.values():
        symbol, entry = event["symbol"], event.pop("entry_index")
        if not policy.allow_overlapping and entry <= last_end.get(symbol, -1):
            skipped.append({"accessions": event["accessions"], "reason": "overlapping_event_window", "entry_date": event["entry_date"]})
            continue
        last_end[symbol] = entry + max(policy.horizons) - 1
        for horizon in policy.horizons:
            end = entry + horizon - 1
            if end >= len(data.days):
                skipped.append({"accessions": event["accessions"], "reason": "incomplete_forward_window", "sessions": horizon})
                continue
            benchmark = window_return(data, policy.benchmark, entry, end, policy.cost_bps_per_side)
            net = window_return(data, symbol, entry, end, policy.cost_bps_per_side)
            event["windows"].append({"sessions": horizon, "entry_date": str(data.days[entry]), "exit_date": str(data.days[end]),
                "entry_price": data.bars[data.days[entry], symbol].open, "exit_price": data.bars[data.days[end], symbol].close,
                "net_return": net, "benchmark_return": benchmark, "excess_return": net - benchmark,
                "double_cost_return": window_return(data, symbol, entry, end, 2 * policy.cost_bps_per_side),
                "double_cost_excess": window_return(data, symbol, entry, end, 2 * policy.cost_bps_per_side)
                    - window_return(data, policy.benchmark, entry, end, 2 * policy.cost_bps_per_side)})
        rows.append(event)
    synthetic = bundle["data_kind"] == "synthetic" or prices["data_kind"] == "synthetic"
    unchanged = code_hash == source_tree_hash()
    report = {**inputs, "kind": "sec_event_study", "execution_completed": unchanged, "source_unchanged": unchanged,
              "research_conclusion": "insufficient" if synthetic or not any(e["windows"] for e in rows) else "not_assessed",
              "data_quality": {"data_kind": "synthetic" if synthetic else "historical_unverified", "point_in_time_verified": False,
                  "availability_mode": policy.availability_mode, "accessions": len(versions), "duplicate_records": duplicates,
                  "revision_versions": sum(len(v) - 1 for v in versions.values()), "event_groups": len(rows),
                  "quarter_count": len(bundle["coverage"]["quarters"]), "historical_universe_verified": False},
              "events": rows, "skipped": skipped, "summary": summarize(rows, policy), "model_calls": 0, "broker_calls": 0,
              "evidence_boundary": "独立事件窗口描述统计，不是资本约束组合回测或因果证据。默认使用首次取得时间；受理时间代理须显式选择，公开延迟没有保证。修订保留版本，默认排除 8-K/A；条目按主文档正则提取，语义未核验。区间使用原始价格并加持有期分红，成本计双边；同日同标的合并，默认排除重叠窗口。区间置信范围按入场日期聚类自助抽样，不能消除跨日相关或反复试验偏差。", "report_path": str(root / "report.json")}
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
    return report
