"""Live-soak journal and evidence command handlers."""

import json

from ..acceptance_policy import CURRENT_ACCEPTANCE_POLICY
from ..evidence import EvidenceRecorder
from ..live import (
    LiveEquitySnapshotJournal,
    LiveTradingJournal,
    build_live_trading_evidence,
    rollup_live_snapshots,
)
from .context import CLIContext
from .support import (
    journal_provenance,
    live_status_payload,
    resolve_live_account_value_loader,
    utc_now_iso,
    write_record_error,
)


def handle_live_soak_record(args, context: CLIContext) -> int:
    try:
        LiveTradingJournal(args.log).append(
            day=args.day,
            errors=args.errors,
            drawdown=args.drawdown,
            notes=args.notes,
        )
    except ValueError as error:
        return write_record_error(context.stdout, error)
    context.stdout.write(json.dumps({"recorded": True, "log": args.log}, sort_keys=True) + "\n")
    return 0


def handle_live_soak_snapshot_record(args, context: CLIContext) -> int:
    timestamp = args.timestamp or utc_now_iso()
    equity = args.equity
    if equity is None:
        equity = resolve_live_account_value_loader(args, context.environ)()
    try:
        LiveEquitySnapshotJournal(args.log).append(
            timestamp=timestamp,
            equity=equity,
            errors=args.errors,
            notes=args.notes,
        )
    except ValueError as error:
        return write_record_error(context.stdout, error)
    context.stdout.write(
        json.dumps(
            {
                "recorded": True,
                "log": args.log,
                "timestamp": timestamp,
                "equity": equity,
                "errors": args.errors,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def handle_live_soak_rollup_snapshots(args, context: CLIContext) -> int:
    rollup = rollup_live_snapshots(
        LiveEquitySnapshotJournal(args.snapshot_log).read_all(),
        baseline_equity=args.baseline_equity,
        min_snapshots_per_day=args.min_snapshots_per_day,
    )
    LiveTradingJournal(args.runs_log).write_all(rollup.sessions)
    context.stdout.write(
        json.dumps(
            {
                "written": True,
                "sessions": len(rollup.sessions),
                "runs_log": args.runs_log,
                "skipped_days": rollup.skipped_days,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


def handle_live_soak_evidence(args, context: CLIContext) -> int:
    runs = LiveTradingJournal(args.log).read_all()
    policy = CURRENT_ACCEPTANCE_POLICY.effective(
        min_live_sessions=args.min_sessions,
        max_live_drawdown=args.max_drawdown,
    )
    evidence = build_live_trading_evidence(
        runs,
        min_sessions=policy.min_live_sessions,
        max_drawdown=policy.max_live_drawdown,
        enforce_policy=True,
    )
    recorder = EvidenceRecorder(args.evidence_file)
    provenance = journal_provenance(runs, "live_trading", policy.live_config())
    for item in evidence.values():
        recorder.record(
            item.requirement_id,
            item.passed,
            item.detail,
            policy_version=item.policy_version,
            **provenance,
        )
    payload = {
        "passed": evidence["live_trading_soak"].passed,
        "detail": evidence["live_trading_soak"].detail,
    }
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["passed"] else 1


def handle_live_soak_status(args, context: CLIContext) -> int:
    payload = live_status_payload(
        LiveTradingJournal(args.log).read_all(),
        min_sessions=args.min_sessions,
        max_drawdown=args.max_drawdown,
    )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["passed"] else 1
