"""数据 API 路由测试 — T-1.1 验收."""

from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.common.data.models import OHLCVBar
from quantbot.common.data.storage import MarketDataStorage
from quantbot.web.app import app


def _make_bar(symbol: str = "AAPL", date_str: str = "2024-01-02", close: float = 100.0) -> OHLCVBar:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return OHLCVBar(
        symbol=symbol, timeframe="1d", timestamp=dt,
        open=close * 0.99, high=close * 1.01, low=close * 0.98, close=close,
        volume=1_000_000.0,
    )


@pytest.fixture
def client_with_storage() -> tuple[TestClient, MarketDataStorage]:
    """返回测试客户端和内存存储."""
    mem_storage = MarketDataStorage(":memory:")
    with patch("quantbot.stocks.api.data.get_storage", return_value=mem_storage):
        with patch("quantbot.stocks.api.data._storage", mem_storage):
            yield TestClient(app), mem_storage  # type: ignore[misc]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


class TestGetBars:
    def test_empty_returns_zero_count(self) -> None:
        mem_storage = MarketDataStorage(":memory:")

        def _override() -> MarketDataStorage:
            return mem_storage

        app.dependency_overrides = {}
        import quantbot.stocks.api.data as data_module
        app.dependency_overrides[data_module.get_storage] = _override

        client = TestClient(app)
        resp = client.get("/data/bars?symbol=AAPL&timeframe=1d")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 0
        assert body["bars"] == []
        app.dependency_overrides = {}

    def test_returns_bars(self) -> None:
        mem_storage = MarketDataStorage(":memory:")
        bars = [_make_bar(date_str=f"2024-01-{i:02d}") for i in range(2, 6)]
        mem_storage.upsert_bars(bars)

        import quantbot.stocks.api.data as data_module
        app.dependency_overrides[data_module.get_storage] = lambda: mem_storage

        client = TestClient(app)
        resp = client.get("/data/bars?symbol=AAPL&timeframe=1d&limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 4
        assert len(body["bars"]) == 4
        app.dependency_overrides = {}

    def test_invalid_date_returns_400(self) -> None:
        import quantbot.stocks.api.data as data_module
        app.dependency_overrides[data_module.get_storage] = lambda: MarketDataStorage(":memory:")
        client = TestClient(app)
        resp = client.get("/data/bars?symbol=AAPL&start=not-a-date")
        assert resp.status_code == 400
        app.dependency_overrides = {}


class TestListSymbols:
    def test_list_symbols(self) -> None:
        mem_storage = MarketDataStorage(":memory:")
        for sym in ["AAPL", "TSLA"]:
            mem_storage.upsert_bars([_make_bar(sym)])

        import quantbot.stocks.api.data as data_module
        app.dependency_overrides[data_module.get_storage] = lambda: mem_storage

        client = TestClient(app)
        resp = client.get("/data/symbols")
        assert resp.status_code == 200
        assert set(resp.json()["symbols"]) == {"AAPL", "TSLA"}
        app.dependency_overrides = {}


class TestMarketUniverse:
    def test_universe_lists_us_equities_and_crypto(self) -> None:
        client = TestClient(app)
        resp = client.get("/data/universe?product=quant-assistant")

        assert resp.status_code == 200
        body = resp.json()
        symbols = {item["symbol"]: item for item in body["instruments"]}
        assert body["product"] == "quant-assistant"
        assert symbols["AAPL"]["default_source"] == "yfinance"
        assert symbols["BTC-USDT"]["default_source"] == "auto"

    def test_universe_rejects_unknown_product(self) -> None:
        client = TestClient(app)
        resp = client.get("/data/universe?product=unknown")

        assert resp.status_code == 422
