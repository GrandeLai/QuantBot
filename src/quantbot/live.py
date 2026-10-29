"""Live-trading soak journals and evidence."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .acceptance_validation import (
    is_nonnegative_integer,
    is_valid_drawdown,
    require_drawdown,
    require_finite_number,
    require_nonnegative_integer,
    valid_error_total,
)
from .acceptance_policy import CURRENT_ACCEPTANCE_POLICY
from .audit import RequirementEvidence
from .evidence import hash_payload, source_tree_hash


@dataclass(frozen=True)
class LiveTradingRun:
    day: str
    errors: int
    drawdown: float
    notes: str = ""
    run_type: Optional[str] = None
    observed_at: Optional[str] = None
    dataset_hash: Optional[str] = None
    config_hash: Optional[str] = None
    code_hash: Optional[str] = None


@dataclass(frozen=True)
class LiveEquitySnapshot:
    timestamp: str
    equity: float
    errors: int = 0
    notes: str = ""
    run_type: Optional[str] = None
    observed_at: Optional[str] = None
    dataset_hash: Optional[str] = None
    config_hash: Optional[str] = None
    code_hash: Optional[str] = None


@dataclass(frozen=True)
class LiveSnapshotRollup:
    sessions: List[LiveTradingRun]
    skipped_days: List[str]


@dataclass(frozen=True)
class LiveTradingDecision:
    passed: bool
    sessions: int
    errors: int
    worst_drawdown: float
    reasons: List[str]


class LiveTradingJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(
        self,
        day: str,
        errors: int,
        drawdown: float,
        notes: str = "",
        run_type: str = "live_trading",
        observed_at: Optional[str] = None,
        dataset_hash: Optional[str] = None,
        config_hash: Optional[str] = None,
        code_hash: Optional[str] = None,
    ) -> None:
        _validate_live_run_values(errors, drawdown)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "day": day,
            "errors": errors,
            "drawdown": drawdown,
            "notes": notes,
        }
        payload.update(
            {
                "run_type": run_type,
                "observed_at": observed_at or _utc_now_iso(),
                "dataset_hash": dataset_hash or hash_payload(
                    {"day": day, "errors": errors, "drawdown": drawdown}
                ),
                "config_hash": config_hash or hash_payload({"journal": "live_trading"}),
                "code_hash": code_hash or source_tree_hash(),
            }
        )
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_all(self, runs: Sequence[LiveTradingRun]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            for run in runs:
                _validate_live_run_values(run.errors, run.drawdown)
                file.write(json.dumps(_live_run_payload(run), sort_keys=True) + "\n")

    def read_all(self) -> List[LiveTradingRun]:
        if not self.path.exists():
            return []
        runs = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                item = json.loads(line)
                runs.append(
                    LiveTradingRun(
                        day=item["day"],
                        errors=item["errors"],
                        drawdown=item["drawdown"],
                        notes=item.get("notes", ""),
                        run_type=item.get("run_type"),
                        observed_at=item.get("observed_at"),
                        dataset_hash=item.get("dataset_hash"),
                        config_hash=item.get("config_hash"),
                        code_hash=item.get("code_hash"),
                    )
                )
        return runs


class LiveEquitySnapshotJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, timestamp: str, equity: float, errors: int = 0, notes: str = "") -> None:
        _validate_snapshot_values(errors, equity)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": timestamp,
            "equity": equity,
            "errors": errors,
            "notes": notes,
            "run_type": "live_trading",
            "observed_at": _utc_now_iso(),
            "dataset_hash": hash_payload(
                {"timestamp": timestamp, "equity": equity, "errors": errors}
            ),
            "config_hash": hash_payload({"journal": "live_snapshot"}),
            "code_hash": source_tree_hash(),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_all(self, snapshots: Sequence[LiveEquitySnapshot]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            for snapshot in snapshots:
                _validate_snapshot_values(snapshot.errors, snapshot.equity)
                file.write(
                    json.dumps(
                        {
                            "timestamp": snapshot.timestamp,
                            "equity": snapshot.equity,
                            "errors": snapshot.errors,
                            "notes": snapshot.notes,
                            "run_type": snapshot.run_type,
                            "observed_at": snapshot.observed_at,
                            "dataset_hash": snapshot.dataset_hash,
                            "config_hash": snapshot.config_hash,
                            "code_hash": snapshot.code_hash,
                        },
                        sort_keys=True,
                    )
                    + "\n"
                )

    def read_all(self) -> List[LiveEquitySnapshot]:
        if not self.path.exists():
            return []
        snapshots = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                item = json.loads(line)
                snapshots.append(
                    LiveEquitySnapshot(
                        timestamp=item["timestamp"],
                        equity=item["equity"],
                        errors=item.get("errors", 0),
                        notes=item.get("notes", ""),
                        run_type=item.get("run_type"),
                        observed_at=item.get("observed_at"),
                        dataset_hash=item.get("dataset_hash"),
                        config_hash=item.get("config_hash"),
                        code_hash=item.get("code_hash"),
                    )
                )
        return snapshots


def rollup_live_snapshots(
    snapshots: Sequence[LiveEquitySnapshot],
    baseline_equity: float = None,
    min_snapshots_per_day: int = 1,
) -> LiveSnapshotRollup:
    grouped: Dict[str, List[LiveEquitySnapshot]] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp):
        grouped.setdefault(live_day_for_timestamp(snapshot.timestamp), []).append(snapshot)

    sessions = []
    skipped_days = []
    peak = float(baseline_equity) if baseline_equity else 0.0
    for day in sorted(grouped):
        day_snapshots = grouped[day]
        if len(day_snapshots) < min_snapshots_per_day:
            skipped_days.append(day)
            continue
        equity = day_snapshots[-1].equity
        if equity <= 0:
            skipped_days.append(day)
            continue
        peak = max(peak, equity)
        drawdown = 1 - equity / peak if peak else 0.0
        sessions.append(
            LiveTradingRun(
                day=day,
                errors=sum(snapshot.errors for snapshot in day_snapshots),
                drawdown=drawdown,
                notes="; ".join(snapshot.notes for snapshot in day_snapshots if snapshot.notes),
                **_snapshot_provenance(day_snapshots, "live_trading"),
            )
        )
    return LiveSnapshotRollup(sessions=sessions, skipped_days=skipped_days)


def live_day_for_timestamp(timestamp: str) -> str:
    return date.fromisoformat(timestamp[:10]).isoformat()


def build_live_trading_evidence(
    runs: Sequence[LiveTradingRun],
    min_sessions: int = 20,
    max_drawdown: float = 0.15,
    enforce_policy: Optional[bool] = None,
):
    if enforce_policy is None:
        enforce_policy = not _is_historical_replay(runs)
    evidence_policy_version = None
    if enforce_policy:
        policy = CURRENT_ACCEPTANCE_POLICY.effective(
            min_live_sessions=min_sessions,
            max_live_drawdown=max_drawdown,
        )
        min_sessions = policy.min_live_sessions
        max_drawdown = policy.max_live_drawdown
        evidence_policy_version = policy.policy_version
    decision = evaluate_live_trading_runs(
        runs,
        min_sessions=min_sessions,
        max_drawdown=max_drawdown,
    )
    return {
        "live_trading_soak": RequirementEvidence(
            "live_trading_soak",
            passed=decision.passed,
            detail=(
                "sessions=%d errors=%d worst_drawdown=%.6f max_drawdown=%.6f reasons=%s"
                % (
                    decision.sessions,
                    decision.errors,
                    decision.worst_drawdown,
                    max_drawdown,
                    ",".join(decision.reasons),
                )
            ),
            policy_version=evidence_policy_version,
        )
    }


def evaluate_live_trading_runs(
    runs: Sequence[LiveTradingRun],
    min_sessions: int = 20,
    max_drawdown: float = 0.15,
) -> LiveTradingDecision:
    sessions = len({run.day for run in runs})
    errors = valid_error_total(run.errors for run in runs)
    valid_drawdowns = [float(run.drawdown) for run in runs if is_valid_drawdown(run.drawdown)]
    worst_drawdown = max(valid_drawdowns, default=0.0)
    reasons = []
    if sessions < min_sessions:
        reasons.append("duration")
    if sessions != len(runs):
        reasons.append("duplicate_day")
    if any(not is_nonnegative_integer(run.errors) for run in runs):
        reasons.append("invalid_errors")
    if errors:
        reasons.append("runtime_errors")
    if any(not is_valid_drawdown(run.drawdown) for run in runs):
        reasons.append("invalid_drawdown")
    if worst_drawdown > max_drawdown:
        reasons.append("drawdown")
    return LiveTradingDecision(
        passed=not reasons,
        sessions=sessions,
        errors=errors,
        worst_drawdown=worst_drawdown,
        reasons=reasons,
    )


def _live_run_payload(run: LiveTradingRun) -> dict:
    return {
        "day": run.day,
        "errors": run.errors,
        "drawdown": run.drawdown,
        "notes": run.notes,
        "run_type": run.run_type,
        "observed_at": run.observed_at,
        "dataset_hash": run.dataset_hash,
        "config_hash": run.config_hash,
        "code_hash": run.code_hash,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_live_run_values(errors, drawdown) -> None:
    require_nonnegative_integer(errors, "errors")
    require_drawdown(drawdown)


def _validate_snapshot_values(errors, equity) -> None:
    require_nonnegative_integer(errors, "errors")
    require_finite_number(equity, "equity")


def _is_historical_replay(runs) -> bool:
    return bool(runs) and all(run.run_type == "historical_replay" for run in runs)


def _snapshot_provenance(snapshots, expected_run_type: str) -> dict:
    run_types = [snapshot.run_type for snapshot in snapshots]
    if "historical_replay" in run_types:
        run_type = "historical_replay"
    elif run_types and all(value == expected_run_type for value in run_types):
        run_type = expected_run_type
    else:
        run_type = "unspecified"
    observed = [snapshot.observed_at for snapshot in snapshots]
    configs = [snapshot.config_hash for snapshot in snapshots]
    codes = [snapshot.code_hash for snapshot in snapshots]
    return {
        "run_type": run_type,
        "observed_at": max(observed) if observed and all(observed) else "unavailable",
        "dataset_hash": hash_payload([snapshot.dataset_hash for snapshot in snapshots]),
        "config_hash": hash_payload(configs) if configs and all(configs) else "unavailable",
        "code_hash": codes[0] if codes and all(codes) and len(set(codes)) == 1 else "unavailable",
    }
