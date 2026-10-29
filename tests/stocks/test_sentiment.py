"""新闻情绪分析测试 — T-4.3 验收."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from quantbot.stocks.sentiment.provider import NewsItem, NewsSentimentProvider, SentimentScore


class TestSentimentScore:
    def test_fields_accessible(self) -> None:
        score = SentimentScore(compound=0.5, positive=0.6, negative=0.1, neutral=0.3)
        assert score.compound == 0.5
        assert score.positive == 0.6

    def test_is_positive(self) -> None:
        pos = SentimentScore(compound=0.6, positive=0.7, negative=0.0, neutral=0.3)
        assert pos.is_positive is True
        neg = SentimentScore(compound=-0.6, positive=0.0, negative=0.7, neutral=0.3)
        assert neg.is_positive is False

    def test_is_negative(self) -> None:
        neg = SentimentScore(compound=-0.6, positive=0.0, negative=0.7, neutral=0.3)
        assert neg.is_negative is True


class TestNewsItem:
    def test_fields(self) -> None:
        item = NewsItem(title="AAPL surges", url="http://example.com", published="2024-01-01")
        assert item.title == "AAPL surges"
        assert item.sentiment is None

    def test_with_sentiment(self) -> None:
        score = SentimentScore(compound=0.8, positive=0.9, negative=0.0, neutral=0.1)
        item = NewsItem(title="Stock soars", url="http://ex.com", published="2024-01-01", sentiment=score)
        assert item.sentiment is not None
        assert item.sentiment.compound == 0.8


class TestNewsSentimentProvider:
    def test_score_text(self) -> None:
        provider = NewsSentimentProvider()
        score = provider.score_text("The stock market is doing great! Bullish outlook.")
        assert isinstance(score, SentimentScore)
        assert score.compound > 0

    def test_score_negative_text(self) -> None:
        provider = NewsSentimentProvider()
        score = provider.score_text("Market crash! Terrible losses and disaster.")
        assert score.compound < 0

    def test_score_neutral_text(self) -> None:
        provider = NewsSentimentProvider()
        score = provider.score_text("The stock closed at 100.")
        assert isinstance(score, SentimentScore)

    def test_fetch_news_mock(self) -> None:
        rss_content = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Apple stock rises on strong earnings</title>
      <link>http://example.com/1</link>
      <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Market faces headwinds from inflation</title>
      <link>http://example.com/2</link>
      <pubDate>Mon, 01 Jan 2024 11:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""
        mock_response = MagicMock()
        mock_response.text = rss_content
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.get", return_value=mock_response):
            provider = NewsSentimentProvider()
            items = provider.fetch_news("AAPL", max_items=5)

        assert len(items) == 2
        assert all(item.sentiment is not None for item in items)
        assert items[0].title == "Apple stock rises on strong earnings"

    def test_aggregate_sentiment_empty(self) -> None:
        provider = NewsSentimentProvider()
        result = provider.aggregate_sentiment([])
        assert result["count"] == 0
        assert result["avg_compound"] == 0.0
