"""General diagnostics, scheduling, evidence, and audit commands."""

import json

from ..audit import (
    CompletionAudit,
    RequirementEvidence,
    load_evidence_file,
    parse_evidence_passed,
)
from ..evidence import EvidenceRecorder, hash_payload
from ..live_smoke import LiveSmokeCheck, LiveSmokeGate
from ..preflight import RuntimePreflight
from ..runtime import build_runtime_evidence
from ..scheduler import AcceptanceCycleSchedulePlan, DailySchedulePlan
from .context import CLIContext
from .support import (
    append_evidence,
    default_import_checker,
    default_runtime_smoke_suite_factory,
    format_preflight,
)


def handle_preflight(args, context: CLIContext) -> int:
    report = RuntimePreflight(
        environ=context.environ,
        import_checker=context.import_checker,
    ).check()
    payload = {
        "ready": report.ready,
        "missing_env": report.missing_env,
        "missing_packages": report.missing_packages,
    }
    output = json.dumps(payload, sort_keys=True) if args.as_json else format_preflight(payload)
    context.stdout.write(output + "\n")
    return 0 if report.ready else 1


def handle_schedule(_args, context: CLIContext) -> int:
    plan = DailySchedulePlan.after_us_close()
    context.stdout.write(
        "timezone=%s run_time=%s steps=%s\n"
        % (plan.timezone, plan.run_time, ",".join(plan.steps))
    )
    return 0


def handle_acceptance_schedule(args, context: CLIContext) -> int:
    plan = AcceptanceCycleSchedulePlan.after_us_close(
        evidence_file=args.evidence_file,
        paper_snapshot_log=args.paper_snapshot_log,
        paper_runs_log=args.paper_runs_log,
        live_snapshot_log=args.live_snapshot_log,
        live_runs_log=args.live_runs_log,
    )
    payload = {
        "timezone": plan.timezone,
        "run_time": plan.run_time,
        "steps": plan.steps,
        "command": plan.command,
    }
    if args.as_json:
        output = json.dumps(payload, sort_keys=True)
    else:
        output = "timezone=%s run_time=%s steps=%s command=%s" % (
            plan.timezone,
            plan.run_time,
            ",".join(plan.steps),
            plan.command,
        )
    context.stdout.write(output + "\n")
    return 0


def handle_live_smoke(args, context: CLIContext) -> int:
    raw_checks = json.loads(args.checks_json)
    checks = [
        LiveSmokeCheck(
            item["name"],
            lambda passed=item["passed"]: parse_evidence_passed(passed),
        )
        for item in raw_checks
    ]
    result = LiveSmokeGate(checks).run()
    if args.evidence_file:
        append_evidence(
            args.evidence_file,
            requirement_id="live_api_smoke",
            passed=result.ready,
            detail="passed=%s failed=%s" % (",".join(result.passed), ",".join(result.failed)),
            run_type="live_api_smoke",
            dataset_hash=hash_payload({"passed": result.passed, "failed": result.failed}),
            config_hash=hash_payload({"checks": raw_checks}),
        )
    context.stdout.write(
        json.dumps(
            {"ready": result.ready, "passed": result.passed, "failed": result.failed},
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if result.ready else 1


def handle_completion_audit(args, context: CLIContext) -> int:
    evidence = _load_completion_evidence(args, context)
    if evidence is None:
        return 1
    report = CompletionAudit.design_default().evaluate(evidence)
    context.stdout.write(
        json.dumps(
            {
                "complete": report.complete,
                "passed_ids": report.passed_ids,
                "failed_ids": report.failed_ids,
                "missing_ids": report.missing_ids,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if report.complete else 1


def _load_completion_evidence(args, context):
    if args.evidence_file:
        try:
            return load_evidence_file(args.evidence_file)
        except FileNotFoundError:
            context.stdout.write(
                json.dumps(
                    {"error": "evidence_file_not_found", "path": args.evidence_file},
                    sort_keys=True,
                )
                + "\n"
            )
            return None
    return {
        requirement_id: RequirementEvidence(
            requirement_id=requirement_id,
            passed=parse_evidence_passed(item.get("passed")),
            detail=item.get("detail", ""),
            run_type=item.get("run_type"),
            observed_at=item.get("observed_at"),
            dataset_hash=item.get("dataset_hash"),
            config_hash=item.get("config_hash"),
            cost_model_hash=item.get("cost_model_hash"),
            code_hash=item.get("code_hash"),
            policy_version=item.get("policy_version"),
            input_materialization_hash=item.get("input_materialization_hash"),
            output_hash=item.get("output_hash"),
            report_hash=item.get("report_hash"),
            cost_suite_hash=item.get("cost_suite_hash"),
            run_id=item.get("run_id"),
            experiment_id=item.get("experiment_id"),
        )
        for requirement_id, item in json.loads(args.evidence_json).items()
    }


def handle_collect_evidence(args, context: CLIContext) -> int:
    recorder = EvidenceRecorder(args.evidence_file)
    if args.offline_tests_detail:
        recorder.record("offline_tests", True, args.offline_tests_detail)
    checker = context.import_checker or default_import_checker
    results = [_collect_import_evidence(item, checker, recorder) for item in args.import_check]
    passed = [requirement_id for requirement_id, ok in results if ok]
    failed = [requirement_id for requirement_id, ok in results if not ok]
    context.stdout.write(
        json.dumps({"ready": not failed, "passed": passed, "failed": failed}, sort_keys=True)
        + "\n"
    )
    return 0 if not failed else 1


def _collect_import_evidence(item, checker, recorder):
    requirement_id, package_name = item.split(":", 1)
    ok = bool(checker(package_name))
    recorder.record(
        requirement_id,
        ok,
        "%s import %s" % (package_name, "ok" if ok else "missing"),
    )
    return requirement_id, ok


def handle_runtime_evidence(args, context: CLIContext) -> int:
    evidence = build_runtime_evidence(import_checker=context.import_checker)
    return _record_runtime_evidence(args.evidence_file, evidence, context)


def handle_runtime_smoke(args, context: CLIContext) -> int:
    suite_factory = (
        context.runtime_smoke_suite_factory or default_runtime_smoke_suite_factory
    )
    return _record_runtime_evidence(args.evidence_file, suite_factory().run_all(), context)


def _record_runtime_evidence(path, evidence, context) -> int:
    recorder = EvidenceRecorder(path)
    passed = []
    failed = []
    for item in evidence.values():
        recorder.record(item.requirement_id, item.passed, item.detail)
        (passed if item.passed else failed).append(item.requirement_id)
    context.stdout.write(
        json.dumps({"ready": not failed, "passed": passed, "failed": failed}, sort_keys=True)
        + "\n"
    )
    return 0 if not failed else 1
