"""在同一冻结数据上执行一项预注册的价格行为实验。

边界刻意保持严格：

* 一次实验只能改一个已存在的、明确列入白名单的标量配置；
* 基线和候选都必须满足生产价格行为政策；
* 两次回测共享同一份内存数据，且必须产出完全相同的权益日期索引；
* 冻结快照必须完成全量 SHA-256 校验，之后才允许调用回测器；
* 输出只是一份候选证据，不会写入生产配置，也不会自动批准发布。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from numbers import Integral, Real
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Protocol

import pandas as pd

from ..backtest.engine import BacktestEngine, BacktestResult
from ..backtest.metrics import compute_metrics
from ..backtest.stats import paired_test
from ..config import config_hash
from ..data.snapshot import MANIFEST_NAME, resolve_snapshot
from ..governance import (
    DecisionSource,
    ExperimentResult,
    HypothesisSpec,
    ReleaseGateDecision,
    ReleaseManifest,
    SourceFingerprint,
    evaluate_release,
    source_fingerprint,
    validate_production_config,
)


# 只允许会改变价格结构识别、价格衍生风控或风险预算的参数。
# 数据、回测成本、券商、运行模式及已被政策禁止的模块均不在此处。
ALLOWED_CHANGE_PATHS = frozenset(
    {
        "strategy.breakout_window",
        "strategy.exit_window",
        "strategy.atr_window",
        "strategy.atr_stop_mult",
        "strategy.atr_trail_mult",
        "strategy.swing_k",
        "strategy.momentum_window",
        "strategy.momentum_filter",
        "strategy.rank_by",
        "strategy.pullback_depth_atr",
        "strategy.use_inside_bar",
        "strategy.use_weekly_confirm",
        "strategy.pyramid_max_adds",
        "strategy.pyramid_trigger_r",
        "strategy.pyramid_size_frac",
        "risk.risk_per_trade",
        "risk.max_positions",
        "risk.max_weight",
        "risk.max_per_sector",
        "risk.dedup_share_classes",
        "regime.momentum_window",
        "regime.recovery_window",
        "regime.structural_reopen",
        "regime.reopen_window",
    }
)

CORE_METRICS = (
    "total_return",
    "cagr",
    "sharpe",
    "max_drawdown",
    "calmar",
    "volatility",
    "num_trades",
    "win_rate",
    "profit_factor",
    "avg_win",
    "avg_loss",
)


class ExperimentValidationError(ValueError):
    """预注册实验的输入或执行结果不满足可复现契约。"""


class BacktestOutput(Protocol):
    """实验层实际依赖的最小回测输出。"""

    equity: pd.Series

    def trades_df(self) -> pd.DataFrame:
        ...


BacktestRunner = Callable[
    [Mapping[str, pd.DataFrame], Mapping[str, Any]], BacktestOutput
]


@dataclass(frozen=True)
class ConfigChange:
    """已经解析并校验的一项候选配置变更。"""

    path: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before": copy.deepcopy(self.before),
            "after": copy.deepcopy(self.after),
        }


@dataclass(frozen=True)
class SnapshotVerification:
    """冻结快照的全量校验凭据。"""

    snapshot_id: str
    verified: bool
    verification_mode: str
    manifest_sha256: str
    file_count: int
    path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "verified": self.verified,
            "verification_mode": self.verification_mode,
            "manifest_sha256": self.manifest_sha256,
            "file_count": self.file_count,
            "path": self.path,
        }


SnapshotVerifier = Callable[[Mapping[str, Any], str], SnapshotVerification]
ProvenanceProvider = Callable[[str | Path], SourceFingerprint]


@dataclass(frozen=True)
class ValidationEvidence:
    """不属于本次双跑的后续证据；默认全部关闭以保证 fail-closed。"""

    walk_forward_passed: bool = False
    stress_test_passed: bool = False
    tests_passed: bool = False
    shadow_passed: bool = False
    paper_passed: bool = False
    paper_trading_days: int = 0
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.paper_trading_days, bool) \
                or not isinstance(self.paper_trading_days, int) \
                or self.paper_trading_days < 0:
            raise ValueError("paper_trading_days 必须是非负整数")


@dataclass(frozen=True)
class ExperimentArtifact:
    """一项实验的完整机器可读证据。"""

    schema_version: ClassVar[int] = 1

    experiment_id: str
    completed_at: str
    hypothesis: HypothesisSpec
    result: ExperimentResult
    change: ConfigChange
    provenance: SourceFingerprint
    snapshot: SnapshotVerification
    baseline_metrics: Mapping[str, int | float | None]
    candidate_metrics: Mapping[str, int | float | None]
    metric_deltas: Mapping[str, int | float | None]
    paired_comparison: Mapping[str, int | float | bool]
    release_manifest: ReleaseManifest | None = None
    release_gate: ReleaseGateDecision | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        release: dict[str, Any] | None = None
        if self.release_manifest is not None:
            release = {
                "manifest": self.release_manifest.to_dict(),
                "gate": self.release_gate.to_dict() if self.release_gate else None,
            }
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "completed_at": self.completed_at,
            "hypothesis": self.hypothesis.to_dict(),
            "result": self.result.to_dict(),
            "change": self.change.to_dict(),
            "provenance": self.provenance.to_dict(),
            "snapshot_verification": self.snapshot.to_dict(),
            "baseline": {
                "config_hash": self.result.baseline_config_hash,
                "metrics": dict(self.baseline_metrics),
            },
            "candidate": {
                "config_hash": self.result.candidate_config_hash,
                "metrics": dict(self.candidate_metrics),
            },
            "metric_deltas": dict(self.metric_deltas),
            "paired_comparison": dict(self.paired_comparison),
            "release": release,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            separators=None if indent is not None else (",", ":"),
            allow_nan=False,
        )


def _single_change(single_change: Mapping[str, Any]) -> tuple[str, Any]:
    if not isinstance(single_change, Mapping) or len(single_change) != 1:
        raise ExperimentValidationError("一次实验必须且只能声明一个 single_change")
    path, value = next(iter(single_change.items()))
    if not isinstance(path, str) or path not in ALLOWED_CHANGE_PATHS:
        allowed = ", ".join(sorted(ALLOWED_CHANGE_PATHS))
        raise ExperimentValidationError(
            f"配置路径 {path!r} 不属于价格行为/风险实验白名单；"
            f"允许：{allowed}"
        )
    return path, value


def build_candidate_config(
    baseline_config: Mapping[str, Any],
    single_change: Mapping[str, Any],
) -> tuple[dict[str, Any], ConfigChange]:
    """在内存副本中应用唯一改动，并校验基线与候选生产政策。

    函数不会改变 ``baseline_config``，也不会读写任何配置文件。
    """
    path, new_value = _single_change(single_change)
    validate_production_config(baseline_config)

    section, field_name = path.split(".", 1)
    section_value = baseline_config.get(section)
    if not isinstance(section_value, Mapping) or field_name not in section_value:
        raise ExperimentValidationError(f"基线配置中不存在预注册路径 {path}")

    before = copy.deepcopy(section_value[field_name])
    if type(before) is type(new_value) and before == new_value:
        raise ExperimentValidationError(f"{path} 的候选值与基线相同，未形成实验")

    candidate = copy.deepcopy(dict(baseline_config))
    candidate_section = dict(candidate[section])
    candidate_section[field_name] = copy.deepcopy(new_value)
    candidate[section] = candidate_section
    validate_production_config(candidate)
    return candidate, ConfigChange(path=path, before=before, after=new_value)


def _validate_hypothesis(
    hypothesis: HypothesisSpec,
    baseline_config: Mapping[str, Any],
    candidate_config: Mapping[str, Any],
) -> None:
    if hypothesis.signal_source is not DecisionSource.PRICE_ACTION:
        raise ExperimentValidationError(
            "候选晋级实验的 signal_source 必须是 PRICE_ACTION"
        )
    if hypothesis.primary_metric != "cagr":
        raise ExperimentValidationError("当前实验与发布契约只支持 primary_metric=cagr")
    if hypothesis.multiple_testing_method != "single_pre_registered_hypothesis":
        raise ExperimentValidationError(
            "一次执行只接受单一预注册假设，禁止运行后择优"
        )
    registered_value = next(iter(hypothesis.single_change.values()))
    if hypothesis.parameter_candidates \
            and tuple(hypothesis.parameter_candidates) != (registered_value,):
        raise ExperimentValidationError(
            "parameter_candidates 只能为空或只包含本次 single_change 的预注册值"
        )
    baseline_hash = config_hash(baseline_config)
    candidate_hash = config_hash(candidate_config)
    if hypothesis.baseline_config_hash != baseline_hash:
        raise ExperimentValidationError(
            "基线配置哈希与预注册假设不一致，"
            "拒绝在漂移后的配置上补做实验"
        )
    if hypothesis.candidate_config_hash != candidate_hash:
        raise ExperimentValidationError(
            "候选配置哈希与预注册假设不一致，拒绝执行未预注册的候选"
        )
    configured_snapshot = baseline_config.get("data", {}).get("snapshot")
    if not configured_snapshot or str(configured_snapshot) != hypothesis.data_snapshot:
        raise ExperimentValidationError(
            "data.snapshot 必须与 HypothesisSpec.data_snapshot 完全一致"
        )


def verify_config_snapshot(
    config: Mapping[str, Any],
    snapshot_id: str,
    *,
    project_root: str | Path | None = None,
) -> SnapshotVerification:
    """对配置引用的快照执行清单与每个文件的 SHA-256 全量校验。"""
    data_cfg = config.get("data", {})
    if str(data_cfg.get("snapshot") or "") != str(snapshot_id):
        raise ExperimentValidationError("配置引用的快照 ID 与预注册快照不一致")
    cache_dir = Path(str(data_cfg.get("cache_dir", "")))
    if not cache_dir.is_absolute() and project_root is not None:
        cache_dir = Path(project_root) / cache_dir
    snap_dir = resolve_snapshot(cache_dir, snapshot_id, verify_hashes=True)
    manifest_path = snap_dir / MANIFEST_NAME
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if str(manifest.get("id")) != str(snapshot_id):
        raise ExperimentValidationError("快照 manifest.id 与预注册快照 ID 不一致")
    return SnapshotVerification(
        snapshot_id=str(snapshot_id),
        verified=True,
        verification_mode="full_sha256",
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        file_count=int(manifest["file_count"]),
        path=str(snap_dir.resolve()),
    )


def default_backtest_runner(
    data: Mapping[str, pd.DataFrame],
    config: Mapping[str, Any],
) -> BacktestResult:
    """用项目正式 ``BacktestEngine`` 执行一次完整回测。"""
    return BacktestEngine(dict(data), copy.deepcopy(dict(config))).run()


def derive_experiment_id(
    hypothesis: HypothesisSpec,
    provenance: SourceFingerprint,
    *,
    n_paths: int = 2000,
    block: int = 21,
    seed: int = 42,
) -> str:
    """由完整预注册契约、源码和统计协议生成稳定实验 ID。"""
    if isinstance(n_paths, bool) or not isinstance(n_paths, int) or n_paths <= 0:
        raise ExperimentValidationError("n_paths 必须是正整数")
    if isinstance(block, bool) or not isinstance(block, int) or block <= 0:
        raise ExperimentValidationError("block 必须是正整数")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ExperimentValidationError("seed 必须是整数")
    identity = {
        "hypothesis": hypothesis.to_dict(),
        "source_hash": provenance.source_hash,
        "protocol": {
            "version": 1,
            "paired_test": "block_bootstrap_daily_return_difference",
            "n_paths": n_paths,
            "block": block,
            "seed": seed,
        },
    }
    digest = hashlib.sha256(
        json.dumps(
            identity, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return f"exp-{digest[:24]}"


def _validate_equity(name: str, equity: Any) -> pd.Series:
    if not isinstance(equity, pd.Series) or equity.empty:
        raise ExperimentValidationError(
            f"{name} 回测必须返回非空 pandas.Series 权益曲线"
        )
    if equity.index.has_duplicates or not equity.index.is_monotonic_increasing:
        raise ExperimentValidationError(f"{name} 权益日期必须唯一且递增")
    values = pd.to_numeric(equity, errors="coerce")
    if values.isna().any() or not all(math.isfinite(float(value)) for value in values):
        raise ExperimentValidationError(f"{name} 权益曲线包含缺失值或非有限值")
    if (values <= 0).any():
        raise ExperimentValidationError(f"{name} 权益曲线必须始终大于 0")
    return equity


def _json_metric(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _core_metrics(result: BacktestOutput) -> dict[str, int | float | None]:
    metrics = compute_metrics(result.equity, result.trades_df())
    return {key: _json_metric(metrics.get(key)) for key in CORE_METRICS}


def _metric_deltas(
    baseline: Mapping[str, int | float | None],
    candidate: Mapping[str, int | float | None],
) -> dict[str, int | float | None]:
    out: dict[str, int | float | None] = {}
    for key in CORE_METRICS:
        before, after = baseline.get(key), candidate.get(key)
        if before is None or after is None:
            out[key] = None
        else:
            out[key] = float(after) - float(before)
    return out


def run_registered_experiment(
    hypothesis: HypothesisSpec,
    baseline_config: Mapping[str, Any],
    data: Mapping[str, pd.DataFrame],
    *,
    runner: BacktestRunner = default_backtest_runner,
    snapshot_verifier: SnapshotVerifier | None = None,
    provenance_provider: ProvenanceProvider = source_fingerprint,
    project_root: str | Path | None = None,
    validation: ValidationEvidence | None = None,
    release_manifest: ReleaseManifest | None = None,
    completed_at: str | None = None,
    n_paths: int = 2000,
    block: int = 21,
    seed: int = 42,
) -> ExperimentArtifact:
    """执行基线/候选双跑并返回不可变实验产物对象。

    ``runner`` 和 ``snapshot_verifier`` 可注入，便于测试和外部编排；正式调用
    的默认路径使用 ``BacktestEngine`` 并对实际快照做全量哈希校验。
    """
    candidate_config, change = build_candidate_config(
        baseline_config, hypothesis.single_change
    )
    _validate_hypothesis(hypothesis, baseline_config, candidate_config)

    root = Path(project_root) if project_root is not None \
        else Path(__file__).resolve().parents[4]
    provenance = provenance_provider(root)
    verifier = snapshot_verifier
    if verifier is None:
        verifier = lambda cfg, snapshot: verify_config_snapshot(
            cfg, snapshot, project_root=root
        )
    snapshot = verifier(baseline_config, hypothesis.data_snapshot)
    if not snapshot.verified or snapshot.verification_mode != "full_sha256":
        raise ExperimentValidationError(
            "实验只接受 verification_mode=full_sha256 的已验证快照"
        )
    if snapshot.snapshot_id != hypothesis.data_snapshot:
        raise ExperimentValidationError(
            "快照校验凭据与 HypothesisSpec 引用的 ID 不一致"
        )
    digest = snapshot.manifest_sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ExperimentValidationError("快照校验凭据缺少完整 manifest SHA-256")
    if snapshot.file_count < 0:
        raise ExperimentValidationError("快照校验凭据的 file_count 不得为负")

    # 同一个 data 对象被传给两次执行，避免调用方无意中加载两版行情。
    baseline_result = runner(data, copy.deepcopy(dict(baseline_config)))
    candidate_result = runner(data, copy.deepcopy(candidate_config))
    baseline_equity = _validate_equity("baseline", baseline_result.equity)
    candidate_equity = _validate_equity("candidate", candidate_result.equity)
    if not baseline_equity.index.equals(candidate_equity.index):
        raise ExperimentValidationError("基线与候选权益日期窗口不完全一致")

    baseline_metrics = _core_metrics(baseline_result)
    candidate_metrics = _core_metrics(candidate_result)
    deltas = _metric_deltas(baseline_metrics, candidate_metrics)
    paired = paired_test(
        candidate_equity,
        baseline_equity,
        n_paths=n_paths,
        block=block,
        seed=seed,
    )
    paired_json: dict[str, int | float | bool] = {
        key: (bool(value) if isinstance(value, bool)
              else int(value) if isinstance(value, Integral)
              else float(value))
        for key, value in paired.items()
    }

    validation = validation or ValidationEvidence()
    timestamp = completed_at or datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat()
    experiment_id = derive_experiment_id(
        hypothesis, provenance, n_paths=n_paths, block=block, seed=seed
    )
    result = ExperimentResult(
        experiment_id=experiment_id,
        hypothesis_id=hypothesis.hypothesis_id,
        baseline_config_hash=hypothesis.baseline_config_hash,
        candidate_config_hash=hypothesis.candidate_config_hash,
        data_snapshot=hypothesis.data_snapshot,
        completed_at=timestamp,
        price_action_policy_passed=True,
        snapshot_verified=True,
        paired_cagr_diff=float(paired["diff_annual"]),
        paired_ci_low=float(paired["ci_low"]),
        paired_ci_high=float(paired["ci_high"]),
        paired_p_value=float(paired["p_value"]),
        walk_forward_passed=validation.walk_forward_passed,
        stress_test_passed=validation.stress_test_passed,
        tests_passed=validation.tests_passed,
        shadow_passed=validation.shadow_passed,
        paper_passed=validation.paper_passed,
        paper_trading_days=validation.paper_trading_days,
        notes=(
            "baseline 与 candidate 使用同一数据对象及完全相同日期窗口",
            "paired_cagr_diff 沿用 paired_test 的同期日收益差年化口径",
            *validation.notes,
        ),
    )

    release_gate = None
    if release_manifest is not None:
        if release_manifest.source_hash != provenance.source_hash \
                or release_manifest.code_version != provenance.code_version:
            raise ExperimentValidationError(
                "发布清单绑定的源码身份与本次实验不一致"
            )
        release_gate = evaluate_release(
            hypothesis,
            result,
            release_manifest,
            candidate_config,
        )

    return ExperimentArtifact(
        experiment_id=experiment_id,
        completed_at=timestamp,
        hypothesis=hypothesis,
        result=result,
        change=change,
        provenance=provenance,
        snapshot=snapshot,
        baseline_metrics=baseline_metrics,
        candidate_metrics=candidate_metrics,
        metric_deltas=deltas,
        paired_comparison=paired_json,
        release_manifest=release_manifest,
        release_gate=release_gate,
        metadata={
            "runner": getattr(runner, "__qualname__", type(runner).__qualname__),
            "bootstrap": {"n_paths": n_paths, "block": block, "seed": seed},
            "config_canonicalization": "sha256(canonical_json)",
        },
    )


def write_experiment_artifact(
    artifact: ExperimentArtifact,
    output_path: str | Path,
) -> Path:
    """原子写入 JSON 产物；目标已存在时拒绝覆盖。"""
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = (artifact.to_json(indent=2) + "\n").encode("utf-8")

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # hard link 是“不覆盖已有目标”的原子发布；
        # 同目录保证同一文件系统。
        os.link(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
    return target
