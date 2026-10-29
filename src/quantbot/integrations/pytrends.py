"""PyTrends adapter for retail search attention signals."""

from datetime import date
from typing import List


class PyTrendsClient:
    def __init__(self, pytrends_client):
        self.pytrends_client = pytrends_client

    def fetch_interest_over_time(
        self,
        keyword: str,
        start: date,
        end: date,
    ) -> List[dict]:
        self.pytrends_client.build_payload(
            [keyword],
            timeframe="%s %s" % (start.isoformat(), end.isoformat()),
        )
        frame = self.pytrends_client.interest_over_time()
        records = _records(frame)
        rows = []
        for item in records:
            if item.get("isPartial"):
                continue
            rows.append(
                {
                    "date": _parse_date(item["date"]),
                    "keyword": keyword,
                    "search_volume": float(item.get(keyword, 0.0)),
                }
            )
        return rows


def _records(frame):
    if isinstance(frame, list):
        return frame
    if hasattr(frame, "reset_index"):
        frame = frame.reset_index()
    if hasattr(frame, "to_dict"):
        return frame.to_dict("records")
    raise TypeError("PyTrends interest_over_time result must be list-like or DataFrame-like")


def _parse_date(value):
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
