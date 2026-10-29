"""Alpaca News API client."""

from datetime import date
from typing import Iterable, List

from .http import UrllibJsonTransport


class AlpacaNewsClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        transport=None,
        base_url: str = "https://data.alpaca.markets/v1beta1/news",
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url

    def search(
        self,
        symbols: Iterable[str],
        start: date,
        end: date,
        limit: int = 50,
    ) -> List[dict]:
        payload = self.transport.get_json(
            self.base_url,
            headers={
                "APCA-API-KEY-ID": self.api_key,
                "APCA-API-SECRET-KEY": self.secret_key,
            },
            params={
                "symbols": ",".join(symbols),
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": limit,
            },
        )
        rows = []
        for item in payload.get("news", []):
            symbols_value = item.get("symbols") or []
            ticker = symbols_value[0] if symbols_value else ""
            rows.append(
                {
                    "ticker": ticker,
                    "title": item.get("headline", ""),
                    "summary": item.get("summary", ""),
                    "url": item.get("url", ""),
                    "published_at": item.get("created_at", ""),
                    "symbols": list(symbols_value),
                }
            )
        return rows
