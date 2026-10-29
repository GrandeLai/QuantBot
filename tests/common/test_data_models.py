"""数据模型测试 — T-1.1 验收."""

from datetime import UTC, datetime

import pytest

from quantbot.common.data.models import (
    TIMEFRAMES,
    AssetType,
    Exchange,
    OHLCVBar,
    OHLCVResponse,
    SymbolInfo,
)


def _bar(timeframe: str = "1d", **kwargs: object) -> OHLCVBar:
    defaults: dict[str, object] = {
        "symbol": "AAPL",
        "timeframe": timeframe,
        "timestamp": datetime(2024, 1, 2, tzinfo=UTC),
        "open": 100.0,
        "high": 110.0,
        "low": 95.0,
        "close": 105.0,
        "volume": 1_000_000.0,
    }
    defaults.update(kwargs)
    return OHLCVBar(**defaults)  # type: ignore[arg-type]


class TestOHLCVBar:
    def test_valid_bar(self) -> None:
        bar = _bar()
        assert bar.symbol == "AAPL"
        assert bar.close == 105.0

    def test_turnover_optional(self) -> None:
        bar = _bar()
        assert bar.turnover is None

        bar2 = _bar(turnover=1_500_000.0)
        assert bar2.turnover == 1_500_000.0

    @pytest.mark.parametrize("tf", list(TIMEFRAMES))
    def test_all_valid_timeframes(self, tf: str) -> None:
        bar = _bar(timeframe=tf)
        assert bar.timeframe == tf

    def test_invalid_timeframe(self) -> None:
        with pytest.raises(ValueError, match="不支持的 timeframe"):
            _bar(timeframe="2h")

    def test_negative_volume_rejected(self) -> None:
        with pytest.raises(ValueError):
            _bar(volume=-1.0)

    def test_zero_price_rejected(self) -> None:
        with pytest.raises(ValueError):
            _bar(open=0.0)


class TestSymbolInfo:
    def test_stock_symbol(self) -> None:
        s = SymbolInfo(
            symbol="600519",
            name="贵州茅台",
            exchange=Exchange.SSE,
            asset_type=AssetType.STOCK,
            currency="CNY",
        )
        assert s.asset_type == AssetType.STOCK
        assert s.currency == "CNY"

    def test_crypto_symbol(self) -> None:
        s = SymbolInfo(
            symbol="BTC-USD",
            name="Bitcoin",
            exchange=Exchange.BINANCE,
            asset_type=AssetType.CRYPTO,
        )
        assert s.currency == "USD"  # 默认值
        assert s.status == "active"  # 默认值


class TestOHLCVResponse:
    def test_response(self) -> None:
        bars = [_bar(), _bar(timestamp=datetime(2024, 1, 3, tzinfo=UTC))]
        resp = OHLCVResponse(symbol="AAPL", timeframe="1d", count=2, bars=bars)
        assert resp.count == 2
        assert len(resp.bars) == 2
