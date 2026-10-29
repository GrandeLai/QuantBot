"""预注册实验与发布的可序列化契约。"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, ClassVar, Mapping, TypeVar

from .policy import DecisionSource


ContractT = TypeVar("ContractT", bound="JsonContract")


class JsonContract:
    """最小 JSON 往返接口；子类显式实现 ``to_dict/from_dict``。"""

    schema_version: ClassVar[int] = 1

    def to_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def from_dict(cls: type[ContractT], value: Mapping[str, Any]) -> ContractT:
        raise NotImplementedError

    def to_json(self, *, indent: int | None = None) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent,
            separators=None if indent is not None else (",", ":"), allow_nan=False,
        )

    @classmethod
    def from_json(cls: type[ContractT], payload: str) -> ContractT:
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError(f"{cls.__name__} JSON 顶层必须是对象")
        return cls.from_dict(value)


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")


def _require_finite(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(float(value)):
        raise ValueError(f"{name} 必须是有限数值")


def _require_sha256(name: str, value: str) -> None:
    _require_text(name, value)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise ValueError(f"{name} 必须是 64 位 SHA-256 十六进制字符串")


def _check_schema(value: Mapping[str, Any]) -> None:
    version = value.get("schema_version", 1)
    if version != JsonContract.schema_version:
        raise ValueError(f"不支持的 schema_version: {version!r}")


def _read_bool(value: Mapping[str, Any], name: str) -> bool:
    item = value[name]
    if not isinstance(item, bool):
        raise ValueError(f"{name} 必须是布尔值")
    return item


def _decision_source(value: DecisionSource | str) -> DecisionSource:
    if isinstance(value, DecisionSource):
        return value
    try:
        return DecisionSource(value)
    except ValueError:
        try:
            return DecisionSource[str(value).upper()]
        except KeyError as exc:
            raise ValueError(f"未知 signal_source: {value!r}") from exc


@dataclass(frozen=True)
class HypothesisSpec(JsonContract):
    """调整前冻结的单变量、可证伪价格行为假设。"""

    hypothesis_id: str
    title: str
    price_action_rationale: str
    baseline_config_hash: str
    candidate_config_hash: str
    data_snapshot: str
    single_change: Mapping[str, Any]
    created_at: str
    primary_metric: str = "cagr"
    signal_source: DecisionSource = DecisionSource.PRICE_ACTION
    expected_direction: str = "increase"
    minimum_effect: float = 0.005
    maximum_p_value: float = 0.10
    evaluation_windows: tuple[str, ...] = ()
    failure_conditions: tuple[str, ...] = ()
    parameter_candidates: tuple[Any, ...] = ()
    multiple_testing_method: str = "single_pre_registered_hypothesis"

    def __post_init__(self) -> None:
        for name in (
            "hypothesis_id", "title", "price_action_rationale",
            "data_snapshot", "created_at", "primary_metric", "multiple_testing_method",
        ):
            _require_text(name, getattr(self, name))
        _require_sha256("baseline_config_hash", self.baseline_config_hash)
        _require_sha256("candidate_config_hash", self.candidate_config_hash)
        if self.baseline_config_hash == self.candidate_config_hash:
            raise ValueError("候选配置必须与基线配置不同")
        if not isinstance(self.single_change, Mapping):
            raise ValueError("single_change 必须是映射")
        if len(self.single_change) != 1:
            raise ValueError("single_change 必须且只能包含一个配置路径")
        object.__setattr__(
            self, "single_change",
            MappingProxyType(copy.deepcopy(dict(self.single_change))),
        )
        object.__setattr__(self, "evaluation_windows", tuple(self.evaluation_windows))
        object.__setattr__(self, "failure_conditions", tuple(self.failure_conditions))
        object.__setattr__(self, "parameter_candidates", tuple(self.parameter_candidates))
        source = self.signal_source
        if not isinstance(source, DecisionSource):
            object.__setattr__(self, "signal_source", _decision_source(source))
        if self.expected_direction != "increase":
            raise ValueError("当前发布门禁只接受 expected_direction=increase")
        _require_finite("minimum_effect", self.minimum_effect)
        _require_finite("maximum_p_value", self.maximum_p_value)
        if self.minimum_effect <= 0:
            raise ValueError("minimum_effect 必须大于 0")
        if not 0 < self.maximum_p_value < 1:
            raise ValueError("maximum_p_value 必须位于 (0, 1)")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "title": self.title,
            "price_action_rationale": self.price_action_rationale,
            "baseline_config_hash": self.baseline_config_hash,
            "candidate_config_hash": self.candidate_config_hash,
            "data_snapshot": self.data_snapshot,
            "single_change": copy.deepcopy(dict(self.single_change)),
            "created_at": self.created_at,
            "primary_metric": self.primary_metric,
            "signal_source": self.signal_source.value,
            "expected_direction": self.expected_direction,
            "minimum_effect": self.minimum_effect,
            "maximum_p_value": self.maximum_p_value,
            "evaluation_windows": list(self.evaluation_windows),
            "failure_conditions": list(self.failure_conditions),
            "parameter_candidates": copy.deepcopy(list(self.parameter_candidates)),
            "multiple_testing_method": self.multiple_testing_method,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HypothesisSpec":
        _check_schema(value)
        return cls(
            hypothesis_id=str(value["hypothesis_id"]),
            title=str(value["title"]),
            price_action_rationale=str(value["price_action_rationale"]),
            baseline_config_hash=str(value["baseline_config_hash"]),
            candidate_config_hash=str(value["candidate_config_hash"]),
            data_snapshot=str(value["data_snapshot"]),
            single_change=copy.deepcopy(dict(value["single_change"])),
            created_at=str(value["created_at"]),
            primary_metric=str(value.get("primary_metric", "cagr")),
            signal_source=_decision_source(value.get(
                "signal_source", DecisionSource.PRICE_ACTION.value
            )),
            expected_direction=str(value.get("expected_direction", "increase")),
            minimum_effect=float(value.get("minimum_effect", 0.005)),
            maximum_p_value=float(value.get("maximum_p_value", 0.10)),
            evaluation_windows=tuple(map(str, value.get("evaluation_windows", ()))),
            failure_conditions=tuple(map(str, value.get("failure_conditions", ()))),
            parameter_candidates=tuple(copy.deepcopy(value.get("parameter_candidates", ()))),
            multiple_testing_method=str(value.get(
                "multiple_testing_method", "single_pre_registered_hypothesis"
            )),
        )


@dataclass(frozen=True)
class ExperimentResult(JsonContract):
    """冻结数据快照上的离线、影子和模拟盘验证结果。"""

    experiment_id: str
    hypothesis_id: str
    baseline_config_hash: str
    candidate_config_hash: str
    data_snapshot: str
    completed_at: str
    price_action_policy_passed: bool
    snapshot_verified: bool
    paired_cagr_diff: float
    paired_ci_low: float
    paired_ci_high: float
    paired_p_value: float
    walk_forward_passed: bool
    stress_test_passed: bool
    tests_passed: bool
    shadow_passed: bool
    paper_passed: bool
    paper_trading_days: int
    artifacts: Mapping[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "experiment_id", "hypothesis_id", "data_snapshot", "completed_at",
        ):
            _require_text(name, getattr(self, name))
        _require_sha256("baseline_config_hash", self.baseline_config_hash)
        _require_sha256("candidate_config_hash", self.candidate_config_hash)
        for name in (
            "price_action_policy_passed", "snapshot_verified", "walk_forward_passed",
            "stress_test_passed", "tests_passed", "shadow_passed", "paper_passed",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} 必须是布尔值")
        for name in ("paired_cagr_diff", "paired_ci_low", "paired_ci_high", "paired_p_value"):
            _require_finite(name, getattr(self, name))
        if self.paired_ci_low > self.paired_ci_high:
            raise ValueError("paired_ci_low 不得大于 paired_ci_high")
        if not 0 <= self.paired_p_value <= 1:
            raise ValueError("paired_p_value 必须位于 [0, 1]")
        if isinstance(self.paper_trading_days, bool) \
                or not isinstance(self.paper_trading_days, int) \
                or self.paper_trading_days < 0:
            raise ValueError("paper_trading_days 必须是非负整数")
        if not isinstance(self.artifacts, Mapping) \
                or any(not isinstance(key, str) or not isinstance(item, str)
                       for key, item in self.artifacts.items()):
            raise ValueError("artifacts 必须是字符串到字符串的映射")
        object.__setattr__(
            self, "artifacts", MappingProxyType(copy.deepcopy(dict(self.artifacts)))
        )
        object.__setattr__(self, "notes", tuple(self.notes))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "baseline_config_hash": self.baseline_config_hash,
            "candidate_config_hash": self.candidate_config_hash,
            "data_snapshot": self.data_snapshot,
            "completed_at": self.completed_at,
            "price_action_policy_passed": self.price_action_policy_passed,
            "snapshot_verified": self.snapshot_verified,
            "paired_cagr_diff": self.paired_cagr_diff,
            "paired_ci_low": self.paired_ci_low,
            "paired_ci_high": self.paired_ci_high,
            "paired_p_value": self.paired_p_value,
            "walk_forward_passed": self.walk_forward_passed,
            "stress_test_passed": self.stress_test_passed,
            "tests_passed": self.tests_passed,
            "shadow_passed": self.shadow_passed,
            "paper_passed": self.paper_passed,
            "paper_trading_days": self.paper_trading_days,
            "artifacts": copy.deepcopy(dict(self.artifacts)),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExperimentResult":
        _check_schema(value)
        boolean_fields = (
            "price_action_policy_passed", "snapshot_verified", "walk_forward_passed",
            "stress_test_passed", "tests_passed", "shadow_passed", "paper_passed",
        )
        return cls(
            experiment_id=str(value["experiment_id"]),
            hypothesis_id=str(value["hypothesis_id"]),
            baseline_config_hash=str(value["baseline_config_hash"]),
            candidate_config_hash=str(value["candidate_config_hash"]),
            data_snapshot=str(value["data_snapshot"]),
            completed_at=str(value["completed_at"]),
            **{name: _read_bool(value, name) for name in boolean_fields},
            paired_cagr_diff=float(value["paired_cagr_diff"]),
            paired_ci_low=float(value["paired_ci_low"]),
            paired_ci_high=float(value["paired_ci_high"]),
            paired_p_value=float(value["paired_p_value"]),
            paper_trading_days=int(value["paper_trading_days"]),
            artifacts={str(key): str(item) for key, item in dict(
                value.get("artifacts", {})
            ).items()},
            notes=tuple(map(str, value.get("notes", ()))),
        )


@dataclass(frozen=True)
class ReleaseManifest(JsonContract):
    """候选配置晋级生产前签署的不可变发布清单。"""

    release_id: str
    hypothesis_id: str
    experiment_id: str
    parent_config_hash: str
    candidate_config_hash: str
    code_version: str
    source_hash: str
    data_snapshot: str
    rollback_config_hash: str
    created_at: str
    effective_date: str
    human_approved: bool
    approved_by: str = ""
    git_head: str = ""
    candidate_bundle_sha256: str = ""
    snapshot_manifest_sha256: str = ""
    paper_campaign_sha256: str = ""
    paper_evidence_sha256: str = ""

    def __post_init__(self) -> None:
        for name in (
            "release_id", "hypothesis_id", "experiment_id", "code_version",
            "data_snapshot", "created_at", "effective_date",
        ):
            _require_text(name, getattr(self, name))
        _require_sha256("parent_config_hash", self.parent_config_hash)
        _require_sha256("candidate_config_hash", self.candidate_config_hash)
        _require_sha256("source_hash", self.source_hash)
        _require_sha256("rollback_config_hash", self.rollback_config_hash)
        if not isinstance(self.human_approved, bool):
            raise ValueError("human_approved 必须是布尔值")
        if self.human_approved and not self.approved_by.strip():
            raise ValueError("human_approved=true 时必须填写 approved_by")
        for name in (
            "candidate_bundle_sha256",
            "snapshot_manifest_sha256",
            "paper_campaign_sha256",
            "paper_evidence_sha256",
        ):
            value = getattr(self, name)
            if value:
                _require_sha256(name, value)
        if self.git_head:
            _require_text("git_head", self.git_head)
        try:
            created = datetime.fromisoformat(
                self.created_at.replace("Z", "+00:00")
            )
            effective = date.fromisoformat(self.effective_date)
        except ValueError as exc:
            raise ValueError(
                "created_at 必须是 ISO 时间，effective_date 必须是 ISO 日期"
            ) from exc
        if created.tzinfo is None or created.utcoffset() is None:
            raise ValueError("created_at 必须包含时区")
        if effective < created.date():
            raise ValueError("effective_date 不得早于发布清单创建日期")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "release_id": self.release_id,
            "hypothesis_id": self.hypothesis_id,
            "experiment_id": self.experiment_id,
            "parent_config_hash": self.parent_config_hash,
            "candidate_config_hash": self.candidate_config_hash,
            "code_version": self.code_version,
            "source_hash": self.source_hash,
            "data_snapshot": self.data_snapshot,
            "rollback_config_hash": self.rollback_config_hash,
            "created_at": self.created_at,
            "effective_date": self.effective_date,
            "human_approved": self.human_approved,
            "approved_by": self.approved_by,
            "git_head": self.git_head,
            "candidate_bundle_sha256": self.candidate_bundle_sha256,
            "snapshot_manifest_sha256": self.snapshot_manifest_sha256,
            "paper_campaign_sha256": self.paper_campaign_sha256,
            "paper_evidence_sha256": self.paper_evidence_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReleaseManifest":
        _check_schema(value)
        return cls(
            release_id=str(value["release_id"]),
            hypothesis_id=str(value["hypothesis_id"]),
            experiment_id=str(value["experiment_id"]),
            parent_config_hash=str(value["parent_config_hash"]),
            candidate_config_hash=str(value["candidate_config_hash"]),
            code_version=str(value["code_version"]),
            source_hash=str(value["source_hash"]),
            data_snapshot=str(value["data_snapshot"]),
            rollback_config_hash=str(value["rollback_config_hash"]),
            created_at=str(value["created_at"]),
            effective_date=str(value["effective_date"]),
            human_approved=_read_bool(value, "human_approved"),
            approved_by=str(value.get("approved_by", "")),
            git_head=str(value.get("git_head", "")),
            candidate_bundle_sha256=str(value.get("candidate_bundle_sha256", "")),
            snapshot_manifest_sha256=str(value.get("snapshot_manifest_sha256", "")),
            paper_campaign_sha256=str(value.get("paper_campaign_sha256", "")),
            paper_evidence_sha256=str(value.get("paper_evidence_sha256", "")),
        )
