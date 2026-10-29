"""Paper 交易日证据的纯审计契约与判定逻辑。

本模块只消费 :meth:`ReviewJournal.records_for_date` 返回的普通字典，不访问
券商、网络、时钟或生产配置。交易日日历必须在 ``PaperCampaignSpec`` 中预先
冻结，避免审计时因外部日历漂移而改变结论。

Schema v1 账本没有 attempt/campaign/执行日/源码/账户关联。这里不会猜测这些
字段：缺失时统一返回 ``LEGACY_EVIDENCE_INCOMPLETE``，旧事实仍可用于人工
复盘，但不能计入 Paper 发布门禁。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, ClassVar, TypeVar

from ..data.session import MarketSession, instant


LEGACY_EVIDENCE_INCOMPLETE = "LEGACY_EVIDENCE_INCOMPLETE"
MISSING_TRADING_DAY = "MISSING_TRADING_DAY"
AMBIGUOUS_RUN = "AMBIGUOUS_RUN"
RUN_FAILED = "RUN_FAILED"
RUN_SKIPPED = "RUN_SKIPPED"
CONFIG_HASH_MISMATCH = "CONFIG_HASH_MISMATCH"
SOURCE_HASH_MISMATCH = "SOURCE_HASH_MISMATCH"
CAMPAIGN_HASH_MISMATCH = "CAMPAIGN_HASH_MISMATCH"
ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
STALE_DATA = "STALE_DATA"
SIGNAL_DATE_INVALID = "SIGNAL_DATE_INVALID"
MARKET_SESSION_INVALID = "MARKET_SESSION_INVALID"
CLOSE_TIME_INVALID = "CLOSE_TIME_INVALID"
UNRESOLVED_ORDER = "UNRESOLVED_ORDER"
ORDER_CHAIN_INCOMPLETE = "ORDER_CHAIN_INCOMPLETE"
ORDER_CHAIN_INCONSISTENT = "ORDER_CHAIN_INCONSISTENT"
UNKNOWN_ORDER_STATUS = "UNKNOWN_ORDER_STATUS"
ORPHAN_FILL = "ORPHAN_FILL"
FILL_MISSING = "FILL_MISSING"
FILL_MISMATCH = "FILL_MISMATCH"
SLIPPAGE_MISSING = "SLIPPAGE_MISSING"
EQUITY_MISSING = "EQUITY_MISSING"
EQUITY_AMBIGUOUS = "EQUITY_AMBIGUOUS"
EQUITY_INVALID = "EQUITY_INVALID"
RECONCILIATION_MISSING = "RECONCILIATION_MISSING"
RECONCILIATION_AMBIGUOUS = "RECONCILIATION_AMBIGUOUS"
RECONCILIATION_UNKNOWN = "RECONCILIATION_UNKNOWN"
RECONCILIATION_DRIFT = "RECONCILIATION_DRIFT"

_REQUIRED_RUN_METADATA = (
    "attempt_id",
    "campaign_id",
    "campaign_sha256",
    "execution_date",
    "source_hash",
    "account_fingerprint",
)
_KNOWN_ORDER_STATUSES = {
    "planned",
    "submitted",
    "filled",
    "partial",
    "canceled",
    "rejected",
    "expired",
    "uncertain",
    "submit_error",
    "dead",
    "state_committed",
}
_FILL_ORDER_TERMINALS = {"filled", "partial"}
_NO_FILL_ORDER_TERMINALS = {"canceled", "rejected", "expired"}
_VALID_ORDER_TERMINALS = _FILL_ORDER_TERMINALS | _NO_FILL_ORDER_TERMINALS
_REQUIRED_RECONCILIATION_CATEGORIES = (
    "positions",
    "working_orders",
    "account",
)


def canonical_sha256(value: Any) -> str:
    """返回 JSON 规范化表示的稳定 SHA-256。"""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


ContractT = TypeVar("ContractT", bound="_JsonContract")


class _JsonContract:
    schema_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls: type[ContractT], value: Mapping[str, Any]) -> ContractT:
        raise NotImplementedError

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_json(cls: type[ContractT], payload: str) -> ContractT:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError(f"{cls.__name__} JSON 顶层必须是对象")
        return cls.from_dict(value)

    @property
    def sha256(self) -> str:
        """契约完整内容的规范化 SHA-256。"""
        return canonical_sha256(self.to_dict())


def _require_schema(value: Mapping[str, Any]) -> None:
    if value.get("schema_version", 1) != 1:
        raise ValueError(f"不支持的 schema_version: {value.get('schema_version')!r}")


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _require_sha256(value: Any, name: str) -> str:
    digest = _require_text(value, name).lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError(f"{name} 必须是 64 位 SHA-256 十六进制字符串")
    return digest


def _iso_date(value: Any, name: str) -> str:
    text = _require_text(value, name)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 ISO 日期 YYYY-MM-DD") from exc


def _dates(values: Sequence[Any], name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} 必须是日期序列")
    normalized = tuple(_iso_date(item, name) for item in values)
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if normalized != tuple(sorted(set(normalized))):
        raise ValueError(f"{name} 必须严格递增且不能重复")
    return normalized


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _nonnegative_int(value, name)
    if result == 0:
        raise ValueError(f"{name} 必须是正整数")
    return result


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _reason_codes(values: Sequence[Any]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("reason_codes 必须是字符串序列")
    normalized = tuple(sorted({_require_text(value, "reason_code") for value in values}))
    return normalized


@dataclass(frozen=True)
class PaperCampaignSpec(_JsonContract):
    """预先冻结的 Paper 观察范围与身份。"""

    campaign_id: str
    candidate_config_hash: str
    source_hash: str
    account_fingerprint: str
    expected_trading_dates: tuple[str, ...]
    expected_signal_dates: tuple[str, ...]
    minimum_trading_days: int = 60
    mode: str = "paper"
    required_reconciliation_categories: tuple[str, ...] = (
        _REQUIRED_RECONCILIATION_CATEGORIES
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "campaign_id", _require_text(self.campaign_id, "campaign_id"))
        object.__setattr__(
            self,
            "candidate_config_hash",
            _require_sha256(self.candidate_config_hash, "candidate_config_hash"),
        )
        object.__setattr__(self, "source_hash", _require_sha256(self.source_hash, "source_hash"))
        object.__setattr__(
            self,
            "account_fingerprint",
            _require_sha256(self.account_fingerprint, "account_fingerprint"),
        )
        object.__setattr__(
            self,
            "expected_trading_dates",
            _dates(self.expected_trading_dates, "expected_trading_dates"),
        )
        signal_dates = _dates(self.expected_signal_dates, "expected_signal_dates")
        if len(signal_dates) != len(self.expected_trading_dates):
            raise ValueError("expected_signal_dates 必须与 expected_trading_dates 等长")
        for index, (signal_day, execution_day) in enumerate(
            zip(signal_dates, self.expected_trading_dates, strict=True)
        ):
            if date.fromisoformat(signal_day) >= date.fromisoformat(execution_day):
                raise ValueError("每个 expected_signal_date 必须早于对应执行日")
            if index > 0 and signal_day != self.expected_trading_dates[index - 1]:
                raise ValueError(
                    "campaign 必须覆盖连续交易会话：后续信号日必须等于前一执行日"
                )
        object.__setattr__(self, "expected_signal_dates", signal_dates)
        object.__setattr__(
            self,
            "minimum_trading_days",
            _positive_int(self.minimum_trading_days, "minimum_trading_days"),
        )
        if self.mode != "paper":
            raise ValueError("PaperCampaignSpec.mode 必须为 paper")
        categories = tuple(
            _require_text(item, "required_reconciliation_category").lower()
            for item in self.required_reconciliation_categories
        )
        if not categories or len(set(categories)) != len(categories):
            raise ValueError("required_reconciliation_categories 不能为空或重复")
        missing_categories = set(_REQUIRED_RECONCILIATION_CATEGORIES) - set(categories)
        if missing_categories:
            raise ValueError(
                "required_reconciliation_categories 不得移除强制类别："
                + ", ".join(sorted(missing_categories))
            )
        object.__setattr__(self, "required_reconciliation_categories", categories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "candidate_config_hash": self.candidate_config_hash,
            "source_hash": self.source_hash,
            "account_fingerprint": self.account_fingerprint,
            "expected_trading_dates": list(self.expected_trading_dates),
            "expected_signal_dates": list(self.expected_signal_dates),
            "minimum_trading_days": self.minimum_trading_days,
            "mode": self.mode,
            "required_reconciliation_categories": list(
                self.required_reconciliation_categories
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PaperCampaignSpec":
        _require_schema(value)
        return cls(
            campaign_id=str(value["campaign_id"]),
            candidate_config_hash=str(value["candidate_config_hash"]),
            source_hash=str(value["source_hash"]),
            account_fingerprint=str(value["account_fingerprint"]),
            expected_trading_dates=tuple(value["expected_trading_dates"]),
            expected_signal_dates=tuple(value["expected_signal_dates"]),
            minimum_trading_days=int(value.get("minimum_trading_days", 60)),
            mode=str(value.get("mode", "paper")),
            required_reconciliation_categories=tuple(
                value.get(
                    "required_reconciliation_categories",
                    _REQUIRED_RECONCILIATION_CATEGORIES,
                )
            ),
        )


@dataclass(frozen=True)
class OrderLifecycleAssessment:
    """同一 client_order_id 的闭环判定。"""

    client_order_id: str
    eligible: bool
    terminal_status: str | None
    reason_codes: tuple[str, ...]
    expected_quantity: int
    filled_quantity: int
    fill_count: int
    slippage_sample_count: int


@dataclass(frozen=True)
class PaperDayAssessment(_JsonContract):
    """一个冻结交易日的 Paper 证据结论。"""

    execution_date: str
    eligible: bool
    reason_codes: tuple[str, ...]
    facts_sha256: str
    attempt_id: str | None = None
    order_count: int = 0
    fill_count: int = 0
    slippage_sample_count: int = 0
    close_equity: float | None = None
    reconciliation_categories: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_date",
            _iso_date(self.execution_date, "execution_date"),
        )
        if not isinstance(self.eligible, bool):
            raise ValueError("eligible 必须是布尔值")
        reasons = _reason_codes(self.reason_codes)
        if self.eligible and reasons:
            raise ValueError("合格交易日不能包含失败原因")
        if not self.eligible and not reasons:
            raise ValueError("不合格交易日必须包含失败原因")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(
            self,
            "facts_sha256",
            _require_sha256(self.facts_sha256, "facts_sha256"),
        )
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _require_text(self.attempt_id, "attempt_id"))
        elif self.eligible:
            raise ValueError("合格交易日必须关联 attempt_id")
        for name in ("order_count", "fill_count", "slippage_sample_count"):
            object.__setattr__(self, name, _nonnegative_int(getattr(self, name), name))
        if self.close_equity is not None:
            if not _finite(self.close_equity) or float(self.close_equity) < 0:
                raise ValueError("close_equity 必须是非负有限数值或 null")
            object.__setattr__(self, "close_equity", float(self.close_equity))
        categories = tuple(sorted({
            _require_text(item, "reconciliation_category").lower()
            for item in self.reconciliation_categories
        }))
        object.__setattr__(self, "reconciliation_categories", categories)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_date": self.execution_date,
            "eligible": self.eligible,
            "reason_codes": list(self.reason_codes),
            "facts_sha256": self.facts_sha256,
            "attempt_id": self.attempt_id,
            "order_count": self.order_count,
            "fill_count": self.fill_count,
            "slippage_sample_count": self.slippage_sample_count,
            "close_equity": self.close_equity,
            "reconciliation_categories": list(self.reconciliation_categories),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PaperDayAssessment":
        _require_schema(value)
        return cls(
            execution_date=str(value["execution_date"]),
            eligible=value["eligible"],
            reason_codes=tuple(value.get("reason_codes", ())),
            facts_sha256=str(value["facts_sha256"]),
            attempt_id=(None if value.get("attempt_id") is None else str(value["attempt_id"])),
            order_count=int(value.get("order_count", 0)),
            fill_count=int(value.get("fill_count", 0)),
            slippage_sample_count=int(value.get("slippage_sample_count", 0)),
            close_equity=(
                None if value.get("close_equity") is None else float(value["close_equity"])
            ),
            reconciliation_categories=tuple(value.get("reconciliation_categories", ())),
        )


def _summary_evidence_sha256(
    campaign_sha256: str,
    expected_dates: Sequence[str],
    days: Sequence[PaperDayAssessment],
) -> str:
    return canonical_sha256(
        {
            "campaign_sha256": campaign_sha256,
            "expected_trading_dates": list(expected_dates),
            "day_assessments": [day.to_dict() for day in days],
        }
    )


@dataclass(frozen=True)
class PaperEvidenceSummary(_JsonContract):
    """完整 Paper campaign 的机器可校验汇总。"""

    campaign_id: str
    campaign_sha256: str
    evidence_sha256: str
    expected_trading_dates: tuple[str, ...]
    day_assessments: tuple[PaperDayAssessment, ...]
    minimum_trading_days: int
    eligible_trading_days: int
    consecutive_eligible_trading_days: int
    passed: bool
    missing_dates: tuple[str, ...] = ()
    failed_dates: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "campaign_id", _require_text(self.campaign_id, "campaign_id"))
        object.__setattr__(
            self,
            "campaign_sha256",
            _require_sha256(self.campaign_sha256, "campaign_sha256"),
        )
        object.__setattr__(
            self,
            "evidence_sha256",
            _require_sha256(self.evidence_sha256, "evidence_sha256"),
        )
        expected = _dates(self.expected_trading_dates, "expected_trading_dates")
        object.__setattr__(self, "expected_trading_dates", expected)
        days = tuple(self.day_assessments)
        if tuple(day.execution_date for day in days) != expected:
            raise ValueError("day_assessments 必须逐日覆盖 expected_trading_dates")
        object.__setattr__(self, "day_assessments", days)
        minimum = _nonnegative_int(self.minimum_trading_days, "minimum_trading_days")
        eligible = _nonnegative_int(self.eligible_trading_days, "eligible_trading_days")
        consecutive = _nonnegative_int(
            self.consecutive_eligible_trading_days,
            "consecutive_eligible_trading_days",
        )
        actual_eligible = sum(day.eligible for day in days)
        actual_consecutive = 0
        for day in reversed(days):
            if not day.eligible:
                break
            actual_consecutive += 1
        if eligible != actual_eligible or consecutive != actual_consecutive:
            raise ValueError("Paper 日数统计与 day_assessments 不一致")
        object.__setattr__(self, "minimum_trading_days", minimum)
        object.__setattr__(self, "eligible_trading_days", eligible)
        object.__setattr__(self, "consecutive_eligible_trading_days", consecutive)
        actual_passed = actual_consecutive >= minimum
        if not isinstance(self.passed, bool) or self.passed != actual_passed:
            raise ValueError("passed 必须由连续合格交易日数计算")
        expected_missing = tuple(
            day.execution_date
            for day in days
            if MISSING_TRADING_DAY in day.reason_codes
        )
        expected_failed = tuple(
            day.execution_date
            for day in days
            if not day.eligible and MISSING_TRADING_DAY not in day.reason_codes
        )
        if tuple(self.missing_dates) != expected_missing:
            raise ValueError("missing_dates 与逐日结论不一致")
        if tuple(self.failed_dates) != expected_failed:
            raise ValueError("failed_dates 与逐日结论不一致")
        object.__setattr__(self, "missing_dates", expected_missing)
        object.__setattr__(self, "failed_dates", expected_failed)
        expected_digest = _summary_evidence_sha256(self.campaign_sha256, expected, days)
        if self.evidence_sha256 != expected_digest:
            raise ValueError("evidence_sha256 与逐日证据不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "campaign_id": self.campaign_id,
            "campaign_sha256": self.campaign_sha256,
            "evidence_sha256": self.evidence_sha256,
            "expected_trading_dates": list(self.expected_trading_dates),
            "day_assessments": [day.to_dict() for day in self.day_assessments],
            "minimum_trading_days": self.minimum_trading_days,
            "eligible_trading_days": self.eligible_trading_days,
            "consecutive_eligible_trading_days": self.consecutive_eligible_trading_days,
            "passed": self.passed,
            "missing_dates": list(self.missing_dates),
            "failed_dates": list(self.failed_dates),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PaperEvidenceSummary":
        _require_schema(value)
        return cls(
            campaign_id=str(value["campaign_id"]),
            campaign_sha256=str(value["campaign_sha256"]),
            evidence_sha256=str(value["evidence_sha256"]),
            expected_trading_dates=tuple(value["expected_trading_dates"]),
            day_assessments=tuple(
                PaperDayAssessment.from_dict(item)
                for item in value["day_assessments"]
            ),
            minimum_trading_days=int(value["minimum_trading_days"]),
            eligible_trading_days=int(value["eligible_trading_days"]),
            consecutive_eligible_trading_days=int(
                value["consecutive_eligible_trading_days"]
            ),
            passed=value["passed"],
            missing_dates=tuple(value.get("missing_dates", ())),
            failed_dates=tuple(value.get("failed_dates", ())),
        )


def _json_object(row: Mapping[str, Any], field: str) -> dict[str, Any] | None:
    value = row.get(field)
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _records_digest(records: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    normalized: dict[str, list[dict[str, Any]]] = {}
    for table in sorted(records):
        rows = [dict(row) for row in records.get(table, ())]
        normalized[table] = sorted(
            rows,
            key=lambda row: (
                str(row.get("id", "")),
                str(row.get("idempotency_key", "")),
                canonical_sha256(row),
            ),
        )
    return canonical_sha256(normalized)


def resolve_order_lifecycle(
    records: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    run_id: str,
) -> tuple[OrderLifecycleAssessment, ...]:
    """归并一个 run 的订单事件；任何未闭环或成交证据缺口均失败。"""
    orders = [dict(row) for row in records.get("orders", ()) if row.get("run_id") == run_id]
    fills = [dict(row) for row in records.get("fills", ()) if row.get("run_id") == run_id]
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for order in orders:
        grouped[str(order.get("client_order_id") or "")].append(order)

    used_fill_ids: set[str] = set()
    results: list[OrderLifecycleAssessment] = []
    for client_order_id in sorted(grouped):
        events = grouped[client_order_id]
        reasons: set[str] = set()
        if not client_order_id:
            reasons.add(ORDER_CHAIN_INCONSISTENT)
        statuses = [str(row.get("status", "")).lower() for row in events]
        unknown = set(statuses) - _KNOWN_ORDER_STATUSES
        if unknown:
            reasons.add(UNKNOWN_ORDER_STATUS)
        if set(statuses) & {"uncertain", "submit_error", "dead", "partial"}:
            reasons.add(UNRESOLVED_ORDER)
        terminal_rows = [
            row for row in events
            if str(row.get("status", "")).lower() in _VALID_ORDER_TERMINALS
        ]
        terminal_status: str | None = None
        expected_quantity = 0
        if len(terminal_rows) != 1:
            reasons.add(UNRESOLVED_ORDER)
        else:
            terminal = terminal_rows[0]
            terminal_status = str(terminal["status"]).lower()
            try:
                expected_quantity = int(terminal["quantity"])
            except (KeyError, TypeError, ValueError):
                reasons.add(ORDER_CHAIN_INCONSISTENT)
            identities: set[tuple[str, str, int]] = set()
            for row in events:
                quantity = row.get("quantity")
                symbol = row.get("symbol")
                side = str(row.get("side", "")).lower()
                if (
                    not isinstance(quantity, int)
                    or isinstance(quantity, bool)
                    or quantity <= 0
                    or not isinstance(symbol, str)
                    or not symbol
                    or side not in {"buy", "sell"}
                ):
                    reasons.add(ORDER_CHAIN_INCONSISTENT)
                    continue
                identities.add((symbol, side, quantity))
            if len(identities) != 1 or expected_quantity <= 0:
                reasons.add(ORDER_CHAIN_INCONSISTENT)
            if statuses.count("planned") != 1 or statuses.count("submitted") != 1:
                reasons.add(ORDER_CHAIN_INCOMPLETE)

        relevant_fills: list[dict[str, Any]] = []
        if len(terminal_rows) == 1:
            terminal_id = terminal_rows[0].get("id")
            relevant_fills = [fill for fill in fills if fill.get("order_id") == terminal_id]
            used_fill_ids.update(str(fill.get("id", "")) for fill in relevant_fills)
        filled_quantity = 0
        for fill in relevant_fills:
            try:
                quantity = int(fill["quantity"])
            except (KeyError, TypeError, ValueError):
                reasons.add(FILL_MISMATCH)
                continue
            filled_quantity += quantity
            terminal = terminal_rows[0]
            if (
                fill.get("symbol") != terminal.get("symbol")
                or str(fill.get("side", "")).lower()
                != str(terminal.get("side", "")).lower()
                or quantity <= 0
            ):
                reasons.add(FILL_MISMATCH)
            reference = fill.get("reference_price")
            slippage = fill.get("slippage_bps")
            if not _finite(reference) or float(reference) <= 0 or not _finite(slippage):
                reasons.add(SLIPPAGE_MISSING)

        if terminal_status in _VALID_ORDER_TERMINALS:
            if terminal_status in _FILL_ORDER_TERMINALS and not relevant_fills:
                reasons.add(FILL_MISSING)
            elif terminal_status == "filled" and filled_quantity != expected_quantity:
                reasons.add(FILL_MISMATCH)
            elif terminal_status == "partial" and not 0 < filled_quantity < expected_quantity:
                reasons.add(FILL_MISMATCH)
            elif terminal_status in _NO_FILL_ORDER_TERMINALS and relevant_fills:
                reasons.add(FILL_MISMATCH)
            if terminal_status == "filled" and len(terminal_rows) == 1:
                terminal_id = terminal_rows[0].get("id")
                committed = []
                for event in events:
                    if str(event.get("status", "")).lower() != "state_committed":
                        continue
                    metadata = _json_object(event, "metadata_json")
                    if metadata and metadata.get("terminal_order_id") == terminal_id:
                        committed.append(event)
                if len(committed) != 1:
                    reasons.add(ORDER_CHAIN_INCOMPLETE)

        results.append(
            OrderLifecycleAssessment(
                client_order_id=client_order_id,
                eligible=not reasons,
                terminal_status=terminal_status,
                reason_codes=tuple(sorted(reasons)),
                expected_quantity=max(0, expected_quantity),
                filled_quantity=max(0, filled_quantity),
                fill_count=len(relevant_fills),
                slippage_sample_count=sum(
                    _finite(fill.get("reference_price"))
                    and float(fill["reference_price"]) > 0
                    and _finite(fill.get("slippage_bps"))
                    for fill in relevant_fills
                ),
            )
        )

    orphan_fills = [
        fill for fill in fills if str(fill.get("id", "")) not in used_fill_ids
    ]
    if orphan_fills:
        results.append(
            OrderLifecycleAssessment(
                client_order_id="__orphan_fill__",
                eligible=False,
                terminal_status=None,
                reason_codes=(ORPHAN_FILL,),
                expected_quantity=0,
                filled_quantity=sum(
                    int(fill.get("quantity", 0))
                    for fill in orphan_fills
                    if isinstance(fill.get("quantity"), int)
                ),
                fill_count=len(orphan_fills),
                slippage_sample_count=0,
            )
        )
    return tuple(results)


def _valid_observation(
    row: Mapping[str, Any], session: MarketSession, *, close: bool
) -> bool:
    """实际观察与账本写入都必须发生在当日相应时段，禁止回填时间。"""
    try:
        observed = instant(row.get("occurred_at"))
        recorded = instant(row.get("recorded_at"))
        contains = (
            session.contains_close_observation if close else session.contains_execution
        )
        return observed <= recorded and contains(observed) and contains(recorded)
    except (TypeError, ValueError):
        return False


def assess_paper_session(
    campaign: PaperCampaignSpec,
    execution_date: str,
    records: Mapping[str, Sequence[Mapping[str, Any]]],
) -> PaperDayAssessment:
    """对一个预期交易日作 fail-closed 合格判定。"""
    day = _iso_date(execution_date, "execution_date")
    if day not in campaign.expected_trading_dates:
        raise ValueError(f"{day} 不在 campaign.expected_trading_dates 中")
    facts_sha256 = _records_digest(records)
    run_rows = [dict(row) for row in records.get("runs", ()) if row.get("mode") == campaign.mode]
    legacy_rows: list[dict[str, Any]] = []
    related: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in run_rows:
        metadata = _json_object(row, "metadata_json")
        if metadata is None or any(not metadata.get(field) for field in _REQUIRED_RUN_METADATA):
            legacy_rows.append(row)
            continue
        if metadata.get("campaign_id") == campaign.campaign_id \
                and metadata.get("execution_date") == day:
            related.append((row, metadata))

    if not related:
        reason = LEGACY_EVIDENCE_INCOMPLETE if legacy_rows else MISSING_TRADING_DAY
        return PaperDayAssessment(
            execution_date=day,
            eligible=False,
            reason_codes=(reason,),
            facts_sha256=facts_sha256,
        )

    reasons: set[str] = set()
    if legacy_rows:
        # 旧行没有 campaign/attempt 身份，无法证明它与当前 campaign 无关。
        reasons.add(LEGACY_EVIDENCE_INCOMPLETE)
    attempts: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for row, metadata in related:
        attempt_id = str(metadata["attempt_id"])
        attempts[attempt_id].append((row, metadata))
        if row.get("config_hash") != campaign.candidate_config_hash:
            reasons.add(CONFIG_HASH_MISMATCH)
        if metadata.get("campaign_sha256") != campaign.sha256:
            reasons.add(CAMPAIGN_HASH_MISMATCH)
        if metadata.get("source_hash") != campaign.source_hash:
            reasons.add(SOURCE_HASH_MISMATCH)
        if metadata.get("account_fingerprint") != campaign.account_fingerprint:
            reasons.add(ACCOUNT_MISMATCH)
        if str(row.get("data_status", "")).lower() != "fresh":
            reasons.add(STALE_DATA)
        try:
            signal_day = date.fromisoformat(str(row.get("signal_date", "")))
            expected_signal_day = campaign.expected_signal_dates[
                campaign.expected_trading_dates.index(day)
            ]
            if signal_day.isoformat() != expected_signal_day:
                reasons.add(SIGNAL_DATE_INVALID)
        except ValueError:
            reasons.add(SIGNAL_DATE_INVALID)

    all_statuses = {
        str(row.get("status", "")).lower()
        for events in attempts.values()
        for row, _ in events
    }
    if "failed" in all_statuses:
        reasons.add(RUN_FAILED)
    for events in attempts.values():
        for row, metadata in events:
            if str(row.get("status", "")).lower() == "skipped" \
                    and metadata.get("reason") != "already_run":
                reasons.add(RUN_SKIPPED)

    completed_attempts = [
        attempt_id
        for attempt_id, events in attempts.items()
        if any(str(row.get("status", "")).lower() == "completed" for row, _ in events)
    ]
    if len(completed_attempts) != 1:
        reasons.add(AMBIGUOUS_RUN)
        return PaperDayAssessment(
            execution_date=day,
            eligible=False,
            reason_codes=tuple(reasons),
            facts_sha256=facts_sha256,
        )

    for attempt_events in attempts.values():
        statuses = {
            str(row.get("status", "")).lower()
            for row, _ in attempt_events
        }
        benign_duplicate = "skipped" in statuses and all(
            str(row.get("status", "")).lower() != "skipped"
            or metadata.get("reason") == "already_run"
            for row, metadata in attempt_events
        )
        if "completed" not in statuses and not benign_duplicate:
            reasons.add(AMBIGUOUS_RUN)

    attempt_id = completed_attempts[0]
    selected_events = attempts[attempt_id]
    started = [
        row for row, _ in selected_events
        if str(row.get("status", "")).lower() == "started"
    ]
    if len(started) != 1:
        reasons.add(AMBIGUOUS_RUN)
        return PaperDayAssessment(
            execution_date=day,
            eligible=False,
            reason_codes=tuple(reasons),
            facts_sha256=facts_sha256,
            attempt_id=attempt_id,
        )
    run_id = str(started[0].get("id", ""))
    if not run_id:
        reasons.add(LEGACY_EVIDENCE_INCOMPLETE)

    session: MarketSession | None = None
    try:
        session = MarketSession.from_dict(
            _json_object(started[0], "metadata_json").get("market_session")
        )
        expected_signal_day = campaign.expected_signal_dates[
            campaign.expected_trading_dates.index(day)
        ]
        if (
            session.trading_date != day
            or session.signal_date != expected_signal_day
            or not _valid_observation(started[0], session, close=False)
            or any(metadata.get("market_session") != session.to_dict()
                   for _, metadata in selected_events)
        ):
            reasons.add(MARKET_SESSION_INVALID)
    except (TypeError, ValueError):
        reasons.add(MARKET_SESSION_INVALID)

    order_assessments = resolve_order_lifecycle(records, run_id=run_id)
    for assessment in order_assessments:
        reasons.update(assessment.reason_codes)

    equity_rows = [
        dict(row)
        for row in records.get("equity_snapshots", ())
        if row.get("run_id") == run_id
    ]
    close_rows: list[dict[str, Any]] = []
    for row in equity_rows:
        metadata = _json_object(row, "metadata_json")
        if metadata is None or not metadata.get("phase"):
            reasons.add(LEGACY_EVIDENCE_INCOMPLETE)
        elif str(metadata["phase"]).lower() == "close":
            if not metadata.get("attempt_id") or not metadata.get(
                "account_fingerprint"
            ):
                reasons.add(LEGACY_EVIDENCE_INCOMPLETE)
            elif metadata.get("attempt_id") != attempt_id:
                reasons.add(EQUITY_AMBIGUOUS)
            elif metadata.get("campaign_sha256") != campaign.sha256:
                reasons.add(CAMPAIGN_HASH_MISMATCH)
            elif metadata.get("account_fingerprint") \
                    != campaign.account_fingerprint:
                reasons.add(ACCOUNT_MISMATCH)
            if (session is None
                    or metadata.get("market_session") != session.to_dict()
                    or not _valid_observation(row, session, close=True)):
                reasons.add(CLOSE_TIME_INVALID)
            close_rows.append(row)
    close_equity: float | None = None
    if not close_rows:
        reasons.add(EQUITY_MISSING)
    elif len(close_rows) != 1:
        reasons.add(EQUITY_AMBIGUOUS)
    else:
        candidate_equity = close_rows[0].get("equity")
        candidate_cash = close_rows[0].get("cash")
        candidate_buying_power = close_rows[0].get("buying_power")
        candidate_position_count = close_rows[0].get("position_count")
        if (
            not _finite(candidate_equity)
            or float(candidate_equity) < 0
            or not _finite(candidate_cash)
            or not _finite(candidate_buying_power)
            or not isinstance(candidate_position_count, int)
            or isinstance(candidate_position_count, bool)
            or candidate_position_count < 0
        ):
            reasons.add(EQUITY_INVALID)
        else:
            close_equity = float(candidate_equity)

    targeted_reconciliations: list[dict[str, Any]] = []
    reconciliation_rows = [dict(row) for row in records.get("reconciliations", ())]
    shadow_run_ids = {row.get("id") for row in records.get("runs", ())
                      if row.get("mode") == "reconcile"}
    legacy_reconciliations = False
    for row in reconciliation_rows:
        if row.get("run_id") in shadow_run_ids:
            # 独立影子对账用于回测偏差分析，既不能替代也不覆盖收盘事实。
            continue
        details = _json_object(row, "details_json")
        if details is None or not details.get("target_attempt_id"):
            legacy_reconciliations = True
        elif details.get("target_attempt_id") == attempt_id:
            if details.get("campaign_sha256") != campaign.sha256:
                reasons.add(CAMPAIGN_HASH_MISMATCH)
            if details.get("account_fingerprint") != campaign.account_fingerprint:
                reasons.add(ACCOUNT_MISMATCH)
            if (row.get("run_id") != run_id
                    or details.get("phase") != "close"
                    or session is None
                    or details.get("market_session") != session.to_dict()
                    or not _valid_observation(row, session, close=True)):
                reasons.add(CLOSE_TIME_INVALID)
            targeted_reconciliations.append(row)

    matched_categories: list[str] = []
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in targeted_reconciliations:
        by_category[str(row.get("category", "")).lower()].append(row)
    for category in campaign.required_reconciliation_categories:
        rows = by_category.get(category, [])
        if not rows:
            reasons.add(RECONCILIATION_MISSING)
            continue
        if len(rows) != 1:
            reasons.add(RECONCILIATION_AMBIGUOUS)
            continue
        status = str(rows[0].get("status", "")).upper()
        if status == "MATCH":
            matched_categories.append(category)
        elif status == "UNKNOWN":
            reasons.add(RECONCILIATION_UNKNOWN)
        elif status == "DRIFT":
            reasons.add(RECONCILIATION_DRIFT)
        else:
            reasons.add(RECONCILIATION_UNKNOWN)
    if legacy_reconciliations:
        reasons.add(LEGACY_EVIDENCE_INCOMPLETE)

    return PaperDayAssessment(
        execution_date=day,
        eligible=not reasons,
        reason_codes=tuple(reasons),
        facts_sha256=facts_sha256,
        attempt_id=attempt_id,
        order_count=len(order_assessments),
        fill_count=sum(item.fill_count for item in order_assessments),
        slippage_sample_count=sum(
            item.slippage_sample_count for item in order_assessments
        ),
        close_equity=close_equity,
        reconciliation_categories=tuple(matched_categories),
    )


def summarize_paper_evidence(
    campaign: PaperCampaignSpec,
    day_assessments: Sequence[PaperDayAssessment],
) -> PaperEvidenceSummary:
    """汇总完整冻结窗口；门禁只使用末尾连续合格交易日数。"""
    days = tuple(day_assessments)
    if tuple(day.execution_date for day in days) != campaign.expected_trading_dates:
        raise ValueError("逐日结论必须严格覆盖 campaign.expected_trading_dates")
    eligible = sum(day.eligible for day in days)
    consecutive = 0
    for day in reversed(days):
        if not day.eligible:
            break
        consecutive += 1
    missing = tuple(
        day.execution_date
        for day in days
        if MISSING_TRADING_DAY in day.reason_codes
    )
    failed = tuple(
        day.execution_date
        for day in days
        if not day.eligible and MISSING_TRADING_DAY not in day.reason_codes
    )
    campaign_digest = campaign.sha256
    evidence_digest = _summary_evidence_sha256(
        campaign_digest, campaign.expected_trading_dates, days
    )
    return PaperEvidenceSummary(
        campaign_id=campaign.campaign_id,
        campaign_sha256=campaign_digest,
        evidence_sha256=evidence_digest,
        expected_trading_dates=campaign.expected_trading_dates,
        day_assessments=days,
        minimum_trading_days=campaign.minimum_trading_days,
        eligible_trading_days=eligible,
        consecutive_eligible_trading_days=consecutive,
        passed=consecutive >= campaign.minimum_trading_days,
        missing_dates=missing,
        failed_dates=failed,
    )


def audit_paper_campaign(
    campaign: PaperCampaignSpec,
    records_by_date: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
) -> PaperEvidenceSummary:
    """审计冻结 campaign；缺失日期使用空事实并显式失败。"""
    assessments = tuple(
        assess_paper_session(
            campaign,
            trading_date,
            records_by_date.get(trading_date, {}),
        )
        for trading_date in campaign.expected_trading_dates
    )
    return summarize_paper_evidence(campaign, assessments)


__all__ = [
    "ACCOUNT_MISMATCH",
    "AMBIGUOUS_RUN",
    "CAMPAIGN_HASH_MISMATCH",
    "CLOSE_TIME_INVALID",
    "CONFIG_HASH_MISMATCH",
    "EQUITY_AMBIGUOUS",
    "EQUITY_INVALID",
    "EQUITY_MISSING",
    "FILL_MISMATCH",
    "FILL_MISSING",
    "LEGACY_EVIDENCE_INCOMPLETE",
    "MARKET_SESSION_INVALID",
    "MISSING_TRADING_DAY",
    "ORDER_CHAIN_INCOMPLETE",
    "ORDER_CHAIN_INCONSISTENT",
    "ORPHAN_FILL",
    "PaperCampaignSpec",
    "PaperDayAssessment",
    "PaperEvidenceSummary",
    "RECONCILIATION_AMBIGUOUS",
    "RECONCILIATION_DRIFT",
    "RECONCILIATION_MISSING",
    "RECONCILIATION_UNKNOWN",
    "RUN_FAILED",
    "RUN_SKIPPED",
    "SLIPPAGE_MISSING",
    "SIGNAL_DATE_INVALID",
    "SOURCE_HASH_MISMATCH",
    "STALE_DATA",
    "UNKNOWN_ORDER_STATUS",
    "UNRESOLVED_ORDER",
    "OrderLifecycleAssessment",
    "assess_paper_session",
    "audit_paper_campaign",
    "canonical_sha256",
    "resolve_order_lifecycle",
    "summarize_paper_evidence",
]
