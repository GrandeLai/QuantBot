"""加密现货研究准出检查。只读本地证据，绝不签发账户交易授权。"""

from copy import deepcopy
from datetime import UTC, datetime
import math
from statistics import mean, stdev

from quantbot.evidence import hash_payload, source_tree_hash

from .reconciliation import reconcile_replay
from .replay_evidence import inspect_replay


_POLICY = {
    "id": "crypto_spot_v1",
    "scope": "BTC/USDT,ETH/USDT spot long-only; no leverage",
    "backtest": {
        "min_history_days": 180, "min_holdout_days": 60, "min_development_windows": 3,
        "min_positive_window_fraction": 2 / 3, "min_holdout_fills": 30,
        "min_holdout_sell_fills": 10, "min_active_days": 20,
        "min_net_return_exclusive": 0, "min_excess_return_exclusive": 0.001,
        "min_daily_sharpe": 1.0, "max_drawdown": 0.10,
        "min_dsr": 0.95, "max_pbo": 0.10, "min_one_way_cost_bps": 15,
        "min_cost_multiplier": 2, "min_execution_delay_bars": 1,
        "max_target_weight": 0.20, "max_target_gross": 0.60,
        "min_cash_buffer": 0.10,
    },
    "okx_demo": {
        "min_elapsed_days": 60, "min_reconciled_daily_sessions": 60,
        "min_exchange_fills": 30, "min_exchange_sell_fills": 10, "min_active_days": 20,
        "min_net_return_exclusive": 0, "min_excess_return_exclusive": 0,
        "min_daily_sharpe": 0.5, "max_drawdown": 0.05, "max_daily_loss": 0.02,
        "min_decision_completion_fraction": 0.99, "max_reconciliation_differences": 0,
        "max_unknown_orders": 0, "max_duplicate_orders": 0, "max_risk_breaches": 0,
        "max_p95_adverse_slippage_bps": 10,
        "require_same_strategy_model_prompt_and_risk": True,
        "require_demo_credentials_and_header": True,
        "require_restart_recovery_and_kill_switch_checks": True,
    },
    "small_live": {
        "requires_backtest_and_okx_demo_pass": True,
        "requires_operator_capital_and_order_limits": True,
        "automatic_activation": False,
    },
}


def crypto_policy():
    """返回独立副本，研究登记时连同配置、数据和代码一并冻结。"""
    return deepcopy(_POLICY)


def _instant(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError("准出证据时间必须带时区")
    return at.astimezone(UTC)


def _metrics(result):
    curve = result["equity_curve"]
    if not curve:
        raise ValueError("准出证据缺少权益曲线")
    daily, active = {}, set()
    peak, drawdown = float(result["initial_equity"]), 0.0
    previous = None
    for row in curve:
        at = _instant(row["as_of"])
        equity = row["equity"]
        if (type(equity) not in (int, float) or not math.isfinite(equity) or equity <= 0
                or (previous is not None and at <= previous)):
            raise ValueError("准出权益必须为正、有限且时间严格递增")
        previous = at
        peak = max(peak, equity)
        drawdown = min(drawdown, equity / peak - 1)
        daily[at.date()] = equity
        if any(float(quantity) > 0 for quantity in row["positions"].values()):
            active.add(at.date())
    values = list(daily.values())
    returns = [b / a - 1 for a, b in zip(values, values[1:])]
    deviation = stdev(returns) if len(returns) >= 2 else 0
    sharpe = mean(returns) / deviation * math.sqrt(365) if deviation > 1e-15 else None
    fills = [o for o in result["orders"] if o["status"] == "filled"]
    return {
        "elapsed_days": (_instant(curve[-1]["as_of"]) - _instant(curve[0]["as_of"])).total_seconds() / 86400,
        "net_return": curve[-1]["equity"] / result["initial_equity"] - 1,
        "max_drawdown": -drawdown, "daily_sharpe": sharpe,
        "daily_return_observations": len(returns), "active_days": len(active),
        "fills": len(fills), "sell_fills": sum(o["side"] == "sell" for o in fills),
    }


def _journal_evidence(study):
    """核对逐步日志与结果绑定，再独立重算账目；缺失日志不能降级为通过。"""
    results = []
    candidates = {j["id"]: j for j in study["plan"]["candidates"]}
    for trial in study["trials"]:
        result = trial["result"] or {}
        item = {"key": trial["key"], "execution_completed": False, "bound": False,
                "accounting_reconciled": False}
        try:
            journal = inspect_replay(result["journal_path"])
            records = journal["records"]
            metadata = records[0]["payload"]
            expected_config = deepcopy(candidates[trial["job_id"]]["config"])
            protocol = study["plan"]["protocol"]
            if trial["stage"] == "stress":
                if trial["key"] == "cost":
                    execution = expected_config["execution"]
                    extra = execution["cost_bps"] * (protocol["cost_multiplier"] - 1)
                    execution["limit_buffer_bps"] += extra
                    execution["cost_bps"] *= protocol["cost_multiplier"]
                elif trial["key"] in {"weight_lower", "weight_higher"}:
                    sign = -1 if trial["key"] == "weight_lower" else 1
                    expected_config["risk"]["max_weight"] = min(
                        1.0, expected_config["risk"]["max_weight"] * (1 + sign * protocol["weight_perturbation"]))
            accounts = [r["payload"] for r in records if r["kind"] == "account"]
            decisions = [r["payload"] for r in records if r["kind"] == "decision"]
            fills = [o for row in accounts for o in row["orders"] if o["status"] == "filled"]
            item["bound"] = (
                metadata["code_hash"] == study["plan"]["code_hash"]
                and metadata["config"] == expected_config
                and result["initial_equity"] == expected_config["execution"]["initial_cash"]
                and metadata["spec"] == candidates[trial["job_id"]]["spec"]
                and metadata["window"][0] == trial["start"]
                and metadata["window"][-1] == trial["end"]
                and [{"as_of": row["as_of"], **row["account"]} for row in accounts] == result["equity_curve"]
                and decisions == result["decisions"]
                and fills == [o for o in result["orders"] if o["status"] == "filled"]
                and journal["last_account"]["account"] == result["account"]
            )
            accounting = reconcile_replay(records)
            item["execution_completed"] = journal["execution_completed"] and trial["status"] == "completed"
            item["accounting_reconciled"] = accounting["status"] == "reconciled"
            item["account_snapshots"] = accounting["account_snapshots"]
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            item["error"] = str(exc)
        results.append(item)
    return results


def assess_crypto_study(store, identifier):
    from .admission import strategy_identity
    from .research import export_study

    study = export_study(store, identifier)
    plan, report = study["plan"], study["report"]
    if plan["candidates"][0]["spec"]["market"] != "crypto":
        raise ValueError("加密准出检查不能用于其他市场")
    if report is None:
        raise ValueError("研究尚未完成，不能进行准出检查")
    policy = crypto_policy()
    limits = policy["backtest"]
    checks = {
        "policy_preregistered": plan.get("promotion_policy") == policy,
        "current_code_matches": source_tree_hash() == plan["code_hash"],
        "research_passed": report.get("research_passed") is True,
        "all_trials_complete": report.get("execution_completed") is True,
        "historical_data": plan["data_quality"]["data_kind"] != "synthetic",
        "unexposed_holdout": report.get("checks", {}).get("unexposed_holdout") is True,
        "holdout_available": "holdout" in report,
    }
    metrics = {}
    journals = _journal_evidence(study)
    protocol = plan["protocol"]
    expected_trials = (len(plan["candidates"]) * protocol["folds"] + 5
                       + bool(protocol.get("execution_delay_bars", 0)) + len(protocol["placebo_seeds"]))
    checks["expected_trial_count"] = len(journals) == expected_trials
    checks["journals_complete_and_bound"] = bool(journals) and all(
        j["execution_completed"] and j["bound"] for j in journals)
    checks["independently_reconciled"] = bool(journals) and all(j["accounting_reconciled"] for j in journals)
    if checks["holdout_available"]:
        trials = {t["key"]: t for t in study["trials"]}
        holdout = trials["holdout"]["result"]
        baseline = trials["rules"]["result"]
        checks["holdout_report_bound"] = report["holdout"] == holdout and report["baseline"] == baseline
        job = next(j for j in plan["candidates"] if j["id"] == report["selected_job_id"])
        selected_dev = [t["result"] for t in study["trials"]
                        if t["stage"] == "development" and t["job_id"] == job["id"]]
        metrics = _metrics(holdout)
        metrics["history_days"] = (_instant(plan["windows"]["holdout"]["end"])
                                   - _instant(plan["windows"]["development"][0]["start"])).total_seconds() / 86400
        metrics["development_windows"] = len(selected_dev)
        metrics["positive_window_fraction"] = sum(_metrics(r)["net_return"] > 0 for r in selected_dev) / len(selected_dev)
        metrics["excess_return"] = metrics["net_return"] - _metrics(baseline)["net_return"]
        metrics["stress"] = {t["key"]: _metrics(t["result"]) for t in study["trials"] if t["stage"] == "stress"}
        risk, execution = job["config"]["risk"], job["config"]["execution"]
        checks.update({
            "selected_ai_candidate": job["spec"]["provider"] == "ai",
            "spot_scope": set(job["spec"]["symbols"]) <= {"BTC/USDT", "ETH/USDT"},
            "history_duration": metrics["history_days"] >= limits["min_history_days"],
            "holdout_duration": metrics["elapsed_days"] >= limits["min_holdout_days"],
            "development_windows": len(selected_dev) >= limits["min_development_windows"],
            "positive_development_windows": metrics["positive_window_fraction"] >= limits["min_positive_window_fraction"],
            "enough_fills": metrics["fills"] >= limits["min_holdout_fills"],
            "enough_sell_fills": metrics["sell_fills"] >= limits["min_holdout_sell_fills"],
            "enough_active_days": metrics["active_days"] >= limits["min_active_days"],
            "positive_net_return": metrics["net_return"] > limits["min_net_return_exclusive"],
            "beats_rules": metrics["excess_return"] > limits["min_excess_return_exclusive"],
            "daily_sharpe": metrics["daily_sharpe"] is not None and metrics["daily_sharpe"] >= limits["min_daily_sharpe"],
            "drawdown": metrics["max_drawdown"] <= limits["max_drawdown"],
            "dsr": report["diagnostics"]["dsr"].get("confidence", -1) >= limits["min_dsr"],
            "pbo": report["diagnostics"]["pbo"].get("probability", 1) <= limits["max_pbo"],
            "cost_assumption": execution["cost_bps"] >= limits["min_one_way_cost_bps"],
            "cost_stress": plan["protocol"]["cost_multiplier"] >= limits["min_cost_multiplier"],
            "delay_stress": plan["protocol"].get("execution_delay_bars", 0) >= limits["min_execution_delay_bars"],
            "risk_caps": risk["max_weight"] <= limits["max_target_weight"]
                and risk["max_gross_exposure"] <= limits["max_target_gross"]
                and risk["cash_buffer"] >= limits["min_cash_buffer"],
            "stress_survives": {"cost", "weight_lower", "weight_higher", "execution_delay"} <= metrics["stress"].keys()
                and all(s["net_return"] > 0 and s["max_drawdown"] <= limits["max_drawdown"]
                        and s["fills"] > 0 and s["active_days"] > 0 for s in metrics["stress"].values()),
        })
    passed = all(checks.values())
    selected = next((candidate for candidate in plan["candidates"]
                     if candidate["id"] == report.get("selected_job_id")), None)
    return {
        "study_id": identifier, "policy": policy, "policy_hash": hash_payload(policy),
        "backtest_passed": passed, "passed": passed, "checks": checks,
        "identity": strategy_identity(selected["spec"], selected["config"], selected["code_hash"])
            if selected else None,
        "broker_paper_authorized": False, "live_authorized": False,
        "reasons": [key for key, value in checks.items() if not value], "metrics": metrics,
        "journals": journals, "next_stage": "okx_demo_setup" if passed else "backtest_research",
        "okx_demo": {"status": "not_started", "execution_authorized": False,
                     "reason": "需要接通冻结策略的 OKX 模拟盘执行、回执与持续对账后才能开始计时"},
        "small_live": {"eligible": False, "execution_authorized": False,
                       "reason": "尚无达到阈值的 OKX 模拟盘证据及明确的小额资金限额"},
        "boundary": "本结果只检查研究准出条件，不是签名执行许可，不连接账户、不启动任务。"
                    "历史行情与模型训练污染未认证；阈值是预登记工程标准，不保证未来收益。"
                    "自动留出暴露检查仅覆盖当前研究目录，跨目录审计需要另行保存。"
                    "模拟盘指标尚待真实 OKX 模拟回执采集器核验，不能导入手填汇总来放行实盘。",
    }
