"""Unit tests for the optional CCXT exchange adapter."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.config.schema import ExchangeConfig
from quantbot.crypto.runtime.arbitrage.scanner import ArbitrageScanner
from quantbot.crypto.runtime.exchanges.ccxt import CCXTExchange
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.exceptions import ExchangeError


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_ccxt_exchange_maps_market_account_and_derivative_models() -> None:
    exchange = CCXTExchange("binance", client=FakeCCXTClient("binance"))

    assert exchange.ping() is True
    assert exchange.list_symbols() == ["BTC/USDT"]

    ticker = exchange.get_ticker("BTC/USDT")
    assert ticker.exchange == "binance"
    assert ticker.bid == Decimal("50000")
    assert ticker.ask == Decimal("50010")
    assert ticker.volume == Decimal("123456")

    orderbook = exchange.get_orderbook("BTC/USDT")
    assert orderbook.best_bid.price == Decimal("50000")
    assert orderbook.depth_notional("ask", levels=2) > Decimal("100000")

    balances = exchange.get_balances()
    assert balances.balance_for("USDT").free == Decimal("2000")
    assert balances.balance_for("BTC").locked == Decimal("0.01")

    funding = exchange.get_funding_rates()[0]
    assert funding.symbol == "BTC/USDT"
    assert funding.funding_rate == Decimal("0.0004")

    spot_perp = exchange.get_spot_perp_quote("BTC/USDT")
    assert spot_perp.perp_bid == Decimal("50200")
    assert spot_perp.funding_rate == Decimal("0.0004")

    instruments = exchange.list_instruments()
    assert {item.market_type for item in instruments} == {"spot", "swap", "futures"}
    assert instruments[0].min_size == Decimal("0.0001")

    basis = exchange.get_futures_basis_quote("BTC/USDT")
    assert basis.futures_bid == Decimal("50500")
    assert basis.futures_expiry is not None


def test_ccxt_exchange_raises_clear_error_when_futures_market_is_missing() -> None:
    exchange = CCXTExchange("binance", client=FakeCCXTClient("binance", include_futures=False))

    with pytest.raises(ExchangeError, match="no futures market"):
        exchange.get_futures_basis_quote("BTC/USDT")


def test_cross_exchange_scanner_uses_enabled_ccxt_exchanges(monkeypatch: pytest.MonkeyPatch) -> None:
    import quantbot.crypto.runtime.exchanges.ccxt as ccxt_module

    def fake_create(exchange_id: str, *, sandbox: bool):
        assert sandbox is True
        return FakeCCXTClient(exchange_id)

    monkeypatch.setattr(ccxt_module, "create_ccxt_client", fake_create)
    settings = load_settings(EXAMPLE_CONFIG)
    settings.exchanges["mock"] = ExchangeConfig(enabled=False, sandbox=True, adapter="mock")
    settings.exchanges["mock_alt"] = ExchangeConfig(enabled=False, sandbox=True, adapter="mock")
    settings.exchanges["binance"] = ExchangeConfig(enabled=True, sandbox=True, adapter="ccxt")
    settings.exchanges["bybit"] = ExchangeConfig(enabled=True, sandbox=True, adapter="ccxt")
    settings.arbitrage.trade_size_usdt = Decimal("100")

    opportunities = ArbitrageScanner(settings, ExchangeFactory(settings)).scan("cross-exchange", symbol="BTC/USDT")

    assert opportunities
    best = opportunities[0]
    assert best.opportunity_id == "cross-exchange-binance-bybit-btc-usdt"
    assert best.buy_exchange == "binance"
    assert best.sell_exchange == "bybit"
    assert best.net_profit > Decimal("0")


class FakeCCXTClient:
    """Small fake CCXT client for offline adapter tests."""

    has = {"fetchFundingRate": True, "fetchFundingRates": True}

    def __init__(self, exchange_id: str, *, include_futures: bool = True) -> None:
        self.exchange_id = exchange_id
        self.markets = _markets(include_futures=include_futures)

    def load_markets(self) -> dict[str, dict[str, object]]:
        return self.markets

    def fetch_ticker(self, symbol: str) -> dict[str, object]:
        prices = {
            "binance": {
                "BTC/USDT": ("50000", "50010", "50005"),
                "BTC/USDT:USDT": ("50200", "50220", "50210"),
                "BTC/USDT-260626": ("50500", "50530", "50510"),
            },
            "bybit": {
                "BTC/USDT": ("50280", "50290", "50285"),
                "BTC/USDT:USDT": ("50400", "50420", "50410"),
                "BTC/USDT-260626": ("50600", "50630", "50610"),
            },
        }[self.exchange_id]
        bid, ask, last = prices[symbol]
        return {
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "last": last,
            "quoteVolume": "123456",
            "timestamp": 1778308800000,
        }

    def fetch_order_book(self, symbol: str, limit: int = 5) -> dict[str, object]:
        ticker = self.fetch_ticker(symbol)
        bid = Decimal(str(ticker["bid"]))
        ask = Decimal(str(ticker["ask"]))
        return {
            "bids": [[str(bid), "3"], [str(bid - Decimal("5")), "3"]],
            "asks": [[str(ask), "3"], [str(ask + Decimal("5")), "3"]],
            "timestamp": 1778308800000,
        }

    def fetch_balance(self) -> dict[str, object]:
        return {
            "free": {"USDT": "2000", "BTC": "0.5"},
            "used": {"USDT": "50", "BTC": "0.01"},
            "total": {"USDT": "2050", "BTC": "0.51"},
        }

    def fetch_funding_rate(self, symbol: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "fundingRate": "0.0004",
            "nextFundingTimestamp": 1778337600000,
        }

    def fetch_funding_rates(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        return {symbol: self.fetch_funding_rate(symbol) for symbol in symbols}


def _markets(*, include_futures: bool) -> dict[str, dict[str, object]]:
    markets: dict[str, dict[str, object]] = {
        "BTC/USDT": {
            "symbol": "BTC/USDT",
            "type": "spot",
            "spot": True,
            "base": "BTC",
            "quote": "USDT",
            "limits": {"amount": {"min": "0.0001"}},
            "precision": {"price": "0.1"},
        },
        "BTC/USDT:USDT": {
            "symbol": "BTC/USDT:USDT",
            "type": "swap",
            "swap": True,
            "base": "BTC",
            "quote": "USDT",
            "limits": {"amount": {"min": "0.001"}},
            "precision": {"price": "0.1"},
            "contractSize": "0.001",
        },
    }
    if include_futures:
        markets["BTC/USDT-260626"] = {
            "symbol": "BTC/USDT-260626",
            "type": "future",
            "future": True,
            "base": "BTC",
            "quote": "USDT",
            "limits": {"amount": {"min": "0.001"}},
            "precision": {"price": "0.1"},
            "contractSize": "0.001",
            "expiry": 1782432000000,
        }
    return markets
