"""Bounded, deterministic factor grammar and offline research orchestration.

The planner never opens price data. Every variant is registered before replay;
search results cannot mutate the grammar, risk policy, or trading state.
"""

from copy import deepcopy
from datetime import UTC, datetime
from itertools import combinations
import math
from pathlib import Path

from quantbot.ai.research_catalog import load
from quantbot.evidence import hash_payload
from quantbot.factors.conditional_study import check_ref, file_ref, read, save

PROTOCOL_ID = "dual_market_intraday_mining_v1"
FEATURES = {
    "signed_efficiency_30m": "(close[t]-close[t-2]) / sum(abs(diff(last 3 completed 15m closes))); flat=0",
    "range_cost_room": "range of preceding 2 completed 15m bars / current close in bps / frozen stress round-trip bps",
    "close_location": "(close-low)/(high-low) of current completed 15m bar; flat=0",
}
FEATURES_V2 = FEATURES | {
    "net_move_cost_room": "(close[t]/close[t-2]-1) in bps / frozen stress round-trip bps; completed 15m closes only",
}
FEATURES_V3 = FEATURES_V2 | {
    "pair_net_move_cost_room": "minimum net_move_30m_bps of exactly two fixed symbols at the same completed 15m close / frozen stress round-trip bps; same session only, missing history is unavailable",
}
FEATURES_BY_VERSION = {1: FEATURES, 2: FEATURES_V2, 3: FEATURES_V3, 4: FEATURES_V3}
DEFAULT_SPACE = {
    "version": 1,
    "seeds": ["control", "vwap_reclaim", "range_breakdown"],
    "atoms": [
        {"feature": "signed_efficiency_30m", "op": "gte", "threshold": 0.35},
        {"feature": "range_cost_room", "op": "gte", "threshold": 2.0},
        {"feature": "close_location", "op": "gte", "threshold": 0.75},
    ],
    "max_terms": 2,
    "max_candidates": 36,
}


def canonical_space(space):
    if (set(space) != set(DEFAULT_SPACE) or type(space["version"]) is not int
            or space["version"] not in FEATURES_BY_VERSION):
        raise ValueError("未知自动挖掘语法版本或字段")
    seeds = space["seeds"]
    allowed_seeds = DEFAULT_SPACE["seeds"] + (["breakout_next_bar"] if space["version"] == 4 else [])
    if (not isinstance(seeds, list) or not seeds or len(set(seeds)) != len(seeds)
            or any(s not in allowed_seeds for s in seeds)):
        raise ValueError("挖掘种子缺失、重复或不支持")
    if (type(space["max_terms"]) is not int or space["max_terms"] not in (1, 2)
            or type(space["max_candidates"]) is not int or not 1 <= space["max_candidates"] <= 64):
        raise ValueError("搜索必须限定为最多两项条件、64个候选")
    atoms = space["atoms"]
    if not isinstance(atoms, list) or not 1 <= len(atoms) <= 12:
        raise ValueError("原子因子数量必须为1到12")
    normalized = []
    definitions = FEATURES_BY_VERSION[space["version"]]
    for atom in atoms:
        if (set(atom) != {"feature", "op", "threshold"} or atom["feature"] not in definitions
                or atom["op"] not in ("gte", "lte")):
            raise ValueError("未知因子或运算符；不执行表达式代码")
        value = atom["threshold"]
        bounds = {"signed_efficiency_30m": (-1, 1), "range_cost_room": (0, 10), "close_location": (0, 1),
                  "net_move_cost_room": (-10, 10), "pair_net_move_cost_room": (-10, 10)}
        low, high = bounds[atom["feature"]]
        if (type(value) not in (float, int) or not math.isfinite(value) or not low <= value <= high):
            raise ValueError("因子阈值不是有效的有界数值")
        normalized.append(atom | {"threshold": float(value) if value != 0 else 0.0})
    if len({hash_payload(a) for a in normalized}) != len(normalized):
        raise ValueError("不能重复登记同一原子因子")
    return space | {"seeds": sorted(seeds), "atoms": sorted(normalized, key=hash_payload)}


def generate_candidates(space, catalog):
    """Enumerate the complete declared grammar, with no price/return input."""
    space = canonical_space(space)
    tried = {t["id"] for t in catalog["trials"]}
    cards, skipped = [], []
    for market in ("crypto", "stocks"):
        for seed in space["seeds"]:
            for size in range(1, space["max_terms"] + 1):
                for atoms in combinations(space["atoms"], size):
                    if len({a["feature"] for a in atoms}) != size:
                        continue
                    recipe = {"version": 1, "seed": seed, "all": list(atoms)}
                    digest = hash_payload(recipe)
                    identity = f"auto_{market}_{digest[:16]}"
                    if identity in tried:
                        skipped.append(identity)
                        continue
                    cards.append({"id": identity, "market": market,
                                  "factor": "mined_" + digest[:16], "control": seed,
                                  "definition_hash": digest, "recipe": recipe})
    if len(cards) > space["max_candidates"]:
        raise ValueError(f"完整搜索空间有{len(cards)}个新候选，超过预算；请缩小空间，不按收益截取")
    if not cards:
        raise ValueError("搜索空间已全部登记；不能把旧候选改名再试")
    return cards, sorted(skipped)


def matches(recipe, value, stress_round_trip_bps):
    """Apply already-validated predicates to completed features; missing is false."""
    features = {key: value.get(key) for key in FEATURES_V3}
    width = value.get("prior_range_bps")
    features["range_cost_room"] = width / stress_round_trip_bps if width is not None else None
    move = value.get("net_move_30m_bps")
    features["net_move_cost_room"] = move / stress_round_trip_bps if move is not None else None
    pair_move = value.get("pair_net_move_30m_bps")
    features["pair_net_move_cost_room"] = pair_move / stress_round_trip_bps if pair_move is not None else None
    for atom in recipe["all"]:
        own = features[atom["feature"]]
        if own is None or not math.isfinite(own):
            return False
        if atom["op"] == "gte" and own < atom["threshold"]:
            return False
        if atom["op"] == "lte" and own > atom["threshold"]:
            return False
    return True


def validate_protocol(protocol, parent, catalog):
    if parent["id"] not in ("dual_market_intraday_v3", PROTOCOL_ID):
        raise ValueError("自动挖掘必须继承机制研究或上一轮自动挖掘")
    fields = ("sampling", "execution_model", "markets", "range", "windows", "policies", "protected_intervals",
              "prior_protocol", "data_policy")
    for field in fields:
        if protocol[field] != parent[field]:
            raise ValueError("不能改变继承的风控、执行、数据或窗口: " + field)
    definitions = FEATURES_BY_VERSION[protocol["search_space"]["version"]]
    if (protocol["independent_validation"] or not protocol["original_control_required"]
            or protocol["new_outcomes_seen"] or protocol["feature_definitions"] != definitions):
        raise ValueError("自动挖掘仅限已暴露开发数据，特征定义必须冻结")
    expected, skipped = generate_candidates(protocol["search_space"], catalog)
    if protocol["factors"] != expected or protocol["skipped_trials"] != skipped:
        raise ValueError("必须预登记完整搜索空间，不能遗漏失败候选")
    if protocol["protected_intervals"] != catalog["protected_intervals"]:
        raise ValueError("原封存边界改变")
    count, added = len(catalog["trials"]), len(expected)
    if len(catalog["outcomes"]) != count or any(c["id"] not in catalog["outcomes"] for c in parent["factors"]):
        raise ValueError("必须保留上一轮完整累计研究结果")
    stats = deepcopy(parent["statistics"])
    stats.update(historical_trials=count, reserved_new_trials=added, minimum_family_size=count + added)
    if protocol["statistics"] != stats:
        raise ValueError("累计检验家族或原准出门槛改变")
    result = read(check_ref(protocol["prior_result"]))
    if result["family_size"] != count or {r["id"] for r in result["results"]} != {c["id"] for c in parent["factors"]}:
        raise ValueError("上一轮报告与完整累计结果不一致")


def plan(parent_path, study_path, manifest_path, output, *, space=None, catalog_path=None):
    from .data import checked_protocol, frozen_policy_snapshots

    study = Path(study_path)
    started = read(study / "started.json")
    execution = read(check_ref(started["freeze"]))
    snapshots = frozen_policy_snapshots(execution)
    parent = (checked_protocol(parent_path, policy_snapshots=snapshots) if snapshots
              else checked_protocol(parent_path))
    if execution["protocol"] != file_ref(parent_path):
        raise ValueError("上一轮研究并非来自指定父方案")
    completed_path, result_path = study / "catalog-completed.json", study / "report.json"
    catalog = load(completed_path)
    if catalog["parent"] != execution["catalog"]:
        raise ValueError("累计账本不是上一轮冻结研究的完成修订")
    # The catalog must bind the actual completed result, not another directory's outcome.
    for card in parent["factors"]:
        if catalog["outcomes"][card["id"]]["evidence"] != file_ref(result_path):
            raise ValueError("累计结果与上一轮报告证据不匹配")
    catalog_path = Path(catalog_path) if catalog_path is not None else completed_path
    if catalog_path != completed_path:
        supplied = load(catalog_path)
        ancestor, required = file_ref(catalog_path), file_ref(completed_path)
        while ancestor != required:
            ancestor = read(check_ref(ancestor))["parent"]
            if ancestor is None:
                raise ValueError("挖掘账本不是上一轮完成账本的连续修订")
        catalog = supplied
    search = canonical_space(DEFAULT_SPACE if space is None else space)
    cards, skipped = generate_candidates(search, catalog)
    manifest = read(manifest_path)
    if manifest["protocol"] != file_ref(parent_path) or execution["data_manifest"] != file_ref(manifest_path):
        raise ValueError("数据清单必须来自上一轮已冻结研究")
    protocol = deepcopy(parent)
    protocol.update(id=PROTOCOL_ID, frozen_at=datetime.now(UTC).isoformat(),
                    parent_intraday_protocol=file_ref(parent_path), prior_catalog=file_ref(catalog_path),
                    prior_result=file_ref(result_path), factors=cards, search_space=search,
                    skipped_trials=skipped,
                    feature_definitions=FEATURES_BY_VERSION[search["version"]],
                    independent_validation=False,
                    original_control_required=True, new_outcomes_seen=False)
    protocol.pop("factor_parameters", None)
    protocol.pop("plan", None)
    protocol["study_id"] = "mining_" + hash_payload({"prior": protocol["prior_catalog"], "space": search})[:16]
    protocol["statistics"].update(historical_trials=len(catalog["trials"]), reserved_new_trials=len(cards),
                                  minimum_family_size=len(catalog["trials"]) + len(cards))
    validate_protocol(protocol, parent, catalog)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    save(output / "PROTOCOL.json", protocol)
    save(output / "PROTOCOL.freeze.json", {"protocol": file_ref(output / "PROTOCOL.json")})
    manifest["protocol"] = file_ref(output / "PROTOCOL.json")
    manifest["parent_manifest"] = file_ref(manifest_path)
    save(output / "data-manifest.json", manifest)
    save(output / "candidates.json", {"candidates": cards, "skipped_trials": skipped,
                                     "search_space": search, "price_data_read": False,
                                     "enabled": False, "trading_enabled": False})
    mapping = None
    if snapshots:
        save(output / "policy-snapshots.json", snapshots)
        mapping = file_ref(output / "policy-snapshots.json")
    return {"protocol": file_ref(output / "PROTOCOL.json"),
            "data_manifest": file_ref(output / "data-manifest.json"),
            "policy_snapshots": mapping,
            "candidate_count": len(cards), "family_size": protocol["statistics"]["minimum_family_size"]}


def write_mining_summary(protocol, report, output):
    """Rank every result conservatively; drafts cannot activate a strategy."""
    rows, drafts = [], []
    cards = {c["id"]: c for c in protocol["factors"]}
    for result in report["results"]:
        card = cards[result["id"]]
        windows = [w for s in result["scenarios"] for w in s["windows"]]
        numeric_reasons = sorted({r for s in result["scenarios"] for r in s["reasons"]})
        rows.append({"id": result["id"], "market": result["market"], "status": result["status"],
                     "recipe": card["recipe"], "worst_window_net_return": min((w["net_return"] for w in windows), default=None),
                     "mean_window_net_return": sum(w["net_return"] for w in windows) / len(windows) if windows else None,
                     "positive_window_scenarios": sum(w["net_return"] > 0 for w in windows),
                     "window_scenarios": len(windows), "numeric_reasons": numeric_reasons,
                     "numeric_screen_passed": bool(windows) and not numeric_reasons,
                     "qualified": result["qualified"], "reasons": result["reasons"]})
        drafts.append({"id": result["id"], "market": result["market"], "entry": card["recipe"],
                       "account_policy": protocol["markets"][result["market"]], "enabled": False,
                       "status": "blocked_data" if not windows else "disabled_research_candidate",
                       "independent_validation_required": True, "qualified": result["qualified"]})
    rows.sort(key=lambda r: (r["worst_window_net_return"] is None,
                            -(r["worst_window_net_return"] or 0), r["id"]))
    for market in ("crypto", "stocks"):
        for rank, row in enumerate((r for r in rows if r["market"] == market and r["window_scenarios"]), 1):
            row["research_rank"] = rank
    summary = {"ranking_basis": "worst net window across ALL frozen costs and latencies; descriptive only",
               "independent_validation": False, "family_size": report["family_size"],
               "all_candidates": rows, "qualified_factors": report["factor_pool"],
               "formal_admission": False, "trading_enabled": False, "search_stopped": True}
    save(Path(output) / "mining-summary.json", summary)
    save(Path(output) / "strategy-drafts.json", {"strategies": drafts, "enabled": False,
                                               "formal_admission": False, "trading_enabled": False})
