"""Unit tests for OKX sandbox validation."""

from __future__ import annotations

from decimal import Decimal

from quantbot.crypto.runtime.exchanges.base import (
    AccountSnapshot,
    Balance,
    FundingRate,
    OrderBook,
    OrderBookLevel,
    SpotPerpQuote,
    Ticker,
    utcnow,
)
from quantbot.crypto.runtime.exchanges.sandbox import OKXSandboxValidator


class FakeOKXExchange:
    """Fake OKX exchange for sandbox validation tests."""

    name = "okx"

    def ping(self) -> bool:
        return True

    def list_symbols(self) -> list[str]:
        return ["BTC/USDT"]

    def get_ticker(self, symbol: str) -> Ticker:
        return Ticker("okx", symbol, Decimal("50000"), Decimal("50010"), Decimal("50005"), Decimal("123"), utcnow())

    def get_orderbook(self, symbol: str) -> OrderBook:
        return OrderBook(
            exchange="okx",
            symbol=symbol,
            bids=[OrderBookLevel(Decimal("50000"), Decimal("1"))],
            asks=[OrderBookLevel(Decimal("50010"), Decimal("1"))],
            timestamp=utcnow(),
        )

    def get_balances(self) -> AccountSnapshot:
        return AccountSnapshot(
            exchange="okx",
            balances={"USDT": Balance("USDT", Decimal("100"), Decimal("100"), Decimal("0"))},
            timestamp=utcnow(),
        )

    def get_funding_rates(self) -> list[FundingRate]:
        return []

    def get_spot_perp_quote(self, symbol: str) -> SpotPerpQuote:
        return SpotPerpQuote("okx", symbol, Decimal("50000"), Decimal("50010"), Decimal("50050"), Decimal("50060"), Decimal("0.0005"))


def test_okx_sandbox_validator_checks_public_and_private_paths() -> None:
    result = OKXSandboxValidator(FakeOKXExchange()).run(symbol="BTC/USDT", include_private=True)
    payload = result.to_dict()

    assert payload["ok"] is True
    assert payload["exchange"] == "okx"
    assert payload["checks"]["ping"]["ok"] is True
    assert payload["checks"]["ticker"]["details"]["last"] == "50005"
    assert payload["checks"]["orderbook"]["details"]["best_ask"] == "50010"
    assert payload["checks"]["account"]["details"]["assets"] == ["USDT"]
    assert payload["live_orders_sent"] is False


def test_okx_sandbox_validator_records_failures_without_raising() -> None:
    class BrokenExchange(FakeOKXExchange):
        def get_orderbook(self, symbol: str) -> OrderBook:
            raise RuntimeError("boom")

    result = OKXSandboxValidator(BrokenExchange()).run(symbol="BTC/USDT", include_private=False)
    payload = result.to_dict()

    assert payload["ok"] is False
    assert payload["checks"]["orderbook"]["ok"] is False
    assert "boom" in payload["checks"]["orderbook"]["error"]
    assert "account" not in payload["checks"]
