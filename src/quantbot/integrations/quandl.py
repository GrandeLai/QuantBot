"""Quandl/Nasdaq Data Link backup client for short-interest signals."""

from datetime import date
from typing import List

from .http import UrllibJsonTransport


class QuandlShortInterestClient:
    def __init__(
        self,
        api_key: str,
        transport=None,
        base_url: str = "https://data.nasdaq.com/api/v3/datatables/FINRA/FNSQ",
    ):
        self.api_key = api_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url

    def fetch_short_interest(self, ticker: str) -> List[dict]:
        payload = self.transport.get_json(
            self.base_url,
            params={
                "ticker": ticker,
                "api_key": self.api_key,
            },
        )
        rows = []
        for item in payload.get("datatable", {}).get("data", []):
            symbol = item[0]
            value_date = date.fromisoformat(str(item[1])[:10])
            short_volume = float(item[2])
            total_volume = float(item[3])
            rows.append(
                {
                    "ticker": symbol,
                    "date": value_date,
                    "short_volume": short_volume,
                    "total_volume": total_volume,
                    "short_ratio": short_volume / total_volume if total_volume else 0.0,
                }
            )
        return rows
