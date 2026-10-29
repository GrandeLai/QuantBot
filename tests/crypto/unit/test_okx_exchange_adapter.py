"""Unit tests for the OKX exchange adapter."""

from __future__ import annotations

from decimal import Decimal

from quantbot.crypto.runtime.exchanges.okx import OKXExchange


class FakeOKXClient:
    """Fake OKX REST client with deterministic payloads."""

    def ping(self) -> bool:
        return True

    def list_symbols(self) -> list[str]:
        return ["BTC-USDT", "ETH-USDT"]

    def get_ticker(self, inst_id: str) -> dict[str, str]:
        assert inst_id == "BTC-USDT"
        return {
            "instId": "BTC-USDT",
            "bidPx": "50000.1",
            "askPx": "50010.2",
            "last": "50005.3",
            "volCcy24h": "1234.5",
            "ts": "1778308800000",
        }

    def get_orderbook(self, inst_id: str, depth: int = 5) -> dict[str, object]:
        assert inst_id == "BTC-USDT"
        assert depth == 5
        return {
            "bids": [["50000.1", "1.2"], ["49999.0", "2.3"]],
            "asks": [["50010.2", "1.1"], ["50011.0", "2.4"]],
            "ts": "1778308800000",
        }

    def get_account_balances(self) -> list[dict[str, str]]:
        return [
            {"asset": "USDT", "free": "1000.5", "locked": "10", "total": "1010.5"},
            {"asset": "BTC", "free": "0.25", "locked": "0.01", "total": "0.26"},
        ]

    def get_funding_rates(self) -> list[dict[str, str]]:
        return [
            {"instId": "BTC-USDT-SWAP", "fundingRate": "0.0005", "nextFundingTime": "1778337600000"},
        ]

    def get_spot_perp_quote(self, inst_id: str) -> dict[str, str]:
        assert inst_id == "BTC-USDT"
        return {
            "instId": "BTC-USDT",
            "spotBid": "50000",
            "spotAsk": "50010",
            "perpBid": "50050",
            "perpAsk": "50060",
            "fundingRate": "0.0005",
        }

    def list_instruments(self, inst_type: str) -> list[dict[str, str]]:
        if inst_type == "SPOT":
            return [
                {
                    "instId": "BTC-USDT",
                    "baseCcy": "BTC",
                    "quoteCcy": "USDT",
                    "minSz": "0.0001",
                    "tickSz": "0.1",
                }
            ]
        if inst_type == "SWAP":
            return [
                {
                    "instId": "BTC-USDT-SWAP",
                    "uly": "BTC-USDT",
                    "minSz": "0.01",
                    "tickSz": "0.1",
                    "ctVal": "0.01",
                }
            ]
        return [
            {
                "instId": "BTC-USDT-260626",
                "uly": "BTC-USDT",
                "minSz": "0.01",
                "tickSz": "0.1",
                "ctVal": "0.01",
                "expTime": "1782432000000",
            }
        ]

    def get_futures_basis_quote(self, inst_id: str) -> dict[str, str]:
        assert inst_id == "BTC-USDT"
        return {
            "instId": "BTC-USDT",
            "spotBid": "50000",
            "spotAsk": "50010",
            "perpBid": "50050",
            "perpAsk": "50060",
            "futuresBid": "50120",
            "futuresAsk": "50130",
            "futuresExpiry": "1782432000000",
            "fundingRate": "0.0005",
        }


def test_okx_exchange_maps_ticker_orderbook_and_balances_to_domain_models() -> None:
    exchange = OKXExchange(client=FakeOKXClient())

    assert exchange.name == "okx"
    assert exchange.ping() is True
    assert exchange.list_symbols() == ["BTC/USDT", "ETH/USDT"]

    ticker = exchange.get_ticker("BTC/USDT")
    assert ticker.exchange == "okx"
    assert ticker.symbol == "BTC/USDT"
    assert ticker.bid == Decimal("50000.1")
    assert ticker.ask == Decimal("50010.2")
    assert ticker.last == Decimal("50005.3")
    assert ticker.volume == Decimal("1234.5")

    orderbook = exchange.get_orderbook("BTC/USDT")
    assert orderbook.best_bid.price == Decimal("50000.1")
    assert orderbook.best_bid.amount == Decimal("1.2")
    assert orderbook.best_ask.price == Decimal("50010.2")
    assert orderbook.best_ask.amount == Decimal("1.1")

    balances = exchange.get_balances()
    assert balances.exchange == "okx"
    assert balances.balance_for("USDT").free == Decimal("1000.5")
    assert balances.balance_for("BTC").locked == Decimal("0.01")


def test_okx_exchange_maps_funding_and_spot_perp_quotes() -> None:
    exchange = OKXExchange(client=FakeOKXClient())

    funding = exchange.get_funding_rates()[0]
    assert funding.exchange == "okx"
    assert funding.symbol == "BTC/USDT"
    assert funding.funding_rate == Decimal("0.0005")

    quote = exchange.get_spot_perp_quote("BTC/USDT")
    assert quote.exchange == "okx"
    assert quote.symbol == "BTC/USDT"
    assert quote.spot_ask == Decimal("50010")
    assert quote.perp_bid == Decimal("50050")
    assert quote.funding_rate == Decimal("0.0005")


def test_okx_exchange_maps_instruments_and_futures_basis_quotes() -> None:
    exchange = OKXExchange(client=FakeOKXClient())

    instruments = exchange.list_instruments()
    assert {item.market_type for item in instruments} == {"spot", "swap", "futures"}
    assert instruments[0].symbol == "BTC/USDT"
    assert instruments[0].base_asset == "BTC"
    assert instruments[0].quote_asset == "USDT"
    assert instruments[0].min_size == Decimal("0.0001")

    futures = exchange.get_futures_basis_quote("BTC/USDT")
    assert futures.symbol == "BTC/USDT"
    assert futures.futures_bid == Decimal("50120")
    assert futures.funding_rate == Decimal("0.0005")
    assert futures.futures_expiry is not None
