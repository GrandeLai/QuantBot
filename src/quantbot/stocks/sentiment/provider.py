"""新闻情绪分析 — RSS 获取 + VADER 评分."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from loguru import logger
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer


@dataclass
class SentimentScore:
    """VADER 情绪评分结果."""

    compound: float
    positive: float
    negative: float
    neutral: float

    @property
    def is_positive(self) -> bool:
        """复合得分 >= 0.05 为正面."""
        return self.compound >= 0.05

    @property
    def is_negative(self) -> bool:
        """复合得分 <= -0.05 为负面."""
        return self.compound <= -0.05

    @property
    def label(self) -> str:
        """情绪标签."""
        if self.is_positive:
            return "bullish"
        if self.is_negative:
            return "bearish"
        return "neutral"


@dataclass
class NewsItem:
    """单条新闻条目."""

    title: str
    url: str
    published: str
    source: str = ""
    description: str = ""
    sentiment: SentimentScore | None = field(default=None)


_DEFAULT_RSS_URLS = [
    "https://feeds.finance.yahoo.com/rss/2.0/headline?s={symbol}&region=US&lang=en-US",
]


def _extract_source(item_el: ElementTree.Element, url_str: str) -> str:
    """从 RSS 条目提取新闻来源名称."""
    src_el = item_el.find("source")
    if src_el is not None and src_el.text:
        return src_el.text.strip()
    # 尝试从 URL 域名推断
    try:
        hostname = urlparse(url_str).hostname or ""
        parts = hostname.replace("www.", "").split(".")
        return parts[0].capitalize() if parts else "Yahoo Finance"
    except Exception:
        return "Yahoo Finance"


def _extract_description(item_el: ElementTree.Element) -> str:
    """从 RSS 条目提取摘要/描述文本."""
    desc_el = item_el.find("description")
    if desc_el is None or not desc_el.text:
        return ""
    # 去除简单 HTML 标签
    text = desc_el.text
    import re
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()[:200]  # 截取前 200 字符


class NewsSentimentProvider:
    """从 RSS 获取新闻并用 VADER 计算情绪得分."""

    def __init__(self) -> None:
        self._analyzer = SentimentIntensityAnalyzer()

    def score_text(self, text: str) -> SentimentScore:
        """对文本进行 VADER 情绪评分."""
        scores = self._analyzer.polarity_scores(text)
        return SentimentScore(
            compound=scores["compound"],
            positive=scores["pos"],
            negative=scores["neg"],
            neutral=scores["neu"],
        )

    def score_impact(self, compound: float) -> str:
        """根据复合得分绝对值估算新闻影响程度."""
        abs_c = abs(compound)
        if abs_c >= 0.5:
            return "high"
        if abs_c >= 0.2:
            return "medium"
        return "low"

    def fetch_news(self, symbol: str, max_items: int = 10) -> list[NewsItem]:
        """从 RSS 源获取新闻并评分."""
        url = _DEFAULT_RSS_URLS[0].format(symbol=symbol)

        try:
            resp = httpx.get(url, timeout=10.0)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.text)
        except Exception as e:
            logger.warning(f"[Sentiment] RSS 获取失败: {e}")
            return []

        channel = root.find("channel")
        if channel is None:
            return []

        items: list[NewsItem] = []
        for item_el in channel.findall("item")[:max_items]:
            title_el = item_el.find("title")
            link_el = item_el.find("link")
            pub_el = item_el.find("pubDate")
            if title_el is None or title_el.text is None:
                continue
            title = title_el.text
            url_str = link_el.text if link_el is not None and link_el.text else ""
            published = pub_el.text if pub_el is not None and pub_el.text else ""
            source = _extract_source(item_el, url_str)
            description = _extract_description(item_el)
            sentiment = self.score_text(title)
            items.append(NewsItem(
                title=title,
                url=url_str,
                published=published,
                source=source,
                description=description,
                sentiment=sentiment,
            ))

        logger.info(f"[Sentiment] 获取 {symbol} 新闻 {len(items)} 条")
        return items

    def aggregate_sentiment(self, items: list[NewsItem]) -> dict[str, float | int]:
        """汇总多条新闻的情绪指标."""
        if not items:
            return {
                "count": 0,
                "avg_compound": 0.0,
                "positive_ratio": 0.0,
                "score": 50,
            }
        compounds = [i.sentiment.compound for i in items if i.sentiment is not None]
        positive_count = sum(1 for i in items if i.sentiment and i.sentiment.is_positive)
        avg_compound = round(sum(compounds) / len(compounds), 4) if compounds else 0.0
        # 将 compound [-1,1] 映射到 score [0,100]
        score = round((avg_compound + 1) / 2 * 100)
        return {
            "count": len(items),
            "avg_compound": avg_compound,
            "positive_ratio": round(positive_count / len(items), 4),
            "score": score,
        }
