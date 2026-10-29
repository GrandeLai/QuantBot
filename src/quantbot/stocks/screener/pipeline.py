"""4-phase LLM analysis pipeline for deep per-stock analysis."""
from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from loguru import logger

from quantbot.models import ChatMessage, get_default_agent


@dataclass
class Decision:
    recommendation: str   # "BUY" | "HOLD" | "SELL"
    conviction: float     # 0-100
    buy_price: float | None = None
    stop_loss: float | None = None
    summary: str = ""
    checklist: list[str] = field(default_factory=list)


@dataclass
class PhaseUpdate:
    phase: int           # 1-4
    title: str = ""
    token: str = ""
    start: bool = False
    done: bool = False
    decision: Decision | None = None


PHASE_PROMPTS: list[tuple[str, str]] = [
    # Phase 1: Market data & price action
    (
        "市场数据分析",
        "分析股票 {symbol} 的价格走势和市场数据。评估其近期K线形态、价格动量和趋势强度。"
        "补充数据：{score_summary}。请简洁分析，不超过200字。",
    ),
    # Phase 2: Technical indicator synthesis
    (
        "技术指标综合",
        "基于前述市场数据，深入分析 {symbol} 的技术指标信号。"
        "重点分析MACD、RSI、均线系统的综合信号，判断当前技术面强弱。不超过200字。",
    ),
    # Phase 3: Intelligence gathering
    (
        "情报收集整合",
        "综合分析 {symbol} 的基本面和市场情绪。"
        "基本面数据：{fundamental_summary}。市场情绪：{sentiment_summary}。"
        "给出综合判断，不超过200字。",
    ),
    # Phase 4: Investment decision
    (
        "投资决策建议",
        "基于以上三个阶段的分析，给出 {symbol} 的最终投资建议。"
        "必须按以下格式输出（每项单独一行）：\n"
        "RECOMMENDATION: BUY|HOLD|SELL\n"
        "CONVICTION: <0-100的整数>\n"
        "BUY_PRICE: <建议买入价，仅BUY时填写>\n"
        "STOP_LOSS: <止损价>\n"
        "SUMMARY: <一句话总结>\n"
        "CHECKLIST: <操作要点1>|<操作要点2>|<操作要点3>",
    ),
]


class AnalysisPipeline:
    """Run 4-phase sequential LLM analysis, yielding PhaseUpdate events as SSE tokens."""

    async def run(
        self,
        symbol: str,
        context: dict[str, str],
    ) -> AsyncIterator[PhaseUpdate]:
        agent = get_default_agent()
        history: list[ChatMessage] = []
        score_summary = context.get("score_summary", "无评分数据")
        fundamental_summary = context.get("fundamental_summary", "无基本面数据")
        sentiment_summary = context.get("sentiment_summary", "情绪中性")

        for phase_idx, (title, prompt_tpl) in enumerate(PHASE_PROMPTS):
            phase = phase_idx + 1
            prompt = prompt_tpl.format(
                symbol=symbol,
                score_summary=score_summary,
                fundamental_summary=fundamental_summary,
                sentiment_summary=sentiment_summary,
            )
            history.append(ChatMessage(role="user", content=prompt))

            yield PhaseUpdate(phase=phase, title=title, start=True)

            accumulated = ""
            try:
                async for token in agent.stream(list(history)):
                    accumulated += token
                    yield PhaseUpdate(phase=phase, token=token)
            except Exception as exc:
                logger.error(f"[Pipeline] phase {phase} error: {exc}")
                yield PhaseUpdate(phase=phase, token=f"[分析失败] {exc}")

            history.append(ChatMessage(role="assistant", content=accumulated))

            decision: Decision | None = None
            if phase == 4:
                decision = self._parse_decision(accumulated)

            yield PhaseUpdate(phase=phase, done=True, decision=decision)

    def _parse_decision(self, text: str) -> Decision:
        def extract(pattern: str) -> str:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(1).strip() if m else ""

        rec_raw = extract(r"RECOMMENDATION:\s*(\w+)")
        rec = rec_raw.upper() if rec_raw.upper() in ("BUY", "HOLD", "SELL") else "HOLD"
        conviction_str = extract(r"CONVICTION:\s*([\d.]+)")
        buy_price_str = extract(r"BUY_PRICE:\s*([\d.]+)")
        stop_loss_str = extract(r"STOP_LOSS:\s*([\d.]+)")
        summary = extract(r"SUMMARY:\s*(.+)")
        checklist_raw = extract(r"CHECKLIST:\s*(.+)")
        checklist = [c.strip() for c in checklist_raw.split("|") if c.strip()]

        return Decision(
            recommendation=rec,
            conviction=float(conviction_str) if conviction_str else 50.0,
            buy_price=float(buy_price_str) if buy_price_str else None,
            stop_loss=float(stop_loss_str) if stop_loss_str else None,
            summary=summary,
            checklist=checklist,
        )
