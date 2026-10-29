"""模拟盘 / 实盘绩效报告：实际表现 vs 影子回测 + 实测滑点。

"3 个月模拟盘验收"能否通过，靠的不是主观印象，而是三个可判定的数字：

    1. 实际权益曲线 vs 影子回测同期曲线的偏差（跟踪误差）；
    2. 实测滑点分布 vs 回测假设的 5bp（若实测显著更高，
       应把 backtest.slippage_bps 上调后重跑回测，校准预期）；
    3. 期间收益是否落在回测统计尺子给出的置信区间内。

数据来源（由 runner 每日自动落盘）：
    账户账本同目录的 equity_history.csv：执行时刻权益（兼容导出按信号日标记）
    账户账本同目录的 fills.csv：每笔成交的参考价/成交价/滑点
    正式收盘权益与采样时刻以 SQLite 的 paper-close 事实为准。

用法：python main.py report --mode sim
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from .paths import RuntimePaths


def cmd_report(cfg: dict, *, paths: RuntimePaths) -> None:
    """生成实盘/模拟盘运行报告。"""
    eq_path = paths.logs_dir / "equity_history.csv"
    if not eq_path.exists():
        print(f"[report] 尚无运行记录（{eq_path} 不存在）。"
              "模拟盘运行至少 1 天后再来。")
        return

    eq = pd.read_csv(eq_path, parse_dates=["date"]).drop_duplicates(
        subset="date", keep="last").set_index("date").sort_index()
    start, end = eq.index[0], eq.index[-1]
    days = len(eq)
    total_ret = eq["equity"].iloc[-1] / eq["equity"].iloc[0] - 1

    lines = [
        "====== 模拟盘/实盘运行报告 ======",
        f"运行区间: {start.date()} ~ {end.date()}（{days} 个交易日）",
        f"当前权益: ${eq['equity'].iloc[-1]:,.0f}"
        f"（期初 ${eq['equity'].iloc[0]:,.0f}，累计 {total_ret:+.2%}）",
        f"平均持仓数: {eq['n_positions'].mean():.1f}，"
        f"开关档位为 0 的天数: {(eq['level'] == 0).sum()}",
    ]

    # ---- 实测滑点 ----
    fills_path = paths.logs_dir / "fills.csv"
    if fills_path.exists():
        fills = pd.read_csv(fills_path)
        if len(fills):
            s = fills["slip_bps"]
            lines += [
                "",
                f"实测滑点（{len(fills)} 笔成交，正值 = 对我不利）:",
                f"  中位 {s.median():+.1f}bp | 均值 {s.mean():+.1f}bp | "
                f"P90 {s.quantile(0.9):+.1f}bp",
                f"  回测假设 {cfg['backtest']['slippage_bps']}bp —— "
                + ("实测更差，建议上调回测滑点后重新校准预期"
                   if s.median() > cfg["backtest"]["slippage_bps"] * 1.5
                   else "实测未显著劣于假设"),
            ]

    # ---- 影子回测同期对比 ----
    # 用与实盘完全相同的配置，把回测起点对齐到实盘首日、
    # 初始资金对齐到实盘期初权益，比较同期表现。
    # 注意：影子回测从空仓起步而实盘可能已有持仓，头几周存在
    # 合理的路径差异；运行越久对比越有意义。
    # 重叠天数不可能超过已有权益样本数；不足五天时无须补数或构建回测。
    if days < 5:
        lines += ["", "影子回测对比: 重叠交易日不足 5 天，继续积累数据"]
    else:
        try:
            from ..backtest.engine import BacktestEngine
            from ..data.downloader import load_market_data

            shadow_cfg = json.loads(json.dumps(cfg))
            shadow_cfg["data"]["snapshot"] = None
            shadow_cfg["data"]["cache_dir"] = str(paths.cache_dir)
            shadow_cfg["backtest"]["start"] = str(start.date())
            shadow_cfg["backtest"]["end"] = str(end.date())
            shadow_cfg["backtest"]["initial_capital"] = float(eq["equity"].iloc[0])
            data = load_market_data(
                shadow_cfg,
                cache_dir_override=str(paths.cache_dir),
            )
            shadow = BacktestEngine(data, shadow_cfg).run().equity
            common = eq.index.intersection(shadow.index)
            if len(common) >= 5:
                a = eq.loc[common, "equity"]
                b = shadow.loc[common]
                shadow_ret = b.iloc[-1] / b.iloc[0] - 1
                diff = (a.pct_change().dropna() - b.pct_change().dropna())
                lines += [
                    "",
                    "影子回测同期对比:",
                    f"  实际 {total_ret:+.2%} vs 影子 {shadow_ret:+.2%}"
                    f"（差 {total_ret - shadow_ret:+.2%}）",
                    f"  日收益跟踪误差(std): {diff.std() * np.sqrt(252):.2%}/年化",
                    "  （上线初期的路径差异属正常，警惕的是持续扩大的偏离）",
                ]
            else:
                lines += ["", "影子回测对比: 重叠交易日不足 5 天，继续积累数据"]
        except Exception as e:  # noqa: BLE001 - 影子回测失败不影响主报告
            lines += ["", f"影子回测对比失败（{e}），主报告不受影响"]

    report = "\n".join(lines)
    print(report)
    (paths.logs_dir / "live_report.txt").write_text(report, encoding="utf-8")
