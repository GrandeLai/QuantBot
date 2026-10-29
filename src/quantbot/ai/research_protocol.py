"""研究方案、可复现对照和统计诊断；不负责账户或任务调度。"""

from dataclasses import asdict, dataclass
import math
import random
from statistics import mean, stdev

from quantbot.statistical_validation import (
    cscv_partition, cscv_probability_of_backtest_overfitting, deflated_sharpe_ratio,
)

from .domain import Allocation, DecisionBatch


@dataclass(frozen=True)
class ResearchProtocol:
    development_bars: int = 30
    folds: int = 3
    gap_bars: int = 1
    holdout_bars: int = 40
    min_observations: int = 30
    min_return: float = 0.0
    min_excess_return: float = 0.001
    max_drawdown: float = 0.10
    min_dsr: float = 0.95
    max_pbo: float = 0.5
    cost_multiplier: float = 2.0
    weight_perturbation: float = 0.10
    placebo_seeds: tuple[int, ...] = (7, 42, 99)
    model_request_budget: int = 500
    execution_delay_bars: int = 0

    def __post_init__(self):
        for key, low, high in (
            ("development_bars", 3, 200), ("folds", 2, 10), ("gap_bars", 1, 100),
            ("holdout_bars", 4, 200), ("min_observations", 3, 500), ("model_request_budget", 0, 10000),
            ("execution_delay_bars", 0, 5),
        ):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{key} 必须为 {low}..{high} 的整数")
        for key, low, high in (
            ("min_return", 0, 10), ("min_excess_return", 0, 10), ("max_drawdown", 0.001, 0.99),
            ("min_dsr", 0.5, 1), ("max_pbo", 0, 0.5), ("cost_multiplier", 1.1, 5),
            ("weight_perturbation", 0.01, 0.5),
        ):
            value = getattr(self, key)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} 必须为 {low}..{high} 的有限数值")
        seeds = self.placebo_seeds
        if (not isinstance(seeds, (list, tuple)) or not 2 <= len(seeds) <= 5
                or any(type(s) is not int or not 0 <= s <= 2**32 - 1 for s in seeds)
                or len(set(seeds)) != len(seeds)):
            raise ValueError("placebo_seeds 必须为 2..5 个不同的非负整数种子")
        object.__setattr__(self, "placebo_seeds", tuple(seeds))
        if self.required_bars > 500:
            raise ValueError("研究最多使用 500 根预热后的行情")

    @property
    def required_bars(self):
        return self.development_bars * self.folds + self.gap_bars + self.holdout_bars


def hypothesis_card(value):
    fields = {"title": 120, "hypothesis": 2000, "rationale": 2000, "failure_condition": 2000}
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("研究卡片必须包含 title、hypothesis、rationale、failure_condition")
    for key, limit in fields.items():
        if not isinstance(value[key], str) or not 1 <= len(value[key].strip()) <= limit:
            raise ValueError(f"研究卡片 {key} 必须为 1..{limit} 字符")
    return {key: value[key].strip() for key in fields}


class RecordedDecisionProvider:
    """固定原始目标持仓，仅改变执行成本/仓位约束；不重新调用模型。"""

    def __init__(self, decisions):
        self.decisions = {d["as_of"]: d for d in decisions}

    def decide(self, context, account, config):
        record = self.decisions.get(context.as_of.isoformat())
        if record is None or record["snapshot_id"] != context.snapshot_id:
            raise ValueError("压力测试缺少对应时点的冻结决策")
        raw = record["raw_decision"]
        return DecisionBatch(tuple(Allocation(**a) for a in raw["allocations"]),
                             "recorded_decisions", raw["model"])


class DelayedRecordedDecisionProvider:
    """延迟已冻结目标，释放时按当时账户和价格重新风控；不调用模型。"""

    def __init__(self, decisions, times, delay_bars):
        self.decisions = {d["as_of"]: d for d in decisions}
        self.times = [at.isoformat() for at in times]
        self.delay_bars = delay_bars

    def decide(self, context, account, config):
        index = self.times.index(context.as_of.isoformat()) - self.delay_bars
        if index < 0:
            allocations = tuple(Allocation(s, 0, 1, "等待延迟目标释放") for s in context.features)
            model = "delayed_initial_cash"
        else:
            raw = self.decisions[self.times[index]]["raw_decision"]
            allocations = tuple(Allocation(**a) for a in raw["allocations"])
            model = raw["model"]
        return DecisionBatch(allocations, "delayed_recorded_decisions", model)


class PlaceboProvider:
    """预登记种子的随机目标仓位对照；与候选共用风险和撮合规则。"""

    def __init__(self, seed):
        self.seed = seed

    def decide(self, context, account, config):
        rng = random.Random(f"{self.seed}:{context.as_of.isoformat()}")
        allocations = tuple(
            Allocation(s, config["risk"]["max_weight"] if rng.random() >= 0.5 else 0, 1.0,
                       f"随机对照，种子 {self.seed}")
            for s in sorted(context.features)
        )
        return DecisionBatch(allocations, "placebo", "seeded_random_allocation")


def returns_of(result):
    equity = [p["equity"] for p in result["equity_curve"]]
    return [after / before - 1 for before, after in zip(equity, equity[1:])]


def statistics(trial_returns, selected_id, holdout, periods_per_year):
    """全部候选在相同开发窗口对齐；留出集只评估预先选定的一个候选。"""
    result = {"scope": "current_preregistered_candidate_set_only",
              "trial_count": len(trial_returns), "observations": len(holdout)}
    try:
        sharpes = []
        for values in trial_returns.values():
            deviation = stdev(values)
            if deviation <= 1e-15:
                raise ValueError("存在收益方差为零的候选，不能可靠估计 DSR")
            sharpes.append(mean(values) / deviation * math.sqrt(periods_per_year))
        result["dsr"] = {"status": "available", **asdict(deflated_sharpe_ratio(
            holdout, sharpes, periods_per_year=periods_per_year,
        ))}
    except ValueError as exc:
        result["dsr"] = {"status": "insufficient", "reason": str(exc)}
    if len(trial_returns) == 1:
        result["pbo"] = {"status": "not_applicable", "reason": "只有一个预登记候选，没有跨候选选择"}
    else:
        try:
            partition = cscv_partition(trial_returns, fold_preference=(8, 6, 4, 2))
            if partition is None:
                raise ValueError("开发窗口样本不足，无法分割 CSCV")
            result["pbo"] = {"status": "available", "dropped_observations": partition.dropped_observations,
                             **asdict(cscv_probability_of_backtest_overfitting(
                                 partition.trial_returns, folds=partition.folds))}
        except ValueError as exc:
            result["pbo"] = {"status": "insufficient", "reason": str(exc)}
    result["selected_id"] = selected_id
    return result


def conclude(protocol, *, execution_completed, development, holdout, baseline, stress, placebos,
             diagnostics, holdout_exposed, comparable_trials, synthetic_data=False):
    checks = {
        "historical_data": not synthetic_data,
        "execution_complete": execution_completed,
        "unexposed_holdout": not holdout_exposed,
        "all_trials_comparable": comparable_trials,
        "enough_development_observations": diagnostics["development_observations"] >= protocol.min_observations,
        "enough_holdout_observations": diagnostics["observations"] >= protocol.min_observations,
        "positive_development_return": development["return_fraction"] > protocol.min_return,
        "positive_holdout_return": holdout["return_fraction"] > protocol.min_return,
        "beats_rules": holdout["return_fraction"] - baseline["return_fraction"] > protocol.min_excess_return,
        "drawdown_within_limit": -holdout["max_drawdown_fraction"] <= protocol.max_drawdown,
        "stress_survives": all(r["return_fraction"] > protocol.min_return
                               and -r["max_drawdown_fraction"] <= protocol.max_drawdown for r in stress),
        "beats_placebos": holdout["return_fraction"] > max(r["return_fraction"] for r in placebos),
        "dsr": diagnostics["dsr"].get("confidence", -1) >= protocol.min_dsr,
        "pbo": diagnostics["pbo"]["status"] == "not_applicable"
               or diagnostics["pbo"].get("probability", 1) <= protocol.max_pbo,
    }
    insufficient = any(not checks[key] for key in (
        "historical_data", "execution_complete", "unexposed_holdout", "all_trials_comparable",
        "enough_development_observations", "enough_holdout_observations",
    )) or any(diagnostics[k]["status"] == "insufficient" for k in ("dsr", "pbo"))
    outcome = "insufficient" if insufficient else "validated" if all(checks.values()) else "rejected"
    return {"conclusion": outcome, "research_passed": outcome == "validated", "checks": checks,
            "reasons": [key for key, passed in checks.items() if not passed]}
