"""CBOE volatility index data client."""

import csv
from datetime import date
from io import StringIO
from typing import List


class CBOEVixClient:
    def __init__(
        self,
        transport,
        url: str = "https://cdn.cboe.com/publish/scheduledtask/mktdata/datahouse/vixcurrent.csv",
    ):
        self.transport = transport
        self.url = url

    def fetch_history(self) -> List[dict]:
        text = self.transport.get_text(self.url)
        rows = []
        for item in csv.DictReader(StringIO(text)):
            value = item.get("VIX") or item.get("vix") or item.get("Close") or item.get("close")
            raw_date = item.get("DATE") or item.get("Date") or item.get("date")
            if not raw_date or value in {None, ""}:
                continue
            rows.append(
                {
                    "date": date.fromisoformat(raw_date[:10]),
                    "series": "VIX",
                    "value": float(value),
                }
            )
        return rows
