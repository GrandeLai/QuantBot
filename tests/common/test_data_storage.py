"""DuckDB 存储层测试 — T-1.1 验收."""

from datetime import UTC, datetime

import polars as pl
import pytest

from quantbot.common.data.models import AssetType, Exchange, OHLCVBar, SymbolInfo
from quantbot.common.data.storage import MarketDataStorage


def _make_bar(
    symbol: str = "AAPL",
    timeframe: str = "1d",
    date_str: str = "2024-01-02",
    close: float = 100.0,
) -> OHLCVBar:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return OHLCVBar(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=dt,
        open=close * 0.99,
        high=close * 1.02,
        low=close * 0.97,
        close=close,
        volume=1_000_000.0,
    )


@pytest.fixture
def storage() -> MarketDataStorage:
    """内存数据库存储实例."""
    return MarketDataStorage(db_path=":memory:")


class TestUpsertBars:
    def test_insert_single_bar(self, storage: MarketDataStorage) -> None:
        bar = _make_bar()
        count = storage.upsert_bars([bar])
        assert count == 1
        assert storage.count_bars("AAPL", "1d") == 1

    def test_insert_multiple_bars(self, storage: MarketDataStorage) -> None:
        bars = [_make_bar(date_str=f"2024-01-0{i}") for i in range(2, 8)]
        count = storage.upsert_bars(bars)
        assert count == 6
        assert storage.count_bars("AAPL", "1d") == 6

    def test_duplicate_ignored(self, storage: MarketDataStorage) -> None:
        bar = _make_bar()
        storage.upsert_bars([bar])
        storage.upsert_bars([bar])  # 重复插入
        assert storage.count_bars("AAPL", "1d") == 1

    def test_empty_list(self, storage: MarketDataStorage) -> None:
        count = storage.upsert_bars([])
        assert count == 0

    def test_multiple_symbols(self, storage: MarketDataStorage) -> None:
        bars = [
            _make_bar("AAPL", "1d", "2024-01-02"),
            _make_bar("TSLA", "1d", "2024-01-02"),
            _make_bar("AAPL", "1h", "2024-01-02"),
        ]
        storage.upsert_bars(bars)
        assert storage.count_bars("AAPL", "1d") == 1
        assert storage.count_bars("TSLA", "1d") == 1
        assert storage.count_bars("AAPL", "1h") == 1


class TestQueryBars:
    def test_query_returns_polars_df(self, storage: MarketDataStorage) -> None:
        bars = [_make_bar(date_str=f"2024-01-0{i}") for i in range(2, 6)]
        storage.upsert_bars(bars)
        df = storage.query_bars("AAPL", "1d")
        assert isinstance(df, pl.DataFrame)
        assert len(df) == 4

    def test_query_limit(self, storage: MarketDataStorage) -> None:
        bars = [_make_bar(date_str=f"2024-01-{i:02d}") for i in range(2, 20)]
        storage.upsert_bars(bars)
        df = storage.query_bars("AAPL", "1d", limit=5)
        assert len(df) == 5

    def test_query_date_range(self, storage: MarketDataStorage) -> None:
        bars = [_make_bar(date_str=f"2024-01-{i:02d}") for i in range(2, 10)]
        storage.upsert_bars(bars)

        start = datetime(2024, 1, 4, tzinfo=UTC)
        end = datetime(2024, 1, 7, tzinfo=UTC)
        df = storage.query_bars("AAPL", "1d", start=start, end=end)
        assert len(df) == 4  # 4, 5, 6, 7

    def test_query_ascending_order(self, storage: MarketDataStorage) -> None:
        bars = [_make_bar(date_str=f"2024-01-{i:02d}") for i in range(2, 6)]
        storage.upsert_bars(bars)
        df = storage.query_bars("AAPL", "1d")
        timestamps = df["timestamp"].to_list()
        assert timestamps == sorted(timestamps)

    def test_query_nonexistent_returns_empty(self, storage: MarketDataStorage) -> None:
        df = storage.query_bars("NOTEXIST", "1d")
        assert len(df) == 0


class TestQueryLatestBar:
    def test_returns_most_recent(self, storage: MarketDataStorage) -> None:
        bars = [
            _make_bar(date_str="2024-01-02", close=100.0),
            _make_bar(date_str="2024-01-03", close=102.0),
            _make_bar(date_str="2024-01-04", close=105.0),
        ]
        storage.upsert_bars(bars)
        latest = storage.query_latest_bar("AAPL", "1d")
        assert latest is not None
        assert latest.close == 105.0

    def test_returns_none_when_empty(self, storage: MarketDataStorage) -> None:
        result = storage.query_latest_bar("AAPL", "1d")
        assert result is None


class TestDateRange:
    def test_date_range(self, storage: MarketDataStorage) -> None:
        bars = [_make_bar(date_str=f"2024-01-{i:02d}") for i in range(2, 6)]
        storage.upsert_bars(bars)
        earliest, latest = storage.get_date_range("AAPL", "1d")
        assert earliest is not None
        assert latest is not None
        assert earliest < latest  # type: ignore[operator]

    def test_empty_range_returns_none(self, storage: MarketDataStorage) -> None:
        earliest, latest = storage.get_date_range("NOTEXIST", "1d")
        assert earliest is None
        assert latest is None


class TestListSymbols:
    def test_list_symbols(self, storage: MarketDataStorage) -> None:
        for symbol in ["AAPL", "TSLA", "GOOGL"]:
            storage.upsert_bars([_make_bar(symbol)])
        symbols = storage.list_symbols()
        assert set(symbols) == {"AAPL", "TSLA", "GOOGL"}

    def test_empty_returns_empty_list(self, storage: MarketDataStorage) -> None:
        assert storage.list_symbols() == []


class TestUpsertSymbols:
    def test_upsert_symbol_info(self, storage: MarketDataStorage) -> None:
        syms = [
            SymbolInfo(
                symbol="AAPL",
                name="Apple Inc.",
                exchange=Exchange.NASDAQ,
                asset_type=AssetType.STOCK,
            )
        ]
        count = storage.upsert_symbols(syms)
        assert count == 1
