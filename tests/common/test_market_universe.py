"""Tests for the shared product market universe."""

from quantbot.common.data.models import AssetType
from quantbot.common.data.universe import (
    find_supported_instrument,
    list_supported_instruments,
)


def test_universe_contains_us_equities_and_crypto_for_both_products() -> None:
    """Both products expose US equities and crypto from the shared universe."""
    quant_symbols = {item.symbol: item for item in list_supported_instruments("quant-assistant")}
    stock_symbols = {item.symbol: item for item in list_supported_instruments("stock-assistant")}

    assert quant_symbols["AAPL"].asset_type is AssetType.STOCK
    assert quant_symbols["BTC-USDT"].asset_type is AssetType.CRYPTO
    assert stock_symbols["AAPL"].default_source == "yfinance"
    assert stock_symbols["BTC-USDT"].default_source == "auto"


def test_find_supported_instrument_accepts_aliases() -> None:
    """Alias lookup keeps common crypto and equity inputs product-friendly."""
    assert find_supported_instrument("BTC/USDT") is not None
    assert find_supported_instrument("apple") is not None
    assert find_supported_instrument("not-a-symbol") is None
