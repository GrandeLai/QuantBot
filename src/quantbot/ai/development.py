"""有限、冻结、离线的双市场策略开发；不写准出状态或连接交易账户。

运行方式：python -m quantbot.ai.development --plan PLAN.json --market crypto|stocks
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
import gzip
import hashlib
import json
import math
from pathlib import Path
import shutil
from statistics import mean, stdev

import numpy as np

from quantbot.evidence import source_tree_hash

from .development_strategies import development_candidates, development_controls, development_provider
from .domain import strict_json
from .evaluation import admission_checks, replay
from .feeds import fetch_snapshot
from .reconciliation import reconcile_replay
from .replay_evidence import inspect_replay


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")


def select_times(snapshot, window):
    start, end = window["start"], window["end"]
    if start > end:
        raise ValueError("开发窗口起止颠倒")
    selected = [at for at in snapshot.times if start <= at.date().isoformat() <= end]
    if len(selected) < 3 or selected != sorted(set(selected)):
        raise ValueError("开发窗口需至少三根、严格递增且不重复")
    if (len(selected) != window["expected_bars"] or selected[0].isoformat() != window["first_close"]
            or selected[-1].isoformat() != window["last_close"]):
        raise ValueError("行情未完整覆盖冻结窗口的首末收盘及根数")
    snapshot.context(selected[0])  # 首根也必须满足预热要求。
    return selected


def cost_config(config, multiplier):
    if multiplier not in (1, 2):
        raise ValueError("开发成本压力只支持冻结的 1/2 倍")
    updated = deepcopy(config)
    cost = config["execution"]["cost_bps"]
    updated["execution"]["cost_bps"] = cost * multiplier
    # 保持费用以外的入场容差不变。
    updated["execution"]["limit_buffer_bps"] += cost * (multiplier - 1)
    return updated


def summarize(result, config, periods):
    curve, account = result["equity_curve"], result["account"]
    daily = [b["equity"] / a["equity"] - 1 for a, b in zip(curve, curve[1:])]
    filled = [o for o in result["orders"] if o["status"] == "filled"]
    cancellations = Counter(o.get("reason", "unknown") for o in result["orders"] if o["status"] == "canceled")
    remaining_value = max(0, account["equity"] - account["cash"] - account["dividend_receivable"])
    liquidation_cost = remaining_value * config["execution"]["cost_bps"] / 10000
    annual = {}
    previous = result["initial_equity"]
    for row in curve:
        year = row["as_of"][:4]
        annual.setdefault(year, {"starting_equity": previous, "ending_equity": row["equity"]})
        annual[year]["ending_equity"] = row["equity"]
        previous = row["equity"]
    for values in annual.values():
        values["return_fraction"] = values["ending_equity"] / values["starting_equity"] - 1
    deviation = stdev(daily) if len(daily) > 1 else 0
    return {
        "return_fraction": result["return_fraction"],
        "net_liquidation_return_estimate": (account["equity"] - liquidation_cost) / result["initial_equity"] - 1,
        "final_equity": account["equity"], "estimated_final_sell_cost": liquidation_cost,
        "max_drawdown_fraction": result["max_drawdown_fraction"],
        "sharpe": mean(daily) / deviation * math.sqrt(periods) if deviation > 1e-15 else None,
        "fills": len(filled), "sells": sum(o["side"] == "sell" for o in filled),
        "protective_stops": sum(o.get("reason") == "protective_stop" for o in filled),
        "take_profits": sum(o.get("reason") == "take_profit" for o in filled),
        "both_touched_stop_first": sum(o.get("both_touched", False) and o.get("reason") == "protective_stop" for o in filled),
        "active_bars": sum(bool(row["positions"]) for row in curve),
        "turnover_notional_over_initial": sum(float(o["reference_price"]) * float(o["quantity"]) for o in filled) / result["initial_equity"],
        "costs": account["estimated_execution_cost"], "dividend_income": account["dividend_income"],
        "unpaid_dividend": account["dividend_receivable"], "remaining_positions": account["positions"],
        "risk_hold": result["risk_hold"], "errors": result["errors"],
        "execution_checks": admission_checks(result, len(curve)),
        "cancellations": dict(cancellations), "annual_account_returns": annual,
        "bars": len(curve), "decision_calls": result["provider_calls"], "model_calls": 0,
        "limitations": ["日线收盘回撤，不能证明盘中最大回撤", "期末清算仅为额外扣费估计，未实际平仓",
                        "分红税费未模拟", "使用已暴露的开发数据，不能授予准出"],
    }


def shortlist(rows, candidate_ids):
    selected, reasons = [], {}
    for identity in candidate_ids:
        own = [row for row in rows if row["candidate"] == identity]
        failures = []
        if len(own) != 8:
            failures.append("incomplete_registered_runs")
        for multiplier in (1, 2):
            group = [r for r in own if r["cost_multiplier"] == multiplier]
            if any(not all(r["execution_checks"].values()) or r["reconciliation"]["status"] != "reconciled" for r in group):
                failures.append(f"cost_{multiplier}:execution_or_risk_or_reconciliation")
            windows = [r for r in group if r["window"] != "continuous"]
            continuous = next((r for r in group if r["window"] == "continuous"), None)
            if len(windows) != 3 or sum(r["net_liquidation_return_estimate"] > 0 for r in windows) < 2:
                failures.append(f"cost_{multiplier}:fewer_than_two_positive_windows")
            if not windows or mean(r["net_liquidation_return_estimate"] for r in windows) <= 0:
                failures.append(f"cost_{multiplier}:nonpositive_mean_window_return")
            if continuous is None or continuous["net_liquidation_return_estimate"] <= 0:
                failures.append(f"cost_{multiplier}:nonpositive_continuous_return")
            if continuous is None or continuous["fills"] < 30 or continuous["sells"] < 10:
                failures.append(f"cost_{multiplier}:insufficient_continuous_trades")
        reasons[identity] = failures
        if not failures:
            selected.append(identity)
    return {"research_shortlist": selected, "rejection_reasons": reasons,
            "formal_admission": False, "stage": "development_only"}


def correlations(curves):
    ids = list(curves)
    values = {}
    dates = None
    for identity, curve in curves.items():
        current_dates = [row["as_of"] for row in curve]
        if dates is not None and current_dates != dates:
            raise ValueError("相关性计算要求完全对齐的账户日期")
        dates = current_dates
        equity = np.array([row["equity"] for row in curve])
        values[identity] = np.diff(equity) / equity[:-1]
    pairs = []
    for i, left in enumerate(ids):
        for right in ids[i + 1:]:
            a, b = values[left], values[right]
            corr = float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 1e-15 and np.std(b) > 1e-15 else None
            pairs.append({"left": left, "right": right, "daily_return_correlation": corr})
    return {"pairs": pairs, "basis": "aligned_development_continuous_accounts_including_cash_and_risk_halt_days",
            "warning": "风控停止导致长时间现金可能扭曲相关性；低相关不构成独立收益来源证明"}


def checked_input(item):
    path = Path(item["path"])
    if file_hash(path) != item["sha256"]:
        raise ValueError(f"冻结输入已改变：{path.name}")
    return path


def registered_selection(market, definitions):
    """每批只执行事先冻结的有限子集；历史批次与失败记录另行保留。"""
    catalog = {c.id: c for c in (*development_candidates(market), *development_controls(market))}
    selected = []
    for definition in definitions:
        candidate = catalog.get(definition.get("id"))
        if candidate is None or definition != strict_json(json.dumps(asdict(candidate))):
            raise ValueError("候选定义与冻结计划不一致")
        if candidate in selected:
            raise ValueError("候选不能重复登记")
        selected.append(candidate)
    if not selected or sum(not c.control for c in selected) > 6:
        raise ValueError("每批需要至少一个定义、最多六个研究候选")
    return selected


def run(plan_path, market):
    plan_path = Path(plan_path)
    plan_hash = file_hash(plan_path)
    plan = strict_json(plan_path.read_text())
    if plan["model_calls_authorized"] != 0 or plan["stage"] != "development_only":
        raise ValueError("开发研究器只接受零模型预算、无交易的开发计划")
    if source_tree_hash() != plan["source_hash"]:
        raise ValueError("源码与冻结计划不一致")
    for item in plan["preserved_policies"]:
        checked_input(item)
    section = plan["markets"][market]
    registered = registered_selection(market, section["candidates_and_controls"])
    config, spec = deepcopy(section["config"]), deepcopy(section["spec"])
    spec["data_path"] = str(checked_input(section["data"]))
    if spec["market"] != market or spec["source"] != "csv":
        raise ValueError("只接受显式冻结的离线 CSV")
    output = Path(section["output"])
    output.mkdir(parents=True, exist_ok=False)
    save(output / "started.json", {"plan": str(plan_path), "plan_hash": plan_hash,
                                  "source_hash": plan["source_hash"], "started_at": datetime.now(UTC).isoformat()})
    try:
        snapshot = fetch_snapshot(spec, config, datetime.now(UTC), output, fresh=False)
        if snapshot.times[-1].date().isoformat() > section["latest_allowed_close_date"]:
            raise ValueError("开发数据越过冻结边界")
        if section.get("actions"):
            actions = strict_json(checked_input(section["actions"]).read_text())
            validate_reconstructed_dividends(actions, set(spec["symbols"]))
            snapshot.actions = actions
        snapshot.provenance = section["provenance"]
        snapshot.freeze(output / "snapshots")
        windows = [*section["windows"], {"id": "continuous", **section["continuous_window"]}]
        if len(windows) != 4 or len({w["id"] for w in windows}) != 4:
            raise ValueError("必须冻结三个分窗和一个连续账户")
        times = {w["id"]: select_times(snapshot, w) for w in windows}
        continuous = times["continuous"]
        concatenated = [at for w in windows[:3] for at in times[w["id"]]]
        if concatenated != continuous:
            raise ValueError("三个分窗必须无重叠地完整覆盖连续账户")
        rows, curves = [], {}
        periods = 365 if market == "crypto" else 252
        for candidate in registered:
            for window in windows:
                for multiplier in (1, 2):
                    run_id = f"{candidate.id}__{window['id']}__cost{multiplier}"
                    adjusted = cost_config(config, multiplier)
                    journal = output / f"{run_id}.jsonl"
                    result = replay(snapshot, spec, adjusted, development_provider(market, candidate.id),
                                    times=times[window["id"]], journal_path=journal)
                    inspection = inspect_replay(journal)
                    reconciliation = reconcile_replay(inspection.pop("records"))
                    if inspection["status"] != "completed" or reconciliation["status"] != "reconciled":
                        raise ValueError(f"运行证据不完整或账目不一致：{run_id}")
                    row = {"candidate": candidate.id, "window": window["id"], "cost_multiplier": multiplier,
                           "cost_bps": adjusted["execution"]["cost_bps"], **summarize(result, adjusted, periods),
                           "reconciliation": reconciliation, "pressure_basis": "rule_recomputed_with_costs"}
                    save(output / f"{run_id}.json", {"summary": row, "equity_curve": result["equity_curve"],
                                                    "orders": result["orders"], "inspection": inspection})
                    digest = file_hash(journal)
                    compressed = journal.with_suffix(".jsonl.gz")
                    with journal.open("rb") as src, gzip.open(compressed, "xb") as target:
                        shutil.copyfileobj(src, target)
                    with gzip.open(compressed, "rb") as restored:
                        if hashlib.sha256(restored.read()).hexdigest() != digest:
                            raise ValueError("压缩日志校验失败")
                    row["journal"] = {"path": str(compressed), "sha256": file_hash(compressed), "uncompressed_sha256": digest}
                    journal.unlink()  # 原始字节完整保存在已校验的压缩日志中。
                    rows.append(row)
                    if window["id"] == "continuous" and multiplier == 1:
                        curves[candidate.id] = result["equity_curve"]
                    print(json.dumps({"market": market, "completed": len(rows), "run": run_id,
                                      "net_return": row["net_liquidation_return_estimate"],
                                      "risk_hold": row["risk_hold"]}), flush=True)
        if source_tree_hash() != plan["source_hash"] or file_hash(plan_path) != plan_hash:
            raise ValueError("运行期间源码或计划改变")
        checked_input(section["data"])
        if section.get("actions"):
            checked_input(section["actions"])
        for item in plan["preserved_policies"]:
            checked_input(item)
        report = {"market": market, "stage": "development_only", "formal_admission": False,
                  "model_calls": 0, "trading_enabled": False, "plan_hash": plan_hash,
                  "source_hash": plan["source_hash"], "runs": rows,
                  "selection": shortlist(rows, [c.id for c in registered if not c.control]),
                  "correlations": correlations(curves), "quality": snapshot.quality()}
        save(output / "report.json", report)
        return report
    except BaseException as exc:
        save(output / "failed.json", {"type": type(exc).__name__, "error": str(exc), "formal_admission": False})
        raise


def validate_reconstructed_dividends(actions, symbols):
    """事后重建的会计事件；不伪造公告时间，也不声明 PIT 可得性。"""
    from datetime import date
    from .corporate_actions import positive_amount

    identities, sessions = set(), set()
    for action in actions:
        if (action.get("type") != "dividend" or action.get("distribution") != "ordinary_cash"
                or action.get("currency") != "USD" or action.get("symbol") not in symbols):
            raise ValueError("开发快照仅支持已核实的普通美元现金分红；拆股和特殊分配需单独建模")
        identity = action.get("id")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("分红身份缺失或重复")
        day, pay = date.fromisoformat(action["date"]), date.fromisoformat(action["pay_date"])
        key = (action["symbol"], day)
        if pay < day or key in sessions:
            raise ValueError("分红支付日期错误或同日事件重复")
        positive_amount(action["amount_per_share"], "amount_per_share")
        identities.add(identity)
        sessions.add(key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--market", choices=("crypto", "stocks"), required=True)
    args = parser.parse_args()
    report = run(args.plan, args.market)
    print(json.dumps({"market": args.market, **report["selection"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
