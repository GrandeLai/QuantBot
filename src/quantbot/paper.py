"""Paper-trading acceptance gates and run journal."""

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

from .acceptance_validation import (
    is_finite_number,
    is_nonnegative_integer,
    require_finite_number,
    require_nonnegative_integer,
)
from .acceptance_policy import CURRENT_ACCEPTANCE_POLICY
from .audit import RequirementEvidence
from .evidence import hash_payload, source_tree_hash


@dataclass(frozen=True)
class PaperTradingRun:
    period: str
    errors: int
    paper_return: float
    backtest_return: float
    run_type: Optional[str] = None
    observed_at: Optional[str] = None
    dataset_hash: Optional[str] = None
    config_hash: Optional[str] = None
    code_hash: Optional[str] = None


@dataclass(frozen=True)
class PaperAccountSnapshot:
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
class PaperTradingDecision:
    passed: bool
    weeks: int
    performance_deviation: float
    reasons: List[str]


@dataclass(frozen=True)
class PaperSnapshotRollup:
    runs: List[PaperTradingRun]
    missing_backtest_periods: List[str]
    skipped_periods: List[str]


class PaperTradingAcceptance:
    def __init__(self, min_weeks: int = 4, max_performance_deviation: float = 0.30):
        self.min_weeks = min_weeks
        self.max_performance_deviation = max_performance_deviation

    def evaluate(self, runs: Sequence[PaperTradingRun]) -> PaperTradingDecision:
        reasons = []
        unique_runs = list({run.period: run for run in reversed(runs)}.values())
        unique_runs.reverse()
        if len(unique_runs) < self.min_weeks:
            reasons.append("duration")
        if len(unique_runs) != len(runs):
            reasons.append("duplicate_period")
        if any(not is_nonnegative_integer(run.errors) for run in runs):
            reasons.append("invalid_errors")
        if any(is_nonnegative_integer(run.errors) and run.errors for run in runs):
            reasons.append("runtime_errors")

        paper_values = [run.paper_return for run in unique_runs]
        backtest_values = [run.backtest_return for run in unique_runs]
        paper_values_valid = all(is_finite_number(value) for value in paper_values)
        backtest_values_valid = all(is_finite_number(value) for value in backtest_values)
        paper_return = sum(paper_values) if paper_values_valid else 0.0
        backtest_return = sum(backtest_values) if backtest_values_valid else 0.0
        if not paper_values_valid or not isfinite(float(paper_return)):
            reasons.append("invalid_paper_return")
            paper_values_valid = False
        if not backtest_values_valid or not isfinite(float(backtest_return)):
            reasons.append("invalid_backtest_return")
            backtest_values_valid = False

        deviation = 0.0
        if paper_values_valid and backtest_values_valid:
            denominator = abs(backtest_return) if backtest_return else 1.0
            deviation = abs(paper_return - backtest_return) / denominator
            if not isfinite(float(deviation)):
                reasons.append("invalid_performance_deviation")
                deviation = 0.0
            elif deviation > self.max_performance_deviation:
                reasons.append("performance_deviation")

        return PaperTradingDecision(
            passed=not reasons,
            weeks=len(unique_runs),
            performance_deviation=deviation,
            reasons=reasons,
        )


class PaperTradingJournal:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(
        self,
        period: str,
        errors: int,
        paper_return: float,
        backtest_return: float,
        run_type: str = "paper_trading",
        observed_at: Optional[str] = None,
        dataset_hash: Optional[str] = None,
        config_hash: Optional[str] = None,
        code_hash: Optional[str] = None,
    ) -> None:
        _validate_paper_run_values(errors, paper_return, backtest_return)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "period": period,
            "errors": errors,
            "paper_return": paper_return,
            "backtest_return": backtest_return,
        }
        payload.update(
            {
                "run_type": run_type,
                "observed_at": observed_at or _utc_now_iso(),
                "dataset_hash": dataset_hash or hash_payload(payload),
                "config_hash": config_hash or hash_payload({"journal": "paper_trading"}),
                "code_hash": code_hash or source_tree_hash(),
            }
        )
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_all(self, runs: Sequence[PaperTradingRun]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as file:
            for run in runs:
                _validate_paper_run_values(
                    run.errors,
                    run.paper_return,
                    run.backtest_return,
                )
                file.write(json.dumps(_paper_run_payload(run), sort_keys=True) + "\n")

    def read_all(self) -> List[PaperTradingRun]:
        if not self.path.exists():
            return []
        runs = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                item = json.loads(line)
                runs.append(
                    PaperTradingRun(
                        period=item["period"],
                        errors=item["errors"],
                        paper_return=item["paper_return"],
                        backtest_return=item["backtest_return"],
                        run_type=item.get("run_type"),
                        observed_at=item.get("observed_at"),
                        dataset_hash=item.get("dataset_hash"),
                        config_hash=item.get("config_hash"),
                        code_hash=item.get("code_hash"),
                    )
                )
        return runs


class PaperAccountSnapshotJournal:
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
            "run_type": "paper_trading",
            "observed_at": _utc_now_iso(),
            "dataset_hash": hash_payload(
                {"timestamp": timestamp, "equity": equity, "errors": errors}
            ),
            "config_hash": hash_payload({"journal": "paper_snapshot"}),
            "code_hash": source_tree_hash(),
        }
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")

    def write_all(self, snapshots: Sequence[PaperAccountSnapshot]) -> None:
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

    def read_all(self) -> List[PaperAccountSnapshot]:
        if not self.path.exists():
            return []
        snapshots = []
        with self.path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                item = json.loads(line)
                snapshots.append(
                    PaperAccountSnapshot(
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


def rollup_paper_snapshots(
    snapshots: Sequence[PaperAccountSnapshot],
    backtest_returns: Mapping[str, float],
    min_snapshots_per_period: int = 2,
) -> PaperSnapshotRollup:
    grouped: Dict[str, List[PaperAccountSnapshot]] = {}
    for snapshot in sorted(snapshots, key=lambda item: item.timestamp):
        grouped.setdefault(paper_period_for_timestamp(snapshot.timestamp), []).append(snapshot)

    runs = []
    missing = []
    skipped = []
    for period in sorted(grouped):
        period_snapshots = grouped[period]
        if len(period_snapshots) < min_snapshots_per_period:
            skipped.append(period)
            continue
        if period not in backtest_returns:
            missing.append(period)
            continue
        start = period_snapshots[0].equity
        end = period_snapshots[-1].equity
        if start <= 0:
            skipped.append(period)
            continue
        runs.append(
            PaperTradingRun(
                period=period,
                errors=sum(snapshot.errors for snapshot in period_snapshots),
                paper_return=end / start - 1.0,
                backtest_return=float(backtest_returns[period]),
                **_snapshot_provenance(period_snapshots, "paper_trading"),
            )
        )

    return PaperSnapshotRollup(
        runs=runs,
        missing_backtest_periods=missing,
        skipped_periods=skipped,
    )


def paper_period_for_timestamp(timestamp: str) -> str:
    value = date.fromisoformat(timestamp[:10])
    iso_year, iso_week, _iso_day = value.isocalendar()
    return "%04d-W%02d" % (iso_year, iso_week)


def build_paper_trading_evidence(
    runs: Sequence[PaperTradingRun],
    min_weeks: int = 4,
    max_performance_deviation: float = 0.30,
    enforce_policy: Optional[bool] = None,
):
    if enforce_policy is None:
        enforce_policy = not _is_historical_replay(runs)
    evidence_policy_version = None
    if enforce_policy:
        policy = CURRENT_ACCEPTANCE_POLICY.effective(
            min_paper_weeks=min_weeks,
            max_paper_performance_deviation=max_performance_deviation,
        )
        min_weeks = policy.min_paper_weeks
        max_performance_deviation = policy.max_paper_performance_deviation
        evidence_policy_version = policy.policy_version
    decision = PaperTradingAcceptance(
        min_weeks=min_weeks,
        max_performance_deviation=max_performance_deviation,
    ).evaluate(runs)
    duration_reasons = [
        reason for reason in decision.reasons if reason != "performance_deviation"
    ]
    return {
        "paper_trading_4_weeks": RequirementEvidence(
            "paper_trading_4_weeks",
            passed=not duration_reasons,
            detail="%d paper trading periods, reasons=%s"
            % (decision.weeks, ",".join(decision.reasons)),
            policy_version=evidence_policy_version,
        ),
        "paper_vs_backtest_deviation": RequirementEvidence(
            "paper_vs_backtest_deviation",
            passed=(
                "performance_deviation" not in decision.reasons
                and not any(reason.startswith("invalid_") for reason in decision.reasons)
            ),
            detail="deviation=%.6f reasons=%s"
            % (decision.performance_deviation, ",".join(decision.reasons)),
            policy_version=evidence_policy_version,
        ),
    }


def _paper_run_payload(run: PaperTradingRun) -> dict:
    return {
        "period": run.period,
        "errors": run.errors,
        "paper_return": run.paper_return,
        "backtest_return": run.backtest_return,
        "run_type": run.run_type,
        "observed_at": run.observed_at,
        "dataset_hash": run.dataset_hash,
        "config_hash": run.config_hash,
        "code_hash": run.code_hash,
    }


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_paper_run_values(errors, paper_return, backtest_return) -> None:
    require_nonnegative_integer(errors, "errors")
    require_finite_number(paper_return, "paper_return")
    require_finite_number(backtest_return, "backtest_return")


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
