"""将冻结 SEC 归档裁成决策时点可见的证据；公告正文永远只是模型输入数据。"""

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, timedelta

from quantbot.stocks.edgar.archive import EASTERN, cik_id
from quantbot.stocks.edgar.event_data import exact, load_event_bundle, validate_bundle_size, visible_events
from quantbot.stocks.edgar.event_study import identities


@dataclass(frozen=True)
class SECContextPolicy:
    availability_mode: str = "observed"
    assumed_public_delay_seconds: int = 900
    processing_delay_seconds: int = 60
    lookback_days: int = 7
    max_events_per_symbol: int = 3
    max_text_chars: int = 2000
    include_amendments: bool = True

    def __post_init__(self):
        if self.availability_mode not in {"observed", "declared", "acceptance_proxy"}:
            raise ValueError("公告输入需选择 observed / declared / acceptance_proxy 可得时间模式")
        for key, lower, upper in (("assumed_public_delay_seconds", 60, 604800),
                                 ("processing_delay_seconds", 1, 86400), ("lookback_days", 1, 90),
                                 ("max_events_per_symbol", 1, 10), ("max_text_chars", 200, 8000)):
            value = getattr(self, key)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"公告输入 {key} 必须为 {lower}..{upper} 的整数")
        if type(self.include_amendments) is not bool:
            raise ValueError("公告输入 include_amendments 必须为布尔值")


class SECEventContext:
    """校验一次原文，逐时点选择版本；完整原文随行情冻结，不送入模型全文。"""

    def __init__(self, payload, symbols):
        validate_bundle_size(payload)
        exact(payload, "schema_version bundle universe policy", "AI 公告输入")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("AI 公告输入需要 schema_version=1")
        self.input = deepcopy(payload)
        try:
            self.policy = SECContextPolicy(**self.input["policy"])
            self.versions, self.duplicates = load_event_bundle(self.input["bundle"])
            self.universe = identities(self.input["universe"], set(symbols))
        except (TypeError, KeyError) as exc:
            raise ValueError(f"AI 公告输入字段无效：{exc}") from None
        ciks = {cik_id(value) for value in self.input["bundle"]["coverage"]["ciks"]}
        if any(row["cik"] not in ciks for row in self.universe):
            raise ValueError("公告历史映射包含未采集的 CIK")
        for index, row in enumerate(self.universe):
            for other in self.universe[:index]:
                if row["symbol"] == other["symbol"] and max(row["start"], other["start"]) <= min(row["end"], other["end"]):
                    raise ValueError("同一股票代码不能在重叠日期映射到多个公司")

    def at(self, as_of, symbols):
        policy = self.policy
        cutoff = as_of.astimezone(UTC) - timedelta(seconds=policy.processing_delay_seconds)
        lower = cutoff - timedelta(days=policy.lookback_days)
        day = cutoff.astimezone(EASTERN).date()
        available = visible_events(self.versions, cutoff, mode=policy.availability_mode,
                                   delay_seconds=policy.assumed_public_delay_seconds)
        coverage = self.input["bundle"]["coverage"]
        chosen, states = {}, {}
        for symbol in symbols:
            mapping = next((row for row in self.universe if row["symbol"] == symbol and row["start"] <= day <= row["end"]), None)
            # 未映射或未覆盖不等于公司没有公告；只汇报导入归档中当时可见的记录。
            rows = [row for row in available if mapping and row["cik"] == mapping["cik"]
                    and lower.isoformat() <= row["effective_available_at"]
                    and (policy.include_amendments or row["form"] != "8-K/A")]
            rows.sort(key=lambda row: (row["effective_available_at"], row["accession"]), reverse=True)
            chosen[symbol] = rows[:policy.max_events_per_symbol]
            states[symbol] = {
                "mapping_available": mapping is not None,
                "index_window_covered": bool(mapping and mapping["start"] <= lower.astimezone(EASTERN).date()
                    and coverage["start"] <= lower.astimezone(EASTERN).date().isoformat()
                    and day.isoformat() <= coverage["end"]),
                "visible_events_in_window": len(rows),
                "omitted_events": max(0, len(rows) - policy.max_events_per_symbol),
            }
        count = sum(len(rows) for rows in chosen.values())
        # 每次决策所有标的合计最多 24000 字符；均分预算，避免股票池顺序饿死后面的标的。
        text_limit = min(policy.max_text_chars, 24000 // count) if count else policy.max_text_chars
        for symbol, rows in chosen.items():
            states[symbol]["events"] = [{
                **{key: row[key] for key in ("accession", "cik", "form", "filing_date", "accepted_at",
                                            "content_hash", "source_url", "items", "items_basis", "amends_accession")},
                "available_at": row["effective_available_at"],
                "text_excerpt": row["text"][:text_limit],
                "text_truncated": len(row["text"]) > text_limit,
            } for row in rows]
        return {
            "as_of": as_of.isoformat(), "information_cutoff": cutoff.isoformat(),
            "data_kind": self.input["bundle"]["data_kind"], "policy": asdict(policy),
            "point_in_time_verified": False, "text_selection": "document_prefix_not_relevance_summary",
            "absence_basis": "only_visible_records_in_imported_archive_not_proof_of_no_filing",
            "symbols": states,
        }

    def quality(self):
        return {
            "enabled": True, "data_kind": self.input["bundle"]["data_kind"],
            "source": self.input["bundle"]["source"], "availability_enforced": True,
            "point_in_time_verified": False, "policy": asdict(self.policy),
            "accessions": len(self.versions), "duplicate_records": self.duplicates,
            "revision_versions": sum(len(rows) - 1 for rows in self.versions.values()),
            "coverage": self.input["bundle"]["coverage"],
        }


def attach_sec_context(snapshot, payload):
    if snapshot.market != "stocks":
        raise ValueError("SEC 公告输入当前仅支持美股回测与研究")
    snapshot.sec_events = SECEventContext(payload, snapshot.config["market"]["symbols"])
    return snapshot
