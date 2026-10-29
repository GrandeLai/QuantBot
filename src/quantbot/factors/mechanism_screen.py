"""固定日历原点的成对账户贡献筛选；保留历史试验和全部失败。"""

import math

import numpy as np

from .entry_screen import _bootstrap
from .time_series import holm_adjust


def screen(card, events, calendars, statistics, *, pit_verified):
    costs = sorted(events)
    if costs != [15, 30] or len(calendars) != 3:
        raise ValueError("需要两档费用和三个开发窗口")
    if (statistics["bootstrap_replicates"], statistics["block_sessions"], statistics["seed"]) != (4999, 20, 1729):
        raise ValueError("统计方法与冻结日历 bootstrap 不一致")
    identity_sets = [set((r["window"], r["signal_at"]) for r in events[c]) for c in costs]
    if identity_sets[0] != identity_sets[1] or any(len(keys) != len(events[c])
                                                  for c, keys in zip(costs, identity_sets)):
        raise ValueError("原点缺失、重复或费用档之间不配对")
    reasons = [] if pit_verified else ["missing_point_in_time_inputs"]
    reports = []
    for cost in costs:
        windows, grids = [], []
        for name, times in calendars.items():
            if len(times) != len(set(times)) or times != sorted(times):
                raise ValueError("日历必须严格递增")
            own = [r for r in events[cost] if r["window"] == name]
            index = {at: i for i, at in enumerate(times)}
            grid = np.zeros((len(times), 4))
            for row in own:
                a, b = row["candidate"], row["control"]
                if (row["signal_at"] not in index or a["signal_at"] != row["signal_at"]
                        or b["signal_at"] != row["signal_at"] or a["end_at"] != b["end_at"]):
                    raise ValueError("候选/对照原点或结束时点不匹配")
                net, delta = a["net_return"], a["net_return"]-b["net_return"]
                if not math.isfinite(net) or not math.isfinite(delta):
                    raise ValueError("非有限账户贡献")
                grid[index[row["signal_at"]]] += (1, 1, net, delta)
            valid = sum(r["candidate"]["feature_valid"] and r["control"]["feature_valid"] for r in own)
            fills = sum(r["candidate"]["buy_fills"] for r in own)
            net, delta = grid[:, 2].sum(), grid[:, 3].sum()
            windows.append({"window": name, "origins": len(own), "feature_coverage": valid/len(own) if own else 0,
                "buy_fills": fills, "mean_net_return_per_origin": float(net/len(own)) if own else None,
                "mean_increment_per_origin": float(delta/len(own)) if own else None,
                "jointly_positive": bool(net > 0 and delta > 0)})
            if not own or valid/len(own) < statistics["minimum_feature_coverage"]:
                reasons.append(f"{cost}:{name}:insufficient_feature_coverage")
            if fills < statistics["minimum_filled_per_window"]:
                reasons.append(f"{cost}:{name}:insufficient_buy_fills")
            if any(r[branch]["risk_hold"] or r[branch]["reconciliation"]["status"] != "reconciled"
                   for r in own for branch in ("candidate", "control")):
                reasons.append(f"{cost}:{name}:risk_hold_or_unreconciled")
            grids.append(grid)
        if sum(len([r for r in events[cost] if r["window"] == w]) for w in calendars) != len(events[cost]):
            raise ValueError("原点引用未知窗口")
        count = sum(w["origins"] for w in windows)
        if not count:
            reasons.append(f"{cost}:no_origins")
            reports.append({"cost_bps": cost, "windows": windows, "p_value": 1.})
            continue
        bootstrap = _bootstrap(grids)
        net = bootstrap["net_contribution_per_kept_event"]
        delta = bootstrap["increment_per_baseline_event"]
        for name, stat in (("net", net), ("increment", delta)):
            if stat["observed"] <= 0:
                reasons.append(f"{cost}:nonpositive_{name}")
            if stat["ci_low"] is None or stat["ci_low"] <= 0:
                reasons.append(f"{cost}:{name}_confidence_interval_not_positive")
        if sum(w["jointly_positive"] for w in windows) < statistics["minimum_positive_windows"]:
            reasons.append(f"{cost}:insufficient_positive_windows")
        if sum(w["buy_fills"] for w in windows) < statistics["minimum_total_filled"]:
            reasons.append(f"{cost}:insufficient_total_buy_fills")
        reports.append({"cost_bps": cost, "windows": windows,
            "buy_fills": sum(w["buy_fills"] for w in windows), "origins": count,
            "net": net, "increment": delta, "p_value": bootstrap["p_value"],
            "bootstrap": {k: v for k, v in bootstrap.items() if k not in
                          ("net_contribution_per_kept_event", "increment_per_baseline_event")}})
    raw = max(r["p_value"] for r in reports)
    return {"id": card["id"], "market": card["market"], "costs": reports, "reasons": reasons,
            "raw_p_value": raw, "p_value": 1. if reasons else raw, "qualified": False,
            "unit": "independent_start_account_return_per_fixed_origin_not_compounded_strategy_return"}


def finalize(results, catalog, statistics):
    ids = [r["id"] for r in results]
    reserved = [t["id"] for t in catalog["trials"] if t["id"] not in catalog["outcomes"]]
    if len(ids) != len(set(ids)) or set(ids) != set(reserved):
        raise ValueError("必须给全部预留假设记录结果，不得删除失败")
    if len(catalog["trials"]) < statistics["minimum_family_size"]:
        raise ValueError("累计试验数量不能缩减")
    historical = [catalog["outcomes"][t["id"]] for t in catalog["trials"] if t["id"] in catalog["outcomes"]]
    values = [r["p_value"] for r in historical] + [r["p_value"] for r in results]
    if any(not math.isfinite(p) or not 0 <= p <= 1 for p in values):
        raise ValueError("累计 p 值无效")
    adjusted = holm_adjust(values)[len(historical):]
    for result, p in zip(results, adjusted):
        result["holm_p_value"] = p
        if p > statistics["familywise_alpha"]:
            result["reasons"].append("cumulative_holm_not_significant")
        result["qualified"] = not result["reasons"]
    return {"results": results, "family_size": len(values),
            "factor_pool": {m: [r["id"] for r in results if r["market"] == m and r["qualified"]]
                            for m in ("crypto", "stocks")},
            "formal_admission": False, "trading_enabled": False, "sealed_holdout_evaluated": False}
