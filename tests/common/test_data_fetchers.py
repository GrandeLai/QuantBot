"""数据抓取器测试（mock 网络）— T-1.1 验收."""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.common.data.fetchers.yfinance_fetcher import YFinanceFetcher
from quantbot.common.data.models import OHLCVBar


def _make_yf_df(n: int = 5) -> pd.DataFrame:
    """生成模拟的 yfinance DataFrame."""
    dates = pd.date_range("2024-01-02", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "Open": [100.0 + i for i in range(n)],
            "High": [105.0 + i for i in range(n)],
            "Low": [98.0 + i for i in range(n)],
            "Close": [102.0 + i for i in range(n)],
            "Volume": [1_000_000.0 + i * 10000 for i in range(n)],
        },
        index=dates,
    )


class TestYFinanceFetcher:
    def setup_method(self) -> None:
        self.fetcher = YFinanceFetcher()

    def test_source_name(self) -> None:
        assert self.fetcher.source_name == "yfinance"

    def test_timeframe_mapping(self) -> None:
        assert self.fetcher._timeframe_to_source("1d") == "1d"
        assert self.fetcher._timeframe_to_source("1h") == "1h"
        assert self.fetcher._timeframe_to_source("1w") == "1wk"
        assert self.fetcher._timeframe_to_source("1M") == "1mo"

    def test_invalid_timeframe_raises(self) -> None:
        with pytest.raises(ValueError, match="不支持"):
            self.fetcher._timeframe_to_source("2h")

    @patch("yfinance.Ticker")
    def test_fetch_ohlcv_returns_bars(self, mock_ticker_cls: MagicMock) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_yf_df(5)
        mock_ticker_cls.return_value = mock_ticker

        bars = self.fetcher.fetch_ohlcv(
            "AAPL", "1d",
            start=date(2024, 1, 2),
            end=date(2024, 1, 6),
        )

        assert len(bars) == 5
        assert all(isinstance(b, OHLCVBar) for b in bars)
        assert bars[0].symbol == "AAPL"
        assert bars[0].timeframe == "1d"
        assert bars[0].open == 100.0
        assert bars[0].close == 102.0

    @patch("yfinance.Ticker")
    def test_fetch_ohlcv_empty_returns_empty_list(self, mock_ticker_cls: MagicMock) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        bars = self.fetcher.fetch_ohlcv("NOTEXIST", "1d", start=date(2024, 1, 2))
        assert bars == []

    @patch("yfinance.Ticker")
    def test_fetch_ohlcv_timestamps_are_utc(self, mock_ticker_cls: MagicMock) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_yf_df(3)
        mock_ticker_cls.return_value = mock_ticker

        bars = self.fetcher.fetch_ohlcv("AAPL", "1d", start=date(2024, 1, 2))
        for bar in bars:
            assert bar.timestamp.tzinfo is not None

    @patch("yfinance.Ticker")
    def test_fetch_ohlcv_ascending_order(self, mock_ticker_cls: MagicMock) -> None:
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = _make_yf_df(5)
        mock_ticker_cls.return_value = mock_ticker

        bars = self.fetcher.fetch_ohlcv("AAPL", "1d", start=date(2024, 1, 2))
        timestamps = [b.timestamp for b in bars]
        assert timestamps == sorted(timestamps)

    def test_search_symbols_returns_empty(self) -> None:
        # YFinance 不支持搜索，返回空列表
        result = self.fetcher.search_symbols("Apple")
        assert result == []
