"""冻结规则的信号漏斗：仅计数，不扫描阈值或计算事后收益。"""

from .mechanism_probes import feature_panel, origins, valid_features


def signal_funnel(snapshot, card, windows):
    if card["mechanism"] != "range_mean_reversion" or snapshot.market != "crypto":
        raise ValueError("此诊断只适用于已冻结的加密震荡回归")
    panel = feature_panel(snapshot)
    reports = []
    for window in windows:
        grid = set(origins(card, snapshot.times, window))
        days = [at for at in snapshot.times if window["start"] <= at.date().isoformat() <= window["end"]]
        eligible = days[20:len(days)-max(20, card["definition"]["horizon_sessions"])]
        for symbol in card["symbols"]:
            rows = []
            for at in eligible:
                value = panel[(at.isoformat(), symbol)]
                valid = valid_features(card["mechanism"], value)
                oversold = valid and value["zscore20"] <= -2
                flat = valid and abs(value["sma_gap"]) <= .02
                inefficient = valid and value["efficiency20"] <= .25
                rows.append({"at": at.isoformat(), "valid": valid, "oversold": oversold,
                             "flat": flat, "inefficient": inefficient, "on_grid": at in grid})
            stages = {
                "valid": lambda r: r["valid"],
                "oversold": lambda r: r["oversold"],
                "flat": lambda r: r["flat"],
                "inefficient": lambda r: r["inefficient"],
                "oversold_and_flat": lambda r: r["oversold"] and r["flat"],
                "oversold_and_inefficient": lambda r: r["oversold"] and r["inefficient"],
                "all_conditions": lambda r: r["oversold"] and r["flat"] and r["inefficient"],
            }
            counts = {scope: {key: sum(test(r) for r in rows if scope == "daily" or r["on_grid"])
                              for key, test in stages.items()} for scope in ("daily", "frozen_grid")}
            matches = [r["at"] for r in rows if stages["all_conditions"](r)]
            flags = [stages["all_conditions"](r) for r in rows]
            episodes = sum(flag and (i == 0 or not flags[i-1]) for i, flag in enumerate(flags))
            reports.append({"window": window["id"], "symbol": symbol, "daily_sessions": len(rows),
                "grid_sessions": sum(r["on_grid"] for r in rows), "counts": counts,
                "daily_signal_episodes": episodes, "all_condition_dates": matches})
    return {"hypothesis": card["id"], "reports": reports,
            "thresholds": {"zscore20_max": -2, "absolute_sma_gap_max": .02, "efficiency20_max": .25},
            "returns_computed": False, "threshold_search": False,
            "note": "每日事件重叠且不等于独立交易；旧网格计数与账户成交另行核对。"}
