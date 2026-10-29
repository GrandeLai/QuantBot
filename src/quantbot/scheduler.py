"""Daily automation schedule definitions."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class DailySchedulePlan:
    timezone: str
    run_time: str
    steps: Tuple[str, ...]

    @classmethod
    def after_us_close(cls):
        return cls(
            timezone="US/Eastern",
            run_time="16:30",
            steps=("data_update", "factor_compute", "signal_generate", "rebalance"),
        )


@dataclass(frozen=True)
class AcceptanceCycleSchedulePlan:
    timezone: str
    run_time: str
    command: str
    steps: Tuple[str, ...]

    @classmethod
    def after_us_close(
        cls,
        evidence_file: str = "data/evidence.jsonl",
        paper_snapshot_log: str = "data/paper_snapshots.jsonl",
        paper_runs_log: str = "data/paper_runs.jsonl",
        live_snapshot_log: str = "data/live_snapshots.jsonl",
        live_runs_log: str = "data/live_runs.jsonl",
    ):
        command = (
            "python3 -m quantbot.cli acceptance-cycle "
            "--evidence-file {evidence_file} "
            "--paper-snapshot-log {paper_snapshot_log} "
            "--paper-runs-log {paper_runs_log} "
            "--live-snapshot-log {live_snapshot_log} "
            "--live-runs-log {live_runs_log}"
        ).format(
            evidence_file=evidence_file,
            paper_snapshot_log=paper_snapshot_log,
            paper_runs_log=paper_runs_log,
            live_snapshot_log=live_snapshot_log,
            live_runs_log=live_runs_log,
        )
        return cls(
            timezone="US/Eastern",
            run_time="16:45",
            command=command,
            steps=(
                "capture_paper_snapshot",
                "rollup_paper_runs",
                "rollup_live_soak",
                "record_evidence",
                "completion_audit",
            ),
        )


@dataclass(frozen=True)
class DayTradeSchedulePlan:
    timezone: str
    run_time: str
    command: str
    steps: Tuple[str, ...]

    @classmethod
    def intraday_paper(
        cls,
        trades_log: str = "data/daytrade_trades.jsonl",
        daily_log: str = "data/daytrade_daily.jsonl",
        evidence_file: str = "data/daytrade_evidence.jsonl",
    ):
        command = (
            "python3 -m quantbot.cli daytrade-paper-cycle "
            "--trades-log {trades_log} "
            "--daily-log {daily_log} "
            "--evidence-file {evidence_file}"
        ).format(
            trades_log=trades_log,
            daily_log=daily_log,
            evidence_file=evidence_file,
        )
        return cls(
            timezone="US/Eastern",
            run_time="09:45-15:50",
            command=command,
            steps=(
                "fetch_minute_bars",
                "generate_intraday_signals",
                "simulate_daytrade_orders",
                "write_daytrade_logs",
                "record_daytrade_status",
            ),
        )


@dataclass(frozen=True)
class MonthlyResearchSchedulePlan:
    timezone: str
    day: int
    run_time: str
    steps: Tuple[str, ...]

    @classmethod
    def rd_agent_discovery(cls):
        return cls(
            timezone="US/Eastern",
            day=1,
            run_time="18:00",
            steps=("rd_agent_factor_discovery", "factor_admission", "candidate_pool_refresh"),
        )
