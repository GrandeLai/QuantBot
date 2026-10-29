"""FRED API client."""

from datetime import date
from typing import List

from .http import UrllibJsonTransport


class FredClient:
    def __init__(self, api_key: str, transport=None, base_url: str = "https://api.stlouisfed.org/fred/series/observations"):
        self.api_key = api_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url

    def fetch_series(self, series_id: str, start: date, end: date) -> List[dict]:
        payload = self.transport.get_json(
            self.base_url,
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
            },
        )
        rows = []
        for observation in payload.get("observations", []):
            value = observation.get("value")
            if value in {None, "."}:
                continue
            rows.append(
                {
                    "date": date.fromisoformat(observation["date"]),
                    "series": series_id,
                    "value": float(value),
                }
            )
        return rows
