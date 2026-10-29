"""Unit tests for exchange, market data, and account services."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.account.service import AccountService
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.base import Exchange
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.market_data.service import MarketDataService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_mock_exchange_implements_exchange_interface() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    exchange = ExchangeFactory(settings).get("mock")

    assert isinstance(exchange, Exchange)
    assert exchange.name == "mock"
    assert exchange.ping() is True
    assert "BTC/USDT" in exchange.list_symbols()
    assert {item.market_type for item in exchange.list_instruments()} >= {"spot", "swap", "futures"}
    assert exchange.get_futures_basis_quote("BTC/USDT").futures_bid > Decimal("0")


def test_market_ticker_returns_decimal_prices() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    service = MarketDataService(ExchangeFactory(settings))

    ticker = service.get_ticker("mock", "BTC/USDT")

    assert ticker.symbol == "BTC/USDT"
    assert ticker.bid == Decimal("50000")
    assert ticker.ask == Decimal("50010")
    assert ticker.last == Decimal("50005")


def test_orderbook_depth_uses_quote_notional() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    service = MarketDataService(ExchangeFactory(settings))

    orderbook = service.get_orderbook("mock", "BTC/USDT")

    assert orderbook.best_bid.price == Decimal("50000")
    assert orderbook.best_ask.price == Decimal("50010")
    assert orderbook.depth_notional("bid", levels=2) >= Decimal("100000")
    assert orderbook.depth_notional("ask", levels=2) >= Decimal("100000")


def test_account_balance_returns_mock_assets() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    service = AccountService(ExchangeFactory(settings))

    account = service.get_balance("mock")

    assert account.exchange == "mock"
    assert account.balance_for("USDT").free == Decimal("10000")
    assert account.balance_for("BTC").total == Decimal("1.5")
