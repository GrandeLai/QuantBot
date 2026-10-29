"""Walk-forward 样本外验证框架。

================== 为什么需要它 ==================
在同一段历史上"调参 + 展示结果"的回测必然含有拟合成分。
Walk-forward 用滚动切分模拟真实的研究流程：

    每一折：只用"训练窗"的数据挑选参数（当时能做到的事），
            然后把选中的参数应用到紧随其后的"测试窗"（未来）。

把各折测试窗的表现拼接起来，得到的就是**样本外（OOS）权益曲线**——
它是对策略未来预期最接近诚实的估计。

================== 本实现的设计 ==================
- 扩展窗（expanding window）切分：训练窗起点固定、逐折延长，
  更贴近"随着时间积累数据"的真实研究状态。
- 参数网格只覆盖被主动调过、最有过拟合嫌疑的资金管理参数
  （跟踪止损宽度 × 单笔风险），K 线通道等结构参数保持经典取值不动，
  因此所有网格配置共享同一份特征表，只需计算一次。
- 训练窗按 Calmar 比率选参（收益与回撤的平衡），并列取 CAGR 高者。
- 同时报告"默认配置"在各测试窗的表现，对比"训练窗选参"是否稳定。
==================================================
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import pandas as pd

from ..data.universe import CASH_PROXY
from ..priceaction.features import build_features
from .engine import BacktestEngine
from .metrics import compute_metrics

# 参数网格：只含不影响特征表的资金管理/离场参数（共享特征，计算高效）。
# time_stop_days 经消融显示方向为正但不显著（+0.2~+2.2pp, p>0.2），
# 纳入网格由各折训练窗自选、样本外裁决——这是处理"疑似有用但
# 统计不确定"改动的标准流程。
DEFAULT_GRID: list[dict] = [
    {"atr_trail_mult": t, "risk_per_trade": r,
     "pyramid_size_frac": p, "time_stop_days": ts}
    for t in (3.5, 4.5, 5.5)
    for r in (0.02, 0.025, 0.03)
    for p in (0.5, 0.65)
    for ts in (0, 40, 60)
]

# 逐年扩展窗切分：训练窗起点固定 2015、逐年延长，测试窗为紧随其后的一年。
# 折数多 → 可报告样本外表现的逐年离散度，而不只是拼接均值。
DEFAULT_FOLDS: list[tuple[str, str, str, str]] = [
    ("2015-01-01", f"{y - 1}-12-31", f"{y}-01-01", f"{y}-12-31")
    for y in range(2019, 2026)
]

# 网格覆盖项 → 配置小节的映射
_STRATEGY_KEYS = {"atr_trail_mult", "pyramid_size_frac", "time_stop_days",
                  "time_stop_min_r", "exit_window", "breakout_window"}


@dataclass
class FoldResult:
    """一折 walk-forward 的结果。"""
    fold: int
    train_window: str
    test_window: str
    chosen: dict                 # 训练窗选中的参数
    train_metrics: dict          # 选中参数在训练窗的表现
    test_metrics: dict           # 选中参数在测试窗的表现（样本外）
    default_test_metrics: dict   # 默认配置在同一测试窗的表现（对照）
    test_equity: pd.Series       # 测试窗权益曲线（拼接 OOS 曲线用）


def _apply(cfg: dict, overrides: dict) -> dict:
    """把网格覆盖项写入配置副本（按键名路由到 strategy / risk 小节）。"""
    out = copy.deepcopy(cfg)
    for key, val in overrides.items():
        section = "strategy" if key in _STRATEGY_KEYS else "risk"
        out[section][key] = val
    return out


def _run(data: dict, cfg: dict, features: dict,
         start: str, end: str) -> tuple[dict, pd.Series]:
    """在指定窗口运行一次回测，返回 (指标, 权益曲线)。"""
    cfg = copy.deepcopy(cfg)
    cfg["backtest"]["start"] = start
    cfg["backtest"]["end"] = end
    res = BacktestEngine(data, cfg, features=features).run()
    return compute_metrics(res.equity, res.trades_df()), res.equity


def run_walkforward(
    data: dict[str, pd.DataFrame],
    base_cfg: dict,
    grid: list[dict] | None = None,
    folds: list[tuple[str, str, str, str]] | None = None,
) -> tuple[list[FoldResult], pd.Series]:
    """执行完整 walk-forward，返回各折结果与拼接的样本外权益曲线。"""
    grid = grid if grid is not None else DEFAULT_GRID
    folds = folds if folds is not None else DEFAULT_FOLDS

    # 特征只算一次（网格不含特征参数），所有折与配置共享
    print("[wf] 预计算特征表（全网格共享）...")
    features = {
        sym: build_features(df, base_cfg["strategy"])
        for sym, df in data.items()
        if sym != CASH_PROXY
    }

    results: list[FoldResult] = []
    oos_segments: list[pd.Series] = []

    for i, (tr_s, tr_e, te_s, te_e) in enumerate(folds, 1):
        print(f"[wf] 第 {i} 折：训练 {tr_s}~{tr_e} → 测试 {te_s}~{te_e}")

        # ---- 训练窗：网格选参（按 Calmar，并列取 CAGR） ----
        best_overrides, best_metrics, best_score = None, None, None
        for overrides in grid:
            m, _ = _run(data, _apply(base_cfg, overrides), features, tr_s, tr_e)
            score = (m["calmar"], m["cagr"])
            if best_score is None or score > best_score:
                best_score, best_overrides, best_metrics = score, overrides, m
            print(f"[wf]   网格 {overrides} → 训练 CAGR {m['cagr']:.1%} "
                  f"Calmar {m['calmar']:.2f}")

        # ---- 测试窗：选中参数（样本外）与默认配置（对照） ----
        test_m, test_eq = _run(
            data, _apply(base_cfg, best_overrides), features, te_s, te_e
        )
        default_m, _ = _run(data, base_cfg, features, te_s, te_e)

        results.append(FoldResult(
            fold=i,
            train_window=f"{tr_s}~{tr_e}",
            test_window=f"{te_s}~{te_e}",
            chosen=best_overrides,
            train_metrics=best_metrics,
            test_metrics=test_m,
            default_test_metrics=default_m,
            test_equity=test_eq,
        ))
        oos_segments.append(test_eq)

    # ---- 拼接样本外权益曲线（各测试窗首尾相接复利） ----
    stitched = []
    level = 1.0
    for seg in oos_segments:
        norm = seg / seg.iloc[0] * level
        stitched.append(norm)
        level = norm.iloc[-1]
    oos_equity = pd.concat(stitched)
    oos_equity = oos_equity[~oos_equity.index.duplicated(keep="last")]

    return results, oos_equity


def format_report(results: list[FoldResult], oos_equity: pd.Series) -> str:
    """渲染 walk-forward 中文报告。"""
    lines = ["====== Walk-Forward 样本外验证报告 ======", ""]
    for r in results:
        lines += [
            f"第 {r.fold} 折  训练 {r.train_window} → 测试 {r.test_window}",
            f"  训练窗选中参数: {r.chosen}"
            f"（训练 CAGR {r.train_metrics['cagr']:+.1%}，"
            f"Calmar {r.train_metrics['calmar']:.2f}）",
            f"  测试窗(样本外): CAGR {r.test_metrics['cagr']:+.1%}，"
            f"回撤 {r.test_metrics['max_drawdown']:.1%}，"
            f"夏普 {r.test_metrics['sharpe']:.2f}",
            f"  默认配置(对照): CAGR {r.default_test_metrics['cagr']:+.1%}，"
            f"回撤 {r.default_test_metrics['max_drawdown']:.1%}，"
            f"夏普 {r.default_test_metrics['sharpe']:.2f}",
            "",
        ]
    # 各折样本外的离散度（逐年折时即逐年 OOS 分布）
    fold_cagrs = [r.test_metrics["cagr"] for r in results]
    lines += [
        "---- 各折样本外 CAGR 离散度 ----",
        "  " + " | ".join(f"{c:+.1%}" for c in fold_cagrs),
        f"  最差折 {min(fold_cagrs):+.1%}，最好折 {max(fold_cagrs):+.1%}",
        "",
    ]
    m = compute_metrics(oos_equity)
    lines += [
        "---- 拼接样本外（OOS）总体表现 ----",
        f"  样本外区间: {oos_equity.index[0].date()} ~ {oos_equity.index[-1].date()}",
        f"  样本外 CAGR:   {m['cagr']:+.2%}",
        f"  样本外最大回撤: {m['max_drawdown']:.1%}",
        f"  样本外夏普:    {m['sharpe']:.2f}",
        f"  样本外 Calmar: {m['calmar']:.2f}",
        "",
        "解读：样本外 CAGR 是对策略未来预期最接近诚实的估计；",
        "若各折训练窗选中的参数彼此接近且与默认配置表现相当，",
        "说明参数面平坦、系统对参数不敏感（好事）。",
    ]
    return "\n".join(lines)
