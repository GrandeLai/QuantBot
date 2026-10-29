"""Nasdaq Data Link client for analyst estimate revisions."""

from datetime import date
from typing import List

from .http import UrllibJsonTransport


class NasdaqDataLinkClient:
    def __init__(self, api_key: str, transport=None, base_url: str = "https://data.nasdaq.com/api/v3/datatables"):
        self.api_key = api_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url.rstrip("/")

    def fetch_eps_revisions(self, ticker: str) -> List[dict]:
        payload = self.transport.get_json(
            "%s/ZACKS/EE.json" % self.base_url,
            params={"ticker": ticker, "api_key": self.api_key},
        )
        rows = []
        for item in payload.get("datatable", {}).get("data", []):
            symbol, as_of, current_eps, previous_eps = item[:4]
            rows.append(
                {
                    "ticker": symbol,
                    "date": date.fromisoformat(as_of),
                    "current_eps": current_eps,
                    "previous_eps": previous_eps,
                    "revision": current_eps / previous_eps - 1 if previous_eps else 0.0,
                }
            )
        return rows
