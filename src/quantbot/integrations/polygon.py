"""Polygon.io market-data client."""

from datetime import date, datetime, timezone
from typing import List

from .http import UrllibJsonTransport


class PolygonClient:
    def __init__(self, api_key: str, transport=None, base_url: str = "https://api.polygon.io"):
        self.api_key = api_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url.rstrip("/")

    def fetch_minute_bars(self, ticker: str, start: date, end: date) -> List[dict]:
        url = "%s/v2/aggs/ticker/%s/range/1/minute/%s/%s" % (
            self.base_url,
            ticker,
            start.isoformat(),
            end.isoformat(),
        )
        payload = self.transport.get_json(url, params={"apiKey": self.api_key, "adjusted": "true", "sort": "asc"})
        rows = []
        for item in payload.get("results", []):
            rows.append(
                {
                    "ticker": ticker,
                    "timestamp": datetime.fromtimestamp(item["t"] / 1000, tz=timezone.utc),
                    "open": item.get("o"),
                    "high": item.get("h"),
                    "low": item.get("l"),
                    "close": item.get("c"),
                    "volume": item.get("v"),
                    "vwap": item.get("vw"),
                }
            )
        return rows

    def fetch_options_snapshot(self, underlying: str) -> List[dict]:
        url = "%s/v3/snapshot/options/%s" % (self.base_url, underlying)
        payload = self.transport.get_json(url, params={"apiKey": self.api_key})
        rows = []
        for item in payload.get("results", []):
            details = item.get("details", {})
            rows.append(
                {
                    "underlying": underlying,
                    "contract": details.get("ticker"),
                    "contract_type": details.get("contract_type"),
                    "implied_volatility": item.get("implied_volatility"),
                }
            )
        return rows
