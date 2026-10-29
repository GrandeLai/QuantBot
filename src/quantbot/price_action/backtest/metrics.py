"""绩效指标计算与回测报告生成。"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

TRADING_DAYS = 252  # 美股年均交易日数


def compute_metrics(equity: pd.Series, trades: pd.DataFrame | None = None) -> dict:
    """由权益曲线（与可选的交易明细）计算核心绩效指标。

    返回 dict，包括：
        total_return   总收益率
        cagr           年化复合收益率
        sharpe         夏普比率（日收益年化，无风险利率取 0）
        max_drawdown   最大回撤
        calmar         Calmar 比率（年化收益 / 最大回撤）
        volatility     年化波动率
        win_rate       单笔胜率
        profit_factor  盈亏比（总盈利 / 总亏损）
        num_trades     交易笔数
    """
    ret = equity.pct_change().dropna()
    n_years = len(equity) / TRADING_DAYS

    total_return = equity.iloc[-1] / equity.iloc[0] - 1
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1 if n_years > 0 else 0.0

    vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = ret.mean() / ret.std() * np.sqrt(TRADING_DAYS) if ret.std() > 0 else 0.0

    # 最大回撤：权益相对历史峰值的最大跌幅
    peak = equity.cummax()
    drawdown = equity / peak - 1
    max_dd = drawdown.min()

    calmar = cagr / abs(max_dd) if max_dd < 0 else float("inf")

    m = {
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "volatility": vol,
    }

    if trades is not None and len(trades) > 0:
        wins = trades[trades["pnl"] > 0]
        losses = trades[trades["pnl"] <= 0]
        gross_win = wins["pnl"].sum()
        gross_loss = abs(losses["pnl"].sum())
        m.update(
            {
                "num_trades": len(trades),
                "win_rate": len(wins) / len(trades),
                "profit_factor": gross_win / gross_loss if gross_loss > 0 else float("inf"),
                "avg_win": wins["ret"].mean() if len(wins) else 0.0,
                "avg_loss": losses["ret"].mean() if len(losses) else 0.0,
            }
        )
    return m


def format_metrics(m: dict, title: str = "策略") -> str:
    """把指标 dict 渲染成适合终端打印的中文表格。"""
    lines = [f"====== {title} 绩效 ======"]
    fmt = [
        ("总收益率", "total_return", "{:+.1%}"),
        ("年化收益率 (CAGR)", "cagr", "{:+.2%}"),
        ("夏普比率", "sharpe", "{:.2f}"),
        ("最大回撤", "max_drawdown", "{:.1%}"),
        ("Calmar 比率", "calmar", "{:.2f}"),
        ("年化波动率", "volatility", "{:.1%}"),
        ("交易笔数", "num_trades", "{}"),
        ("胜率", "win_rate", "{:.1%}"),
        ("盈亏比 (Profit Factor)", "profit_factor", "{:.2f}"),
        ("平均盈利单收益", "avg_win", "{:+.1%}"),
        ("平均亏损单收益", "avg_loss", "{:+.1%}"),
    ]
    for name, key, f in fmt:
        if key in m:
            lines.append(f"  {name:<22s} {f.format(m[key])}")
    return "\n".join(lines)


def save_report(result, out_dir: str | Path = "reports") -> Path:
    """生成回测报告：权益曲线图 + 回撤图 + 交易明细 CSV + 指标文本。

    返回报告目录路径。
    """
    import matplotlib
    matplotlib.use("Agg")  # 无界面环境下渲染
    import matplotlib.pyplot as plt

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    equity = result.equity
    trades = result.trades_df()

    # ---- 指标 ----
    m_strat = compute_metrics(equity, trades)
    text = format_metrics(m_strat, "PABot 策略")
    if result.benchmark is not None:
        m_bench = compute_metrics(result.benchmark)
        text += "\n\n" + format_metrics(m_bench, "SPY 买入持有基准")

    # ---- 统计尺子：置信区间与利润集中度（诚实的不确定性刻画） ----
    from .stats import bootstrap_metrics, concentration, format_stats_report

    boot = bootstrap_metrics(equity)
    conc = concentration(trades, equity)
    text += "\n\n" + format_stats_report(boot, conc)

    (out / "metrics.txt").write_text(text, encoding="utf-8")

    # ---- 交易明细 ----
    trades.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")

    # ---- 图：权益曲线（对数轴）+ 回撤 ----
    fig, axes = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )
    ax1, ax2 = axes
    ax1.plot(equity.index, equity.values, label="PABot", linewidth=1.4)
    if result.benchmark is not None:
        ax1.plot(
            result.benchmark.index, result.benchmark.values,
            label="SPY Buy&Hold", linewidth=1.1, alpha=0.8,
        )
    ax1.set_yscale("log")
    ax1.set_title("Equity Curve (log scale)")
    ax1.legend()
    ax1.grid(alpha=0.3)

    dd = equity / equity.cummax() - 1
    ax2.fill_between(dd.index, dd.values, 0, alpha=0.4)
    ax2.set_title("Drawdown")
    ax2.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(out / "equity_curve.png", dpi=130)
    plt.close(fig)

    # 权益曲线原始数据也存一份，便于后续分析
    equity.to_csv(out / "equity.csv", encoding="utf-8")

    print(text)
    print(f"\n[report] 报告已保存至 {out.resolve()}/")
    return out
