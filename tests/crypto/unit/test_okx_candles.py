"""Unit tests for OKX candle market-data parsing."""

from __future__ import annotations

from decimal import Decimal

from quantbot.crypto.runtime.exchanges.okx import OKXExchange


class FakeOKXCandleClient:
    """Fake OKX client exposing only candle rows."""

    def get_candles(self, inst_id: str, bar: str = "15m", limit: int = 100, history: bool = False) -> list[list[str]]:
        assert inst_id == "BTC-USDT"
        assert bar == "15m"
        assert limit == 3
        assert history is False
        return [
            ["1760000300000", "103", "105", "102", "104", "14", "1400", "1456", "0"],
            ["1760000200000", "102", "104", "101", "103", "13", "1300", "1339", "1"],
            ["1760000100000", "101", "103", "100", "102", "12", "1200", "1224", "1"],
        ]


def test_okx_candle_parser_returns_completed_candles_oldest_first() -> None:
    candles = OKXExchange(name="okx", client=FakeOKXCandleClient()).get_candles(
        "BTC/USDT",
        bar="15m",
        limit=3,
    )

    assert [candle.close for candle in candles] == [Decimal("102"), Decimal("103")]
    assert all(candle.complete is True for candle in candles)
    assert all(candle.exchange == "okx" for candle in candles)
    assert all(candle.symbol == "BTC/USDT" for candle in candles)
