"""Yahoo Finance chart fallback client."""

from datetime import date, datetime, timezone
from typing import List

from .http import UrllibJsonTransport


class YFinanceClient:
    def __init__(self, transport=None, base_url: str = "https://query1.finance.yahoo.com/v8/finance/chart"):
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url.rstrip("/")

    def fetch_daily_bars(self, ticker: str, start: date, end: date) -> List[dict]:
        payload = self.transport.get_json(
            "%s/%s" % (self.base_url, ticker),
            params={
                "period1": _epoch_seconds(start),
                "period2": _epoch_seconds(end),
                "interval": "1d",
                "events": "history",
            },
        )
        result = payload.get("chart", {}).get("result", [{}])[0]
        timestamps = result.get("timestamp", [])
        quote = result.get("indicators", {}).get("quote", [{}])[0]
        adjclose = result.get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])
        rows = []
        for index, timestamp in enumerate(timestamps):
            rows.append(
                {
                    "ticker": ticker,
                    "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date(),
                    "open": quote.get("open", [None])[index],
                    "high": quote.get("high", [None])[index],
                    "low": quote.get("low", [None])[index],
                    "close": quote.get("close", [None])[index],
                    "volume": quote.get("volume", [None])[index],
                    "adjusted_close": adjclose[index] if index < len(adjclose) else quote.get("close", [None])[index],
                }
            )
        return rows


def _epoch_seconds(value: date) -> int:
    return int(datetime(value.year, value.month, value.day, tzinfo=timezone.utc).timestamp())
