"""Paper-trading command handlers."""

import json

from ..acceptance_policy import CURRENT_ACCEPTANCE_POLICY
from ..evidence import EvidenceRecorder
from ..paper import (
    PaperAccountSnapshotJournal,
    PaperTradingAcceptance,
    PaperTradingJournal,
    PaperTradingRun,
    build_paper_trading_evidence,
    rollup_paper_snapshots,
)
from .context import CLIContext
from .support import (
    journal_provenance,
    paper_status_payload,
    resolve_paper_account_value_loader,
    utc_now_iso,
    write_record_error,
)


def handle_paper_acceptance(args, context: CLIContext) -> int:
    runs = _load_paper_runs(args)
    decision = PaperTradingAcceptance().evaluate(runs)
    context.stdout.write(
        json.dumps(
            {
                "passed": decision.passed,
                "weeks": decision.weeks,
                "performance_deviation": decision.performance_deviation,
                "reasons": decision.reasons,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if decision.passed else 1


def _load_paper_runs(args):
    if args.runs_log:
        return PaperTradingJournal(args.runs_log).read_all()
    return [
        PaperTradingRun(
            period=item["period"],
            errors=int(item["errors"]),
            paper_return=float(item["paper_return"]),
            backtest_return=float(item["backtest_return"]),
        )
        for item in json.loads(args.runs_json)
    ]


def handle_paper_run_record(args, context: CLIContext) -> int:
    try:
        PaperTradingJournal(args.log).append(
            period=args.period,
            errors=args.errors,
            paper_return=args.paper_return,
            backtest_return=args.backtest_return,
        )
    except ValueError as error:
        return write_record_error(context.stdout, error)
    context.stdout.write(json.dumps({"recorded": True, "log": args.log}, sort_keys=True) + "\n")
    return 0


def handle_paper_snapshot_record(args, context: CLIContext) -> int:
    timestamp = args.timestamp or utc_now_iso()
    equity = _paper_equity(args, context)
    try:
        PaperAccountSnapshotJournal(args.log).append(
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


def _paper_equity(args, context):
    if args.equity is not None:
        return args.equity
    loader = context.paper_account_value_loader or resolve_paper_account_value_loader(
        args, context.environ
    )
    return loader()


def handle_paper_rollup_snapshots(args, context: CLIContext) -> int:
    backtest_returns = {
        str(period): float(value)
        for period, value in json.loads(args.backtest_returns_json).items()
    }
    rollup = rollup_paper_snapshots(
        PaperAccountSnapshotJournal(args.snapshot_log).read_all(),
        backtest_returns=backtest_returns,
        min_snapshots_per_period=args.min_snapshots_per_period,
    )
    if rollup.missing_backtest_periods:
        payload = {
            "written": False,
            "runs": len(rollup.runs),
            "missing_backtest_periods": rollup.missing_backtest_periods,
            "skipped_periods": rollup.skipped_periods,
        }
        exit_code = 1
    else:
        PaperTradingJournal(args.runs_log).write_all(rollup.runs)
        payload = {
            "written": True,
            "runs": len(rollup.runs),
            "runs_log": args.runs_log,
            "skipped_periods": rollup.skipped_periods,
        }
        exit_code = 0
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return exit_code


def handle_paper_evidence(args, context: CLIContext) -> int:
    runs = PaperTradingJournal(args.log).read_all()
    policy = CURRENT_ACCEPTANCE_POLICY
    evidence = build_paper_trading_evidence(runs, enforce_policy=True)
    recorder = EvidenceRecorder(args.evidence_file)
    provenance = journal_provenance(runs, "paper_trading", policy.paper_config())
    for item in evidence.values():
        recorder.record(
            item.requirement_id,
            item.passed,
            item.detail,
            policy_version=item.policy_version,
            **provenance,
        )
    passed = all(item.passed for item in evidence.values())
    context.stdout.write(
        json.dumps(
            {
                "passed": passed,
                "requirements": {
                    requirement_id: item.passed
                    for requirement_id, item in evidence.items()
                },
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if passed else 1


def handle_paper_status(args, context: CLIContext) -> int:
    payload = paper_status_payload(
        PaperTradingJournal(args.log).read_all(),
        min_weeks=args.min_weeks,
        max_performance_deviation=args.max_performance_deviation,
    )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["passed"] else 1
