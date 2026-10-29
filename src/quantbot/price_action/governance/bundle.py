"""不可覆盖的候选配置证据包。

本模块只把一份已经生成的实验产物和其精确基线配置冻结成可审计目录。
它没有、也不会提供把候选配置写回 ``config.yaml`` 的能力。

候选目录以候选配置的语义 SHA-256 命名，先在同一文件系统的临时目录中
完整构建并同步，再通过目录改名一次性发布。验证函数全程只读，任何文件、
配置、源码或快照漂移都会失败；只有“没有 Git HEAD / 工作区非干净”被记录
为不可晋级状态，以便仍能保留研究证据。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import canonical_config_json, config_hash
from ..domain.decision import DecisionSource
from .contracts import ExperimentResult, HypothesisSpec
from .provenance import SourceFingerprint, source_fingerprint


BUNDLE_SCHEMA_VERSION = 1
BUNDLE_TYPE = "pabot_candidate_bundle"

EXPERIMENT_FILE = "experiment.json"
CANDIDATE_CONFIG_FILE = "candidate.config.json"
ROLLBACK_CONFIG_FILE = "rollback.config.json"
SOURCE_FILE = "source.json"
SNAPSHOT_FILE = "snapshot.json"
BUNDLE_FILE = "bundle.json"

PAYLOAD_FILES = frozenset(
    {
        EXPERIMENT_FILE,
        CANDIDATE_CONFIG_FILE,
        ROLLBACK_CONFIG_FILE,
        SOURCE_FILE,
        SNAPSHOT_FILE,
    }
)
EXPECTED_FILES = PAYLOAD_FILES | {BUNDLE_FILE}


class CandidateBundleError(ValueError):
    """实验产物或候选包无法形成可信闭环。"""


@dataclass(frozen=True)
class SnapshotIdentity:
    """候选包真正依赖的快照身份；路径不是稳定身份的一部分。"""

    snapshot_id: str
    verified: bool
    verification_mode: str
    manifest_sha256: str
    file_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "verified": self.verified,
            "verification_mode": self.verification_mode,
            "manifest_sha256": self.manifest_sha256,
            "file_count": self.file_count,
        }


@dataclass(frozen=True)
class CandidateBundleVerification:
    """候选包完成闭环校验后的只读摘要。"""

    path: Path
    candidate_config_hash: str
    parent_config_hash: str
    experiment_id: str
    bundle_sha256: str
    source_hash: str
    git_head: str | None
    data_snapshot: str
    snapshot_manifest_sha256: str
    eligible: bool
    ineligibility_reasons: tuple[str, ...]
    file_sha256: Mapping[str, str]
    experiment: ExperimentResult
    hypothesis: HypothesisSpec
    forward_evidence_verified: bool


@dataclass(frozen=True)
class _ParsedExperiment:
    raw: bytes
    hypothesis: HypothesisSpec
    result: ExperimentResult
    candidate_config: dict[str, Any]
    change: dict[str, Any]
    provenance: SourceFingerprint
    snapshot: SnapshotIdentity


SourceProvider = Callable[[str | Path], SourceFingerprint]
SnapshotVerifier = Callable[..., Any]


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _config_bytes(config: Mapping[str, Any]) -> bytes:
    return (canonical_config_json(config) + "\n").encode("utf-8")


def _require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CandidateBundleError(f"{name} 必须是 JSON 对象")
    return value


def _require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateBundleError(f"{name} 必须是非空字符串")
    return value


def _require_sha256(value: Any, name: str) -> str:
    text = _require_text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise CandidateBundleError(f"{name} 必须是完整 SHA-256")
    return text


def _read_json_object(payload: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateBundleError(f"{name} 不是有效 UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise CandidateBundleError(f"{name} 顶层必须是 JSON 对象")
    return value


def _source_from_mapping(value: Mapping[str, Any], name: str) -> SourceFingerprint:
    code_version = _require_text(value.get("code_version"), f"{name}.code_version")
    source_hash = _require_sha256(value.get("source_hash"), f"{name}.source_hash")
    git_head = value.get("git_head")
    if git_head is not None:
        git_head = _require_text(git_head, f"{name}.git_head")
    dirty = value.get("dirty")
    if not isinstance(dirty, bool):
        raise CandidateBundleError(f"{name}.dirty 必须是布尔值")
    file_count = value.get("file_count")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
        raise CandidateBundleError(f"{name}.file_count 必须是非负整数")
    return SourceFingerprint(
        code_version=code_version,
        source_hash=source_hash,
        git_head=git_head,
        dirty=dirty,
        file_count=file_count,
    )


def _snapshot_from_mapping(value: Mapping[str, Any], name: str) -> SnapshotIdentity:
    verified = value.get("verified")
    if not isinstance(verified, bool):
        raise CandidateBundleError(f"{name}.verified 必须是布尔值")
    file_count = value.get("file_count")
    if isinstance(file_count, bool) or not isinstance(file_count, int) or file_count < 0:
        raise CandidateBundleError(f"{name}.file_count 必须是非负整数")
    return SnapshotIdentity(
        snapshot_id=_require_text(value.get("snapshot_id"), f"{name}.snapshot_id"),
        verified=verified,
        verification_mode=_require_text(
            value.get("verification_mode"), f"{name}.verification_mode"
        ),
        manifest_sha256=_require_sha256(
            value.get("manifest_sha256"), f"{name}.manifest_sha256"
        ),
        file_count=file_count,
    )


def _snapshot_from_verifier(value: Any, name: str) -> SnapshotIdentity:
    try:
        payload = {
            "snapshot_id": value.snapshot_id,
            "verified": value.verified,
            "verification_mode": value.verification_mode,
            "manifest_sha256": value.manifest_sha256,
            "file_count": value.file_count,
        }
    except AttributeError as exc:
        raise CandidateBundleError(f"{name} 未返回完整快照校验凭据") from exc
    return _snapshot_from_mapping(payload, name)


def _canonical_equal(left: Any, right: Any) -> bool:
    try:
        left_json = json.dumps(
            left, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        right_json = json.dumps(
            right, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        return False
    return left_json == right_json


def _parse_experiment(raw: bytes, baseline_config: Mapping[str, Any]) -> _ParsedExperiment:
    artifact = _read_json_object(raw, EXPERIMENT_FILE)
    if artifact.get("schema_version") != 1:
        raise CandidateBundleError("experiment.json schema_version 不受支持")

    try:
        hypothesis = HypothesisSpec.from_dict(
            _require_mapping(artifact.get("hypothesis"), "experiment.hypothesis")
        )
        result = ExperimentResult.from_dict(
            _require_mapping(artifact.get("result"), "experiment.result")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CandidateBundleError(f"实验契约无法解析: {exc}") from exc

    # 局部导入避免 governance 包初始化时与 experiment.runner 形成循环。
    from ..experiment.runner import build_candidate_config

    try:
        candidate_config, change = build_candidate_config(
            baseline_config, hypothesis.single_change
        )
    except (TypeError, ValueError) as exc:
        raise CandidateBundleError(f"无法由基线重建唯一候选配置: {exc}") from exc

    baseline_hash = config_hash(baseline_config)
    candidate_hash = config_hash(candidate_config)
    if hypothesis.signal_source is not DecisionSource.PRICE_ACTION:
        raise CandidateBundleError("候选实验信号来源不是 PRICE_ACTION")
    if not result.price_action_policy_passed or not result.snapshot_verified:
        raise CandidateBundleError("实验没有通过价格行为政策或快照校验")
    if artifact.get("experiment_id") != result.experiment_id:
        raise CandidateBundleError("外层 experiment_id 与 ExperimentResult 不一致")
    if artifact.get("completed_at") != result.completed_at:
        raise CandidateBundleError("外层 completed_at 与 ExperimentResult 不一致")
    if result.hypothesis_id != hypothesis.hypothesis_id:
        raise CandidateBundleError("HypothesisSpec 与 ExperimentResult 的假设 ID 不一致")
    if result.data_snapshot != hypothesis.data_snapshot:
        raise CandidateBundleError("HypothesisSpec 与 ExperimentResult 的快照 ID 不一致")
    if not (
        baseline_hash
        == hypothesis.baseline_config_hash
        == result.baseline_config_hash
    ):
        raise CandidateBundleError("基线配置语义哈希未闭环")
    if not (
        candidate_hash
        == hypothesis.candidate_config_hash
        == result.candidate_config_hash
    ):
        raise CandidateBundleError("候选配置语义哈希未闭环")

    baseline_section = _require_mapping(artifact.get("baseline"), "experiment.baseline")
    candidate_section = _require_mapping(artifact.get("candidate"), "experiment.candidate")
    if baseline_section.get("config_hash") != baseline_hash:
        raise CandidateBundleError("实验外层 baseline.config_hash 不一致")
    if candidate_section.get("config_hash") != candidate_hash:
        raise CandidateBundleError("实验外层 candidate.config_hash 不一致")

    paired = _require_mapping(
        artifact.get("paired_comparison"), "experiment.paired_comparison"
    )
    paired_links = {
        "diff_annual": result.paired_cagr_diff,
        "ci_low": result.paired_ci_low,
        "ci_high": result.paired_ci_high,
        "p_value": result.paired_p_value,
    }
    for field, expected in paired_links.items():
        actual = paired.get(field)
        if isinstance(actual, bool) or not isinstance(actual, (int, float)) \
                or float(actual) != float(expected):
            raise CandidateBundleError(
                f"ExperimentResult 与 paired_comparison.{field} 不一致"
            )

    expected_change = change.to_dict()
    actual_change = _require_mapping(artifact.get("change"), "experiment.change")
    if not _canonical_equal(actual_change, expected_change):
        raise CandidateBundleError("实验外层 change 与预注册唯一改动不一致")

    provenance = _source_from_mapping(
        _require_mapping(artifact.get("provenance"), "experiment.provenance"),
        "experiment.provenance",
    )
    snapshot = _snapshot_from_mapping(
        _require_mapping(
            artifact.get("snapshot_verification"),
            "experiment.snapshot_verification",
        ),
        "experiment.snapshot_verification",
    )
    if not snapshot.verified or snapshot.verification_mode != "full_sha256":
        raise CandidateBundleError("实验快照必须是 full_sha256 全量校验结果")
    if snapshot.snapshot_id != hypothesis.data_snapshot:
        raise CandidateBundleError("实验快照凭据与预注册快照 ID 不一致")

    return _ParsedExperiment(
        raw=raw,
        hypothesis=hypothesis,
        result=result,
        candidate_config=candidate_config,
        change=expected_change,
        provenance=provenance,
        snapshot=snapshot,
    )


def _source_eligibility(source: SourceFingerprint) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if source.git_head is None:
        reasons.append("git_head_missing")
    elif (
        len(source.git_head) not in {40, 64}
        or any(char not in "0123456789abcdef" for char in source.git_head.lower())
        or source.code_version != source.git_head
    ):
        reasons.append("git_head_invalid")
    if source.dirty:
        reasons.append("source_worktree_dirty")
    return not reasons, tuple(reasons)


def _validate_current_identities(
    parsed: _ParsedExperiment,
    baseline_config: Mapping[str, Any],
    *,
    project_root: Path,
    source_provider: SourceProvider,
    snapshot_verifier: SnapshotVerifier,
) -> tuple[SourceFingerprint, SnapshotIdentity, bool, tuple[str, ...]]:
    current_source = _source_from_mapping(
        source_provider(project_root).to_dict(), "current_source"
    )
    if current_source != parsed.provenance:
        raise CandidateBundleError("当前源码指纹与实验 provenance 不一致")

    try:
        snapshot_value = snapshot_verifier(
            baseline_config,
            parsed.hypothesis.data_snapshot,
            project_root=project_root,
        )
    except CandidateBundleError:
        raise
    except Exception as exc:  # noqa: BLE001 - 外部校验失败统一收敛为 fail-closed
        raise CandidateBundleError(f"当前快照全量校验失败: {exc}") from exc
    current_snapshot = _snapshot_from_verifier(snapshot_value, "current_snapshot")
    if not current_snapshot.verified \
            or current_snapshot.verification_mode != "full_sha256":
        raise CandidateBundleError("当前快照没有完成 full_sha256 全量校验")
    if current_snapshot != parsed.snapshot:
        raise CandidateBundleError(
            "当前快照与实验快照的 ID、manifest SHA-256 或文件数不一致"
        )

    eligible, reasons = _source_eligibility(current_source)
    return current_source, current_snapshot, eligible, reasons


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_bytes(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _path_exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _publish_directory(staging: Path, target: Path) -> None:
    if _path_exists(target):
        raise FileExistsError(f"候选包已存在，拒绝覆盖：{target}")
    try:
        staging.rename(target)
    except OSError as exc:
        if _path_exists(target):
            raise FileExistsError(f"候选包已存在，拒绝覆盖：{target}") from exc
        raise
    _fsync_dir(target.parent)


def _default_snapshot_verifier(
    config: Mapping[str, Any],
    snapshot_id: str,
    *,
    project_root: str | Path,
) -> Any:
    from ..experiment.runner import verify_config_snapshot

    return verify_config_snapshot(
        config, snapshot_id, project_root=project_root
    )


def create_candidate_bundle(
    baseline_config: Mapping[str, Any],
    experiment_artifact: str | Path,
    output_root: str | Path,
    *,
    project_root: str | Path | None = None,
    source_provider: SourceProvider = source_fingerprint,
    snapshot_verifier: SnapshotVerifier = _default_snapshot_verifier,
    created_at: str | None = None,
) -> CandidateBundleVerification:
    """生成不可覆盖的候选包，但绝不修改或应用生产配置。

    结构、配置、源码或快照闭环不成立时不生成目录。无 Git HEAD 或工作区
    非干净时仍保留包，但返回 ``eligible=False``，供研究审计而非发布。
    """
    experiment_path = Path(experiment_artifact)
    try:
        raw_experiment = experiment_path.read_bytes()
    except OSError as exc:
        raise CandidateBundleError(f"无法读取实验产物 {experiment_path}: {exc}") from exc
    parsed = _parse_experiment(raw_experiment, baseline_config)

    root = Path(project_root).resolve() if project_root is not None \
        else Path(__file__).resolve().parents[4]
    current_source, current_snapshot, eligible, reasons = _validate_current_identities(
        parsed,
        baseline_config,
        project_root=root,
        source_provider=source_provider,
        snapshot_verifier=snapshot_verifier,
    )

    candidate_hash = config_hash(parsed.candidate_config)
    parent_hash = config_hash(baseline_config)
    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    target = output / candidate_hash
    if _path_exists(target):
        raise FileExistsError(f"候选包已存在，拒绝覆盖：{target}")

    timestamp = created_at or datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    _require_text(timestamp, "created_at")

    source_payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "experiment": parsed.provenance.to_dict(),
        "current": current_source.to_dict(),
        "matched": True,
        "eligible": eligible,
        "ineligibility_reasons": list(reasons),
    }
    snapshot_payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "experiment": parsed.snapshot.to_dict(),
        "current": current_snapshot.to_dict(),
        "matched": True,
    }
    payloads = {
        EXPERIMENT_FILE: parsed.raw,
        CANDIDATE_CONFIG_FILE: _config_bytes(parsed.candidate_config),
        ROLLBACK_CONFIG_FILE: _config_bytes(baseline_config),
        SOURCE_FILE: _json_bytes(source_payload),
        SNAPSHOT_FILE: _json_bytes(snapshot_payload),
    }
    file_hashes = {name: _sha256_bytes(payload) for name, payload in payloads.items()}
    bundle_payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "bundle_type": BUNDLE_TYPE,
        "created_at": timestamp,
        "hypothesis_id": parsed.hypothesis.hypothesis_id,
        "experiment_id": parsed.result.experiment_id,
        "candidate_config_hash": candidate_hash,
        "parent_config_hash": parent_hash,
        "rollback_config_hash": parent_hash,
        "data_snapshot": parsed.snapshot.snapshot_id,
        "snapshot_manifest_sha256": parsed.snapshot.manifest_sha256,
        "source_hash": parsed.provenance.source_hash,
        "git_head": parsed.provenance.git_head,
        "eligible": eligible,
        "ineligibility_reasons": list(reasons),
        "files": file_hashes,
    }
    bundle_bytes = _json_bytes(bundle_payload)

    staging = Path(tempfile.mkdtemp(prefix=f".{candidate_hash}.", dir=output))
    try:
        for name, payload in payloads.items():
            _write_bytes(staging / name, payload)
        _write_bytes(staging / BUNDLE_FILE, bundle_bytes)
        _fsync_dir(staging)
        _publish_directory(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return CandidateBundleVerification(
        path=target,
        candidate_config_hash=candidate_hash,
        parent_config_hash=parent_hash,
        experiment_id=parsed.result.experiment_id,
        bundle_sha256=_sha256_bytes(bundle_bytes),
        source_hash=parsed.provenance.source_hash,
        git_head=parsed.provenance.git_head,
        data_snapshot=parsed.snapshot.snapshot_id,
        snapshot_manifest_sha256=parsed.snapshot.manifest_sha256,
        eligible=eligible,
        ineligibility_reasons=reasons,
        file_sha256=dict(file_hashes),
        experiment=parsed.result,
        hypothesis=parsed.hypothesis,
        forward_evidence_verified=False,
    )


def _validate_bundle_layout(bundle_dir: Path) -> None:
    if bundle_dir.is_symlink() or not bundle_dir.is_dir():
        raise CandidateBundleError(f"候选包不是普通目录：{bundle_dir}")
    actual: set[str] = set()
    for path in bundle_dir.iterdir():
        if path.is_symlink() or not path.is_file():
            raise CandidateBundleError(f"候选包含非普通文件：{path.name}")
        actual.add(path.name)
    if actual != EXPECTED_FILES:
        extra = sorted(actual - EXPECTED_FILES)
        missing = sorted(EXPECTED_FILES - actual)
        raise CandidateBundleError(
            f"候选包布局不闭合；多出 {extra}，缺失 {missing}"
        )


def _load_config_file(path: Path, name: str) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    value = _read_json_object(raw, name)
    if raw != _config_bytes(value):
        raise CandidateBundleError(f"{name} 不是规范 JSON 配置")
    return value, raw


def verify_candidate_bundle(
    bundle_dir: str | Path,
    *,
    project_root: str | Path | None = None,
    source_provider: SourceProvider = source_fingerprint,
    snapshot_verifier: SnapshotVerifier = _default_snapshot_verifier,
) -> CandidateBundleVerification:
    """只读重算候选包的文件、配置、源码和快照闭环。"""
    path = Path(bundle_dir)
    _validate_bundle_layout(path)

    bundle_raw = (path / BUNDLE_FILE).read_bytes()
    bundle = _read_json_object(bundle_raw, BUNDLE_FILE)
    if bundle_raw != _json_bytes(bundle):
        raise CandidateBundleError("bundle.json 不是规范 JSON")
    if bundle.get("schema_version") != BUNDLE_SCHEMA_VERSION \
            or bundle.get("bundle_type") != BUNDLE_TYPE:
        raise CandidateBundleError("bundle.json 版本或类型不受支持")

    declared_files = _require_mapping(bundle.get("files"), "bundle.files")
    if set(declared_files) != PAYLOAD_FILES:
        raise CandidateBundleError("bundle.files 必须且只能声明五个候选包载荷")
    file_hashes: dict[str, str] = {}
    for name in sorted(PAYLOAD_FILES):
        expected = _require_sha256(declared_files.get(name), f"bundle.files.{name}")
        actual = _sha256_file(path / name)
        if actual != expected:
            raise CandidateBundleError(f"{name} 字节 SHA-256 不一致")
        file_hashes[name] = actual

    rollback_config, rollback_raw = _load_config_file(
        path / ROLLBACK_CONFIG_FILE, ROLLBACK_CONFIG_FILE
    )
    candidate_config, candidate_raw = _load_config_file(
        path / CANDIDATE_CONFIG_FILE, CANDIDATE_CONFIG_FILE
    )
    parsed = _parse_experiment((path / EXPERIMENT_FILE).read_bytes(), rollback_config)
    if candidate_raw != _config_bytes(parsed.candidate_config):
        raise CandidateBundleError("candidate.config.json 与实验重建候选配置不一致")
    if rollback_raw != _config_bytes(rollback_config):
        raise CandidateBundleError("rollback.config.json 无法规范重放")

    candidate_hash = config_hash(candidate_config)
    parent_hash = config_hash(rollback_config)
    if candidate_hash != config_hash(parsed.candidate_config):
        raise CandidateBundleError("候选配置语义 SHA-256 与实验不一致")
    if path.name != candidate_hash:
        raise CandidateBundleError("候选包目录名不是候选配置 SHA-256")

    root = Path(project_root).resolve() if project_root is not None \
        else Path(__file__).resolve().parents[4]
    current_source, current_snapshot, eligible, reasons = _validate_current_identities(
        parsed,
        rollback_config,
        project_root=root,
        source_provider=source_provider,
        snapshot_verifier=snapshot_verifier,
    )

    source_payload = _read_json_object((path / SOURCE_FILE).read_bytes(), SOURCE_FILE)
    expected_source_payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "experiment": parsed.provenance.to_dict(),
        "current": current_source.to_dict(),
        "matched": True,
        "eligible": eligible,
        "ineligibility_reasons": list(reasons),
    }
    if not _canonical_equal(source_payload, expected_source_payload) \
            or (path / SOURCE_FILE).read_bytes() != _json_bytes(source_payload):
        raise CandidateBundleError("source.json 与当前/实验源码身份不一致")

    snapshot_payload = _read_json_object(
        (path / SNAPSHOT_FILE).read_bytes(), SNAPSHOT_FILE
    )
    expected_snapshot_payload = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "experiment": parsed.snapshot.to_dict(),
        "current": current_snapshot.to_dict(),
        "matched": True,
    }
    if not _canonical_equal(snapshot_payload, expected_snapshot_payload) \
            or (path / SNAPSHOT_FILE).read_bytes() != _json_bytes(snapshot_payload):
        raise CandidateBundleError("snapshot.json 与当前/实验快照身份不一致")

    expected_bundle_fields = {
        "hypothesis_id": parsed.hypothesis.hypothesis_id,
        "experiment_id": parsed.result.experiment_id,
        "candidate_config_hash": candidate_hash,
        "parent_config_hash": parent_hash,
        "rollback_config_hash": parent_hash,
        "data_snapshot": parsed.snapshot.snapshot_id,
        "snapshot_manifest_sha256": parsed.snapshot.manifest_sha256,
        "source_hash": parsed.provenance.source_hash,
        "git_head": parsed.provenance.git_head,
        "eligible": eligible,
        "ineligibility_reasons": list(reasons),
    }
    for name, expected in expected_bundle_fields.items():
        if not _canonical_equal(bundle.get(name), expected):
            raise CandidateBundleError(f"bundle.{name} 与重算结果不一致")
    _require_text(bundle.get("created_at"), "bundle.created_at")

    return CandidateBundleVerification(
        path=path,
        candidate_config_hash=candidate_hash,
        parent_config_hash=parent_hash,
        experiment_id=parsed.result.experiment_id,
        bundle_sha256=_sha256_bytes(bundle_raw),
        source_hash=parsed.provenance.source_hash,
        git_head=parsed.provenance.git_head,
        data_snapshot=parsed.snapshot.snapshot_id,
        snapshot_manifest_sha256=parsed.snapshot.manifest_sha256,
        eligible=eligible,
        ineligibility_reasons=reasons,
        file_sha256=file_hashes,
        experiment=parsed.result,
        hypothesis=parsed.hypothesis,
        forward_evidence_verified=False,
    )


__all__ = [
    "BUNDLE_FILE",
    "CANDIDATE_CONFIG_FILE",
    "CandidateBundleError",
    "CandidateBundleVerification",
    "EXPERIMENT_FILE",
    "ROLLBACK_CONFIG_FILE",
    "SNAPSHOT_FILE",
    "SOURCE_FILE",
    "create_candidate_bundle",
    "verify_candidate_bundle",
]
