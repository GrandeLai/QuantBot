"""End-to-end acceptance-cycle orchestration."""

import json

from ..acceptance_policy import CURRENT_ACCEPTANCE_POLICY
from ..audit import CompletionAudit, load_evidence_file
from ..evidence import EvidenceRecorder
from ..live import (
    LiveEquitySnapshotJournal,
    LiveTradingJournal,
    build_live_trading_evidence,
    rollup_live_snapshots,
)
from ..paper import (
    PaperAccountSnapshotJournal,
    PaperTradingJournal,
    build_paper_trading_evidence,
    rollup_paper_snapshots,
)
from .context import CLIContext
from .support import (
    journal_provenance,
    live_status_payload,
    paper_status_payload,
    resolve_live_account_value_loader,
    resolve_paper_account_value_loader,
    utc_now_iso,
)


def handle_acceptance_cycle(args, context: CLIContext) -> int:
    recorder = EvidenceRecorder(args.evidence_file)
    policy = _acceptance_policy(args)
    paper_snapshot = _capture_paper_snapshot(args, context)
    paper_rollup = _rollup_paper(args)
    paper_status = _record_paper_acceptance(args, policy, recorder)
    live_snapshot = _capture_live_snapshot(args, context)
    live_rollup, live_status = _record_live_acceptance(args, policy, recorder)
    audit_report = CompletionAudit.design_default().evaluate(
        load_evidence_file(args.evidence_file)
    )
    payload = _acceptance_payload(
        audit_report,
        paper_snapshot,
        paper_rollup,
        paper_status,
        live_snapshot,
        live_rollup,
        live_status,
    )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _acceptance_policy(args):
    return CURRENT_ACCEPTANCE_POLICY.effective(
        min_paper_weeks=args.min_paper_weeks,
        max_paper_performance_deviation=args.max_paper_performance_deviation,
        min_live_sessions=args.min_live_sessions,
        max_live_drawdown=args.max_live_drawdown,
    )


def _capture_paper_snapshot(args, context):
    if args.skip_paper_snapshot:
        return None
    timestamp = args.paper_timestamp or utc_now_iso()
    equity = args.paper_equity
    if equity is None:
        loader = context.paper_account_value_loader or resolve_paper_account_value_loader(
            args, context.environ
        )
        equity = loader()
    PaperAccountSnapshotJournal(args.paper_snapshot_log).append(
        timestamp=timestamp,
        equity=equity,
        errors=0,
        notes="acceptance-cycle",
    )
    return {"timestamp": timestamp, "equity": equity}


def _rollup_paper(args):
    current_runs = PaperTradingJournal(args.paper_runs_log).read_all()
    payload = {"written": False, "runs": len(current_runs)}
    if not args.paper_backtest_returns_json:
        return payload
    backtest_returns = {
        str(period): float(value)
        for period, value in json.loads(args.paper_backtest_returns_json).items()
    }
    rollup = rollup_paper_snapshots(
        PaperAccountSnapshotJournal(args.paper_snapshot_log).read_all(),
        backtest_returns=backtest_returns,
        min_snapshots_per_period=args.paper_min_snapshots_per_period,
    )
    payload = {
        "written": not rollup.missing_backtest_periods,
        "runs": len(rollup.runs),
        "missing_backtest_periods": rollup.missing_backtest_periods,
        "skipped_periods": rollup.skipped_periods,
    }
    if not rollup.missing_backtest_periods:
        PaperTradingJournal(args.paper_runs_log).write_all(rollup.runs)
    return payload


def _record_paper_acceptance(args, policy, recorder):
    runs = PaperTradingJournal(args.paper_runs_log).read_all()
    evidence = build_paper_trading_evidence(
        runs,
        min_weeks=policy.min_paper_weeks,
        max_performance_deviation=policy.max_paper_performance_deviation,
        enforce_policy=True,
    )
    provenance = journal_provenance(runs, "paper_trading", policy.paper_config())
    _record_evidence_set(recorder, evidence, provenance)
    return paper_status_payload(
        runs,
        min_weeks=policy.min_paper_weeks,
        max_performance_deviation=policy.max_paper_performance_deviation,
    )


def _capture_live_snapshot(args, context):
    if not (args.capture_live_snapshot or args.live_equity is not None):
        return None
    timestamp = args.live_timestamp or utc_now_iso()
    equity = args.live_equity
    if equity is None:
        equity = resolve_live_account_value_loader(args, context.environ)()
    LiveEquitySnapshotJournal(args.live_snapshot_log).append(
        timestamp=timestamp,
        equity=equity,
        errors=0,
        notes="acceptance-cycle",
    )
    return {"timestamp": timestamp, "equity": equity}


def _record_live_acceptance(args, policy, recorder):
    rollup = rollup_live_snapshots(
        LiveEquitySnapshotJournal(args.live_snapshot_log).read_all(),
        baseline_equity=args.live_baseline_equity,
        min_snapshots_per_day=args.live_min_snapshots_per_day,
    )
    LiveTradingJournal(args.live_runs_log).write_all(rollup.sessions)
    runs = LiveTradingJournal(args.live_runs_log).read_all()
    evidence = build_live_trading_evidence(
        runs,
        min_sessions=policy.min_live_sessions,
        max_drawdown=policy.max_live_drawdown,
        enforce_policy=True,
    )
    provenance = journal_provenance(runs, "live_trading", policy.live_config())
    _record_evidence_set(recorder, evidence, provenance)
    status = live_status_payload(
        runs,
        min_sessions=policy.min_live_sessions,
        max_drawdown=policy.max_live_drawdown,
    )
    return rollup, status


def _record_evidence_set(recorder, evidence, provenance):
    for item in evidence.values():
        recorder.record(
            item.requirement_id,
            item.passed,
            item.detail,
            policy_version=item.policy_version,
            **provenance,
        )


def _acceptance_payload(
    audit_report,
    paper_snapshot,
    paper_rollup,
    paper_status,
    live_snapshot,
    live_rollup,
    live_status,
):
    return {
        "complete": audit_report.complete,
        "paper": {
            "snapshot": paper_snapshot,
            "rollup": paper_rollup,
            "status": paper_status,
        },
        "live": {
            "snapshot": live_snapshot,
            "rollup": {
                "sessions": len(live_rollup.sessions),
                "skipped_days": live_rollup.skipped_days,
            },
            "status": live_status,
        },
        "audit": {
            "passed_ids": audit_report.passed_ids,
            "failed_ids": audit_report.failed_ids,
            "missing_ids": audit_report.missing_ids,
        },
    }
