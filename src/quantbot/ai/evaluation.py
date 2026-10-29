"""候选版本的统一历史评估：同一数据、风控和次根撮合；不自动批准实盘。"""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import time
from uuid import uuid4

from quantbot.evidence import hash_payload, source_tree_hash

from .feeds import fetch_snapshot
from .journal import runtime_lock
from .local_execution import (
    account_view,
    advance,
    cancel_pending,
    create_plan,
    model_account,
    matching_model,
    new_account,
    queue_plan,
)
from .models import DemoProvider, PriceActionProvider
from .corporate_actions import check_action_window
from .replay_evidence import ReplayJournal
from .runtime import encode, provider_for
from .transport import ProviderError


def replay(snapshot, spec, config, provider, *, last_bars=30, times=None, journal_path=None):
    selected = snapshot.times[-last_bars:] if times is None else times
    if journal_path is None:
        result = _replay(snapshot, spec, config, provider, last_bars=last_bars, times=times)
        return {**result, "engineering_passed": all(engineering_checks(result, len(selected)).values()),
                "broker_paper_authorized": False}
    with ReplayJournal(journal_path, {"spec": spec, "config": config, "code_hash": source_tree_hash(),
            "corporate_actions": snapshot.actions or [],
            "provider_implementation": type(provider).__qualname__, "window": [at.isoformat() for at in selected]}) as journal:
        result = _replay(snapshot, spec, config, provider, last_bars=last_bars, times=times, journal=journal)
        checks = engineering_checks(result, len(selected))
        journal.write("completed", {"execution_completed": all(checks.values()),
                                   "engineering_passed": all(checks.values()),
                                   "broker_paper_authorized": False,
                                   "checks": checks, "account": result["account"]})
    return {**result, "journal_path": str(journal_path), "engineering_passed": all(checks.values()),
            "broker_paper_authorized": False}


def _replay(snapshot, spec, config, provider, *, last_bars=30, times=None, journal=None):
    selected_times = snapshot.times[-last_bars:] if times is None else times
    if len(selected_times) < 3 or (times is None and len(selected_times) != last_bars):
        raise ValueError("历史评估需要完整的请求区间，且至少 3 根预热完成后的 K 线")
    # 指标也读取回测开始前的历史，不能让预热区间中的未知拆股污染信号。
    check_action_window(snapshot.actions, snapshot.times[0].date().isoformat(), selected_times[-1].date().isoformat())
    selected = [snapshot.context(at) for at in selected_times]
    state = new_account(config["execution"]["initial_cash"])
    records, errors, curve, decisions, execution_waits = [], [], [], [], []
    elapsed, tokens, provider_calls, usage_calls = 0.0, 0, 0, 0
    confidence = {"0.0-0.5": [], "0.5-0.7": [], "0.7-0.9": [], "0.9-1.0": []}
    for index, context in enumerate(selected):
        action_count = len(state["corporate_action_events"])
        step_orders = advance(state, snapshot, context, config, spec)
        records += step_orders
        view = account_view(state)
        curve.append({"as_of": context.as_of.isoformat(), **view})
        if journal is not None:
            journal.write("account", {"as_of": context.as_of.isoformat(), "account": view, "orders": step_orders,
                                      "corporate_actions": state["corporate_action_events"][action_count:],
                                      "valuation_prices": dict(context.prices)})
        # Execution-aware research policies consume actual reports, never infer
        # cancellations from a future corporate-action calendar. Isolate the ledger.
        observer = getattr(provider, "observe_execution", None)
        if observer is not None:
            observer(context, tuple(deepcopy(step_orders)))
        if index == len(selected) - 1 or state["risk_latched"]:
            canceled = cancel_pending(state, "evaluation_end_or_risk_hold",
                                      keep_exits=index != len(selected) - 1)
            records += canceled
            if journal is not None and canceled:
                journal.write("orders_canceled", {"as_of": context.as_of.isoformat(), "orders": canceled})
            continue
        if state["pending"]:
            waiting = {"as_of": context.as_of.isoformat(),
                       "pending_orders": deepcopy(state["pending"]), "reason": "awaiting_fills"}
            execution_waits.append(waiting)
            if journal is not None:
                journal.write("execution_wait", waiting)
            continue
        start = time.monotonic()
        provider_calls += 1
        account = model_account(state, context, spec["market"])
        if journal is not None:
            journal.write("decision_started", {"as_of": context.as_of.isoformat(), "market": context.payload(),
                                                "account": account.payload(context)})
        decision = None
        try:
            decision = provider.decide(context, account, config)
            usage = decision.usage.get("total_tokens")
            if type(usage) is int and usage >= 0:
                tokens += usage
                usage_calls += 1
            plan = create_plan(state, context, decision, config, spec["market"], "evaluation")
            records += queue_plan(state, plan)
            decisions.append({"as_of": context.as_of.isoformat(), "snapshot_id": context.snapshot_id,
                              **plan, "raw_decision": asdict(decision)})
            if context.sec_events is not None:
                decisions[-1]["sec_events"] = context.sec_events
            future = selected[index + 1]
            for allocation in decision.allocations:
                if allocation.target_weight <= 0:
                    continue
                if any(a["symbol"] == allocation.symbol and context.as_of.date().isoformat() < a["date"]
                       <= future.as_of.date().isoformat() for a in snapshot.actions or []):
                    continue  # 公司行动跨日的原始收盘价不能直接比较涨跌。
                bucket = (
                    "0.0-0.5"
                    if allocation.confidence < 0.5
                    else "0.5-0.7"
                    if allocation.confidence < 0.7
                    else "0.7-0.9"
                    if allocation.confidence < 0.9
                    else "0.9-1.0"
                )
                confidence[bucket].append(
                    future.prices[allocation.symbol] > context.prices[allocation.symbol]
                )
        except (ProviderError, ValueError) as exc:
            errors.append({"as_of": context.as_of.isoformat(), "error": str(exc)})
            if decision is not None:
                errors[-1]["raw_decision"] = asdict(decision)
            if context.sec_events is not None:
                errors[-1]["sec_events"] = context.sec_events
            if journal is not None:
                journal.write("decision_error", errors[-1])
        else:
            if journal is not None:
                journal.write("decision", decisions[-1])
        elapsed += time.monotonic() - start
    view = account_view(state)
    return {
        "initial_equity": config["execution"]["initial_cash"],
        "final_equity": view["equity"],
        "return_fraction": view["equity"] / config["execution"]["initial_cash"] - 1,
        "max_drawdown_fraction": min(row["drawdown_fraction"] for row in curve),
        "fills": sum(o["status"] == "filled" for o in records),
        "orders": records,
        "equity_curve": curve,
        "decisions": decisions,
        "execution_waits": execution_waits,
        "errors": errors,
        "provider_calls": provider_calls,
        "reported_tokens": tokens if usage_calls else None,
        "usage_reported_calls": usage_calls,
        "usage_missing_calls": provider_calls - usage_calls,
        "decision_seconds": elapsed,
        "account": view,
        "corporate_action_events": state["corporate_action_events"],
        "corporate_action_basis": {
            "dividend_amount": "gross_without_tax_withholding",
            "cash_availability": "pay_date_or_next_session_close",
            "pending_orders": "cancel_entire_plan_if_any_order_affected_and_replan",
            "fractional_shares": "rejected_without_issuer_terms",
        } if spec["market"] == "stocks" else None,
        "risk_hold": state["risk_latched"],
        "confidence_observations": {
            k: {"samples": len(v), "next_close_up_fraction": sum(v) / len(v) if v else None}
            for k, v in confidence.items()
        },
        "confidence_basis": "模型自报置信度的分组观察，排除公司行动跨日；不是概率校准或预测准确率证明",
        "execution_basis": "shared_volume_bar_v1",
        "execution_model": matching_model(config, spec),
    }


def engineering_checks(result, bars):
    """检查回放工程完整性；执行等待是有效处理，不代表盈利或券商模拟盘授权。"""
    return {
        "complete_window": len(result["equity_curve"]) == bars,
        "complete_decisions": len(result["decisions"]) + len(result.get("execution_waits", [])) == bars - 1,
        "no_model_errors": not result["errors"],
        "no_risk_hold": not result["risk_hold"],
    }


def admission_checks(result, bars):
    """兼容旧调用名：仅工程完整性检查，不授予盈利准出或券商模拟盘权限。"""
    return engineering_checks(result, bars)


def check_candidates(jobs, allow_model_calls):
    code_hash = source_tree_hash()
    for job in jobs:
        if job["stage"] == "paper":
            raise ValueError("运行版本已冻结；新建候选版本后评估")
        if job["code_hash"] != code_hash:
            raise ValueError("候选代码已变化；请创建新版本")
    if any(job["spec"]["provider"] == "ai" for job in jobs) and not allow_model_calls:
        raise ValueError("AI 历史评估需要明确允许模型调用（包括所有参与对比的版本）")


def save_reports(store, jobs, reports):
    # 同一比较批次全部提交或全部撤回，不保留部分版本的准入状态。
    if source_tree_hash() != jobs[0]["code_hash"]:
        raise ValueError("评估期间代码版本改变；请创建新版本")
    saved = []
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for job, report in zip(jobs, reports, strict=True):
            current = store.unpack(db.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
            if current["revision"] != job["revision"] or current["stage"] == "paper":
                raise ValueError("评估期间任务状态改变；请重新评估")
            report_id = uuid4().hex
            db.execute(
                "INSERT INTO evaluations VALUES (?,?,?,?)",
                (report_id, job["id"], encode(report), time.time()),
            )
            db.execute(
                "UPDATE jobs SET stage=?,revision=revision+1 WHERE id=?",
                ("evaluated" if report["passed"] else "candidate", job["id"]),
            )
            store.event(db, job["id"], "evaluation", {"id": report_id, "passed": report["passed"],
                        "engineering_passed": report["engineering_passed"], "broker_paper_authorized": False})
            saved.append({"id": report_id, "job_id": job["id"], "passed": report["passed"],
                          "engineering_passed": report["engineering_passed"], "broker_paper_authorized": False})
    return saved


def _start_evaluation(store, jobs, frozen, times, kind, protocol=None):
    """先冻结执行身份并撤回旧准入；失败或强制退出不能继续沿用上次通过状态。"""
    directory = store.root / "evaluations" / uuid4().hex
    directory.mkdir(parents=True, exist_ok=False)
    metadata = {"run_id": directory.name, "kind": kind, "snapshot": frozen,
                "window_start": times[0].isoformat(), "window_end": times[-1].isoformat(),
                "journal_directory": str(directory), "protocol": protocol,
                "candidates": [{key: job[key] for key in ("id", "spec", "config", "code_hash")} for job in jobs]}
    (directory / "manifest.json").write_text(encode(metadata))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        for job in jobs:
            current = db.execute("SELECT revision,stage FROM jobs WHERE id=?", (job["id"],)).fetchone()
            if current["revision"] != job["revision"] or current["stage"] == "paper":
                raise ValueError("开始评估前任务状态改变；请重新评估")
            db.execute("UPDATE jobs SET stage='candidate',revision=revision+1 WHERE id=?", (job["id"],))
            store.event(db, job["id"], "evaluation_started", metadata)
    for job in jobs:
        job["revision"] += 1
    return directory


def evaluate(store, job_id, *, last_bars=30, allow_model_calls=False):
    if type(last_bars) is not int or not 3 <= last_bars <= 500:
        raise ValueError("评估区间必须为 3..500 根 K 线")
    with runtime_lock(store.root / "evaluations" / job_id):
        job = store.get(job_id)
        check_candidates([job], allow_model_calls)
        spec, config = job["spec"], job["config"]
        snapshot = fetch_snapshot(spec, config, datetime.now(UTC), store.root, fresh=False)
        frozen = snapshot.freeze(store.root / "snapshots")
        directory = _start_evaluation(store, [job], frozen, snapshot.times[-last_bars:], "replay")
        candidate = replay(snapshot, spec, config, provider_for(spec, config), last_bars=last_bars,
                           journal_path=directory / "candidate.jsonl")
        baseline = PriceActionProvider() if spec["market"] == "stocks" else DemoProvider()
        baseline_spec = deepcopy(spec)
        baseline_spec["provider"] = "price_action" if spec["market"] == "stocks" else "demo"
        comparison = replay(snapshot, baseline_spec, config, baseline, last_bars=last_bars,
                            journal_path=directory / "baseline.jsonl")
        checks = engineering_checks(candidate, last_bars)
        report = {
            "kind": "replay",
            "run_id": directory.name, "journal_directory": str(directory),
            "candidate": candidate,
            "baseline": comparison,
            "baseline_name": "price_action" if spec["market"] == "stocks" else "momentum_rules",
            "cash_baseline_return": 0,
            "snapshot": frozen,
            "data_quality": snapshot.quality(),
            "code_hash": job["code_hash"],
            "config_hash": hash_payload(config),
            "spec_hash": hash_payload(spec),
            "passed": all(checks.values()),
            "engineering_passed": all(checks.values()),
            "broker_paper_authorized": False,
            "execution_completed": all(checks.values()),
            "research_conclusion": "not_assessed",
            "checks": checks,
            "admission_basis": "仅工程完整性：请求区间完整、每个决策或执行等待时点有效、零模型错误且未触发账户熔断；不是交易准入",
            "evidence_boundary": "当前流程以历史回测与复盘结束；不自动启用模拟盘，不证明盈利、真实渠道或前瞻模型效果",
        }
        saved = save_reports(store, [job], [report])[0]
        return {"id": saved["id"], **report}


def usage_summary(results):
    usage_calls = sum(r["usage_reported_calls"] for r in results)
    return {
        "provider_calls": sum(r["provider_calls"] for r in results),
        "reported_tokens": sum(r["reported_tokens"] or 0 for r in results) if usage_calls else None,
        "usage_reported_calls": usage_calls,
        "usage_missing_calls": sum(r["usage_missing_calls"] for r in results),
        "decision_seconds": sum(r["decision_seconds"] for r in results),
    }


def window_summary(results):
    """独立初始资金的等长窗口，不拼接权益、不虚构复利或组合回撤。"""
    return {
        **usage_summary(results),
        "windows": len(results),
        "return_fraction": sum(r["return_fraction"] for r in results) / len(results) if results else None,
        "worst_return_fraction": min((r["return_fraction"] for r in results), default=None),
        "max_drawdown_fraction": min((r["max_drawdown_fraction"] for r in results), default=None),
        "fills": sum(r["fills"] for r in results),
        "errors": [e for r in results for e in r["errors"]],
        "risk_holds": sum(bool(r["risk_hold"]) for r in results),
        "estimated_execution_cost": sum(r["account"]["estimated_execution_cost"] for r in results),
        "aggregation_basis": "independent_windows_arithmetic_mean_no_compounding",
    }


def walk_forward(
    store, job_ids, *, selection_bars=30, test_bars=10, folds=3, gap_bars=1, allow_model_calls=False
):
    if (
        not isinstance(job_ids, list)
        or not 1 <= len(job_ids) <= 6
        or any(not isinstance(j, str) for j in job_ids)
        or len(set(job_ids)) != len(job_ids)
    ):
        raise ValueError("滚动评估需要 1..6 个不重复的候选版本")
    for name, value, lower, upper in (
        ("selection_bars", selection_bars, 3, 200),
        ("test_bars", test_bars, 3, 100),
        ("folds", folds, 2, 10),
        ("gap_bars", gap_bars, 0, 100),
    ):
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{name} 必须为 {lower}..{upper} 的整数")
    required = selection_bars + gap_bars + folds * test_bars
    if required > 500:
        raise ValueError("滚动评估最多使用 500 根预热后的 K 线，请缩短区间")
    with ExitStack() as locks:
        for job_id in sorted(job_ids):
            locks.enter_context(runtime_lock(store.root / "evaluations" / job_id))
        jobs = [store.get(job_id) for job_id in job_ids]
        check_candidates(jobs, allow_model_calls)
        first = jobs[0]
        # 策略、模型和仓位风控可以不同；行情、起始资金和执行成本必须一致。
        spec_keys = (
            "market",
            "symbols",
            "bar",
            "source",
            "data_path",
            "start",
            "data_delay_seconds",
            "max_daily_loss",
            "max_drawdown",
        )
        execution_keys = ("initial_cash", "cost_bps", "limit_buffer_bps")
        for job in jobs[1:]:
            if job["spec"].get("sec_context_path", "") != first["spec"].get("sec_context_path", ""):
                raise ValueError("对比版本必须使用相同 SEC 公告输入")
            if any(job["spec"][k] != first["spec"][k] for k in spec_keys) or any(
                job["config"]["execution"][k] != first["config"]["execution"][k] for k in execution_keys
            ):
                raise ValueError("对比版本必须使用相同标的、行情、周期、起始资金、执行成本和账户熔断阈值")
        snapshot = fetch_snapshot(first["spec"], first["config"], datetime.now(UTC), store.root, fresh=False)
        times = snapshot.times[-required:]
        if len(times) != required:
            raise ValueError(f"滚动评估需要 {required} 根预热后的 K 线，当前行情不足")
        snapshots = [snapshot.with_config(job["config"]) for job in jobs]
        # 在任何收费调用前检查每个配置的预热长度，绝不悄悄缩短窗口。
        for data in snapshots:
            data.context(times[0])
        frozen = snapshot.freeze(store.root / "snapshots")
        protocol = {
            "selection_bars": selection_bars,
            "test_bars": test_bars,
            "folds": folds,
            "gap_bars": gap_bars,
            "required_bars": required,
            "selection_rule": "eligible_reference_return_desc_then_drawdown_then_job_id",
            "account_basis": "independent_cash_account_per_window_end_mark_to_market",
        }
        calls_per_job = folds * (selection_bars + test_bars - 2)
        protocol["max_candidate_decisions"] = calls_per_job * len(jobs)
        protocol["max_model_requests"] = calls_per_job * sum(
            1 + len(j["config"]["llm"]["fallback_models"]) for j in jobs if j["spec"]["provider"] == "ai"
        )
        identities = [
            {
                "job_id": j["id"],
                "name": j["spec"]["name"],
                "config_hash": hash_payload(j["config"]),
                "spec_hash": hash_payload(j["spec"]),
            }
            for j in jobs
        ]
        directory = _start_evaluation(store, jobs, frozen, times, "walk_forward", protocol)
        run_id = directory.name
        per_job = {j["id"]: [] for j in jobs}
        fold_reports, selected_results = [], []
        for fold in range(folds):
            test_start = selection_bars + gap_bars + fold * test_bars
            reference = times[test_start - gap_bars - selection_bars : test_start - gap_bars]
            testing = times[test_start : test_start + test_bars]
            reference_results = [
                replay(data, j["spec"], j["config"], provider_for(j["spec"], j["config"]), times=reference,
                       journal_path=directory / f"selection-{fold + 1}-{j['id']}.jsonl")
                for j, data in zip(jobs, snapshots, strict=True)
            ]
            scores = [
                {
                    "job_id": j["id"],
                    "return_fraction": r["return_fraction"],
                    "max_drawdown_fraction": r["max_drawdown_fraction"],
                    "eligible": all(engineering_checks(r, selection_bars).values()),
                    "engineering_passed": all(engineering_checks(r, selection_bars).values()),
                    "broker_paper_authorized": False,
                }
                for j, r in zip(jobs, reference_results, strict=True)
            ]
            eligible = [s for s in scores if s["eligible"]]
            winner = (
                min(
                    eligible, key=lambda s: (-s["return_fraction"], -s["max_drawdown_fraction"], s["job_id"])
                )["job_id"]
                if eligible
                else None
            )
            window = {
                "fold": fold + 1,
                "selection_start": reference[0].isoformat(),
                "selection_end": reference[-1].isoformat(),
                "test_start": testing[0].isoformat(),
                "test_end": testing[-1].isoformat(),
                "selected_job_id": winner,
                "selection_scores": scores,
            }
            # 先记录只依据较早窗口的选择，再请求测试窗口的任何决策。
            with store.connect() as db:
                for job in jobs:
                    store.event(db, job["id"], "evaluation_selection", {"run_id": run_id, **window})
            baseline = replay(
                snapshots[0],
                {**first["spec"], "provider": "price_action" if snapshot.market == "stocks" else "demo"},
                first["config"],
                PriceActionProvider() if snapshot.market == "stocks" else DemoProvider(),
                times=testing,
                journal_path=directory / f"baseline-{fold + 1}.jsonl",
            )
            for job, data, reference_result in zip(jobs, snapshots, reference_results, strict=True):
                result = replay(
                    data, job["spec"], job["config"], provider_for(job["spec"], job["config"]), times=testing,
                    journal_path=directory / f"candidate-{fold + 1}-{job['id']}.jsonl",
                )
                checks = {
                    **{
                        "selection_" + k: v
                        for k, v in engineering_checks(reference_result, selection_bars).items()
                    },
                    **{"test_" + k: v for k, v in engineering_checks(result, test_bars).items()},
                }
                per_job[job["id"]].append(
                    {
                        **window,
                        "selection": reference_result,
                        "candidate": result,
                        "baseline": baseline,
                        "checks": checks,
                        "passed": all(checks.values()),
                        "engineering_passed": all(checks.values()),
                        "broker_paper_authorized": False,
                    }
                )
                if job["id"] == winner:
                    selected_results.append(result)
            fold_reports.append(window)
        comparisons = []
        for job, identity in zip(jobs, identities, strict=True):
            results = per_job[job["id"]]
            comparisons.append(
                {
                    **identity,
                    "candidate": window_summary([r["candidate"] for r in results]),
                    "usage": usage_summary(
                        [r[section] for r in results for section in ("selection", "candidate")]
                    ),
                    "passed": all(r["passed"] for r in results),
                    "engineering_passed": all(r["passed"] for r in results),
                    "broker_paper_authorized": False,
                    "passed_folds": sum(r["passed"] for r in results),
                }
            )
        common = {
            "kind": "walk_forward",
            "run_id": run_id,
            "journal_directory": str(directory),
            "protocol": protocol,
            "snapshot": frozen,
            "data_quality": snapshot.quality(),
            "code_hash": first["code_hash"],
            "comparisons": comparisons,
            "baseline_name": "price_action" if snapshot.market == "stocks" else "momentum_rules",
            "baseline_config_hash": hash_payload(first["config"]),
            "cash_baseline_return": 0,
            "selected_strategy": {
                **window_summary(selected_results),
                "unselected_folds": folds - len(selected_results),
            },
            "admission_basis": "仅工程完整性：所有参考/测试窗口完整、每个决策或执行等待时点有效、零模型错误、无账户熔断；不是交易准入",
            "evidence_boundary": "固定候选的历史时间隔离评估，未训练模型；不能排除大模型训练数据污染或事后选参，不证明前瞻收益或真实交易",
        }
        reports = [
            {
                **common,
                **identity,
                "folds": per_job[j["id"]],
                "candidate": comparison["candidate"],
                "usage": comparison["usage"],
                "baseline": window_summary([r["baseline"] for r in per_job[j["id"]]]),
                "passed": comparison["passed"],
                "engineering_passed": comparison["engineering_passed"],
                "broker_paper_authorized": False,
                "execution_completed": comparison["passed"],
                "research_conclusion": "not_assessed",
            }
            for j, identity, comparison in zip(jobs, identities, comparisons, strict=True)
        ]
        saved = save_reports(store, jobs, reports)
        return {
            **common,
            "passed": all(r["passed"] for r in reports),
            "engineering_passed": all(r["engineering_passed"] for r in reports),
            "broker_paper_authorized": False,
            "execution_completed": all(r["passed"] for r in reports),
            "research_conclusion": "not_assessed",
            "folds": fold_reports,
            "evaluations": saved,
        }
