"""从 SQLite 事实账本生成按交易日的中文价格行为复盘报告。"""

from __future__ import annotations

import json
import math
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from statistics import mean, median
from typing import Any

from .journal import ReviewJournal


def _counts(rows: list[dict[str, Any]], field: str) -> str:
    values = Counter(str(row[field]) for row in rows)
    return "、".join(f"{key} {values[key]}" for key in sorted(values))


def _day(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except (AttributeError, ValueError) as exc:
        raise ValueError("trading_date 必须是 ISO 日期 YYYY-MM-DD") from exc


def _percentile_90(values: list[float]) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * 0.9) - 1)
    return ordered[index]


def render_daily_review(
    journal: ReviewJournal | str | Path,
    trading_date: date | datetime | str,
) -> str:
    """生成某交易日的中文 Markdown 复盘报告。

    报告只读取 SQLite 账本；缺样本时明确说明，绝不把“没有记录”解释
    为“没有异常”或“没有交易”。
    """
    day = _day(trading_date)
    owns_journal = not isinstance(journal, ReviewJournal)
    source = ReviewJournal(journal) if owns_journal else journal
    try:
        records = source.records_for_date(day)
    finally:
        if owns_journal:
            source.close()

    runs = records["runs"]
    decisions = records["decisions"]
    orders = records["orders"]
    fills = records["fills"]
    position_events = records["position_events"]
    equity = records["equity_snapshots"]
    reconciliations = records["reconciliations"]

    lines = [
        f"# 价格行为交易复盘（{day}）",
        "",
        "> 事实源：SQLite 只追加账本。CSV 或人工表格仅可作为导出物。",
        "",
        "## 一、数据复盘",
        "",
    ]
    if not runs:
        lines += [
            "- 无运行样本：无法确认当天是否刷新行情、生成信号或执行任务。",
            "- 无数据版本样本：不得据此推断数据正常。",
        ]
    else:
        lines.append(f"- 运行记录：{len(runs)} 条（{_counts(runs, 'status')}）。")
        lines.append(f"- 数据状态：{_counts(runs, 'data_status')}。")
        as_of = sorted({row["data_as_of"] for row in runs if row["data_as_of"]})
        lines.append(
            f"- 数据截至：{'、'.join(as_of)}。"
            if as_of
            else "- 数据截至日期未记录：无法独立判断行情新鲜度。"
        )
        hashes = sorted({row["data_hash"] for row in runs if row["data_hash"]})
        lines.append(
            f"- 数据版本哈希：{len(hashes)} 个。"
            if hashes
            else "- 数据版本哈希未记录：本次运行的行情快照不可复现。"
        )

    lines += ["", "## 二、决策复盘", ""]
    if not decisions:
        lines.append(
            "- 无决策样本：可能是无候选，也可能是决策未落账；需结合运行记录判断。"
        )
    else:
        lines.append(f"- 决策：{len(decisions)} 条（{_counts(decisions, 'outcome')}）。")
        lines.append(f"- 动作分布：{_counts(decisions, 'action')}。")
        rejected = [row for row in decisions if row["outcome"].lower() in {"rejected", "reject"}]
        if rejected:
            lines.append(f"- 拒绝原因：{_counts(rejected, 'reason_code')}。")
        evidenced = 0
        for row in decisions:
            try:
                evidenced += bool(json.loads(row["evidence_json"]))
            except (TypeError, json.JSONDecodeError):
                pass
        lines.append(f"- 价格行为证据覆盖：{evidenced}/{len(decisions)}。")

    lines += ["", "## 三、执行复盘", ""]
    if not orders:
        lines.append("- 无订单样本：不能据此认定当天没有下单。")
    else:
        lines.append(f"- 订单事件：{len(orders)} 条（{_counts(orders, 'status')}）。")
    if not fills:
        lines.append("- 无成交样本：无法评估成交数量、价格与滑点。")
    else:
        lines.append(
            f"- 成交：{len(fills)} 笔，共 {sum(row['quantity'] for row in fills)} 股。"
        )
        slips = [float(row["slippage_bps"]) for row in fills if row["slippage_bps"] is not None]
        if slips:
            lines.append(
                "- 不利滑点："
                f"中位 {median(slips):+.1f}bp，均值 {mean(slips):+.1f}bp，"
                f"P90 {_percentile_90(slips):+.1f}bp。"
            )
        else:
            lines.append("- 滑点样本缺失：成交未记录参考价或滑点。")
    if not reconciliations:
        lines.append("- 无对账样本：仓位、订单和资金的一致性尚未得到证明。")
    else:
        lines.append(
            f"- 对账：{len(reconciliations)} 条（{_counts(reconciliations, 'status')}）。"
        )
        mismatches = [
            row
            for row in reconciliations
            if row["status"].lower() not in {"matched", "match", "ok", "一致"}
        ]
        if mismatches:
            lines.append(f"- 需处理的非一致项：{len(mismatches)} 条。")

    lines += ["", "## 四、交易与账户复盘", ""]
    if not position_events:
        lines.append("- 无持仓事件样本：无法重建当日建仓、加仓、止损移动或平仓路径。")
    else:
        lines.append(
            f"- 持仓事件：{len(position_events)} 条（"
            f"{_counts(position_events, 'event_type')}）。"
        )
    if not equity:
        lines.append("- 无权益快照样本：无法确认收盘权益、现金和持仓数量。")
    else:
        latest = equity[-1]
        lines.append(
            f"- 最新权益：${latest['equity']:,.2f}；现金 ${latest['cash']:,.2f}；"
            f"购买力 ${latest['buying_power']:,.2f}；持仓 {latest['position_count']} 个。"
        )
        if len(equity) >= 2 and equity[0]["equity"]:
            change = latest["equity"] / equity[0]["equity"] - 1
            lines.append(f"- 当日账本内权益变化：{change:+.2%}。")
        else:
            lines.append("- 权益变化样本不足 2 个，仅展示最新快照。")

    lines += [
        "",
        "## 复盘结论边界",
        "",
        "- 本报告只陈述已落账事实；缺记录不等于无事件。",
        "- 单日结果不足以支持策略调参，参数调整应进入独立的预注册实验流程。",
    ]
    return "\n".join(lines) + "\n"


def write_daily_review(
    journal: ReviewJournal | str | Path,
    trading_date: date | datetime | str,
    output_path: str | Path,
) -> Path:
    """生成报告并写入 UTF-8 Markdown 文件，返回最终路径。"""
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_daily_review(journal, trading_date), encoding="utf-8")
    return output
