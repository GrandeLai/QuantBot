"""Freeze, run once, and retain factor evidence using the existing experiment ledger."""

import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import version
from math import isfinite
from pathlib import Path
import platform
from uuid import uuid4

from quantbot.evidence import hash_payload, source_tree_hash
from quantbot.evidence_auth import append_json_line_locked
from quantbot.experiment_registry import ExperimentRegistry

from .crypto_panel import FACTORS, build_labels, build_panel, load_daily_data
from .time_series import PROTOCOL, evaluate_panel, hypotheses, study_protocol


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _runtime_versions():
    return {"python": platform.python_version(), "numpy": version("numpy"), "pandas": version("pandas")}


def register_study(root: Path, data: Path, *, development_end: str, protected_holdout_start: str,
                   provenance: Path | None = None, validation_days: int = 180,
                   prior_studies: tuple[Path, ...] = ()) -> Path:
    """Freeze window geometry and known factor trials; never reset prior failed searches."""
    root = Path(root)
    study_protocol(validation_days)
    frame = load_daily_data(data, development_end, protected_holdout_start)
    evidence = json.loads(provenance.read_text()) if provenance else {"source": "user_import", "verified": False}
    prior_evidence = _collect_prior_evidence(root, prior_studies)
    prior_count = sum(len(item["inferential_trials"]) for item in prior_evidence)
    protocol = study_protocol(validation_days, prior_count)
    root.mkdir(parents=True, exist_ok=True)
    identifier = "factor-" + uuid4().hex
    directory = root / identifier
    directory.mkdir()
    now = datetime.now(UTC)
    registry = ExperimentRegistry()
    declared = hypotheses()
    record = registry.register_factor_screen(experiment_id=identifier,
                                             hypothesis_hashes=[hash_payload(item) for item in declared],
                                             preregistered_at=now)
    # Serialize only the validated frame, avoiding races with the imported CSV.
    frame.drop(columns="as_of").to_csv(directory / "data.csv", index=False)
    _write(directory / "provenance.json", evidence)
    _write(directory / "prior-factor-evidence.json", prior_evidence)
    preregistration = {
        "experiment_id": identifier, "preregistered_at": now.isoformat(),
        "protocol": protocol, "factors": [definition.to_dict() for definition in FACTORS],
        "prior_study_paths": [item["study"] for item in prior_evidence],
        "prior_declared_trials": sum(item["declared_trials"] for item in prior_evidence),
        "hypotheses": [{**item, "trial_id": trial} for item, trial in zip(declared, record.trial_ids, strict=True)],
        "declared_trial_count": record.declared_trial_count,
        "development_end": development_end, "protected_holdout_start": protected_holdout_start,
        "input_source_sha256": _sha(data), "source_hash": source_tree_hash(),
        "runtime_versions": _runtime_versions(),
        "history_days": int(frame.groupby("symbol").size().min()),
        "prior_exposure": "development data may have been used in previous research; no independent holdout claim",
        "point_in_time_verified": False,
        "trading_admission": False, "model_calls_authorized": False,
    }
    _write(directory / "preregistration.json", preregistration)
    _write(directory / "freeze.json", {name: _sha(directory/name)
                                       for name in ("data.csv", "provenance.json", "preregistration.json",
                                                    "prior-factor-evidence.json")})
    append_json_line_locked(root / "ledger.jsonl", {
        "experiment_id": identifier, "event": "registered", "at": now.isoformat(),
        "declared_trials": record.declared_trial_count, "trial_ids": list(record.trial_ids),
        "freeze_sha256": _sha(directory / "freeze.json"),
        "source_hash": preregistration["source_hash"],
    })
    return directory


def _read_registration(directory):
    """Check immutable registration independently of the currently installed source version."""
    freeze = json.loads((directory / "freeze.json").read_text())
    entries = [json.loads(line) for line in (directory.parent / "ledger.jsonl").read_text().splitlines()]
    matches = [entry for entry in entries if entry["experiment_id"] == directory.name and entry["event"] == "registered"]
    if len(matches) != 1 or matches[0]["freeze_sha256"] != _sha(directory / "freeze.json"):
        raise ValueError("registration does not match family ledger")
    if freeze.get("preregistration.json") != _sha(directory / "preregistration.json"):
        raise ValueError("frozen input changed: preregistration.json")
    return json.loads((directory / "preregistration.json").read_text()), freeze, entries


def _collect_prior_evidence(root, extra_paths):
    ledger = root / "ledger.jsonl"
    local = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
    pending = [root / item["experiment_id"] for item in local if item["event"] == "registered"]
    pending.extend(Path(path) for path in extra_paths)
    visited, snapshots = set(), {}
    while pending:
        directory = pending.pop().resolve()
        if directory in visited:
            continue
        visited.add(directory)
        registration, freeze, entries = _read_registration(directory)
        identifier = registration["experiment_id"]
        if identifier != directory.name:
            raise ValueError("prior study identity does not match its directory")
        pending.extend(Path(path) for path in registration.get("prior_study_paths", []))
        completed = [item for item in entries if item["experiment_id"] == identifier and item["event"] == "completed"]
        failed = [item for item in entries if item["experiment_id"] == identifier and item["event"] == "failed"]
        if len(completed) > 1 or len(failed) > 1 or (completed and failed):
            raise ValueError("conflicting prior study outcomes")
        result_rows, result_hash = None, None
        if completed:
            manifest_path = directory / "artifact-manifest.json"
            if completed[0]["manifest_sha256"] != _sha(manifest_path):
                raise ValueError("prior completed manifest changed")
            manifest = json.loads(manifest_path.read_text())
            required = ("data.csv", "provenance.json", "preregistration.json", "freeze.json", "result.json")
            for name in required:
                if manifest.get(name) != _sha(directory/name):
                    raise ValueError("prior completed evidence changed: " + name)
            result = json.loads((directory / "result.json").read_text())
            result_rows = {item["trial_id"]: item for item in result["results"]}
            declared_ids = {item["trial_id"] for item in registration["hypotheses"]}
            if len(result_rows) != len(result["results"]) or set(result_rows) != declared_ids:
                raise ValueError("prior completed trial set does not match registration")
            result_hash = _sha(directory / "result.json")
        elif failed and failed[0]["failure_sha256"] != _sha(directory / "failure.json"):
            raise ValueError("prior failure evidence changed")
        trials = []
        for hypothesis in registration["hypotheses"]:
            if not hypothesis["inferential"]:
                continue
            p = result_rows[hypothesis["trial_id"]]["p_value"] if result_rows is not None else 1.0
            if isinstance(p, bool) or not isinstance(p, (float, int)) or not isfinite(p) or not 0 <= p <= 1:
                raise ValueError("invalid prior factor p value")
            trials.append({"trial_id": hypothesis["trial_id"], "key": hypothesis["key"], "p_value": p})
        snapshot = {
            "study": str(directory), "experiment_id": identifier,
            "status": "completed" if completed else ("failed" if failed else "incomplete"),
            "declared_trials": registration["declared_trial_count"], "inferential_trials": trials,
            "registration_sha256": freeze["preregistration.json"], "result_sha256": result_hash,
        }
        if identifier in snapshots and snapshots[identifier] != snapshot:
            raise ValueError("conflicting prior study identity")
        snapshots[identifier] = snapshot
    return [snapshots[key] for key in sorted(snapshots)]


def _verified_registration(directory):
    registration, freeze, _ = _read_registration(directory)
    for name in ("data.csv", "provenance.json", "prior-factor-evidence.json"):
        if freeze.get(name) != _sha(directory / name):
            raise ValueError("frozen input changed: " + name)
    prior_evidence = json.loads((directory / "prior-factor-evidence.json").read_text())
    prior_count = sum(len(item["inferential_trials"]) for item in prior_evidence)
    if registration["source_hash"] != source_tree_hash():
        raise ValueError("source changed since preregistration; register a new counted trial")
    if registration["runtime_versions"] != _runtime_versions():
        raise ValueError("runtime versions changed since preregistration")
    expected = study_protocol(registration["protocol"]["validation_days"], prior_count)
    if registration["protocol"] != expected or registration["factors"] != json.loads(json.dumps(
        [definition.to_dict() for definition in FACTORS]
    )):
        raise ValueError("frozen protocol or factor definitions changed")
    return registration, prior_evidence


def run_study(directory: Path):
    directory = Path(directory).resolve()
    # An exclusive marker keeps interrupted/failed attempts from silently rerunning.
    with (directory / "started.json").open("x") as handle:
        json.dump({"at": datetime.now(UTC).isoformat()}, handle)
    try:
        registration, prior_evidence = _verified_registration(directory)
        data = load_daily_data(directory / "data.csv", registration["development_end"],
                               registration["protected_holdout_start"])
        panel = build_panel(data)
        labels = build_labels(data, PROTOCOL["one_way_cost_bps"])
        panel.to_csv(directory / "factor-panel.csv", index=False)
        labels.to_csv(directory / "forward-labels.csv", index=False)
        result = evaluate_panel(panel, labels, data,
                                trial_ids=[item["trial_id"] for item in registration["hypotheses"]],
                                validation_days=registration["protocol"]["validation_days"],
                                prior_p_values=[trial["p_value"] for item in prior_evidence
                                                for trial in item["inferential_trials"]])
        if registration["source_hash"] != source_tree_hash():
            raise ValueError("source changed during evaluation")
        result["experiment_id"] = registration["experiment_id"]
        result["source_hash"] = registration["source_hash"]
        result["data_sha256"] = _sha(directory / "data.csv")
        result["family_registered_trials"] = registration["declared_trial_count"] + registration["prior_declared_trials"]
        result["family_registered_studies"] = 1 + len(prior_evidence)
        result["limitations"] = [
            "Development selection only; no independent holdout or portfolio backtest was performed.",
            "Fixed open-to-open event returns omit stop-loss/take-profit, order fills and portfolio accounting.",
            "One-way 15 bps (stress 30 bps) is a frozen research assumption, not a measured OKX execution cost.",
            "Historical availability/revisions are not independently verified; instantaneous next-open fill is an assumption.",
            "Bootstrap uncertainty is exploratory; overlapping labels are used only for IC/quantile diagnostics.",
            "Holm covers the factor family known at registration; manual/strategy searches and future rounds are outside it.",
            "State/risk diagnostics are not applied as filters; strategy-level ablations remain future work.",
        ]
        _write(directory / "result.json", result)
        (directory / "report.md").write_text(render_report(result, registration))
        _write(directory / "artifact-manifest.json", {path.name: _sha(path) for path in sorted(directory.iterdir())
                                                     if path.is_file() and path.name != "artifact-manifest.json"})
        append_json_line_locked(directory.parent / "ledger.jsonl", {
            "experiment_id": directory.name, "event": "completed", "at": datetime.now(UTC).isoformat(),
            "completed_trials": len(result["results"]), "pool": result["pool"],
            "manifest_sha256": _sha(directory / "artifact-manifest.json"),
        })
        return result
    except Exception as error:
        _write(directory / "failure.json", {"error_type": type(error).__name__, "error": str(error),
                                            "at": datetime.now(UTC).isoformat(), "trading_admission": False})
        append_json_line_locked(directory.parent / "ledger.jsonl", {
            "experiment_id": directory.name, "event": "failed", "at": datetime.now(UTC).isoformat(),
            "failure_sha256": _sha(directory / "failure.json"),
        })
        raise


def render_report(result, registration):
    lines = ["# 加密因子开发验证", "", f"结论：{result['status']}；入池 {len(result['pool'])} 个。",
             "", "这是因子诊断，未完成策略回测或独立留出验证，不代表账户盈利或交易准出。", "",
             f"历史：每币 {result['history_days']} 根日线；开发截止 {registration['development_end']}。",
             f"本轮登记 {result['declared_trials']} 个诊断、{result['inferential_hypotheses']} 个 alpha 假设；"
             f"连同 {result['prior_inferential_hypotheses']} 个既有假设，对 {result['correction_family_size']} 项统一做 Holm 校正。",
             f"{result['validation_folds']} 个 {result['validation_days']} 日开发验证窗口；标签跨界删除，训练标签与验证起点隔离 1 日。",
             "单边成本 15 bps，压力 30 bps；1/5/10 日固定持有事件，不使用止盈止损。",
             "事件按固定持有期网格去重；分位与 IC 使用的重叠标签仅作诊断。", "",
             "| 因子 | 币种 | 天数 | 有效事件 | 校正 p | 状态 | 原因 |",
             "| --- | --- | ---: | ---: | ---: | --- | --- |"]
    for row in result["results"]:
        p = "—" if row["holm_p_value"] is None else f"{row['holm_p_value']:.4f}"
        lines.append(f"| {row['factor']} | {row['symbol']} | {row['horizon']} | {row['selected_events']} | {p} | "
                     f"{row['status']} | {', '.join(row['reasons']) or 'development only'} |")
    lines.extend(["", "入池：" + (", ".join(result["pool"]) or "空池；保留失败，不自动追加搜索。"), "",
                  "完整数值、各窗口 Rank IC、训练分位阈值、分位净事件收益、费用压力、缺失原因、"
                  "区块不确定性和训练相关性见 result.json。", "",
                  "状态/风险因子只做条件诊断；组合与策略消融属于后续阶段。", "",
                  "限制：历史发布时间及修订未独立验证；研究仍属于已暴露的开发数据；"
                  "多重比较校正覆盖登记时已知的因子研究家族，不包含未登记的人工、策略搜索或以后新增研究。", "",
                  "真实策略模型调用：0；交易回测：0；模拟/实盘：0；独立留出：未使用。", ""])
    return "\n".join(lines)
