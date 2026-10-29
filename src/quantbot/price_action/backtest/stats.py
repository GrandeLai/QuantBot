"""统计尺子：bootstrap 置信区间、尾部集中度、消融配对显著性检验。

================== 为什么需要它 ==================
回测给出的是"这条历史路径"上的点估计。趋势跟随的利润高度集中在
少数大赢家上（本系统 top-10 交易贡献了总利润的 100% 以上），
点估计对"是否恰好抓住那几笔"极其敏感。本模块提供三把尺子：

1. bootstrap_metrics —— 对日收益做块状 bootstrap（保留短期自相关），
   给出 CAGR / 最大回撤 / 夏普的置信区间，而非单一数字；
2. concentration —— 量化利润对头部交易的依赖度
   （top-N 利润占比、剔除头部交易后的年化）；
3. paired_test —— 两组配置的消融对比是否可区分于数据噪声
   （对同期日收益差做块状 bootstrap，输出差值区间与 p 值）。

★ 方法局限（报告中已注明）：
   块状 bootstrap 打乱了跨块的长期趋势结构，对趋势策略而言
   回撤区间偏悲观、收益区间偏宽——把它读作"下界视角"而非精确分布。
====================================================
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def _block_bootstrap_paths(
    returns: np.ndarray, n_paths: int, block: int, rng: np.random.Generator
) -> np.ndarray:
    """块状 bootstrap：按 block 长度整块重抽样，拼出 n_paths 条等长收益路径。

    返回形状 (n_paths, len(returns)) 的矩阵。
    序列短于一个 block 时退化为逐日重抽样（等价 block=1）。
    """
    n = len(returns)
    if block >= n:
        block = 1   # 短序列退化为逐日重抽样（保持路径间的随机差异）
    n_blocks = int(np.ceil(n / block))
    # 每条路径抽 n_blocks 个随机起点，各取 block 长的连续片段。
    # 上界 n - block + 1（integers 高端开区间）→ 起点最大 n - block，
    # 保证最后一天的收益也能被采样到。
    starts = rng.integers(0, n - block + 1, size=(n_paths, n_blocks))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]).reshape(n_paths, -1)
    return returns[idx[:, :n]]


def bootstrap_metrics(
    equity: pd.Series,
    n_paths: int = 2000,
    block: int = 21,
    seed: int = 42,
) -> dict:
    """对权益曲线的日收益做块状 bootstrap，返回核心指标的分位数。

    返回 dict:
        cagr_p5/p50/p95, maxdd_p5/p50/p95, sharpe_p5/p50/p95
    （maxdd 的 p5 是"最深 5% 分位"，即更差的那端）
    """
    rets = equity.pct_change().dropna().to_numpy()
    n = len(rets)
    rng = np.random.default_rng(seed)
    paths = _block_bootstrap_paths(rets, n_paths, block, rng)

    # 逐路径指标（向量化）
    growth = np.cumprod(1 + paths, axis=1)
    total = growth[:, -1]
    cagr = total ** (TRADING_DAYS / n) - 1
    peak = np.maximum.accumulate(growth, axis=1)
    maxdd = (growth / peak - 1).min(axis=1)
    std = paths.std(axis=1)
    sharpe = np.where(std > 0, paths.mean(axis=1) / std * np.sqrt(TRADING_DAYS), 0.0)

    def q(x, p):
        return float(np.percentile(x, p))

    return {
        "cagr_p5": q(cagr, 5), "cagr_p50": q(cagr, 50), "cagr_p95": q(cagr, 95),
        "maxdd_p5": q(maxdd, 5), "maxdd_p50": q(maxdd, 50), "maxdd_p95": q(maxdd, 95),
        "sharpe_p5": q(sharpe, 5), "sharpe_p50": q(sharpe, 50), "sharpe_p95": q(sharpe, 95),
        "n_paths": n_paths, "block": block,
    }


def concentration(trades: pd.DataFrame, equity: pd.Series) -> dict:
    """利润集中度：量化结果对头部交易的依赖。

    返回:
        top10_share    top-10 交易利润占总利润比例（可 >1：其余交易合计为负）
        top1_share     最大单笔占比
        cagr_full      全样本年化
        cagr_ex_top1/3/5  剔除头部 1/3/5 笔交易利润后的近似年化
                       （近似法：期末权益减去对应利润重算 CAGR，
                        忽略复利路径变化，方向与量级正确）
    """
    n_years = len(equity) / TRADING_DAYS
    start, end = float(equity.iloc[0]), float(equity.iloc[-1])
    total_pnl = trades["pnl"].sum()
    top = trades.nlargest(10, "pnl")["pnl"]

    out = {
        "top10_share": float(top.sum() / total_pnl) if total_pnl else float("nan"),
        "top1_share": float(top.iloc[0] / total_pnl) if total_pnl else float("nan"),
        "cagr_full": (end / start) ** (1 / n_years) - 1,
    }
    for k in (1, 3, 5):
        removed = trades.nlargest(k, "pnl")["pnl"].sum()
        adj_end = max(end - removed, start * 0.01)
        out[f"cagr_ex_top{k}"] = (adj_end / start) ** (1 / n_years) - 1
    return out


def paired_test(
    equity_a: pd.Series,
    equity_b: pd.Series,
    n_paths: int = 2000,
    block: int = 21,
    seed: int = 42,
) -> dict:
    """消融配对检验：A/B 两组配置的差异能否与噪声区分。

    对同期（按日期对齐）的日收益差做块状 bootstrap：
        diff_annual   年化收益差的点估计（A - B）
        ci_low/high   差值的 90% 置信区间
        p_value       双侧 p 值（差值分布中 0 所处的分位）

    使用约定（写进项目文化）：
        significant（p < 0.10 且区间不跨零）→ 差异统计上可信；
        actionable（且 |年化差| >= 0.5pp）→ 效应量才值得为之改动系统。
        两者都不满足时按【简单优先 / 默认保守】处理，
        不要因为点估计好看就采纳复杂改动。
    """
    a = equity_a.pct_change().dropna()
    b = equity_b.pct_change().dropna()
    idx = a.index.intersection(b.index)
    diff = (a.loc[idx] - b.loc[idx]).to_numpy()
    n = len(diff)
    if n < block * 3:
        raise ValueError("重叠样本太短，无法做配对检验")

    rng = np.random.default_rng(seed)
    paths = _block_bootstrap_paths(diff, n_paths, block, rng)
    annual_diffs = paths.mean(axis=1) * TRADING_DAYS

    point = float(diff.mean() * TRADING_DAYS)
    ci_low, ci_high = np.percentile(annual_diffs, [5, 95])
    # 双侧 p 值：bootstrap 分布中越过零的比例
    frac_below = float((annual_diffs < 0).mean())
    p_value = 2 * min(frac_below, 1 - frac_below)

    significant = bool(p_value < 0.10 and ci_low * ci_high > 0)
    return {
        "diff_annual": point,
        "ci_low": float(ci_low),
        "ci_high": float(ci_high),
        "p_value": p_value,
        "significant": significant,
        # 统计显著 + 效应量达标（年化差 >= 0.5pp）才值得改动系统
        "actionable": bool(significant and abs(point) >= 0.005),
        "n_days": n,
    }


def format_stats_report(boot: dict, conc: dict) -> str:
    """渲染统计尺子的中文报告段落。"""
    lines = [
        "====== 统计尺子（block bootstrap，下界视角） ======",
        f"  CAGR 90% 区间     [{boot['cagr_p5']:+.1%}, {boot['cagr_p95']:+.1%}]"
        f"（中位 {boot['cagr_p50']:+.1%}）",
        f"  最大回撤 90% 区间  [{boot['maxdd_p5']:.1%}, {boot['maxdd_p95']:.1%}]"
        f"（中位 {boot['maxdd_p50']:.1%}）",
        f"  夏普 90% 区间     [{boot['sharpe_p5']:.2f}, {boot['sharpe_p95']:.2f}]",
        "",
        "====== 利润集中度 ======",
        f"  top-10 交易利润占比   {conc['top10_share']:.0%}",
        f"  最大单笔利润占比      {conc['top1_share']:.0%}",
        f"  剔除 top-1/3/5 笔后年化  {conc['cagr_ex_top1']:+.1%} / "
        f"{conc['cagr_ex_top3']:+.1%} / {conc['cagr_ex_top5']:+.1%}"
        f"（全样本 {conc['cagr_full']:+.1%}）",
        "",
        "  注：块状 bootstrap 打乱长趋势结构，对趋势策略回撤偏悲观；",
        "      利润高度集中是趋势跟随的本质而非缺陷，但意味着",
        "      错过头部交易的路径风险真实存在，仓位信心应据此校准。",
    ]
    return "\n".join(lines)
