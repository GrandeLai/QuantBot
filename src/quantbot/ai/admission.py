"""Connect frozen research evidence to runtime identity without granting trading permission.

The AI runners do not yet execute the formally admitted stock families or the
frozen OKX strategy. An engineering replay, a caller-supplied ``passed`` flag,
or an authentic result for another implementation cannot authorize them.
"""

from dataclasses import dataclass
from pathlib import Path

from quantbot.audit import CompletionAudit, load_evidence_file
from quantbot.evidence import hash_payload, source_tree_hash


def strategy_identity(spec, config, code_hash):
    """Identify the actual frozen decision, risk, data and execution configuration."""
    identity = {
        "market": spec["market"], "provider": spec["provider"],
        "spec_hash": hash_payload(spec), "config_hash": hash_payload(config),
        "code_hash": code_hash,
        "risk_hash": hash_payload({
            "risk": config["risk"],
            "max_daily_loss": spec["max_daily_loss"],
            "max_drawdown": spec["max_drawdown"],
            "max_model_failures": spec["max_model_failures"],
        }),
        "execution_hash": hash_payload({"execution": config["execution"], "bar": spec["bar"]}),
    }
    return {**identity, "strategy_version": hash_payload(identity)}


@dataclass(frozen=True)
class AdmissionReference:
    """References persisted evidence; no caller-supplied approval booleans."""

    root: str
    job_id: str
    study_id: str | None = None
    evidence_path: str | None = None
    strategy_family: str = "daytrade"


def assess_strategy_admission(store, job_id, *, study_id=None, evidence_path=None, strategy_family="daytrade"):
    """Read existing market policies and bind their evidence to the selected job."""
    job = store.get(job_id)
    identity = strategy_identity(job["spec"], job["config"], job["code_hash"])
    checks = {"current_code_matches": job["code_hash"] == source_tree_hash()}
    evidence = None
    if job["spec"]["market"] == "crypto":
        from .crypto_admission import assess_crypto_study
        from .research import get_study

        checks["study_provided"] = bool(study_id)
        if study_id:
            study = get_study(store, study_id)
            selected = (study["report"] or {}).get("selected_job_id")
            candidate = next((j for j in study["plan"]["candidates"] if j["id"] == selected), None)
            checks["selected_frozen_strategy_matches"] = bool(
                candidate and selected == job_id
                and strategy_identity(candidate["spec"], candidate["config"], candidate["code_hash"]) == identity
            )
            # Do not open another strategy's input snapshot merely to reject it.
            if checks["selected_frozen_strategy_matches"]:
                evidence = assess_crypto_study(store, study_id)
                checks["market_backtest_policy_passed"] = evidence["backtest_passed"] is True
    else:
        if strategy_family not in {"normal", "daytrade"}:
            raise ValueError("美股准出策略族必须为 normal 或 daytrade")
        requirement = strategy_family + "_strategy_admission"
        checks["formal_evidence_provided"] = bool(evidence_path)
        if evidence_path:
            records = load_evidence_file(Path(evidence_path))
            audit = CompletionAudit([requirement]).evaluate(records)
            checks["authenticated_market_policy_passed"] = audit.complete
            record = records.get(requirement)
            checks["frozen_config_and_code_match"] = bool(
                record and record.config_hash == identity["config_hash"]
                and record.code_hash == identity["code_hash"]
            )
            evidence = {"requirement_id": requirement,
                        "formal_record_hash": record.formal_record_hash if record else None,
                        "policy_passed": audit.complete}
        # The authenticated stock proofs currently identify fixed H1/H9
        # implementations, not any AI/price-action provider loaded by this runner.
        checks["admitted_strategy_implementation_bound"] = False
    backtest_passed = all(checks.values())
    return {
        "job_id": job_id, "identity": identity, "evidence": evidence,
        "backtest_passed": backtest_passed, "passed": backtest_passed,
        "checks": checks, "reasons": [name for name, passed in checks.items() if not passed],
        "broker_paper_authorized": False, "live_authorized": False,
        "execution_checks": {"frozen_strategy_broker_adapter_verified": False},
        "next_stage": "frozen_paper_adapter_validation" if backtest_passed else "backtest_research",
        "boundary": "只读准出检查；工程完成不等于盈利准出。当前券商执行器尚未与冻结策略及撮合契约绑定，"
                    "不签发模拟盘或实盘授权；既有市场阈值不变。",
    }


def require_broker_paper_admission(config, provider, reference):
    """Fail before broker construction; never accept a detached approval report."""
    from .models import DemoProvider, PriceActionProvider
    from .runtime import RuntimeStore

    if not isinstance(reference, AdmissionReference):
        raise ValueError("券商模拟盘缺少冻结策略准出引用；confirm-paper 不是盈利准出证据")
    root = Path(reference.root).expanduser().resolve()
    if not (root / "runtime.sqlite").is_file():
        raise ValueError("准出任务目录不存在，不能创建空证据后放行")
    store = RuntimeStore(root)
    job = store.get(reference.job_id)
    provider_name = "demo" if isinstance(provider, DemoProvider) else (
        "price_action" if isinstance(provider, PriceActionProvider) else "ai")
    if (job["spec"]["market"] != "stocks" or job["spec"]["provider"] != provider_name
            or job["config"] != config):
        raise ValueError("券商执行的市场、策略或完整配置与冻结任务不一致")
    report = assess_strategy_admission(
        store, reference.job_id, study_id=reference.study_id,
        evidence_path=reference.evidence_path, strategy_family=reference.strategy_family,
    )
    if not report["backtest_passed"]:
        raise ValueError("策略尚未通过绑定版本的盈利准出：" + ",".join(report["reasons"]))
    if not report["broker_paper_authorized"]:
        raise ValueError("冻结策略尚未完成券商撮合与账本契约验证，模拟盘未授权")
    return report
