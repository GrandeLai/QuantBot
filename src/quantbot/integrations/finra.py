"""FINRA short-volume client."""

from datetime import date
from typing import List

from .http import UrllibJsonTransport


class FinraShortVolumeClient:
    def __init__(self, transport=None, base_url: str = "https://cdn.finra.org/equity/regsho/daily"):
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url.rstrip("/")

    def fetch_short_volume(self, trading_date: date) -> List[dict]:
        filename = "CNMSshvol%s.txt" % trading_date.strftime("%Y%m%d")
        text = self.transport.get_text("%s/%s" % (self.base_url, filename))
        rows = []
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return rows
        headers = lines[0].split("|")
        for line in lines[1:]:
            values = dict(zip(headers, line.split("|")))
            total = float(values.get("TotalVolume", 0) or 0)
            short = float(values.get("ShortVolume", 0) or 0)
            rows.append(
                {
                    "date": trading_date,
                    "ticker": values.get("Symbol"),
                    "short_volume": short,
                    "total_volume": total,
                    "short_ratio": short / total if total else 0.0,
                }
            )
        return rows
