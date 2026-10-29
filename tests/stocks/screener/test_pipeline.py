"""Tests for the 4-phase LLM analysis pipeline."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from quantbot.stocks.screener.pipeline import AnalysisPipeline


@pytest.mark.asyncio
async def test_pipeline_yields_phase_updates() -> None:
    async def fake_stream(history: list) -> object:
        yield "分析结果"
        yield " 完成"

    with patch("quantbot.stocks.screener.pipeline.get_default_agent") as mock_agent:
        agent = AsyncMock()
        agent.stream = fake_stream
        mock_agent.return_value = agent

        pipeline = AnalysisPipeline()
        updates = []
        async for u in pipeline.run("000001", {}):
            updates.append(u)

    starts = [u for u in updates if u.start]
    dones = [u for u in updates if u.done]
    assert len(starts) == 4
    assert len(dones) == 4


@pytest.mark.asyncio
async def test_pipeline_emits_decision_in_phase4() -> None:
    async def fake_stream(history: list) -> object:
        yield (
            "RECOMMENDATION: BUY\n"
            "CONVICTION: 75\n"
            "BUY_PRICE: 12.50\n"
            "STOP_LOSS: 11.00\n"
            "SUMMARY: 强势上涨\n"
            "CHECKLIST: 确认量能|设置止损|分批建仓"
        )

    with patch("quantbot.stocks.screener.pipeline.get_default_agent") as mock_agent:
        agent = AsyncMock()
        agent.stream = fake_stream
        mock_agent.return_value = agent

        pipeline = AnalysisPipeline()
        decision_updates = []
        async for u in pipeline.run("000001", {}):
            if u.decision:
                decision_updates.append(u)

    assert len(decision_updates) == 1
    d = decision_updates[0].decision
    assert d is not None
    assert d.recommendation == "BUY"
    assert d.conviction == 75.0
    assert d.buy_price == 12.50
    assert d.stop_loss == 11.00
    assert d.summary == "强势上涨"
    assert len(d.checklist) == 3
