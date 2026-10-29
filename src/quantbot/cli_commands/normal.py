"""Normal-frequency backtest and historical-replay command handlers."""

import importlib
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from ..acceptance_policy import ACCEPTANCE_POLICY_VERSION
from ..analytics import StrategyAcceptanceGate
from ..audit import RequirementEvidence
from ..evidence import EvidenceRecorder, hash_payload, source_tree_hash
from ..formal_evidence import (
    admission_evidence_payload,
    materialize_completed_strategy_admission,
)
from ..experiment_reporting import CostScenario, CostSuite, ExperimentRunReport
from ..experiment_execution import (
    ExperimentExecutionIdentity,
    ExperimentExecutionLedger,
)
from ..historical_replay import build_historical_replay
from ..live import LiveEquitySnapshotJournal, LiveTradingJournal, build_live_trading_evidence
from ..oos_backtest import (
    MomentumOOSBacktestConfig,
    normalized_prices_payload,
    require_legacy_universe_mode,
    run_momentum_oos_backtest,
)
from ..data import NORMAL_H1_REQUIRED_TABLES, PointInTimeParquetBundle
from ..exposure import (
    ExposureLedger,
    ValidationWindowManifest,
    canonical_exposure_ledger_path,
)
from ..normal import (
    NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV,
    NORMAL_H2_AUDIT_ARTIFACT_PATH_ENV,
    NORMAL_H1_EXPERIMENT_ID,
    NORMAL_H2_BASELINE_CONFIG,
    NORMAL_H2_CANDIDATE_CONFIG,
    NORMAL_H2_EXPERIMENT_ID,
    NormalH1AdapterOutput,
    NormalH1ExperimentInputs,
    NormalAdaptiveValidationArtifact,
    NormalH2HistoricalAudit,
    load_normal_h2_historical_audit,
    normal_trial_return_history_hash,
    require_implemented_execution_price,
    require_exact_normal_experiment_window,
    run_normal_h1_experiment,
    run_normal_h2_experiment,
    trusted_normal_trial_keys,
    verify_normal_adaptive_validation_artifact,
)
from ..normal.adapter_trust import (
    NORMAL_ADAPTER_ATTESTATION_PATH_ENV,
    normal_adapter_artifact_identity,
    require_formal_normal_adapter,
    verify_normal_adapter_attestation,
)
from ..normal.execution_artifact import (
    build_normal_canonical_execution_artifact,
    normal_cost_model_payload,
)
from ..validation_protocol import assert_validation_data_released
from ..strategy_admission import (
    EvaluationIdentity,
    NormalAdmissionMetrics,
    build_normal_admission_evidence,
    evaluate_normal_admission,
)
from ..trading import RunManifest
from ..paper import (
    PaperAccountSnapshotJournal,
    PaperTradingJournal,
    build_paper_trading_evidence,
)
from .context import CLIContext
from .support import (
    append_evidence,
    live_status_payload,
    oos_backtest_config_payload,
    paper_status_payload,
    parse_date,
    parse_symbols,
    resolve_oos_price_loader,
    resolve_runtime_config,
    summary_from_json,
    summary_to_payload,
    utc_now_iso,
)


NORMAL_ADAPTER_ENV = "STOCKBOT_NORMAL_ADAPTER"


@dataclass(frozen=True, slots=True)
class _PendingNormalAdaptiveValidation:
    """Unverified sealed evidence read before, but verified after, the claim."""

    raw_bytes: bytes
    file_hash: str
    path: Path


def handle_backtest_acceptance(args, context: CLIContext) -> int:
    summary = summary_from_json(args.summary_json)
    decision = StrategyAcceptanceGate().evaluate(
        summary=summary,
        annual_turnover=args.annual_turnover,
        two_way_cost_rate=args.two_way_cost_rate,
        summary_includes_costs=args.summary_includes_costs,
    )
    detail = (
        "excess_alpha=%.6f sharpe=%.6f max_drawdown=%.6f annual_turnover=%.6f "
        "annual_cost_drag=%.6f net_excess_alpha=%.6f reasons=%s"
        % (
            summary.excess_alpha_annual,
            summary.sharpe,
            summary.max_drawdown,
            args.annual_turnover,
            decision.annual_cost_drag,
            decision.net_excess_alpha_annual,
            ",".join(decision.reasons),
        )
    )
    append_evidence(
        args.evidence_file,
        requirement_id="oos_backtest_acceptance",
        passed=decision.passed,
        detail=detail,
    )
    context.stdout.write(
        json.dumps(
            {
                "passed": decision.passed,
                "annual_cost_drag": decision.annual_cost_drag,
                "net_excess_alpha_annual": decision.net_excess_alpha_annual,
                "reasons": decision.reasons,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0 if decision.passed else 1


def handle_oos_backtest_run(args, context: CLIContext) -> int:
    runtime = resolve_runtime_config(args, context.environ)
    if runtime is not None:
        # The legacy mega-cap path cannot serve a point-in-time universe.
        require_legacy_universe_mode(runtime.normal.universe_mode)
    config = _oos_config(args, include_cost=True)
    data_start = parse_date(args.data_start)
    data_end = parse_date(args.data_end)
    assert_validation_data_released(
        strategy_family="normal",
        start=data_start,
        end=data_end,
    )
    loader, source_name = resolve_oos_price_loader(
        args, context.environ, context.oos_price_loader
    )
    prices = loader(
        symbols=config.symbols,
        benchmark=config.benchmark,
        start=data_start,
        end=data_end,
    )
    result = run_momentum_oos_backtest(prices, config)
    decision = StrategyAcceptanceGate().evaluate(
        summary=result.summary,
        annual_turnover=result.annual_turnover,
        two_way_cost_rate=args.two_way_cost_rate,
        summary_includes_costs=True,
    )
    reasons = (
        list(decision.reasons)
        + list(result.window_report.reasons)
        + list(result.coverage_reasons)
    )
    if result.train_observations == 0:
        reasons.append("no_train_returns")
    if result.validation_observations == 0:
        reasons.append("no_validation_returns")
    passed = not reasons
    _record_oos_evidence(args, prices, config, result, decision, reasons, source_name, passed)
    payload = {
        "passed": passed,
        "run_type": "development_smoke",
        "formal_admission_eligible": False,
        "admission_scope": "legacy_fixed_universe_compatibility_only",
        "reasons": reasons,
        "annual_cost_drag": decision.annual_cost_drag,
        "net_excess_alpha_annual": decision.net_excess_alpha_annual,
        "annual_turnover": result.annual_turnover,
        "summary": summary_to_payload(result.summary),
        "train_summary": summary_to_payload(result.train_summary),
        "train_observations": result.train_observations,
        "validation_observations": result.validation_observations,
        "validation_rebalances": result.validation_rebalances,
        "coverage_reasons": list(result.coverage_reasons),
        "symbols": list(result.symbols),
        "benchmark": result.benchmark,
        "source": source_name,
    }
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if passed else 1


def handle_normal_experiment_run(args, context: CLIContext) -> int:
    """Run one preregistered PIT normal experiment behind a permit."""

    _require_normal_experiment_runtime(args, context)
    experiment_id = _normal_experiment_id(args)
    _require_normal_experiment_execution_mode(args, experiment_id)
    bundle = _normal_experiment_bundle(args, context)
    loader = _resolve_normal_experiment_loader(args, context)
    if loader is None:
        return _normal_adapter_unavailable(
            experiment_id,
            bundle,
            context,
        )
    environ = context.environ if context.environ is not None else os.environ
    if args.access_mode == "sealed":
        require_formal_normal_adapter(loader)
    adapter_identity = normal_adapter_artifact_identity(
        loader
    )
    adapter_code_hash = adapter_identity.adapter_artifact_hash
    adapter_attestation = _normal_adapter_attestation(
        args,
        bundle,
        adapter_identity,
        environ=environ,
    )
    manifest, identity = _normal_experiment_manifest(
        args,
        bundle,
        adapter_code_hash=adapter_code_hash,
        adapter_attestation=adapter_attestation,
    )
    (
        start,
        end,
        warmup_start,
        warmup_end,
        config_hash,
        cost_model_hash,
        code_hash,
        stockbot_source_hash,
        strategy_protocol_hash,
    ) = identity
    _require_bundle_coverage(bundle, manifest)
    historical_h2_audit = _resolve_normal_h2_historical_audit(
        args,
        environ=environ,
        experiment_id=experiment_id,
        mode=args.access_mode,
        request_start=start,
        request_end=end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
        dataset_hash=bundle.dataset_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
    )
    pending_adaptive_validation = _resolve_pending_normal_adaptive_validation(
        args,
        environ=environ,
        experiment_id=experiment_id,
        mode=args.access_mode,
    )
    if historical_h2_audit is not None:
        config_hash = hash_payload(
            {
                "normal_protocol_config_hash": config_hash,
                "historical_h2_audit_hash": historical_h2_audit.content_hash,
            }
        )
        manifest = replace(manifest, config_hash=config_hash)
    if pending_adaptive_validation is not None:
        config_hash = hash_payload(
            {
                "normal_protocol_config_hash": config_hash,
                "adaptive_validation_artifact_file_hash": (
                    pending_adaptive_validation.file_hash
                ),
            }
        )
        manifest = replace(manifest, config_hash=config_hash)
    result_path = _require_normal_experiment_result_path(args.output_file)
    _require_normal_canonical_ledger(args.exposure_ledger)
    permit = ExposureLedger(args.exposure_ledger).claim(
        manifest,
        run_id=args.run_id,
    )
    execution_ledger, execution_identity, execution_state = (
        _begin_normal_experiment_execution(
            args,
            permit=permit,
            experiment_id=experiment_id,
            result_path=result_path,
        )
    )
    if execution_state.completed:
        return _materialize_completed_normal_experiment(
            args,
            context,
            execution_state.result_payload,
            exit_code=execution_state.exit_code,
            execution_ledger=execution_ledger,
            execution_identity=execution_identity,
            result_path=result_path,
        )
    adaptive_validation = _verify_pending_normal_adaptive_validation(
        pending_adaptive_validation,
        environ=environ,
        strategy_protocol_hash=strategy_protocol_hash,
        cost_model_hash=cost_model_hash,
        stockbot_source_hash=stockbot_source_hash,
        adapter_artifact_hash=adapter_code_hash,
    )
    assert_validation_data_released(
        strategy_family="normal",
        start=manifest.actual_start,
        end=manifest.actual_end,
        permit=permit,
    )
    read_session = bundle.begin_read_session(
        permit=permit,
        strategy_family="normal",
    )
    adapter_output = loader(
        read_session=read_session,
        start=start,
        end=end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
        strategy_family="normal",
    )
    if not isinstance(adapter_output, NormalH1AdapterOutput):
        raise TypeError("normal_experiment_loader must return NormalH1AdapterOutput")
    _require_unchanged_normal_adapter_trust(
        args,
        bundle,
        loader,
        adapter_identity,
        adapter_attestation,
        code_hash=code_hash,
        environ=environ,
    )
    read_evidence = read_session.seal(required_tables=NORMAL_H1_REQUIRED_TABLES)
    inputs = NormalH1ExperimentInputs.from_adapter_output(
        adapter_output,
        permit=permit,
        read_evidence=read_evidence,
        adapter_code_hash=adapter_code_hash,
    )
    if historical_h2_audit is not None:
        historical_h2_audit.require_materialization(
            input_materialization_hash=_normal_input_materialization_hash(inputs),
            session_hash=inputs.session_hash,
        )
    result = _run_selected_normal_experiment(
        args,
        inputs,
        historical_h2_audit=historical_h2_audit,
        adaptive_validation=adaptive_validation,
    )
    payload = _normal_experiment_payload(
        args,
        bundle,
        permit,
        result,
        config_hash,
        cost_model_hash,
        code_hash,
        inputs,
        adapter_attestation,
        adaptive_validation=adaptive_validation,
    )
    payload["evidence_materialization"] = _normal_evidence_materialization(
        args,
        payload,
    )
    exit_code = _normal_experiment_exit_code(args, payload)
    completed_payload = execution_ledger.complete(
        execution_identity,
        run_id=args.run_id,
        result_path=result_path,
        payload=payload,
        exit_code=exit_code,
    )
    return _materialize_completed_normal_experiment(
        args,
        context,
        completed_payload,
        exit_code=exit_code,
        execution_ledger=execution_ledger,
        execution_identity=execution_identity,
        result_path=result_path,
    )


def _require_normal_experiment_runtime(args, context: CLIContext) -> None:
    runtime = resolve_runtime_config(args, context.environ)
    if runtime is not None:
        require_implemented_execution_price(runtime.normal.execution_price)


def _normal_experiment_exit_code(args, payload) -> int:
    if _normal_experiment_id(args) != NORMAL_H1_EXPERIMENT_ID:
        return 1
    if args.access_mode == "sealed":
        return 0 if payload["formal_admission_eligible"] else 1
    evidence = payload.get("evidence_materialization")
    return 0 if isinstance(evidence, dict) and evidence.get("passed") is True else 1


def _normal_experiment_id(args) -> str:
    experiment_id = str(
        getattr(args, "experiment_id", NORMAL_H1_EXPERIMENT_ID)
        or NORMAL_H1_EXPERIMENT_ID
    ).strip()
    if experiment_id not in {
        NORMAL_H1_EXPERIMENT_ID,
        NORMAL_H2_EXPERIMENT_ID,
    }:
        raise ValueError("unsupported normal experiment_id")
    return experiment_id


def _run_selected_normal_experiment(
    args,
    inputs,
    *,
    historical_h2_audit: NormalH2HistoricalAudit | None = None,
    adaptive_validation: NormalAdaptiveValidationArtifact | None = None,
):
    baseline_provider, candidate_provider = inputs.providers()
    common = {
        "sessions": inputs.sessions,
        "initial_equity": args.initial_equity,
        "cost_bps_per_side": args.cost_bps_per_side,
        "stress_cost_bps_per_side": args.stress_cost_bps_per_side,
    }
    if _normal_experiment_id(args) == NORMAL_H2_EXPERIMENT_ID:
        return run_normal_h2_experiment(
            provider=baseline_provider,
            historical_audit_replay=True,
            **common,
        )
    return run_normal_h1_experiment(
        baseline_provider=baseline_provider,
        candidate_provider=candidate_provider,
        historical_h2_audit=historical_h2_audit,
        adaptive_validation=adaptive_validation,
        **common,
    )


def _require_normal_experiment_execution_mode(args, experiment_id: str) -> None:
    audit_replay = bool(getattr(args, "historical_audit_replay", False))
    if experiment_id == NORMAL_H1_EXPERIMENT_ID:
        if audit_replay:
            raise ValueError(
                "historical_audit_replay is reserved for rejected normal H2"
            )
        return
    if not audit_replay:
        raise PermissionError(
            "normal H2 is completed_rejected; use --historical-audit-replay "
            "for non-selectable diagnostics"
        )
    if args.access_mode not in {"research", "adaptive_development"}:
        raise PermissionError(
            "normal H2 historical audit cannot consume fixed or sealed data"
        )


def _resolve_normal_h2_historical_audit(
    args,
    *,
    environ,
    experiment_id: str,
    mode: str,
    request_start,
    request_end,
    warmup_start,
    warmup_end,
    dataset_hash: str,
    cost_model_hash: str,
    code_hash: str,
) -> NormalH2HistoricalAudit | None:
    explicit = str(
        getattr(args, "historical_trial_artifact", "") or ""
    ).strip()
    configured = str(
        environ.get(NORMAL_H2_AUDIT_ARTIFACT_PATH_ENV, "") or ""
    ).strip()
    path = explicit or configured
    if mode == "sealed":
        if explicit:
            raise ValueError(
                "sealed normal H1 requires adaptive validation evidence, not an H2 audit"
            )
        return None
    if experiment_id == NORMAL_H2_EXPERIMENT_ID:
        if path:
            raise ValueError(
                "normal H2 audit cannot consume its own historical artifact"
            )
        return None
    if not path:
        raise PermissionError(
            "normal H1 requires a signed, date-aligned H2 audit artifact before claim"
        )
    audit = load_normal_h2_historical_audit(
        Path(path),
        trusted_keys=trusted_normal_trial_keys(environ),
    )
    audit.require_compatible(
        mode=mode,
        request_start=request_start,
        request_end=request_end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
        dataset_hash=dataset_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
    )
    return audit


def _resolve_pending_normal_adaptive_validation(
    args,
    *,
    environ,
    experiment_id: str,
    mode: str,
) -> _PendingNormalAdaptiveValidation | None:
    """Read exact artifact bytes now; verify only after execution begin.

    Binding the file hash into the permit and execution identity prevents a
    time-of-check/time-of-use replacement.  Delaying JSON/signature validation
    until after ``ExperimentExecutionLedger.begin`` means a malformed sealed
    attempt cannot inspect protected data under a fresh retry identity.
    """

    explicit = str(
        getattr(args, "adaptive_validation_artifact", "") or ""
    ).strip()
    configured = str(
        environ.get(NORMAL_ADAPTIVE_VALIDATION_ARTIFACT_PATH_ENV, "") or ""
    ).strip()
    if mode != "sealed":
        if explicit:
            raise ValueError(
                "adaptive validation artifact is reserved for sealed normal H1"
            )
        return None
    if experiment_id != NORMAL_H1_EXPERIMENT_ID:
        raise PermissionError("sealed normal H2 execution is forbidden")
    path = Path(explicit or configured) if explicit or configured else None
    if path is None:
        raise PermissionError(
            "sealed normal H1 requires a signed adaptive validation artifact"
        )
    try:
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise ValueError(
            "normal adaptive validation artifact cannot be read"
        ) from error
    return _PendingNormalAdaptiveValidation(
        raw_bytes=raw_bytes,
        file_hash=sha256(raw_bytes).hexdigest(),
        path=path,
    )


def _verify_pending_normal_adaptive_validation(
    pending: _PendingNormalAdaptiveValidation | None,
    *,
    environ,
    strategy_protocol_hash: str,
    cost_model_hash: str,
    stockbot_source_hash: str,
    adapter_artifact_hash: str,
) -> NormalAdaptiveValidationArtifact | None:
    if pending is None:
        return None
    try:
        payload = json.loads(pending.raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "normal adaptive validation artifact is invalid JSON"
        ) from error
    artifact = verify_normal_adaptive_validation_artifact(
        payload,
        trusted_keys=trusted_normal_trial_keys(environ),
    )
    artifact.require_compatible(
        strategy_protocol_hash=strategy_protocol_hash,
        cost_model_hash=cost_model_hash,
        stockbot_source_hash=stockbot_source_hash,
        adapter_artifact_hash=adapter_artifact_hash,
    )
    return artifact


def _require_normal_experiment_result_path(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            "controlled normal experiments require a non-empty absolute output file"
        )
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        raise ValueError(
            "controlled normal experiments require an absolute output file"
        )
    return path.resolve()


def _begin_normal_experiment_execution(
    args,
    *,
    permit,
    experiment_id: str,
    result_path: Path,
):
    identity = ExperimentExecutionIdentity(
        strategy_family="normal",
        experiment_id=experiment_id,
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
        result_path=result_path,
    )
    return ledger, identity, state


def _resolve_normal_experiment_loader(args, context: CLIContext):
    """Resolve a deployment adapter without making it part of strategy logic."""

    if context.normal_experiment_loader is not None:
        return context.normal_experiment_loader
    environ = context.environ if context.environ is not None else os.environ
    spec = str(
        getattr(args, "adapter", "")
        or environ.get(NORMAL_ADAPTER_ENV, "")
        or ""
    ).strip()
    if not spec:
        return None
    module_name, separator, attribute = spec.partition(":")
    if (
        separator != ":"
        or not module_name.strip()
        or not attribute.strip()
        or "." in attribute
    ):
        raise ValueError(
            "normal adapter must use the exact module:callable format"
        )
    try:
        module = importlib.import_module(module_name.strip())
        loader = getattr(module, attribute.strip())
    except (ImportError, AttributeError) as error:
        raise ValueError("normal adapter cannot be imported") from error
    if not callable(loader):
        raise TypeError("normal adapter target must be callable")
    return loader


def _require_normal_canonical_ledger(ledger_path: str) -> None:
    canonical = os.environ.get(
        "STOCKBOT_EXPOSURE_LEDGER_PATH",
        "",
    ).strip()
    if not canonical:
        raise ValueError(
            "STOCKBOT_EXPOSURE_LEDGER_PATH must pin the canonical exposure ledger"
        )
    if canonical_exposure_ledger_path(
        canonical
    ) != canonical_exposure_ledger_path(ledger_path):
        raise ValueError("normal data access must use the canonical exposure ledger")


def _normal_experiment_bundle(args, context):
    environ = context.environ if context.environ is not None else os.environ
    bundle_root = args.bundle_root or environ.get("STOCKBOT_DATA_ROOT", "")
    if not bundle_root:
        raise ValueError(
            "normal-experiment-run requires --bundle-root or STOCKBOT_DATA_ROOT"
        )
    return PointInTimeParquetBundle(Path(bundle_root))


def _normal_adapter_unavailable(experiment_id, bundle, context):
    context.stdout.write(
        json.dumps(
            {
                "executed": False,
                "experiment_id": experiment_id,
                "formal_admission_eligible": False,
                "reason": "verified_pit_normal_adapter_unavailable",
                "dataset_hash": bundle.dataset_hash,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 2


def _normal_adapter_code_hash(loader):
    return normal_adapter_artifact_identity(loader).adapter_artifact_hash


def _normal_adapter_attestation(
    args,
    bundle,
    adapter_identity,
    *,
    environ=None,
):
    """Verify the external trust assertion before a sealed claim is recorded."""

    source = os.environ if environ is None else environ
    attestation_path = source.get(
        NORMAL_ADAPTER_ATTESTATION_PATH_ENV,
        "",
    ).strip()
    if not attestation_path:
        if args.access_mode == "sealed":
            raise PermissionError(
                f"sealed normal admission requires {NORMAL_ADAPTER_ATTESTATION_PATH_ENV}"
            )
        return None
    return verify_normal_adapter_attestation(
        Path(attestation_path),
        adapter_identity=adapter_identity,
        bundle_manifest=bundle.manifest,
        bundle_dataset_hash=bundle.dataset_hash,
        environ=source,
    )


def _normal_adapter_trust_identity(attestation):
    if attestation is None:
        return {
            "status": "untrusted_development",
            "issuer": None,
            "content_hash": None,
        }
    return {
        "status": "trusted",
        "issuer": attestation.issuer,
        "content_hash": attestation.content_hash,
    }


def _normal_experiment_code_hash(adapter_code_hash, adapter_attestation):
    return _normal_experiment_code_hash_from_source(
        source_tree_hash(),
        adapter_code_hash,
        adapter_attestation,
    )


def _normal_experiment_code_hash_from_source(
    stockbot_source_hash,
    adapter_code_hash,
    adapter_attestation,
):
    return hash_payload(
        {
            "stockbot_source_hash": stockbot_source_hash,
            "adapter_code_hash": adapter_code_hash,
            "adapter_trust": _normal_adapter_trust_identity(
                adapter_attestation
            ),
        }
    )


def _normal_strategy_protocol_config(experiment_id: str) -> dict[str, object]:
    """Return only strategy semantics, excluding data and adapter identity."""

    protocol_config: dict[str, object] = {
        "experiment": experiment_id,
        "baseline": "residual_momentum_12_1_neutral",
        "top_n": 20,
        "asset_lookback_sessions": 252,
        "adv_lookback_sessions": 20,
        "minimum_adv_dollars": 5_000_000.0,
    }
    if experiment_id == NORMAL_H1_EXPERIMENT_ID:
        protocol_config.update(
            {
                "candidate_addition": "quality_profitability_neutral",
                "quality_fields": (
                    "return_on_equity",
                    "gross_margin",
                    "debt_to_assets",
                    "accrual_ratio",
                ),
            }
        )
    elif experiment_id == NORMAL_H2_EXPERIMENT_ID:
        protocol_config.update(
            {
                "candidate_addition": "capital_efficient_allocation",
                "baseline_allocator": asdict(NORMAL_H2_BASELINE_CONFIG),
                "candidate_allocator": asdict(NORMAL_H2_CANDIDATE_CONFIG),
            }
        )
    else:
        raise ValueError("unsupported normal experiment_id")
    return protocol_config


def _normal_strategy_protocol_hash(experiment_id: str) -> str:
    return hash_payload(_normal_strategy_protocol_config(experiment_id))


def _require_unchanged_normal_adapter_trust(
    args,
    bundle,
    loader,
    adapter_identity,
    adapter_attestation,
    *,
    code_hash,
    environ,
):
    if args.access_mode == "sealed":
        require_formal_normal_adapter(loader)
    current_identity = normal_adapter_artifact_identity(loader)
    if current_identity != adapter_identity:
        raise PermissionError(
            "normal adapter artifact or schema mapping changed after claim"
        )
    current_attestation = _normal_adapter_attestation(
        args,
        bundle,
        current_identity,
        environ=environ,
    )
    if current_attestation != adapter_attestation:
        raise PermissionError("normal adapter attestation changed after claim")
    if (
        _normal_experiment_code_hash(
            current_identity.adapter_artifact_hash,
            current_attestation,
        )
        != code_hash
    ):
        raise PermissionError("normal source or adapter trust identity changed after claim")


def _normal_experiment_manifest(
    args,
    bundle,
    *,
    adapter_code_hash,
    adapter_attestation=None,
):
    experiment_id = _normal_experiment_id(args)
    if args.cost_bps_per_side != 5.0:
        raise ValueError("normal cost_bps_per_side is frozen at 5.0")
    if args.stress_cost_bps_per_side != 10.0:
        raise ValueError("normal stress_cost_bps_per_side is frozen at 10.0")
    start = parse_date(args.start)
    end = parse_date(args.end)
    warmup_start = parse_date(args.warmup_start) if args.warmup_start else None
    warmup_end = parse_date(args.warmup_end) if args.warmup_end else None
    if warmup_start is None or warmup_end is None:
        raise ValueError("normal experiments require the complete frozen warmup")
    require_exact_normal_experiment_window(
        mode=args.access_mode,
        request_start=start,
        request_end=end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
    )
    trust_identity = _normal_adapter_trust_identity(adapter_attestation)
    strategy_protocol_config = _normal_strategy_protocol_config(experiment_id)
    strategy_protocol_hash = hash_payload(strategy_protocol_config)
    protocol_config = {
        **strategy_protocol_config,
        "strategy_protocol_hash": strategy_protocol_hash,
        "adapter_code_hash": adapter_code_hash,
        "adapter_trust": trust_identity,
    }
    config_hash = hash_payload(protocol_config)
    cost_model_hash = hash_payload(
        normal_cost_model_payload(
            cost_bps_per_side=args.cost_bps_per_side,
            stress_cost_bps_per_side=args.stress_cost_bps_per_side,
        )
    )
    stockbot_source_hash = source_tree_hash()
    code_hash = _normal_experiment_code_hash_from_source(
        stockbot_source_hash,
        adapter_code_hash,
        adapter_attestation,
    )
    manifest = ValidationWindowManifest(
        strategy_family="normal",
        mode=args.access_mode,
        request_start=start,
        request_end=end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
        dataset_hash=bundle.dataset_hash,
        config_hash=config_hash,
        cost_model_hash=cost_model_hash,
        code_hash=code_hash,
    )
    return manifest, (
        start,
        end,
        warmup_start,
        warmup_end,
        config_hash,
        cost_model_hash,
        code_hash,
        stockbot_source_hash,
        strategy_protocol_hash,
    )


def _require_bundle_coverage(bundle, manifest):
    if (
        manifest.actual_start < bundle.manifest.coverage_start
        or manifest.actual_end > bundle.manifest.coverage_end
    ):
        raise ValueError("PIT bundle coverage does not contain the claimed interval")


def _normal_experiment_payload(
    args,
    bundle,
    permit,
    result,
    config_hash,
    cost_model_hash,
    code_hash,
    inputs,
    adapter_attestation,
    *,
    adaptive_validation: NormalAdaptiveValidationArtifact | None = None,
):
    experiment_id = _normal_experiment_id(args)
    if (
        permit.dataset_hash != bundle.dataset_hash
        or permit.config_hash != config_hash
        or permit.cost_model_hash != cost_model_hash
        or permit.code_hash != code_hash
    ):
        raise PermissionError("normal result identity changed after the data claim")
    decision = {
        "passed": result.decision.passed,
        "reasons": list(result.decision.reasons),
        "cagr_improvement": result.decision.cagr_improvement,
        "sharpe_improvement": result.decision.sharpe_improvement,
        "positive_window_fraction": result.decision.positive_window_fraction,
        "top_three_profit_contribution": result.decision.top_three_profit_contribution,
    }
    if experiment_id == NORMAL_H2_EXPERIMENT_ID:
        decision.update(
            {
                "baseline_capital_utilization": (
                    result.decision.baseline_capital_utilization
                ),
                "candidate_capital_utilization": (
                    result.decision.candidate_capital_utilization
                ),
            }
        )
    baseline = _normal_result_payload(result.baseline)
    candidate = _normal_result_payload(result.candidate)
    stress_baseline = _normal_result_payload(result.stressed_baseline)
    stress_candidate = _normal_result_payload(result.stressed_candidate)
    gross_leverage = _max_normal_gross_leverage(result.candidate)
    trial_history = _normal_trial_history_payload(result.candidate)
    metrics = {
        "declared_trials": result.declared_trials,
        "completed_trials": result.completed_trials,
        "decision": decision,
        "baseline": baseline,
        "candidate": candidate,
        "stress_baseline": stress_baseline,
        "stress_candidate": stress_candidate,
        "dsr_confidence": None if result.dsr is None else result.dsr.confidence,
        "pbo": None if result.pbo is None else result.pbo.probability,
        "pbo_strategy_ids": (
            [] if result.pbo is None else list(result.pbo.selected_strategy_ids)
        ),
        "gross_leverage": gross_leverage,
        "historical_h2_audit_hash": getattr(
            result, "historical_h2_audit_hash", None
        ),
        "adaptive_validation_hash": getattr(
            result, "adaptive_validation_hash", None
        ),
        "trial_history": trial_history,
    }
    input_materialization_hash = _normal_input_materialization_hash(inputs)
    output_hash = hash_payload(metrics)
    cost_suite = CostSuite(
        (
            CostScenario(
                "baseline",
                args.cost_bps_per_side,
                "baseline",
            ),
            CostScenario(
                "double_cost_stress",
                args.stress_cost_bps_per_side,
                "stress",
            ),
        )
    )
    is_sealed_h1 = (
        args.access_mode == "sealed"
        and experiment_id == NORMAL_H1_EXPERIMENT_ID
    )
    canonical_execution_artifact = None
    admission_metrics = None
    formal_decision = None
    if is_sealed_h1:
        if not inputs.provenance_verified or adapter_attestation is None:
            raise PermissionError(
                "sealed normal admission requires verified PIT provenance and adapter trust"
            )
        if not isinstance(adaptive_validation, NormalAdaptiveValidationArtifact):
            raise PermissionError(
                "sealed normal admission requires verified adaptive validation"
            )
        if result.adaptive_validation_hash != adaptive_validation.content_hash:
            raise PermissionError(
                "sealed normal result does not match adaptive validation"
            )
        canonical_execution_artifact = build_normal_canonical_execution_artifact(
            experiment_id=experiment_id,
            baseline=result.baseline,
            candidate=result.candidate,
            stress_candidate=result.stressed_candidate,
            adaptive_validation=adaptive_validation,
            cost_model_payload=normal_cost_model_payload(
                cost_bps_per_side=args.cost_bps_per_side,
                stress_cost_bps_per_side=args.stress_cost_bps_per_side,
            ),
            expected_cost_model_hash=permit.cost_model_hash,
        )
        admission_metrics = _normal_admission_metrics(
            result,
            permit=permit,
            gross_leverage=gross_leverage,
        )
        # This preview is used only to make the immutable report self-describing.
        # The RequirementEvidence below is created exclusively by the canonical
        # builder and is the sole formal promotion decision.
        formal_decision = evaluate_normal_admission(admission_metrics)
    if experiment_id == NORMAL_H2_EXPERIMENT_ID:
        status = "historical_audit_completed_rejected"
        report_passed = False
        report_reasons = list(result.decision.reasons)
    elif formal_decision is not None:
        status = "admitted_candidate" if formal_decision.passed else "sealed_rejected"
        report_passed = formal_decision.passed
        report_reasons = list(formal_decision.reasons)
    else:
        status = (
            "development_rejected"
            if not result.decision.passed
            else "development_not_proven"
        )
        report_passed = False
        report_reasons = list(result.decision.reasons)
    observed_at = datetime.now(timezone.utc)
    report = ExperimentRunReport(
        experiment_id=experiment_id,
        run_id=args.run_id,
        run_type=(
            "historical_audit_replay"
            if experiment_id == NORMAL_H2_EXPERIMENT_ID
            else "sealed_backtest"
            if args.access_mode == "sealed"
            else "historical_backtest"
        ),
        observed_at=observed_at,
        dataset_hash=permit.dataset_hash,
        input_materialization_hash=input_materialization_hash,
        config_hash=permit.config_hash,
        code_hash=permit.code_hash,
        cost_model_hash=permit.cost_model_hash,
        cost_suite=cost_suite,
        output_hash=output_hash,
        metrics=metrics,
        admission={
            "passed": report_passed,
            "formal_admission_eligible": report_passed,
            "strategy_gate_passed": result.decision.passed,
            "reasons": report_reasons,
            "status": status,
        },
    )
    admission_evidence = None
    if admission_metrics is not None:
        manifest = RunManifest(
            run_type="sealed_backtest",
            observed_at=observed_at,
            dataset_hash=permit.dataset_hash,
            config_hash=permit.config_hash,
            code_hash=permit.code_hash,
            policy_version=ACCEPTANCE_POLICY_VERSION,
            cost_model_hash=permit.cost_model_hash,
            input_materialization_hash=input_materialization_hash,
            output_hash=output_hash,
            report_hash=report.report_hash,
            cost_suite_hash=cost_suite.suite_hash,
            run_id=args.run_id,
            experiment_id=experiment_id,
        )
        admission_evidence = build_normal_admission_evidence(
            admission_metrics,
            manifest,
        )
        if admission_evidence.passed != report_passed:
            raise RuntimeError("normal admission report and canonical evidence disagree")
    formally_proven = bool(
        admission_evidence is not None and admission_evidence.passed
    )
    payload = {
        "executed": True,
        "experiment_id": experiment_id,
        "status": status,
        "access_mode": args.access_mode,
        "formal_admission_eligible": formally_proven,
        "dataset_hash": permit.dataset_hash,
        "config_hash": permit.config_hash,
        "cost_model_hash": permit.cost_model_hash,
        "code_hash": permit.code_hash,
        "permit_record_hash": permit.record_hash,
        "adapter_code_hash": inputs.adapter_code_hash,
        "adapter_attestation_verified": adapter_attestation is not None,
        "adapter_attestation_issuer": (
            None if adapter_attestation is None else adapter_attestation.issuer
        ),
        "adapter_attestation_hash": (
            None if adapter_attestation is None else adapter_attestation.content_hash
        ),
        "session_hash": inputs.session_hash,
        "provenance_verified": inputs.provenance_verified,
        "bundle_read_evidence_hash": inputs.read_evidence.evidence_hash,
        "bundle_read_tables": sorted(inputs.read_evidence.tables),
        "input_materialization_hash": input_materialization_hash,
        "output_hash": output_hash,
        "cost_suite_hash": cost_suite.suite_hash,
        "report_hash": report.report_hash,
        "experiment_run_report": report.to_payload(),
        "admission_evidence": (
            None
            if admission_evidence is None
            else admission_evidence_payload(admission_evidence)
        ),
        "declared_trials": result.declared_trials,
        "completed_trials": result.completed_trials,
        "decision": decision,
        "baseline": baseline,
        "candidate": candidate,
        "stress_baseline": stress_baseline,
        "stress_candidate": stress_candidate,
        "dsr_confidence": metrics["dsr_confidence"],
        "pbo": metrics["pbo"],
        "gross_leverage": gross_leverage,
        "historical_h2_audit_hash": metrics["historical_h2_audit_hash"],
        "adaptive_validation_hash": metrics["adaptive_validation_hash"],
        "trial_history": trial_history,
    }
    if canonical_execution_artifact is not None:
        payload["canonical_execution_artifact"] = canonical_execution_artifact
        payload["canonical_execution_artifact_hash"] = hash_payload(
            canonical_execution_artifact
        )
    return payload


def _normal_admission_metrics(result, *, permit, gross_leverage):
    identity = EvaluationIdentity(
        start=permit.request_start,
        end=permit.request_end,
        dataset_hash=permit.dataset_hash,
        cost_model_hash=permit.cost_model_hash,
        config_hash=permit.config_hash,
        code_hash=permit.code_hash,
    )
    return NormalAdmissionMetrics(
        candidate_identity=identity,
        baseline_identity=identity,
        net_cagr=result.candidate.summary.annual_return,
        baseline_net_cagr=result.baseline.summary.annual_return,
        sharpe=result.candidate.summary.sharpe,
        baseline_sharpe=result.baseline.summary.sharpe,
        net_excess_annual=result.candidate.summary.excess_alpha_annual,
        max_drawdown=result.candidate.summary.max_drawdown,
        annual_turnover=result.candidate.annual_turnover,
        stressed_net_excess_annual=(
            result.stressed_candidate.summary.excess_alpha_annual
        ),
        positive_window_fraction=result.decision.positive_window_fraction,
        dsr_confidence=0.0 if result.dsr is None else result.dsr.confidence,
        pbo=1.0 if result.pbo is None else result.pbo.probability,
        top_three_profit_contribution=(
            result.decision.top_three_profit_contribution
        ),
        gross_leverage=gross_leverage,
        declared_trials=result.declared_trials,
        completed_trials=result.completed_trials,
        sealed_data_access_permit=permit,
    )


def _max_normal_gross_leverage(result) -> float:
    return max(
        (
            sum(abs(float(weight)) for weight in point.weights.values())
            for point in result.points
        ),
        default=0.0,
    )


def _normal_trial_history_payload(result) -> dict[str, object]:
    dates = tuple(point.trading_date for point in result.points)
    returns = tuple(result.daily_returns)
    return {
        "observation_dates": [value.isoformat() for value in dates],
        "daily_returns": list(returns),
        "return_history_hash": normal_trial_return_history_hash(dates, returns),
    }


def _normal_input_materialization_hash(inputs) -> str:
    """Hash actual PIT partitions and sessions without run-id-specific permit data."""

    receipts = sorted(
        (receipt.as_payload() for receipt in inputs.read_evidence.receipts),
        key=hash_payload,
    )
    return hash_payload(
        {
            "dataset_hash": inputs.read_evidence.dataset_hash,
            "receipts": receipts,
            "trading_calendar_sessions": [
                session.isoformat()
                for session in inputs.read_evidence.trading_calendar_sessions
            ],
            "session_hash": inputs.session_hash,
        }
    )


def _normal_evidence_materialization(args, payload) -> dict[str, object]:
    """Build the complete deterministic evidence record before completion."""

    is_sealed = args.access_mode == "sealed"
    if is_sealed:
        raw_evidence = payload.get("admission_evidence")
        if not isinstance(raw_evidence, dict):
            raise ValueError(
                "sealed normal output requires canonical admission evidence"
            )
        evidence = RequirementEvidence(**raw_evidence)
        if (
            evidence.requirement_id != "normal_strategy_admission"
            or evidence.passed != payload["formal_admission_eligible"]
            or evidence.report_hash != payload["report_hash"]
            or evidence.run_id != args.run_id
            or evidence.experiment_id != NORMAL_H1_EXPERIMENT_ID
        ):
            raise PermissionError(
                "sealed normal admission evidence does not match the run payload"
            )
        fields = {
            "requirement_id": evidence.requirement_id,
            "passed": evidence.passed,
            "detail": evidence.detail,
            "run_type": evidence.run_type,
            "observed_at": evidence.observed_at,
            "dataset_hash": evidence.dataset_hash,
            "input_materialization_hash": evidence.input_materialization_hash,
            "output_hash": evidence.output_hash,
            "report_hash": evidence.report_hash,
            "cost_suite_hash": evidence.cost_suite_hash,
            "config_hash": evidence.config_hash,
            "cost_model_hash": evidence.cost_model_hash,
            "code_hash": evidence.code_hash,
            "policy_version": evidence.policy_version,
            "run_id": evidence.run_id,
            "experiment_id": evidence.experiment_id,
        }
    else:
        is_h2 = payload.get("experiment_id") == NORMAL_H2_EXPERIMENT_ID
        report = payload.get("experiment_run_report")
        if not isinstance(report, dict) or not isinstance(
            report.get("observed_at"), str
        ):
            raise ValueError("normal experiment report must contain observed_at")
        requirement_id = (
            "normal_h2_experiment" if is_h2 else "normal_h1_experiment"
        )
        evidence_passed = False if is_h2 else bool(
            payload["decision"]["passed"]
            and payload.get("provenance_verified", False)
        )
        detail = (
            "experiment_id=%s decision=%s reasons=%s permit_record_hash=%s "
            "bundle_read_evidence_hash=%s cost_model_hash=%s"
            % (
                payload.get("experiment_id", NORMAL_H1_EXPERIMENT_ID),
                str(bool(payload["decision"]["passed"])).lower(),
                ",".join(payload["decision"]["reasons"]),
                payload["permit_record_hash"],
                payload["bundle_read_evidence_hash"],
                payload["cost_model_hash"],
            )
        )
        fields = {
            "requirement_id": requirement_id,
            "passed": evidence_passed,
            "detail": detail,
            "run_type": "historical_audit_replay" if is_h2 else "historical_backtest",
            "observed_at": report["observed_at"],
            "dataset_hash": payload["dataset_hash"],
            "input_materialization_hash": payload["input_materialization_hash"],
            "output_hash": payload["output_hash"],
            "report_hash": payload["report_hash"],
            "cost_suite_hash": payload["cost_suite_hash"],
            "config_hash": payload["config_hash"],
            "cost_model_hash": payload["cost_model_hash"],
            "code_hash": payload["code_hash"],
            "policy_version": None,
            "run_id": args.run_id,
            "experiment_id": payload["experiment_id"],
        }
    return fields


def _record_normal_evidence_idempotently(path: Path, materialization) -> None:
    if not isinstance(materialization, dict):
        raise ValueError("normal evidence materialization must be an object")
    required = {
        "requirement_id",
        "passed",
        "detail",
        "run_type",
        "observed_at",
        "dataset_hash",
        "input_materialization_hash",
        "output_hash",
        "report_hash",
        "cost_suite_hash",
        "config_hash",
        "cost_model_hash",
        "code_hash",
        "policy_version",
        "run_id",
        "experiment_id",
    }
    if set(materialization) != required:
        raise ValueError("normal evidence materialization fields are invalid")
    EvidenceRecorder(Path(path)).record(
        materialization["requirement_id"],
        materialization["passed"],
        materialization["detail"],
        run_type=materialization["run_type"],
        observed_at=materialization["observed_at"],
        dataset_hash=materialization["dataset_hash"],
        input_materialization_hash=materialization[
            "input_materialization_hash"
        ],
        output_hash=materialization["output_hash"],
        report_hash=materialization["report_hash"],
        cost_suite_hash=materialization["cost_suite_hash"],
        config_hash=materialization["config_hash"],
        cost_model_hash=materialization["cost_model_hash"],
        code_hash=materialization["code_hash"],
        policy_version=materialization["policy_version"],
        run_id=materialization["run_id"],
        experiment_id=materialization["experiment_id"],
        idempotency_fields=("requirement_id", "run_id", "report_hash"),
    )


def _materialize_completed_normal_experiment(
    args,
    context,
    payload,
    *,
    exit_code,
    execution_ledger,
    execution_identity,
    result_path,
) -> int:
    completed = execution_ledger.verify_completed(
        execution_identity,
        run_id=args.run_id,
        result_path=result_path,
    )
    payload = completed.result_payload
    if not isinstance(payload, dict):
        raise ValueError("completed normal result payload is unavailable")
    if (
        payload.get("execution_complete") is not True
        or payload.get("execution_exit_code") != exit_code
        or payload.get("execution_run_id") != args.run_id
    ):
        raise PermissionError("completed normal result identity is invalid")
    materialization = payload.get("evidence_materialization")
    if args.access_mode == "sealed":
        materialize_completed_strategy_admission(
            evidence_path=Path(args.evidence_file),
            exposure_ledger_path=args.exposure_ledger,
            execution_ledger=execution_ledger,
            execution_identity=execution_identity,
            run_id=args.run_id,
            result_path=result_path,
        )
    else:
        _record_normal_evidence_idempotently(
            Path(args.evidence_file),
            materialization,
        )
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return int(exit_code)


def _write_normal_experiment_payload(args, context, payload):
    """Compatibility writer for tests and non-ledger callers.

    Controlled CLI execution uses ``ExperimentExecutionLedger.complete`` and
    then ``_materialize_completed_normal_experiment`` instead.
    """

    if args.access_mode == "sealed":
        raise ValueError(
            "sealed normal evidence requires an authenticated completed execution"
        )
    materialization = _normal_evidence_materialization(args, payload)
    payload["evidence_materialization"] = materialization
    EvidenceRecorder(Path(args.evidence_file)).record(
        materialization["requirement_id"],
        materialization["passed"],
        materialization["detail"],
        **{
            key: value
            for key, value in materialization.items()
            if key not in {"requirement_id", "passed", "detail"}
        },
    )
    if args.output_file:
        output = Path(args.output_file)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")


def _normal_result_payload(result):
    return {
        "annual_return": result.summary.annual_return,
        "benchmark_annual_return": result.summary.benchmark_annual_return,
        "excess_alpha_annual": result.summary.excess_alpha_annual,
        "sharpe": result.summary.sharpe,
        "max_drawdown": result.summary.max_drawdown,
        "annual_turnover": result.annual_turnover,
        "fees": result.fees,
        "observations": len(result.points),
    }


def _oos_config(args, include_cost=False):
    values = {
        "symbols": parse_symbols(args.symbols),
        "benchmark": args.benchmark.strip().upper(),
        "train_start": parse_date(args.train_start),
        "train_end": parse_date(args.train_end),
        "validation_start": parse_date(args.validation_start),
        "validation_end": parse_date(args.validation_end),
        "lookback_days": args.lookback_days,
        "top_n": args.top_n,
    }
    if include_cost:
        values["two_way_cost_rate"] = args.two_way_cost_rate
    return MomentumOOSBacktestConfig(**values)


def _record_oos_evidence(args, prices, config, result, decision, reasons, source_name, passed):
    detail = (
        "source=%s symbols=%d benchmark=%s train=%s..%s validation=%s..%s "
        "lookback_days=%d top_n=%d train_observations=%d validation_observations=%d "
        "validation_rebalances=%d coverage_reasons=%s train_excess_alpha=%.6f "
        "train_sharpe=%.6f excess_alpha=%.6f sharpe=%.6f max_drawdown=%.6f "
        "annual_turnover=%.6f annual_cost_drag=%.6f reasons=%s"
        % (
            source_name,
            len(result.symbols),
            result.benchmark,
            config.train_start.isoformat(),
            config.train_end.isoformat(),
            config.validation_start.isoformat(),
            config.validation_end.isoformat(),
            config.lookback_days,
            config.top_n,
            result.train_observations,
            result.validation_observations,
            result.validation_rebalances,
            ",".join(result.coverage_reasons),
            result.train_summary.excess_alpha_annual,
            result.train_summary.sharpe,
            result.summary.excess_alpha_annual,
            result.summary.sharpe,
            result.summary.max_drawdown,
            result.annual_turnover,
            decision.annual_cost_drag,
            ",".join(reasons),
        )
    )
    append_evidence(
        args.evidence_file,
        requirement_id="oos_backtest_acceptance",
        passed=passed,
        detail=detail,
        run_type="development_smoke",
        dataset_hash=hash_payload(normalized_prices_payload(prices)),
        config_hash=hash_payload(oos_backtest_config_payload(config, args, source_name)),
        code_hash=source_tree_hash(),
    )


def handle_historical_replay(args, context: CLIContext) -> int:
    config = _oos_config(args)
    data_start = parse_date(args.data_start)
    data_end = parse_date(args.data_end)
    assert_validation_data_released(
        strategy_family="normal",
        start=data_start,
        end=data_end,
    )
    loader, source_name = resolve_oos_price_loader(
        args, context.environ, context.oos_price_loader
    )
    prices = loader(
        symbols=config.symbols,
        benchmark=config.benchmark,
        start=data_start,
        end=data_end,
    )
    result = run_momentum_oos_backtest(prices, config)
    replay = build_historical_replay(
        result.validation_daily_returns,
        initial_equity=args.initial_equity,
    )
    replay, provenance = _stamp_historical_replay(replay, result, config, source_name)
    _write_historical_journals(args, replay)
    _record_historical_evidence(args, replay, provenance)
    payload = _historical_payload(args, replay, result, source_name)
    context.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


def _stamp_historical_replay(replay, result, config, source_name):
    dataset_hash = hash_payload(
        {
            "source": source_name,
            "validation_returns": [
                {
                    "trading_date": point.trading_date,
                    "portfolio_return": point.portfolio_return,
                    "benchmark_return": point.benchmark_return,
                }
                for point in result.validation_daily_returns
            ],
        }
    )
    provenance = {
        "run_type": "historical_replay",
        "observed_at": utc_now_iso(),
        "dataset_hash": dataset_hash,
        "config_hash": hash_payload(config.__dict__),
        "code_hash": source_tree_hash(),
    }
    return replace(
        replay,
        paper_snapshots=[replace(snapshot, **provenance) for snapshot in replay.paper_snapshots],
        paper_runs=[replace(run, **provenance) for run in replay.paper_runs],
        live_snapshots=[replace(snapshot, **provenance) for snapshot in replay.live_snapshots],
        live_sessions=[replace(run, **provenance) for run in replay.live_sessions],
    ), provenance


def _write_historical_journals(args, replay):
    PaperAccountSnapshotJournal(args.paper_snapshot_log).write_all(replay.paper_snapshots)
    PaperTradingJournal(args.paper_runs_log).write_all(replay.paper_runs)
    LiveEquitySnapshotJournal(args.live_snapshot_log).write_all(replay.live_snapshots)
    LiveTradingJournal(args.live_runs_log).write_all(replay.live_sessions)


def _record_historical_evidence(args, replay, provenance):
    recorder = EvidenceRecorder(args.evidence_file)
    evidence_sets = (
        build_paper_trading_evidence(replay.paper_runs),
        build_live_trading_evidence(
            replay.live_sessions,
            min_sessions=args.min_live_sessions,
            max_drawdown=args.max_live_drawdown,
        ),
    )
    for evidence in evidence_sets:
        for item in evidence.values():
            recorder.record(
                item.requirement_id,
                item.passed,
                "historical_replay_only %s" % item.detail,
                **provenance,
            )


def _historical_payload(args, replay, result, source_name):
    paper_status = paper_status_payload(
        replay.paper_runs,
        min_weeks=args.min_paper_weeks,
        max_performance_deviation=args.max_paper_performance_deviation,
        enforce_policy=False,
    )
    live_status = live_status_payload(
        replay.live_sessions,
        min_sessions=args.min_live_sessions,
        max_drawdown=args.max_live_drawdown,
        enforce_policy=False,
    )
    return {
        "historical_replay": True,
        "counts_for_real_acceptance": False,
        "source": source_name,
        "paper": {
            "snapshots": len(replay.paper_snapshots),
            "runs": len(replay.paper_runs),
            "status": paper_status,
            "skipped_periods": replay.skipped_paper_periods,
        },
        "live": {
            "snapshots": len(replay.live_snapshots),
            "sessions": len(replay.live_sessions),
            "status": live_status,
            "skipped_days": replay.skipped_live_days,
        },
        "oos": {
            "summary": summary_to_payload(result.summary),
            "train_summary": summary_to_payload(result.train_summary),
            "validation_observations": result.validation_observations,
            "validation_rebalances": result.validation_rebalances,
            "coverage_reasons": list(result.coverage_reasons),
        },
        "logs": {
            "evidence_file": args.evidence_file,
            "paper_snapshot_log": args.paper_snapshot_log,
            "paper_runs_log": args.paper_runs_log,
            "live_snapshot_log": args.live_snapshot_log,
            "live_runs_log": args.live_runs_log,
        },
    }
