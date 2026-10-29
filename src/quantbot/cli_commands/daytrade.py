"""Intraday strategy command handlers."""

import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..audit import RequirementEvidence
from ..acceptance_policy import ACCEPTANCE_POLICY_VERSION
from ..daytrade import (
    DAYTRADE_MINUTE_CACHE_SCHEMA_VERSION,
    DayTradeConfig,
    DayTradeDailyJournal,
    DayTradeMinuteCache,
    DayTradeTradeJournal,
    build_daytrade_historical_evidence,
    build_daytrade_soak_evidence,
    build_daytrade_walk_forward_evidence,
    compare_daytrade_configs,
    daytrade_candidate_feature_requirements,
    daytrade_candidate_requires_features,
    daytrade_config_from_payload,
    daytrade_config_to_payload,
    daytrade_h3_candidate_configs,
    daytrade_h6_candidate_configs,
    daytrade_h7_candidate_configs,
    daytrade_h8_candidate_configs,
    daytrade_h9_candidate_configs,
    evaluate_daytrade_config,
    minute_bar_from_row,
    run_daytrade_walk_forward_optimization,
    summarize_daytrade_status,
)
from ..evidence import EvidenceRecorder, hash_payload, source_tree_hash
from ..exposure import ExposureLedger
from ..experiment_reporting import CostScenario, CostSuite, ExperimentRunReport
from ..formal_evidence import (
    admission_evidence_payload,
    materialize_completed_strategy_admission,
)
from ..experiment_dispositions import fixed_experiment_disposition
from ..experiment_execution import (
    ExperimentExecutionIdentity,
    ExperimentExecutionLedger,
)
from ..daytrade_strategy import (
    IntradayFeatureRequirements,
    build_intraday_feature_snapshots,
    preregistered_daytrade_h3_experiments,
    preregistered_daytrade_h6_experiments,
    preregistered_daytrade_h7_experiments,
    preregistered_daytrade_h8_experiments,
    preregistered_daytrade_experiments,
    preregistered_daytrade_global_experiments,
)
from ..daytrade_validation_artifact import (
    DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    DayTradeAdaptiveValidationArtifact,
    load_daytrade_adaptive_validation_artifact,
    trusted_daytrade_experiment_keys,
)
from ..scheduler import DayTradeSchedulePlan
from ..strategy_admission import (
    DayTradeAdmissionMetrics,
    EvaluationIdentity,
    build_daytrade_admission_evidence,
    evaluate_daytrade_admission,
)
from ..trading import RunManifest
from ..validation_protocol import DAYTRADE_VALIDATION_PROTOCOL
from .context import CLIContext
from .daytrade_support import (
    claim_daytrade_static_data_access,
    daytrade_data_access_payload,
    daytrade_config_from_args,
    daytrade_feature_metadata,
    daytrade_optimization_payload,
    daytrade_result_payload,
    daytrade_runs_hash,
    load_daytrade_bars_from_args,
    load_daytrade_bars_for_symbols_from_args,
    load_daytrade_feature_inputs_from_args,
    minute_bars_hash,
    parse_daytrade_symbols,
    prepare_daytrade_data_access,
    resolve_daytrade_expected_sessions,
    resolve_daytrade_minute_loader,
    run_daytrade_from_args,
    write_json_file,
)
from .support import parse_date


_DAYTRADE_EXPERIMENT_SET_IDS = {
    "h1-h4": (
        "daytrade-h1-session-vwap",
        "daytrade-h2-opening-window",
        "daytrade-h3-pre-entry-confirmation",
        "daytrade-h4-risk-scale",
    ),
    "h3-rvol": ("daytrade-h3-pre-entry-confirmation",),
    "h6-market": ("daytrade-h6-market-confirmation",),
    "h7-rvol-window": ("daytrade-h7-rvol-entry-window",),
    "h8-rvol-risk": ("daytrade-h8-rvol-risk-scale",),
    "h9-rvol-reward-risk": ("daytrade-h9-rvol-reward-risk",),
}


def handle_daytrade_schedule(args, context: CLIContext) -> int:
    plan = DayTradeSchedulePlan.intraday_paper(
        trades_log=args.trades_log,
        daily_log=args.daily_log,
        evidence_file=args.evidence_file,
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


def handle_daytrade_cache_fill(args, context: CLIContext) -> int:
    config = DayTradeConfig()
    access = prepare_daytrade_data_access(
        args,
        config,
        config_identity={
            "command": "daytrade-cache-fill",
            "daytrade": daytrade_config_to_payload(config),
        },
    )
    if access.universe_schedule is not None:
        bars, source_name = load_daytrade_bars_for_symbols_from_args(
            args,
            context.environ,
            context.daytrade_minute_loader,
            access.symbols,
            parse_date(args.start),
            parse_date(args.end),
            context.daytrade_session_loader,
            data_access=access,
        )
        payload = {
            "source": source_name,
            "symbols": list(access.symbols),
            "bars": len(bars),
            "files": [],
            "cache_dir": args.cache_dir,
            **daytrade_data_access_payload(access),
        }
        context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0
    symbols = parse_daytrade_symbols(args.symbols, config)
    request_start = parse_date(args.start)
    request_end = parse_date(args.end)
    loader, source_name = resolve_daytrade_minute_loader(
        args, context.environ, context.daytrade_minute_loader
    )
    expected_sessions = resolve_daytrade_expected_sessions(
        args,
        context.environ,
        context.daytrade_session_loader,
        source_name,
        request_start,
        request_end,
    )
    rows = loader(symbols=symbols, start=request_start, end=request_end)
    bars = [minute_bar_from_row(row) for row in rows]
    manifest_sessions = expected_sessions
    if manifest_sessions is None:
        manifest_sessions = sorted({bar.timestamp.date() for bar in bars})
    cache = DayTradeMinuteCache(Path(args.cache_dir), source_name)
    written = cache.write_all(
        bars,
        request_start=request_start,
        request_end=request_end,
        expected_sessions=manifest_sessions,
    )
    payload = {
        "source": source_name,
        "symbols": list(symbols),
        "bars": len(bars),
        "files": [str(path) for path in written],
        "cache_dir": args.cache_dir,
    }
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def handle_daytrade_backtest_run(args, context: CLIContext) -> int:
    result, source_name, access, actual_input_hash = _run_daytrade(args, context)
    DayTradeTradeJournal(args.trades_log).write_all(result.trades)
    DayTradeDailyJournal(args.daily_log).write_all(result.daily_results)
    evidence = build_daytrade_historical_evidence(result)
    config = daytrade_config_from_args(args)
    dataset_hash = _evidence_dataset_hash(access, actual_input_hash)
    input_materialization_hash = _daytrade_input_materialization_hash(
        actual_input_hash,
        access,
    )
    output_hash = daytrade_runs_hash(result.daily_results, result.trades)
    config_hash = (
        access.permit.config_hash
        if access.permit is not None
        else hash_payload(_daytrade_strategy_config_payload(config))
    )
    cost_model_hash = hash_payload(
        {
            "execution": "next_available_minute_event",
            "cost_bps_per_side": config.cost_bps,
            "max_volume_participation": config.max_volume_participation,
        }
    )
    if access.permit is not None:
        cost_model_hash = access.permit.cost_model_hash
    code_hash = (
        access.permit.code_hash
        if access.permit is not None
        else source_tree_hash()
    )
    run_id = str(getattr(args, "run_id", "") or "").strip() or (
        "daytrade-legacy-%s-%s-%s"
        % (args.start, args.end, input_materialization_hash[:12])
    )
    payload = daytrade_result_payload(
        result=result,
        source_name=source_name,
        evidence=evidence,
        logs={
            "trades_log": args.trades_log,
            "daily_log": args.daily_log,
            "evidence_file": args.evidence_file,
        },
    )
    payload.update(daytrade_data_access_payload(access))
    cost_suite = CostSuite(
        (
            CostScenario(
                "configured_cost",
                config.cost_bps,
                "baseline",
            ),
        )
    )
    metrics = _finite_json_payload(
        {
            key: value
            for key, value in payload.items()
            if key not in {"logs", "data_access", "permit"}
        }
    )
    report = ExperimentRunReport(
        experiment_id="daytrade-legacy-orb-smoke",
        run_id=run_id,
        run_type="historical_backtest",
        observed_at=datetime.now(timezone.utc),
        dataset_hash=dataset_hash,
        input_materialization_hash=input_materialization_hash,
        config_hash=config_hash,
        code_hash=code_hash,
        cost_model_hash=cost_model_hash,
        cost_suite=cost_suite,
        output_hash=output_hash,
        metrics=metrics,
        admission={
            "passed": False,
            "formal_admission_eligible": False,
            "historical_evidence_passed": evidence.passed,
            "reasons": ["legacy_fixed_universe_development_smoke"],
        },
    )
    payload.update(
        {
            "dataset_hash": dataset_hash,
            "input_materialization_hash": input_materialization_hash,
            "output_hash": output_hash,
            "config_hash": config_hash,
            "cost_model_hash": cost_model_hash,
            "code_hash": code_hash,
            "cost_suite_hash": cost_suite.suite_hash,
            "report_hash": report.report_hash,
            "experiment_run_report": report.to_payload(),
        }
    )
    EvidenceRecorder(args.evidence_file).record(
        evidence.requirement_id,
        evidence.passed,
        "source=%s %s" % (source_name, evidence.detail),
        run_type="historical_backtest",
        dataset_hash=dataset_hash,
        input_materialization_hash=input_materialization_hash,
        output_hash=output_hash,
        report_hash=report.report_hash,
        cost_suite_hash=cost_suite.suite_hash,
        config_hash=config_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
        run_id=run_id,
        experiment_id="daytrade-legacy-orb-smoke",
    )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if evidence.passed else 1


def handle_daytrade_optimize_run(args, context: CLIContext) -> int:
    config = daytrade_config_from_args(args)
    effective_max_candidates = _effective_daytrade_max_candidates(args)
    (
        optimization_base_config,
        experiment_registry,
        candidate_configs,
        feature_requirements,
    ) = _daytrade_experiment_plan(args, config)
    experiment_governance = _daytrade_experiment_governance(
        args,
        experiment_registry,
    )
    _require_exact_daytrade_experiment_window(args)
    adaptive_validation = _resolve_h9_adaptive_validation_artifact(
        args,
        context.environ,
    )

    if args.feature_warmup_days <= 0:
        raise ValueError("feature_warmup_days must be a positive integer")
    warmup_start = parse_date(args.start) - timedelta(
        days=args.feature_warmup_days
    )
    optimization_protocol = _daytrade_optimization_protocol(
        args,
        effective_max_candidates=effective_max_candidates,
    )
    config_identity = _daytrade_optimization_config_identity(
        args,
        optimization_base_config=optimization_base_config,
        candidate_configs=candidate_configs,
        feature_requirements=feature_requirements,
        optimization_protocol=optimization_protocol,
        experiment_governance=experiment_governance,
        adaptive_validation=adaptive_validation,
    )
    access = prepare_daytrade_data_access(
        args,
        optimization_base_config,
        warmup_start=warmup_start,
        config_identity=config_identity,
        cost_identity=_daytrade_optimization_cost_identity(
            args,
            optimization_base_config,
        ),
    )
    execution_ledger, execution_identity, resumed_exit_code = (
        _prepare_h9_execution(args, access, context.stdout)
    )
    if resumed_exit_code is not None:
        return resumed_exit_code
    (
        bars,
        feature_bars,
        source_name,
        feature_config,
        warmup_start,
    ) = load_daytrade_feature_inputs_from_args(
        args,
        context.environ,
        context.daytrade_minute_loader,
        optimization_base_config,
        context.daytrade_session_loader,
        data_access=access,
        feature_requirements=feature_requirements,
    )
    feature_snapshots = build_intraday_feature_snapshots(
        feature_bars,
        feature_config,
    )
    result = run_daytrade_walk_forward_optimization(
        bars=bars,
        start=parse_date(args.start),
        end=parse_date(args.end),
        base_config=optimization_base_config,
        train_months=args.train_months,
        validation_months=args.validation_months,
        max_candidates=effective_max_candidates,
        feature_snapshots=feature_snapshots,
        experiment_registry=experiment_registry,
        candidate_configs=candidate_configs,
        universe_schedule=access.universe_schedule,
        historical_trial_artifact=access.historical_trial_artifact,
        adaptive_validation_artifact=adaptive_validation,
        evaluation_dataset_hash=(
            access.permit.dataset_hash if access.permit is not None else None
        ),
    )
    evidence = build_daytrade_walk_forward_evidence(result)
    feature_metadata = daytrade_feature_metadata(
        feature_config,
        feature_bars,
        feature_snapshots,
        warmup_start,
    )
    payload = daytrade_optimization_payload(
        result,
        source_name,
        evidence,
        feature_metadata,
    )
    payload["experiment_set"] = args.experiment_set
    payload.update(experiment_governance)
    fixed_evaluation = None
    if args.experiment_set == "h9-rvol-reward-risk":
        fixed_evaluation = _h9_fixed_candidate_evaluation(result)
        payload["fixed_candidate_evaluation"] = fixed_evaluation
        payload["decision_basis"] = "fixed_h3_baseline_vs_fixed_2r_candidate"
    payload.update(daytrade_data_access_payload(access))
    if fixed_evaluation is not None:
        admission = _h9_admission_semantics(fixed_evaluation, access)
        payload.update(admission)
        is_sealed = access.permit is not None and access.permit.mode == "sealed"
        payload["requirement_id"] = (
            "daytrade_strategy_admission"
            if is_sealed
            else "daytrade_h9_fixed_candidate_survival"
        )
        evidence = RequirementEvidence(
            payload["requirement_id"],
            (
                admission["formal_admission_eligible"]
                if is_sealed
                else admission["candidate_survived"]
            ),
            "reasons=" + ",".join(admission["admission_reasons"]),
        )
    reporting = _attach_daytrade_optimization_report(
        args,
        access,
        payload,
        fixed_evaluation=fixed_evaluation,
        optimization_base_config=optimization_base_config,
        config_hash=hash_payload(config_identity),
        bars=bars,
        feature_metadata=feature_metadata,
        fallback_run_type=experiment_governance["run_type"],
    )
    if reporting["formal_sealed"]:
        canonical_execution_artifact = _build_h9_canonical_execution_artifact(
            args,
            result,
            adaptive_validation=adaptive_validation,
            optimization_base_config=optimization_base_config,
        )
        payload["canonical_execution_artifact"] = (
            canonical_execution_artifact
        )
        payload["canonical_execution_artifact_hash"] = hash_payload(
            canonical_execution_artifact
        )
    exit_code = 0 if evidence.passed else 1
    if execution_ledger is not None and execution_identity is not None:
        completed_evidence = _completed_daytrade_admission_evidence(
            args,
            access,
            evidence,
            fixed_evaluation=fixed_evaluation,
            source_name=source_name,
            run_type=reporting["run_type"],
            actual_dataset_hash=reporting["input_materialization_hash"],
            actual_config_hash=hash_payload(config_identity),
            formal_sealed=reporting["formal_sealed"],
            output_hash=reporting["output_hash"],
            report_hash=reporting["report_hash"],
            cost_suite_hash=reporting["cost_suite_hash"],
            experiment_id=reporting["experiment_id"],
            observed_at=payload["experiment_run_report"]["observed_at"],
        )
        payload["admission_evidence"] = admission_evidence_payload(
            completed_evidence
        )
        payload["admission_evidence_hash"] = hash_payload(
            payload["admission_evidence"]
        )
        payload["admission_evidence_execution_identity_hash"] = (
            execution_identity.identity_hash
        )
        payload["permit_record_hash"] = access.permit.record_hash
        payload = execution_ledger.complete(
            execution_identity,
            run_id=args.run_id,
            result_path=args.output_file,
            payload=payload,
            exit_code=exit_code,
        )
        _materialize_daytrade_evidence_from_completed_payload(
            evidence_file=Path(args.evidence_file),
            exposure_ledger_path=Path(args.exposure_ledger),
            execution_ledger=execution_ledger,
            execution_identity=execution_identity,
            run_id=args.run_id,
            result_path=Path(args.output_file),
        )
    else:
        _record_daytrade_optimization_evidence(
            args,
            access,
            evidence,
            source_name=source_name,
            run_type=reporting["run_type"],
            actual_dataset_hash=reporting["input_materialization_hash"],
            actual_config_hash=hash_payload(config_identity),
            formal_sealed=reporting["formal_sealed"],
            output_hash=reporting["output_hash"],
            report_hash=reporting["report_hash"],
            cost_suite_hash=reporting["cost_suite_hash"],
            experiment_id=reporting["experiment_id"],
        )
        write_json_file(args.output_file, payload)
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return exit_code


def _begin_h9_execution(args, access):
    permit = access.permit
    if permit is None:
        raise ValueError("H9 execution requires a permit-bound dynamic dataset")
    identity = ExperimentExecutionIdentity(
        strategy_family="daytrade",
        experiment_id="daytrade-h9-rvol-reward-risk",
        access_mode=permit.mode,
        request_start=permit.request_start,
        request_end=permit.request_end,
        dataset_hash=permit.dataset_hash,
        config_hash=permit.config_hash,
        cost_model_hash=permit.cost_model_hash,
        code_hash=permit.code_hash,
    )
    ledger = ExperimentExecutionLedger(args.exposure_ledger)
    state = ledger.begin(
        identity,
        run_id=args.run_id,
        result_path=args.output_file,
    )
    return ledger, identity, state if state.completed else None


def _prepare_h9_execution(args, access, stdout):
    if args.experiment_set != "h9-rvol-reward-risk":
        return None, None, None
    ledger, identity, resumed = _begin_h9_execution(args, access)
    if resumed is None:
        return ledger, identity, None
    _materialize_daytrade_evidence_from_completed_payload(
        evidence_file=Path(args.evidence_file),
        exposure_ledger_path=Path(args.exposure_ledger),
        execution_ledger=ledger,
        execution_identity=identity,
        run_id=args.run_id,
        result_path=Path(args.output_file),
    )
    stdout.write(json.dumps(resumed.result_payload, sort_keys=True) + "\n")
    return ledger, identity, int(resumed.exit_code)


def _daytrade_experiment_plan(args, config):
    requirements = IntradayFeatureRequirements.rvol_only()
    if args.experiment_set == "h3-rvol":
        return (
            config,
            preregistered_daytrade_h3_experiments(),
            [("baseline", config), *daytrade_h3_candidate_configs(config)],
            requirements,
        )
    if args.experiment_set == "h6-market":
        return (
            config,
            preregistered_daytrade_h6_experiments(),
            [("baseline", config), *daytrade_h6_candidate_configs(config)],
            None,
        )
    if args.experiment_set == "h7-rvol-window":
        baseline = replace(config, min_relative_volume=2.0, entry_end="10:30")
        return (
            baseline,
            preregistered_daytrade_h7_experiments(),
            [
                ("daytrade-h7-rvol-entry-window:baseline", baseline),
                *daytrade_h7_candidate_configs(baseline),
            ],
            None,
        )
    if args.experiment_set == "h8-rvol-risk":
        baseline = replace(
            config,
            min_relative_volume=2.0,
            entry_end="10:30",
            stop_basis="fixed_pct",
        )
        return (
            baseline,
            preregistered_daytrade_h8_experiments(),
            [
                ("daytrade-h8-rvol-risk-scale:baseline", baseline),
                *daytrade_h8_candidate_configs(baseline),
            ],
            None,
        )
    if args.experiment_set == "h9-rvol-reward-risk":
        if not args.universe_manifest:
            raise ValueError(
                "daytrade H9 requires --universe-manifest for the PIT dynamic universe"
            )
        _require_h9_execution_constraints(config)
        baseline = replace(
            config,
            min_relative_volume=2.0,
            entry_end="10:30",
            stop_basis="fixed_pct",
            stop_loss_pct=0.006,
            reward_risk=1.5,
        )
        _require_h9_frozen_protocol(baseline)
        frozen_candidates = daytrade_h9_candidate_configs(baseline)
        _require_h9_candidate_diff(baseline, frozen_candidates)
        return (
            baseline,
            preregistered_daytrade_global_experiments(),
            [
                ("daytrade-h9-rvol-reward-risk:baseline", baseline),
                *frozen_candidates,
            ],
            requirements,
        )
    return config, preregistered_daytrade_experiments(), None, None


def _require_exact_daytrade_experiment_window(args) -> None:
    """Freeze H3/H9 request windows before any data-access preparation."""

    experiment_set = str(getattr(args, "experiment_set", "") or "").strip()
    if experiment_set not in {"h3-rvol", "h9-rvol-reward-risk"}:
        return
    mode = str(getattr(args, "access_mode", "") or "").strip()
    protocol = DAYTRADE_VALIDATION_PROTOCOL
    if experiment_set == "h3-rvol":
        if mode != "research":
            raise ValueError(
                "H3 baseline reconstruction requires access_mode research"
            )
        expected_start, expected_end = (
            protocol.research_start,
            protocol.research_end,
        )
        label = "H3 research"
    else:
        windows = {
            "research": (protocol.research_start, protocol.research_end),
            "adaptive_development": (
                protocol.walk_forward_start,
                protocol.walk_forward_end,
            ),
            "sealed": (protocol.sealed_start, protocol.sealed_end),
        }
        if mode not in windows:
            raise ValueError(
                "H9 access_mode must be research, adaptive_development, or sealed"
            )
        expected_start, expected_end = windows[mode]
        label = f"H9 {mode}"
    actual_start = parse_date(args.start)
    actual_end = parse_date(args.end)
    if (actual_start, actual_end) != (expected_start, expected_end):
        raise ValueError(
            "%s window is frozen at %s..%s"
            % (label, expected_start.isoformat(), expected_end.isoformat())
        )


def _daytrade_experiment_governance(args, registry) -> dict:
    """Resolve experiment purpose before any permit claim or market-data read."""

    experiment_ids = _DAYTRADE_EXPERIMENT_SET_IDS[args.experiment_set]
    dispositions = {
        experiment_id: registry.disposition_record(experiment_id).disposition.value
        for experiment_id in experiment_ids
    }
    retired = tuple(
        experiment_id
        for experiment_id, disposition in dispositions.items()
        if disposition == "retired"
    )
    audit_replay = bool(getattr(args, "historical_audit_replay", False))
    if retired and not audit_replay:
        raise ValueError(
            "retired experiment set requires --historical-audit-replay before "
            "data access: " + ",".join(retired)
        )
    if audit_replay:
        if not retired:
            raise ValueError(
                "--historical-audit-replay is only valid for a retired experiment set"
            )
        if getattr(args, "access_mode", "") in {"fixed_holdout", "sealed"}:
            raise ValueError(
                "retired experiment audit replay cannot consume fixed-holdout or "
                "sealed data"
            )
        return {
            "run_type": "historical_audit_replay",
            "execution_purpose": "historical_trial_backfill",
            "selection_eligible": False,
            "formal_admission_eligible": False,
            "experiment_dispositions": dispositions,
        }
    for experiment_id in experiment_ids:
        registry.require_candidate_selectable(experiment_id)
    if args.experiment_set == "h3-rvol":
        return {
            "run_type": "historical_baseline_reconstruction",
            "execution_purpose": "frozen_h3_baseline_reconstruction",
            "selection_eligible": False,
            "formal_admission_eligible": False,
            "experiment_dispositions": dispositions,
        }
    return {
        "run_type": "historical_optimization",
        "execution_purpose": "frozen_candidate_evaluation",
        "selection_eligible": True,
        "formal_admission_eligible": False,
        "experiment_dispositions": dispositions,
    }


def _require_h9_frozen_protocol(config: DayTradeConfig) -> None:
    expected = daytrade_config_to_payload(_canonical_h9_baseline_config())
    actual = daytrade_config_to_payload(config)
    drifted = [
        name
        for name, value in expected.items()
        if actual.get(name) != value
    ]
    if drifted:
        raise ValueError(
            "H9 frozen protocol mismatch before data claim: "
            + ",".join(drifted)
        )


def _canonical_h9_baseline_config() -> DayTradeConfig:
    return replace(
        DayTradeConfig(),
        cost_bps=5.0,
        capital_fraction=0.10,
        max_concurrent_positions=3,
        max_trades_per_day=6,
        max_volume_participation=0.10,
        min_relative_volume=2.0,
        entry_end="10:30",
        stop_basis="fixed_pct",
        stop_loss_pct=0.006,
        reward_risk=1.5,
    )


def _require_h9_candidate_diff(
    baseline: DayTradeConfig,
    candidates,
) -> None:
    if len(candidates) != 1:
        raise ValueError("H9 frozen protocol requires exactly one candidate")
    candidate_name, candidate = candidates[0]
    if candidate_name != "daytrade-h9-rvol-reward-risk:trial-01":
        raise ValueError("H9 frozen candidate trial identity mismatch")
    baseline_payload = daytrade_config_to_payload(baseline)
    candidate_payload = daytrade_config_to_payload(candidate)
    changed = tuple(
        name
        for name in baseline_payload
        if baseline_payload[name] != candidate_payload[name]
    )
    if changed != ("reward_risk",) or candidate_payload["reward_risk"] != 2.0:
        raise ValueError("H9 candidate must change only reward_risk to 2.0")


def _require_h9_execution_constraints(config: DayTradeConfig) -> None:
    expected = {
        "cost_bps": 5.0,
        "capital_fraction": 0.10,
        "max_concurrent_positions": 3,
        "max_trades_per_day": 6,
        "max_volume_participation": 0.10,
    }
    drifted = [
        name
        for name, value in expected.items()
        if getattr(config, name) != value
    ]
    if drifted:
        raise ValueError(
            "H9 frozen protocol mismatch before data claim: "
            + ",".join(drifted)
        )


def _effective_daytrade_max_candidates(args) -> int:
    requested = getattr(args, "max_candidates", None)
    if args.experiment_set == "h9-rvol-reward-risk":
        if requested is None:
            return 2
        if requested != 2:
            raise ValueError("H9 max_candidates is frozen at 2 before data claim")
        return 2
    effective = 250 if requested is None else requested
    if effective <= 0:
        raise ValueError("max_candidates must be a positive integer")
    return effective


def _daytrade_optimization_protocol(
    args,
    *,
    effective_max_candidates: int,
) -> dict:
    if args.train_months <= 0:
        raise ValueError("train_months must be a positive integer")
    if args.validation_months <= 0:
        raise ValueError("validation_months must be a positive integer")
    raw_sector_proxies = str(getattr(args, "sector_proxies_json", "") or "").strip()
    if raw_sector_proxies:
        try:
            sector_proxies = json.loads(raw_sector_proxies)
        except json.JSONDecodeError as error:
            raise ValueError("sector_proxies_json must be valid JSON") from error
        if not isinstance(sector_proxies, dict):
            raise ValueError("sector_proxies_json must be a JSON object")
    else:
        sector_proxies = None
    protocol = {
        "train_months": args.train_months,
        "validation_months": args.validation_months,
        "max_candidates": effective_max_candidates,
        "purge_days": 1,
        "embargo_days": 1,
        "feature_warmup_days": args.feature_warmup_days,
        "feature_observation_end": (
            str(getattr(args, "feature_observation_end", "") or "").strip()
            or None
        ),
        "sector_proxies": sector_proxies,
    }
    if (
        args.experiment_set == "h3-rvol"
        and protocol["feature_warmup_days"] != 60
    ):
        raise ValueError("H3 feature_warmup_days is frozen at 60 before data claim")
    if args.experiment_set == "h9-rvol-reward-risk":
        expected = {
            "train_months": 3,
            "validation_months": 1,
            "max_candidates": 2,
            "purge_days": 1,
            "embargo_days": 1,
            "feature_warmup_days": 60,
            "feature_observation_end": None,
            "sector_proxies": None,
        }
        drifted = [
            name
            for name, value in expected.items()
            if protocol[name] != value
        ]
        if drifted:
            raise ValueError(
                "H9 optimization protocol mismatch before data claim: "
                + ",".join(drifted)
            )
    return protocol


def _daytrade_optimization_config_identity(
    args,
    *,
    optimization_base_config: DayTradeConfig,
    candidate_configs,
    feature_requirements,
    optimization_protocol: dict,
    experiment_governance: dict | None = None,
    adaptive_validation: DayTradeAdaptiveValidationArtifact | None = None,
) -> dict:
    identity = {
        "command": "daytrade-optimize-run",
        "experiment_set": args.experiment_set,
        "base_config": _daytrade_strategy_config_payload(
            optimization_base_config
        ),
        "candidate_configs": {
            name: _daytrade_strategy_config_payload(item)
            for name, item in (candidate_configs or ())
        },
        "feature_requirements": (
            feature_requirements.to_payload()
            if feature_requirements is not None
            else IntradayFeatureRequirements.all().to_payload()
        ),
        "optimization_protocol": dict(optimization_protocol),
        "experiment_governance": dict(experiment_governance or {}),
    }
    if adaptive_validation is not None:
        identity["adaptive_validation_artifact"] = (
            adaptive_validation.metadata()
        )
    return identity


def _resolve_h9_adaptive_validation_artifact(
    args,
    environ,
) -> DayTradeAdaptiveValidationArtifact | None:
    """Load frozen adaptive statistics only for the formal sealed H9 run."""

    if (
        str(getattr(args, "experiment_set", "") or "").strip()
        != "h9-rvol-reward-risk"
        or str(getattr(args, "access_mode", "") or "").strip() != "sealed"
    ):
        return None
    runtime_environ = os.environ if environ is None else environ
    path = str(
        getattr(args, "adaptive_validation_artifact", "")
        or runtime_environ.get(
            DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
            "",
        )
        or ""
    ).strip()
    if not path:
        raise ValueError(
            "sealed H9 requires --adaptive-validation-artifact or "
            f"{DAYTRADE_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV}"
        )
    return load_daytrade_adaptive_validation_artifact(
        path,
        trusted_keys=trusted_daytrade_experiment_keys(runtime_environ),
    )


def _daytrade_optimization_cost_identity(
    args,
    config: DayTradeConfig,
) -> dict:
    identity = {
        "execution": "next_available_minute_event",
        "cost_bps_per_side": config.cost_bps,
        "max_volume_participation": config.max_volume_participation,
    }
    if args.experiment_set == "h9-rvol-reward-risk":
        identity["stress_cost_bps_per_side"] = 12.0
    return identity


def _build_h9_canonical_execution_artifact(
    args,
    result,
    *,
    adaptive_validation: DayTradeAdaptiveValidationArtifact | None,
    optimization_base_config: DayTradeConfig,
) -> dict:
    """Build formal H9 facts without consulting presentation summaries."""

    if adaptive_validation is None:
        raise ValueError(
            "formal sealed H9 requires verified adaptive statistical trials"
        )
    raw = result.statistical_validation.get("canonical_execution_inputs")
    if not isinstance(raw, Mapping):
        raise ValueError("sealed H9 raw execution inputs are missing")
    expected_raw_fields = {
        "initial_capital",
        "observation_dates",
        "baseline_5bps",
        "candidate_5bps",
        "candidate_12bps",
    }
    if set(raw) != expected_raw_fields:
        raise ValueError("sealed H9 raw execution input fields are invalid")
    cost_model_payload = _daytrade_optimization_cost_identity(
        args,
        optimization_base_config,
    )
    if (
        set(cost_model_payload)
        != {
            "execution",
            "cost_bps_per_side",
            "max_volume_participation",
            "stress_cost_bps_per_side",
        }
        or cost_model_payload["execution"] != "next_available_minute_event"
        or float(cost_model_payload["cost_bps_per_side"]) != 5.0
        or float(cost_model_payload["stress_cost_bps_per_side"]) != 12.0
    ):
        raise ValueError("sealed H9 requires the frozen 5/12 bps cost suite")
    statistical_dates, statistical_trials = (
        _adaptive_h9_statistical_trials(adaptive_validation)
    )
    selected_trial_id = "daytrade-h9-rvol-reward-risk:trial-01"
    if selected_trial_id not in statistical_trials:
        raise ValueError(
            "adaptive statistical trials do not contain the frozen H9 candidate"
        )
    return {
        "artifact_version": 1,
        "experiment_id": "daytrade-h9-rvol-reward-risk",
        "cost_model_payload": _canonical_json_copy(cost_model_payload),
        "initial_capital": _finite_number(
            raw["initial_capital"],
            "sealed H9 initial_capital",
            positive=True,
        ),
        "observation_dates": _canonical_date_array(
            raw["observation_dates"],
            "sealed H9 observation_dates",
            minimum=2,
        ),
        "baseline_5bps": _canonical_json_copy(raw["baseline_5bps"]),
        "candidate_5bps": _canonical_json_copy(raw["candidate_5bps"]),
        "candidate_12bps": _canonical_json_copy(raw["candidate_12bps"]),
        "statistical_observation_dates": statistical_dates,
        "selected_trial_id": selected_trial_id,
        "statistical_trials": statistical_trials,
    }


def _adaptive_h9_statistical_trials(
    adaptive_validation: DayTradeAdaptiveValidationArtifact,
) -> tuple[list[str], dict[str, list[float]]]:
    statistics = adaptive_validation.report.metrics.get(
        "statistical_validation"
    )
    if not isinstance(statistics, Mapping):
        raise ValueError("adaptive statistical_validation is missing")
    dates = _canonical_date_array(
        statistics.get("observation_dates"),
        "adaptive H9 observation_dates",
        minimum=4,
    )
    parsed_dates = tuple(date.fromisoformat(value) for value in dates)
    protocol = DAYTRADE_VALIDATION_PROTOCOL
    if any(
        value < protocol.walk_forward_start
        or value > protocol.walk_forward_end
        for value in parsed_dates
    ):
        raise ValueError("adaptive H9 observations must stay inside 2023-2024")
    if len({(value.year, value.month) for value in parsed_dates}) < 24:
        raise ValueError("adaptive H9 statistical trials must cover 24 months")
    raw_trials = statistics.get("trial_daily_returns")
    if not isinstance(raw_trials, Mapping):
        raise ValueError("adaptive H9 trial_daily_returns are missing")
    if len(raw_trials) != adaptive_validation.declared_trial_count + 1:
        raise ValueError(
            "adaptive H9 statistical trials must contain baseline plus 21 trials"
        )
    trials: dict[str, list[float]] = {}
    for trial_id in sorted(raw_trials):
        if not isinstance(trial_id, str) or not trial_id.strip():
            raise ValueError("adaptive H9 trial ids must be non-empty strings")
        raw_returns = raw_trials[trial_id]
        if (
            not isinstance(raw_returns, Sequence)
            or isinstance(raw_returns, (str, bytes, bytearray))
            or len(raw_returns) != len(dates)
        ):
            raise ValueError(
                f"adaptive H9 trial is not date-aligned: {trial_id}"
            )
        trials[trial_id] = [
            _finite_number(
                value,
                f"adaptive H9 return {trial_id}",
                minimum=-1.0,
            )
            for value in raw_returns
        ]
    return dates, trials


def _canonical_date_array(value, name: str, *, minimum: int) -> list[str]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) < minimum
    ):
        raise ValueError(f"{name} must contain at least {minimum} dates")
    output = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{name} must contain canonical ISO dates")
        try:
            parsed = date.fromisoformat(item)
        except ValueError as error:
            raise ValueError(
                f"{name} must contain canonical ISO dates"
            ) from error
        if parsed.isoformat() != item:
            raise ValueError(f"{name} must contain canonical ISO dates")
        output.append(item)
    if output != sorted(set(output)):
        raise ValueError(f"{name} must be ascending and unique")
    return output


def _finite_number(
    value,
    name: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    if positive and number <= 0.0:
        raise ValueError(f"{name} must be positive")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return number


def _canonical_json_copy(value):
    try:
        return json.loads(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise ValueError("canonical execution facts must be strict JSON") from error


def _daytrade_strategy_config_payload(config: DayTradeConfig) -> dict:
    """Return strategy identity without execution-cost assumptions."""

    payload = daytrade_config_to_payload(config)
    payload.pop("cost_bps", None)
    payload.pop("max_volume_participation", None)
    return payload


def _daytrade_h9_experiment_report(
    args,
    access,
    payload,
    *,
    optimization_base_config: DayTradeConfig,
    config_hash: str,
    input_materialization_hash: str,
    formal_sealed: bool,
) -> ExperimentRunReport:
    permit = access.permit
    dataset_hash = (
        permit.dataset_hash
        if permit is not None
        else input_materialization_hash
    )
    resolved_config_hash = permit.config_hash if permit is not None else config_hash
    cost_model_hash = (
        permit.cost_model_hash
        if permit is not None
        else hash_payload(
            _daytrade_optimization_cost_identity(
                args,
                optimization_base_config,
            )
        )
    )
    code_hash = permit.code_hash if permit is not None else source_tree_hash()
    fixed_candidate_evaluation = dict(payload["fixed_candidate_evaluation"])
    raw_admission_metrics = fixed_candidate_evaluation.get("admission_metrics")
    if permit is not None and isinstance(raw_admission_metrics, dict):
        canonical_metrics = _h9_daytrade_admission_metrics(
            raw_admission_metrics,
            permit,
        )
        fixed_candidate_evaluation["admission_metrics"] = {
            name: getattr(canonical_metrics, name)
            for name in raw_admission_metrics
        }
    metrics = _finite_json_payload(
        {
            "fixed_candidate_evaluation": fixed_candidate_evaluation,
            "declared_trial_count": payload["declared_trial_count"],
            "completed_trial_count": payload["completed_trial_count"],
            "executed_trial_ids": payload["executed_trial_ids"],
            "statistical_validation": payload["statistical_validation"],
            "rankings": payload["rankings"],
            "windows": payload["windows"],
        }
    )
    output_hash = hash_payload(metrics)
    cost_suite = CostSuite(
        (
            CostScenario("baseline", 5.0, "baseline"),
            CostScenario("stress_12bps", 12.0, "stress"),
        )
    )
    return ExperimentRunReport(
        experiment_id="daytrade-h9-rvol-reward-risk",
        run_id=str(getattr(args, "run_id", "") or "").strip() or (
            "daytrade-h9-%s-%s-%s"
            % (args.start, args.end, input_materialization_hash[:12])
        ),
        run_type=(
            "sealed_backtest"
            if formal_sealed
            else str(payload.get("run_type", "historical_backtest"))
        ),
        observed_at=datetime.now(timezone.utc),
        dataset_hash=dataset_hash,
        input_materialization_hash=input_materialization_hash,
        config_hash=resolved_config_hash,
        code_hash=code_hash,
        cost_model_hash=cost_model_hash,
        cost_suite=cost_suite,
        output_hash=output_hash,
        metrics=metrics,
        admission={
            "passed": bool(payload.get("formal_admission_eligible", False)),
            "formal_admission_eligible": bool(
                payload.get("formal_admission_eligible", False)
            ),
            "candidate_survived": bool(
                payload.get("candidate_survived", False)
            ),
            "reasons": list(payload.get("admission_reasons", ())),
            "status": payload.get("status", "reject"),
        },
    )


def _attach_daytrade_optimization_report(
    args,
    access,
    payload,
    *,
    fixed_evaluation,
    optimization_base_config,
    config_hash,
    bars,
    feature_metadata,
    fallback_run_type,
):
    input_materialization_hash = hash_payload(
        {
            "trade_bars": minute_bars_hash(bars),
            "feature_data": feature_metadata["feature_data_hash"],
            "feature_snapshots": feature_metadata["feature_snapshot_hash"],
            "universe_schedule": _daytrade_universe_schedule_payload(access),
        }
    )
    formal_sealed = bool(
        fixed_evaluation is not None
        and access.permit is not None
        and access.permit.mode == "sealed"
    )
    if fixed_evaluation is None:
        return {
            "run_type": fallback_run_type,
            "formal_sealed": False,
            "input_materialization_hash": input_materialization_hash,
            "output_hash": None,
            "report_hash": None,
            "cost_suite_hash": None,
            "experiment_id": None,
        }
    report = _daytrade_h9_experiment_report(
        args,
        access,
        payload,
        optimization_base_config=optimization_base_config,
        config_hash=config_hash,
        input_materialization_hash=input_materialization_hash,
        formal_sealed=formal_sealed,
    )
    payload.update(
        {
            "dataset_hash": report.dataset_hash,
            "input_materialization_hash": input_materialization_hash,
            "config_hash": report.config_hash,
            "cost_model_hash": report.cost_model_hash,
            "code_hash": report.code_hash,
            "output_hash": report.output_hash,
            "report_hash": report.report_hash,
            "cost_suite_hash": report.cost_suite.suite_hash,
            "experiment_run_report": report.to_payload(),
        }
    )
    return {
        "run_type": report.run_type,
        "formal_sealed": formal_sealed,
        "input_materialization_hash": input_materialization_hash,
        "output_hash": report.output_hash,
        "report_hash": report.report_hash,
        "cost_suite_hash": report.cost_suite.suite_hash,
        "experiment_id": report.experiment_id,
    }


def _finite_json_payload(value):
    """Normalize undefined performance ratios for strict JSON reports."""

    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {
            key: _finite_json_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_finite_json_payload(item) for item in value]
    return value


def _daytrade_input_materialization_hash(
    minute_data_hash: str,
    access,
) -> str:
    schedule = _daytrade_universe_schedule_payload(access)
    if schedule is None:
        return minute_data_hash
    return hash_payload(
        {
            "minute_bars": minute_data_hash,
            "universe_schedule": schedule,
        }
    )


def _daytrade_universe_schedule_payload(access):
    if access.universe_schedule is None:
        return None
    return [
        {
            "trading_day": snapshot.trading_day.isoformat(),
            "ticker_by_security_id": dict(
                snapshot.ticker_by_security_id
            ),
        }
        for snapshot in access.universe_schedule
    ]


def _h9_fixed_candidate_evaluation(result) -> dict:
    baseline_name = "daytrade-h9-rvol-reward-risk:baseline"
    candidate_name = "daytrade-h9-rvol-reward-risk:trial-01"
    statistics = result.statistical_validation
    trial_metrics = statistics.get("trial_metrics", {})
    trial_configs = statistics.get("trial_configs", {})
    baseline = trial_metrics.get(baseline_name, {})
    candidate = trial_metrics.get(candidate_name, {})
    candidate_config = trial_configs.get(candidate_name, {})
    observation_dates = tuple(
        date.fromisoformat(value)
        for value in statistics.get("observation_dates", ())
    )
    oos_months = len({(value.year, value.month) for value in observation_dates})
    dsr = statistics.get("deflated_sharpe_ratio", {})
    pbo = statistics.get("probability_of_backtest_overfitting", {})
    oos_evidence = statistics.get("fixed_candidate_oos_evidence", {})
    stress = oos_evidence.get("stress_12bps", {})
    quarters = oos_evidence.get("quarter_consistency", {})
    concentration = oos_evidence.get("profit_concentration", {})
    checks = {
        "oos_months": oos_months >= 24,
        "oos_trades": candidate.get("trade_count", 0) >= 300,
        "five_bps_cost_model": candidate_config.get("cost_bps") == 5.0,
        "five_bps_net_return": candidate.get("cumulative_return", 0.0) > 0.0,
        "profit_factor": candidate.get("profit_factor", 0.0) >= 1.20,
        "sharpe": candidate.get("sharpe", 0.0) >= 1.0,
        "max_drawdown": candidate.get("max_drawdown", 1.0) <= 0.05,
        "annual_return_improvement": (
            candidate.get("annual_return", float("-inf"))
            >= baseline.get("annual_return", float("inf")) + 0.02
        ),
        "sharpe_improvement": (
            candidate.get("sharpe", float("-inf"))
            >= baseline.get("sharpe", float("inf")) + 0.25
        ),
        "drawdown_not_worse": (
            candidate.get("max_drawdown", float("inf"))
            <= baseline.get("max_drawdown", float("-inf"))
        ),
        "global_trial_histories": (
            result.completed_trial_count == result.declared_trial_count
        ),
        "global_dsr": bool(
            statistics.get("available")
            and dsr.get("available")
            and dsr.get("confidence", 0.0) >= 0.95
        ),
        "global_pbo": bool(
            statistics.get("available")
            and pbo.get("available")
            and pbo.get("probability", 1.0) <= 0.05
        ),
        "twelve_bps_stress": bool(
            stress.get("available")
            and stress.get("cumulative_return", float("-inf")) >= -0.02
            and stress.get("max_drawdown", float("inf")) <= 0.07
        ),
        "positive_quarters": bool(
            quarters.get("available")
            and quarters.get("positive_quarter_fraction", 0.0) >= 0.60
        ),
        "single_symbol_concentration": bool(
            concentration.get("available")
            and concentration.get(
                "top_symbol_profit_contribution",
                float("inf"),
            ) <= 0.25
        ),
        "top_two_concentration": bool(
            concentration.get("available")
            and concentration.get(
                "top_two_profit_contribution",
                float("inf"),
            ) <= 0.50
        ),
        "gross_leverage": candidate.get("gross_leverage", float("inf")) <= 1.0,
    }
    evidence_sections = {
        "twelve_bps_stress": stress,
        "quarter_consistency": quarters,
        "profit_concentration": concentration,
    }
    unavailable = {
        name: section.get("reason") or f"{name} is unavailable"
        for name, section in evidence_sections.items()
        if not section.get("available")
    }
    reasons = [name for name, passed in checks.items() if not passed]
    reasons.extend(f"{name}_unavailable" for name in unavailable)
    passed = all(checks.values()) and not unavailable
    return {
        "passed": passed,
        "decision": "candidate_survived" if passed else "reject",
        "reasons": reasons,
        "baseline_config_name": baseline_name,
        "candidate_config_name": candidate_name,
        "baseline": baseline,
        "candidate": candidate,
        "oos_months": oos_months,
        "config_diff": ["reward_risk"],
        "checks": checks,
        "statistical_validation": {
            "available": bool(statistics.get("available")),
            "reason": statistics.get("reason"),
            "deflated_sharpe_ratio": dsr,
            "probability_of_backtest_overfitting": pbo,
        },
        "stress_12bps": stress,
        "quarter_consistency": quarters,
        "profit_concentration": concentration,
        "unavailable_evidence": {
            name: {"available": False, "reason": reason}
            for name, reason in unavailable.items()
        },
        "admission_metrics": {
            "oos_months": oos_months,
            "oos_trades": candidate.get("trade_count", 0),
            "net_return_after_5bps": candidate.get("cumulative_return", 0.0),
            "profit_factor": candidate.get("profit_factor", 0.0),
            "sharpe": candidate.get("sharpe", 0.0),
            "max_drawdown": candidate.get("max_drawdown", 1.0),
            "annual_net_return": candidate.get("annual_return", 0.0),
            "baseline_annual_net_return": baseline.get("annual_return", 0.0),
            "baseline_sharpe": baseline.get("sharpe", 0.0),
            "baseline_max_drawdown": baseline.get("max_drawdown", 1.0),
            "stressed_12bps_cumulative_return": stress.get(
                "cumulative_return", -1.0
            ),
            "stressed_12bps_max_drawdown": stress.get("max_drawdown", 1.0),
            "positive_quarter_fraction": quarters.get(
                "positive_quarter_fraction", 0.0
            ),
            "top_symbol_profit_contribution": concentration.get(
                "top_symbol_profit_contribution", 1.0
            ),
            "top_two_profit_contribution": concentration.get(
                "top_two_profit_contribution", 1.0
            ),
            "dsr_confidence": dsr.get("confidence", 0.0),
            "pbo": pbo.get("probability", 1.0),
            "gross_leverage": candidate.get("gross_leverage", float("inf")),
            "declared_trials": result.declared_trial_count,
            "completed_trials": result.completed_trial_count,
        },
        "protocol_reasons": (
            [] if checks["five_bps_cost_model"] else ["five_bps_cost_model"]
        ),
        "walk_forward_best_is_diagnostic": True,
    }


def _h9_admission_semantics(
    fixed_evaluation: dict,
    access,
    *,
    _clock=None,
) -> dict:
    """Separate development survival from canonical sealed admission."""

    permit = access.permit
    access_mode = permit.mode if permit is not None else "development_smoke"
    protocol_reasons = list(fixed_evaluation.get("protocol_reasons", ()))
    metric_payload = fixed_evaluation.get("admission_metrics")
    formally_proven = False
    if permit is not None and isinstance(metric_payload, dict):
        metrics = _h9_daytrade_admission_metrics(metric_payload, permit)
        unified_decision = evaluate_daytrade_admission(metrics)
        # The unified evaluator owns every financial/statistical threshold.
        # Its sealed-holdout check uses the real clock; keep tests and future
        # replay deterministic by re-verifying only that one permit condition
        # with the caller's trusted clock.
        strategy_reasons = [
            reason
            for reason in unified_decision.reasons
            if reason != "sealed_holdout"
        ]
        candidate_survived = not strategy_reasons and not protocol_reasons
        permit_admissible = False
        if permit.mode == "sealed":
            try:
                permit_admissible = permit.admits(
                    strategy_family="daytrade",
                    start=permit.request_start,
                    end=permit.request_end,
                    dataset_hash=permit.dataset_hash,
                    config_hash=permit.config_hash,
                    cost_model_hash=permit.cost_model_hash,
                    code_hash=permit.code_hash,
                    _clock=_clock,
                )
                if permit_admissible:
                    ExposureLedger(_clock=_clock).verify_permit(permit)
            except (OSError, PermissionError, ValueError):
                permit_admissible = False
        formally_proven = candidate_survived and permit_admissible
        admission_reasons = [*strategy_reasons, *protocol_reasons]
        if not candidate_survived:
            pass
        elif permit.mode != "sealed":
            admission_reasons.append(f"access_mode_not_sealed:{access_mode}")
        elif not permit_admissible:
            admission_reasons.append("sealed_permit_not_admissible")
    else:
        # Backward-compatible seam for development callers that predate the
        # structured metric payload.  Canonical H9 runs always take the branch
        # above and therefore cannot bypass the unified admission evaluator.
        candidate_survived = bool(fixed_evaluation.get("passed"))
        admission_reasons = list(fixed_evaluation.get("reasons", ()))
        if candidate_survived:
            if permit is None or permit.mode != "sealed":
                admission_reasons.append(f"access_mode_not_sealed:{access_mode}")
            else:
                try:
                    formally_proven = permit.admits(
                        strategy_family="daytrade",
                        start=permit.request_start,
                        end=permit.request_end,
                        dataset_hash=permit.dataset_hash,
                        config_hash=permit.config_hash,
                        cost_model_hash=permit.cost_model_hash,
                        code_hash=permit.code_hash,
                        _clock=_clock,
                    )
                    if formally_proven:
                        ExposureLedger(_clock=_clock).verify_permit(permit)
                except (OSError, PermissionError, ValueError):
                    formally_proven = False
                if not formally_proven:
                    admission_reasons.append("sealed_permit_not_admissible")

    status = (
        "admitted_candidate"
        if formally_proven
        else "development_not_proven"
        if candidate_survived
        else "development_rejected"
    )
    decision = (
        "admit"
        if formally_proven
        else "candidate_survived"
        if candidate_survived
        else "reject"
    )
    return {
        "passed": formally_proven,
        "decision": decision,
        "status": status,
        "candidate_survived": candidate_survived,
        "formal_admission_eligible": formally_proven,
        "admission_access_mode": access_mode,
        "admission_reasons": admission_reasons,
    }


def _h9_daytrade_admission_metrics(payload: dict, permit):
    identity = EvaluationIdentity(
        start=permit.request_start,
        end=permit.request_end,
        dataset_hash=permit.dataset_hash,
        cost_model_hash=permit.cost_model_hash,
        config_hash=permit.config_hash,
        code_hash=permit.code_hash,
    )

    def finite_number(name, fallback):
        value = payload.get(name, fallback)
        if value is None:
            return fallback
        try:
            number = float(value)
        except (TypeError, ValueError):
            return fallback
        if math.isfinite(number):
            return number
        if name == "profit_factor" and number > 0.0:
            return 1e12
        return fallback

    return DayTradeAdmissionMetrics(
        candidate_identity=identity,
        baseline_identity=identity,
        oos_months=max(0, int(payload.get("oos_months", 0) or 0)),
        oos_trades=max(0, int(payload.get("oos_trades", 0) or 0)),
        net_return_after_5bps=finite_number("net_return_after_5bps", -1.0),
        profit_factor=finite_number("profit_factor", 0.0),
        sharpe=finite_number("sharpe", -1e12),
        max_drawdown=finite_number("max_drawdown", 1.0),
        annual_net_return=finite_number("annual_net_return", -1e12),
        baseline_annual_net_return=finite_number(
            "baseline_annual_net_return", 1e12
        ),
        baseline_sharpe=finite_number("baseline_sharpe", 1e12),
        baseline_max_drawdown=finite_number("baseline_max_drawdown", 1.0),
        stressed_12bps_cumulative_return=finite_number(
            "stressed_12bps_cumulative_return", -1.0
        ),
        stressed_12bps_max_drawdown=finite_number(
            "stressed_12bps_max_drawdown", 1.0
        ),
        positive_quarter_fraction=finite_number(
            "positive_quarter_fraction", 0.0
        ),
        top_symbol_profit_contribution=finite_number(
            "top_symbol_profit_contribution", 1.0
        ),
        top_two_profit_contribution=finite_number(
            "top_two_profit_contribution", 1.0
        ),
        dsr_confidence=finite_number("dsr_confidence", 0.0),
        pbo=finite_number("pbo", 1.0),
        gross_leverage=finite_number("gross_leverage", 2.0),
        declared_trials=max(0, int(payload.get("declared_trials", 0) or 0)),
        completed_trials=max(0, int(payload.get("completed_trials", 0) or 0)),
        sealed_data_access_permit=permit,
    )


def _evidence_dataset_hash(access, actual_hash: str) -> str:
    if access.permit is not None:
        return access.permit.dataset_hash
    return actual_hash


def _record_daytrade_optimization_evidence(
    args,
    access,
    evidence: RequirementEvidence,
    *,
    source_name: str,
    run_type: str,
    actual_dataset_hash: str,
    actual_config_hash: str,
    formal_sealed: bool,
    output_hash: str | None = None,
    report_hash: str | None = None,
    cost_suite_hash: str | None = None,
    experiment_id: str | None = None,
    _clock=None,
) -> None:
    canonical = _canonical_daytrade_admission_evidence(
        args,
        access,
        evidence,
        source_name=source_name,
        run_type=run_type,
        actual_dataset_hash=actual_dataset_hash,
        actual_config_hash=actual_config_hash,
        formal_sealed=formal_sealed,
        output_hash=output_hash,
        report_hash=report_hash,
        cost_suite_hash=cost_suite_hash,
        experiment_id=experiment_id,
        _clock=_clock,
    )
    _append_daytrade_evidence(Path(args.evidence_file), canonical)


def _completed_daytrade_admission_evidence(
    args,
    access,
    evidence: RequirementEvidence,
    *,
    fixed_evaluation: dict | None,
    source_name: str,
    run_type: str,
    actual_dataset_hash: str,
    actual_config_hash: str,
    formal_sealed: bool,
    output_hash: str | None,
    report_hash: str | None,
    cost_suite_hash: str | None,
    experiment_id: str | None,
    observed_at: str,
) -> RequirementEvidence:
    """Build the exact canonical evidence embedded in a completed H9 result."""

    if formal_sealed:
        permit = access.permit
        if permit is None or permit.mode != "sealed":
            raise ValueError("formal DayTrade admission requires a sealed permit")
        if fixed_evaluation is None or not isinstance(
            fixed_evaluation.get("admission_metrics"), dict
        ):
            raise ValueError("sealed H9 requires structured admission metrics")
        required_hashes = {
            "input_materialization_hash": actual_dataset_hash,
            "output_hash": output_hash,
            "report_hash": report_hash,
            "cost_suite_hash": cost_suite_hash,
        }
        if any(value is None for value in required_hashes.values()):
            raise ValueError("sealed H9 report identity is incomplete")
        manifest = RunManifest(
            run_type="sealed_backtest",
            observed_at=_parse_report_observed_at(observed_at),
            dataset_hash=permit.dataset_hash,
            config_hash=permit.config_hash,
            code_hash=permit.code_hash,
            policy_version=ACCEPTANCE_POLICY_VERSION,
            cost_model_hash=permit.cost_model_hash,
            input_materialization_hash=actual_dataset_hash,
            output_hash=output_hash,
            report_hash=report_hash,
            cost_suite_hash=cost_suite_hash,
            run_id=str(args.run_id).strip(),
            experiment_id=experiment_id,
        )
        canonical = build_daytrade_admission_evidence(
            _h9_daytrade_admission_metrics(
                fixed_evaluation["admission_metrics"],
                permit,
            ),
            manifest,
        )
        expected = bool(evidence.passed)
        if canonical.passed != expected:
            raise RuntimeError(
                "DayTrade admission report and canonical evidence disagree"
            )
        return canonical

    # Development H9 receipts remain non-formal but retain one immutable,
    # report-bound RequirementEvidence for audit replay and idempotent resume.
    canonical = _canonical_daytrade_admission_evidence(
        args,
        access,
        evidence,
        source_name=source_name,
        run_type=run_type,
        actual_dataset_hash=actual_dataset_hash,
        actual_config_hash=actual_config_hash,
        formal_sealed=False,
        output_hash=output_hash,
        report_hash=report_hash,
        cost_suite_hash=cost_suite_hash,
        experiment_id=experiment_id,
        observed_at=observed_at,
    )
    return RequirementEvidence(
        requirement_id=canonical["requirement_id"],
        passed=canonical["passed"],
        detail=canonical["detail"],
        run_type=canonical["run_type"],
        observed_at=canonical["observed_at"],
        dataset_hash=canonical["dataset_hash"],
        config_hash=canonical["config_hash"],
        cost_model_hash=canonical["cost_model_hash"],
        code_hash=canonical["code_hash"],
        policy_version=canonical["policy_version"],
        input_materialization_hash=canonical["input_materialization_hash"],
        output_hash=canonical["output_hash"],
        report_hash=canonical["report_hash"],
        cost_suite_hash=canonical["cost_suite_hash"],
        run_id=canonical["run_id"],
        experiment_id=canonical["experiment_id"],
    )


def _parse_report_observed_at(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("experiment report observed_at must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise ValueError("experiment report observed_at is invalid") from error
    return parsed


def _canonical_daytrade_admission_evidence(
    args,
    access,
    evidence: RequirementEvidence,
    *,
    source_name: str,
    run_type: str,
    actual_dataset_hash: str,
    actual_config_hash: str,
    formal_sealed: bool,
    output_hash: str | None = None,
    report_hash: str | None = None,
    cost_suite_hash: str | None = None,
    experiment_id: str | None = None,
    observed_at: str | None = None,
    execution_identity_hash: str | None = None,
    _clock=None,
) -> dict[str, object]:
    """Build canonical evidence before an execution result becomes immutable."""

    permit = access.permit
    if formal_sealed and (permit is None or permit.mode != "sealed"):
        raise ValueError("formal DayTrade admission requires a sealed permit")
    if permit is not None:
        ExposureLedger(_clock=_clock).verify_permit(permit)
        if formal_sealed:
            permit.assert_allows(
                dataset_hash=permit.dataset_hash,
                start=permit.request_start,
                end=permit.request_end,
                strategy_family="daytrade",
                _clock=_clock,
            )
        dataset_hash = permit.dataset_hash
        config_hash = permit.config_hash
        cost_model_hash = permit.cost_model_hash
        code_hash = permit.code_hash
    else:
        dataset_hash = actual_dataset_hash
        config_hash = actual_config_hash
        cost_model_hash = None
        code_hash = source_tree_hash()
    observed = observed_at or datetime.now(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    unsigned = {
        "schema_version": 1,
        "requirement_id": evidence.requirement_id,
        "passed": bool(evidence.passed),
        "detail": "source=%s %s" % (source_name, evidence.detail),
        "run_type": run_type,
        "observed_at": observed,
        "dataset_hash": dataset_hash,
        "input_materialization_hash": actual_dataset_hash,
        "output_hash": output_hash,
        "report_hash": report_hash,
        "cost_suite_hash": cost_suite_hash,
        "config_hash": config_hash,
        "cost_model_hash": cost_model_hash,
        "code_hash": code_hash,
        "policy_version": (
            ACCEPTANCE_POLICY_VERSION if formal_sealed else None
        ),
        "run_id": str(getattr(args, "run_id", "") or "").strip() or None,
        "experiment_id": experiment_id,
        "execution_identity_hash": execution_identity_hash,
    }
    return {**unsigned, "evidence_hash": hash_payload(unsigned)}


def _materialize_daytrade_evidence_from_completed_payload(
    *,
    evidence_file: Path,
    exposure_ledger_path: Path,
    execution_ledger: ExperimentExecutionLedger,
    execution_identity: ExperimentExecutionIdentity,
    run_id: str,
    result_path: Path,
) -> bool:
    """Idempotently materialize evidence only from an immutable result payload."""

    if execution_identity.access_mode == "sealed":
        materialize_completed_strategy_admission(
            evidence_path=evidence_file,
            exposure_ledger_path=exposure_ledger_path,
            execution_ledger=execution_ledger,
            execution_identity=execution_identity,
            run_id=run_id,
            result_path=result_path,
        )
        return True
    completed = execution_ledger.verify_completed(
        execution_identity,
        run_id=run_id,
        result_path=result_path,
    )
    completed_payload = completed.result_payload
    if not isinstance(completed_payload, dict):
        raise ValueError("completed DayTrade result payload is unavailable")
    if completed_payload.get("execution_complete") is not True:
        raise ValueError("DayTrade evidence requires a completed execution payload")
    canonical = completed_payload.get("admission_evidence")
    if not isinstance(canonical, dict):
        raise ValueError("completed DayTrade payload lacks canonical admission_evidence")
    try:
        structured_evidence = RequirementEvidence(**canonical)
    except TypeError as error:
        raise ValueError(
            "completed DayTrade admission_evidence fields are invalid"
        ) from error
    if admission_evidence_payload(structured_evidence) != canonical:
        raise ValueError("completed DayTrade admission_evidence is not canonical")
    if completed_payload.get("admission_evidence_hash") != hash_payload(canonical):
        raise ValueError("completed DayTrade admission_evidence hash is invalid")
    if completed_payload.get(
        "admission_evidence_execution_identity_hash"
    ) != completed_payload.get(
        "execution_identity_hash"
    ):
        raise ValueError("DayTrade admission evidence is not bound to the execution")
    if canonical.get("report_hash") != completed_payload.get("report_hash"):
        raise ValueError("DayTrade admission evidence report_hash mismatch")
    if canonical.get("requirement_id") != completed_payload.get("requirement_id"):
        raise ValueError("DayTrade admission evidence requirement mismatch")
    expected_passed = (
        completed_payload.get("formal_admission_eligible")
        if canonical.get("requirement_id") == "daytrade_strategy_admission"
        else completed_payload.get("candidate_survived")
    )
    if canonical.get("passed") is not bool(expected_passed):
        raise ValueError("DayTrade admission evidence decision mismatch")

    return _append_daytrade_evidence(
        evidence_file,
        canonical,
        idempotency_fields=("requirement_id", "run_id", "report_hash"),
    )


def _append_daytrade_evidence(
    evidence_file: Path,
    canonical: dict[str, object],
    *,
    idempotency_fields: tuple[str, ...] | None = None,
) -> bool:
    return EvidenceRecorder(evidence_file).record(
        canonical["requirement_id"],
        canonical["passed"],
        canonical["detail"],
        run_type=canonical["run_type"],
        observed_at=canonical["observed_at"],
        dataset_hash=canonical["dataset_hash"],
        input_materialization_hash=canonical["input_materialization_hash"],
        output_hash=canonical.get("output_hash"),
        report_hash=canonical.get("report_hash"),
        cost_suite_hash=canonical.get("cost_suite_hash"),
        config_hash=canonical["config_hash"],
        cost_model_hash=canonical.get("cost_model_hash"),
        code_hash=canonical["code_hash"],
        policy_version=canonical.get("policy_version"),
        run_id=canonical.get("run_id"),
        experiment_id=canonical.get("experiment_id"),
        idempotency_fields=idempotency_fields,
    )


def handle_daytrade_compare_configs(args, context: CLIContext) -> int:
    config = daytrade_config_from_args(args)
    best_payload = json.loads(Path(args.walk_forward_config_json).read_text(encoding="utf-8"))
    best_config_item = best_payload.get("best_config", best_payload)
    best_config_name = best_payload.get("best_config_name", "walk_forward_best")
    walk_forward_config = daytrade_config_from_payload(best_config_item, base_config=config)
    experiment_governance = _daytrade_comparison_governance(
        args,
        best_config_name,
    )
    access = _prepare_daytrade_comparison_access(
        args,
        config,
        walk_forward_config,
        best_config_name,
    )
    bars, source_name, feature_snapshots, feature_metadata = (
        _load_daytrade_comparison_inputs(
            args,
            context,
            config,
            best_config_name,
            access,
        )
    )
    payload = {
        "source": source_name,
        "run_type": (
            "historical_%s" % access.permit.mode
            if access.permit is not None
            else "development_smoke"
        ),
        "formal_admission_eligible": False,
        **experiment_governance,
        "candidate_config_name": best_config_name,
        "trade_bar_count": len(bars),
        "trade_data_hash": minute_bars_hash(bars),
        "configs": compare_daytrade_configs(
            bars,
            config,
            walk_forward_config,
            walk_forward_name=best_config_name,
            feature_snapshots=feature_snapshots,
            universe_schedule=access.universe_schedule,
        ),
        **feature_metadata,
        **daytrade_data_access_payload(access),
    }
    if access.permit is not None:
        payload["holdout_access_claim"] = access.permit.as_payload()
    if args.stress_cost_bps is not None:
        stress_config = daytrade_config_from_payload(
            {"cost_bps": args.stress_cost_bps},
            base_config=walk_forward_config,
        )
        payload["stress"] = {
            "cost_bps": args.stress_cost_bps,
            "candidate": evaluate_daytrade_config(
                bars,
                stress_config,
                candidate_name=best_config_name,
                feature_snapshots=feature_snapshots,
                universe_schedule=access.universe_schedule,
            ),
        }
    if args.output_file:
        write_json_file(args.output_file, payload)
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _daytrade_comparison_governance(args, candidate_name: str) -> dict:
    """Prevent a retired candidate comparison from becoming a new validation."""

    experiment_id = next(
        (
            identifier
            for identifier in _DAYTRADE_EXPERIMENT_SET_IDS["h1-h4"]
            + (
                "daytrade-h5-opening-range-top4",
                "daytrade-h6-market-confirmation",
                "daytrade-h7-rvol-entry-window",
                "daytrade-h8-rvol-risk-scale",
            )
            if candidate_name == identifier
            or candidate_name.startswith(identifier + ":")
        ),
        None,
    )
    disposition = (
        fixed_experiment_disposition(experiment_id)
        if experiment_id is not None
        else None
    )
    retired = disposition is not None and disposition.disposition.value == "retired"
    audit_replay = bool(getattr(args, "historical_audit_replay", False))
    if retired and not audit_replay:
        raise ValueError(
            "retired experiment comparison requires --historical-audit-replay "
            "before data access: " + experiment_id
        )
    if audit_replay:
        if not retired:
            raise ValueError(
                "--historical-audit-replay is only valid for a retired experiment"
            )
        if (
            getattr(args, "access_mode", "") in {"fixed_holdout", "sealed"}
            or bool(getattr(args, "holdout_manifest", ""))
        ):
            raise ValueError(
                "retired experiment audit replay cannot consume fixed-holdout or "
                "sealed data"
            )
        return {
            "run_type": "historical_audit_replay",
            "execution_purpose": "historical_trial_backfill",
            "selection_eligible": False,
            "experiment_disposition": disposition.disposition.value,
        }
    return {
        "execution_purpose": "diagnostic_comparison",
        "selection_eligible": False,
        "experiment_disposition": (
            None if disposition is None else disposition.disposition.value
        ),
    }


def _load_daytrade_comparison_inputs(
    args,
    context: CLIContext,
    config: DayTradeConfig,
    candidate_name: str,
    access,
):
    if not daytrade_candidate_requires_features(candidate_name):
        bars, source_name = load_daytrade_bars_from_args(
            args,
            context.environ,
            context.daytrade_minute_loader,
            config,
            context.daytrade_session_loader,
            data_access=access,
        )
        return bars, source_name, (), {
            "feature_required": False,
            "feature_config": None,
            "feature_warmup_start": None,
            "feature_bar_count": 0,
            "feature_data_hash": hash_payload({"feature_required": False}),
            "feature_snapshot_count": 0,
            "feature_snapshot_hash": hash_payload([]),
        }
    (
        bars,
        feature_bars,
        source_name,
        feature_config,
        warmup_start,
    ) = load_daytrade_feature_inputs_from_args(
        args,
        context.environ,
        context.daytrade_minute_loader,
        config,
        context.daytrade_session_loader,
        data_access=access,
        feature_requirements=daytrade_candidate_feature_requirements(candidate_name),
    )
    feature_snapshots = build_intraday_feature_snapshots(feature_bars, feature_config)
    return bars, source_name, feature_snapshots, {
        "feature_required": True,
        **daytrade_feature_metadata(
            feature_config,
            feature_bars,
            feature_snapshots,
            warmup_start,
        ),
    }


def _prepare_daytrade_comparison_access(
    args,
    baseline_config: DayTradeConfig,
    candidate_config: DayTradeConfig,
    candidate_name: str,
):
    feature_required = daytrade_candidate_requires_features(candidate_name)
    if feature_required and args.feature_warmup_days <= 0:
        raise ValueError("feature_warmup_days must be a positive integer")
    warmup_start = (
        parse_date(args.start) - timedelta(days=args.feature_warmup_days)
        if feature_required
        else None
    )
    config_identity = {
        "command": "daytrade-compare-configs",
        "candidate_name": candidate_name,
        "baseline": daytrade_config_to_payload(baseline_config),
        "candidate": daytrade_config_to_payload(candidate_config),
        "stress_cost_bps": args.stress_cost_bps,
        "feature_requirements": (
            daytrade_candidate_feature_requirements(candidate_name).to_payload()
            if feature_required
            else None
        ),
    }
    cost_identity = {
        "baseline_cost_bps": baseline_config.cost_bps,
        "candidate_cost_bps": candidate_config.cost_bps,
        "stress_cost_bps": args.stress_cost_bps,
        "max_volume_participation": baseline_config.max_volume_participation,
    }
    legacy_manifest = None
    if args.holdout_manifest:
        legacy_manifest = json.loads(
            Path(args.holdout_manifest).read_text(encoding="utf-8")
        )
        expected = {
            "manifest_version": 1,
            "status": "frozen_unrun",
            "run_limit": 1,
            "strategy_family": "daytrade",
            "holdout_start": args.start,
            "holdout_end": args.end,
            "candidate_name": candidate_name,
            "feature_required": feature_required,
            "data_source": args.data_source,
            "alpaca_feed": args.alpaca_feed,
            "feature_warmup_days": args.feature_warmup_days,
            "minute_cache_schema_version": DAYTRADE_MINUTE_CACHE_SCHEMA_VERSION,
            "baseline_config_hash": hash_payload(
                daytrade_config_to_payload(baseline_config)
            ),
            "candidate_config_hash": hash_payload(
                daytrade_config_to_payload(candidate_config)
            ),
            "code_hash": source_tree_hash(),
            "stress_cost_bps": args.stress_cost_bps,
        }
        if not args.universe_manifest:
            expected["symbols"] = list(
                parse_daytrade_symbols(args.symbols, baseline_config)
            )
        mismatches = [
            name
            for name, value in expected.items()
            if legacy_manifest.get(name) != value
        ]
        if args.run_id != legacy_manifest.get("run_id"):
            mismatches.append("run_id")
        if mismatches:
            raise ValueError(
                "fixed holdout manifest mismatch: "
                + ",".join(sorted(set(mismatches)))
            )

    if args.universe_manifest:
        access = prepare_daytrade_data_access(
            args,
            baseline_config,
            config_identity=config_identity,
            cost_identity=cost_identity,
            warmup_start=warmup_start,
        )
        if (
            legacy_manifest is not None
            and legacy_manifest.get("dataset_hash") != access.permit.dataset_hash
        ):
            raise ValueError("fixed holdout manifest dataset_hash mismatch")
        return access
    if legacy_manifest is not None:
        dataset_hash = legacy_manifest.get("dataset_hash") or args.dataset_hash
        return claim_daytrade_static_data_access(
            args,
            baseline_config,
            dataset_hash=dataset_hash,
            config_identity=config_identity,
            cost_identity=cost_identity,
            warmup_start=warmup_start,
            minute_data_hash=legacy_manifest.get("minute_data_hash"),
        )
    return prepare_daytrade_data_access(args, baseline_config)


def handle_daytrade_paper_cycle(args, context: CLIContext) -> int:
    result, source_name, access, _input_hash = _run_daytrade(args, context)
    DayTradeTradeJournal(args.trades_log).append_all(result.trades)
    DayTradeDailyJournal(args.daily_log).append_all(result.daily_results)
    trades, daily_results = _read_daytrade_journals(args)
    evidence = build_daytrade_soak_evidence(
        daily_results,
        trades,
        requirement_id="daytrade_paper_soak",
        min_sessions=args.min_sessions,
        max_drawdown=args.max_drawdown,
    )
    _record_soak_evidence(args, source_name, evidence, daily_results, trades, "paper_trading")
    payload = _daytrade_status(args, daily_results, trades)
    payload.update(
        {
            "mode": "paper",
            "source": source_name,
            "logs": _daytrade_logs(args),
            **daytrade_data_access_payload(access),
        }
    )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def handle_daytrade_live_cycle(args, context: CLIContext) -> int:
    rejection = _live_rejection(args)
    if rejection:
        context.stdout.write(json.dumps(rejection, sort_keys=True) + "\n")
        return 1
    result, source_name, access, _input_hash = _run_daytrade(args, context)
    DayTradeTradeJournal(args.trades_log).append_all(result.trades)
    DayTradeDailyJournal(args.daily_log).append_all(result.daily_results)
    trades, daily_results = _read_daytrade_journals(args)
    evidence = build_daytrade_soak_evidence(
        daily_results,
        trades,
        requirement_id="daytrade_live_soak",
        min_sessions=args.min_sessions,
        max_drawdown=args.max_drawdown,
    )
    _record_live_replay_evidence(args, source_name, evidence, result)
    payload = _daytrade_status(args, daily_results, trades)
    payload.update(
        {
            "mode": "live",
            "run_type": "historical_replay",
            "source": source_name,
            "orders_submitted": 0,
            "logs": _daytrade_logs(args),
            **daytrade_data_access_payload(access),
        }
    )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _live_rejection(args):
    if args.submit_orders:
        return {
            "ready": False,
            "error": "live_trading_disabled",
            "detail": (
                "real order submission is disabled; daytrade-live-cycle is "
                "historical replay only"
            ),
        }
    if not args.confirm_live:
        return {
            "ready": False,
            "error": "confirm_live_required",
            "detail": "daytrade-live-cycle requires --confirm-live",
        }
    return None


def handle_daytrade_status(args, context: CLIContext) -> int:
    trades, daily_results = _read_daytrade_journals(args)
    payload = _daytrade_status(args, daily_results, trades)
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if payload["passed"] else 1


def _run_daytrade(args, context):
    config = daytrade_config_from_args(args)
    access = prepare_daytrade_data_access(
        args,
        config,
        config_identity=_daytrade_strategy_config_payload(config),
        cost_identity={
            "execution": "next_available_minute_event",
            "cost_bps_per_side": config.cost_bps,
            "max_volume_participation": config.max_volume_participation,
        },
    )
    result, source_name, input_hash = run_daytrade_from_args(
        args,
        context.environ,
        context.daytrade_minute_loader,
        context.daytrade_session_loader,
        data_access=access,
    )
    return result, source_name, access, input_hash


def _load_daytrade(args, context, config):
    return load_daytrade_bars_from_args(
        args,
        context.environ,
        context.daytrade_minute_loader,
        config,
        context.daytrade_session_loader,
    )


def _read_daytrade_journals(args):
    return (
        DayTradeTradeJournal(args.trades_log).read_all(),
        DayTradeDailyJournal(args.daily_log).read_all(),
    )


def _daytrade_status(args, daily_results, trades):
    return summarize_daytrade_status(
        daily_results,
        trades,
        min_sessions=args.min_sessions,
        max_drawdown=args.max_drawdown,
    )


def _daytrade_logs(args):
    return {
        "trades_log": args.trades_log,
        "daily_log": args.daily_log,
        "evidence_file": args.evidence_file,
    }


def _record_soak_evidence(args, source_name, evidence, daily_results, trades, run_type):
    EvidenceRecorder(args.evidence_file).record(
        evidence.requirement_id,
        evidence.passed,
        "source=%s %s" % (source_name, evidence.detail),
        run_type=run_type,
        dataset_hash=daytrade_runs_hash(daily_results, trades),
        config_hash=hash_payload(daytrade_config_to_payload(daytrade_config_from_args(args))),
    )


def _record_live_replay_evidence(args, source_name, evidence, result):
    EvidenceRecorder(args.evidence_file).record(
        evidence.requirement_id,
        evidence.passed,
        "source=%s orders_submitted=0 %s" % (source_name, evidence.detail),
        run_type="historical_replay",
        dataset_hash=hash_payload(
            {
                "source": source_name,
                "start": args.start,
                "end": args.end,
                "daily_results": [
                    {
                        "day": item.day,
                        "trades": item.trades,
                        "pnl": item.pnl,
                        "ending_equity": item.ending_equity,
                    }
                    for item in result.daily_results
                ],
            }
        ),
        config_hash=hash_payload(daytrade_config_to_payload(daytrade_config_from_args(args))),
    )
