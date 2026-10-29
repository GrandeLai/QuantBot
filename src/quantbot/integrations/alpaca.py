"""Alpaca market-data and trading clients."""

from datetime import date, datetime, timezone
from typing import Iterable, List

from .http import UrllibJsonTransport
from ..trading import BrokerAccountSnapshot, broker_account_snapshot_from_payload


class AlpacaDataClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        transport=None,
        base_url: str = "https://data.alpaca.markets/v2/stocks/bars",
        feed: str = "",
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url
        self.feed = feed

    def fetch_daily_bars(self, symbols: Iterable[str], start: date, end: date) -> List[dict]:
        rows = []
        for row in self._fetch_bars(symbols=symbols, start=start, end=end, timeframe="1Day"):
            rows.append(
                {
                    "ticker": row["ticker"],
                    "date": date.fromisoformat(row["timestamp"][:10]),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "adjusted_close": row.get("close"),
                    "volume": row.get("volume"),
                    "vwap": row.get("vwap"),
                }
            )
        return rows

    def fetch_minute_bars(self, symbols: Iterable[str], start: date, end: date) -> List[dict]:
        rows = []
        for row in self._fetch_bars(symbols=symbols, start=start, end=end, timeframe="1Min"):
            rows.append(
                {
                    "ticker": row["ticker"],
                    "timestamp": datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")),
                    "open": row.get("open"),
                    "high": row.get("high"),
                    "low": row.get("low"),
                    "close": row.get("close"),
                    "volume": row.get("volume"),
                    "vwap": row.get("vwap"),
                }
            )
        return rows

    def _fetch_bars(self, symbols: Iterable[str], start: date, end: date, timeframe: str) -> List[dict]:
        rows = []
        page_token = None
        normalized_symbols = [str(symbol).upper() for symbol in symbols]
        while True:
            params = {
                "symbols": ",".join(normalized_symbols),
                "timeframe": timeframe,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": "all",
                "limit": 10_000,
            }
            if self.feed:
                params["feed"] = self.feed
            if page_token:
                params["page_token"] = page_token
            payload = self.transport.get_json(
                self.base_url,
                headers={
                    "APCA-API-KEY-ID": self.api_key,
                    "APCA-API-SECRET-KEY": self.secret_key,
                },
                params=params,
            )
            for ticker, bars in payload.get("bars", {}).items():
                for bar in bars:
                    rows.append(
                        {
                            "ticker": ticker,
                            "timestamp": bar["t"],
                            "open": bar.get("o"),
                            "high": bar.get("h"),
                            "low": bar.get("l"),
                            "close": bar.get("c"),
                            "volume": bar.get("v"),
                            "vwap": bar.get("vw"),
                        }
                    )
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return rows


class AlpacaTradingClient:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        transport=None,
        base_url: str = "https://paper-api.alpaca.markets",
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.transport = transport or UrllibJsonTransport()
        self.base_url = base_url.rstrip("/")

    def fetch_account(self) -> dict:
        return self.transport.get_json(
            "%s/v2/account" % self.base_url,
            headers=self._headers(),
        )

    def fetch_portfolio_value(self) -> float:
        account = self.fetch_account()
        value = account.get("portfolio_value")
        if value is None:
            value = account.get("equity")
        if value is None:
            raise KeyError("portfolio_value")
        return float(value)

    def account_snapshot(self) -> BrokerAccountSnapshot:
        return broker_account_snapshot_from_payload(
            self.fetch_account(),
            broker="alpaca",
            observed_at=datetime.now(timezone.utc),
        )

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        return self.account_snapshot()

    def fetch_calendar(self, start: date, end: date) -> List[date]:
        payload = self.transport.get_json(
            "%s/v2/calendar" % self.base_url,
            headers=self._headers(),
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
        return sorted({date.fromisoformat(str(item["date"])) for item in payload})

    def submit_order(
        self,
        symbol: str,
        notional: float = None,
        qty: float = None,
        side: str = "buy",
        order_type: str = "market",
        time_in_force: str = "day",
    ) -> dict:
        payload = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "time_in_force": time_in_force,
        }
        if notional is not None:
            payload["notional"] = round(float(notional), 2)
        if qty is not None:
            payload["qty"] = float(qty)
        return self.transport.post_json(
            "%s/v2/orders" % self.base_url,
            headers=self._headers(),
            payload=payload,
        )

    def close_all_positions(self) -> dict:
        return self.transport.delete_json(
            "%s/v2/positions" % self.base_url,
            headers=self._headers(),
        )

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }
