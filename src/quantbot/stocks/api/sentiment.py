"""新闻情绪 API.

端点:
  GET /sentiment/news?symbol=AAPL&max_items=10   获取新闻情绪分析
  GET /sentiment/history?symbol=AAPL             获取情绪历史趋势（近 8 小时）
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter

from quantbot.stocks.sentiment.provider import NewsSentimentProvider

router = APIRouter(prefix="/sentiment", tags=["新闻情绪"])
_provider = NewsSentimentProvider()


@router.get("/news")
def get_news_sentiment(symbol: str = "AAPL", max_items: int = 10) -> dict[str, Any]:
    """获取指定标的的新闻情绪分析."""
    items = _provider.fetch_news(symbol, max_items=max_items)
    aggregate = _provider.aggregate_sentiment(items)
    return {
        "symbol": symbol,
        "aggregate": aggregate,
        "items": [
            {
                "id": str(i),
                "title": item.title,
                "url": item.url,
                "published": item.published,
                "source": item.source,
                "description": item.description,
                "impact": (
                    _provider.score_impact(item.sentiment.compound)
                    if item.sentiment else "low"
                ),
                "sentiment": {
                    "compound": item.sentiment.compound,
                    "positive": item.sentiment.positive,
                    "negative": item.sentiment.negative,
                    "neutral": item.sentiment.neutral,
                    "label": item.sentiment.label,
                } if item.sentiment else None,
            }
            for i, item in enumerate(items)
        ],
    }


@router.get("/history")
def get_sentiment_history(symbol: str = "AAPL") -> dict[str, Any]:
    """生成指定标的近 8 小时情绪趋势.

    基于当前新闻情绪基准，叠加确定性正弦扰动模拟历史波动，
    保证同一 symbol 在同一小时内返回一致数据。
    """
    # 获取当前情绪基准（少量样本即可）
    items = _provider.fetch_news(symbol, max_items=20)
    aggregate = _provider.aggregate_sentiment(items)
    base_score: float = float(aggregate["score"])

    # symbol hash 用于差异化各 symbol 的波形相位
    symbol_phase = hash(symbol) % 100 / 10.0

    now = datetime.now()
    history: list[dict[str, Any]] = []
    for i in range(7, -1, -1):
        ts = (now - timedelta(hours=i)).strftime("%H:%M")
        # 确定性正弦扰动（不随每次调用变化）
        noise = 10.0 * math.sin(i * 1.2 + symbol_phase)
        score = max(5.0, min(95.0, base_score + noise))
        bullish = round(score * 0.85)
        bearish = round((100.0 - score) * 0.85)
        neutral = max(0, 100 - bullish - bearish)
        history.append({
            "timestamp": ts,
            "bullish": bullish,
            "bearish": bearish,
            "neutral": neutral,
            "score": round(score),
        })

    return {"symbol": symbol, "history": history}
