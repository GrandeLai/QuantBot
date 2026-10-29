"""pytest 公共配置与 fixtures (stock-assistant)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """返回隔离存储的 FastAPI 测试客户端."""
    monkeypatch.setenv("QUANTBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("QUANTBOT_DUCKDB_PATH", str(tmp_path / "quantpilot.duckdb"))
    monkeypatch.setenv("QUANTBOT_SQLITE_PATH", str(tmp_path / "quantpilot.sqlite"))
    monkeypatch.setenv("QUANTBOT_STRATEGY_DIR", str(tmp_path / "strategies"))
    monkeypatch.setenv("QUANTBOT_LOG_DIR", str(tmp_path / "logs"))

    # Lazy imports: deferred until env vars are set
    from quantbot.web.app import app

    import quantbot.common.data as _common_data
    from quantbot.stocks.broker.provider import get_trading_provider
    from quantbot.stocks.trading.oms import get_order_event_store

    _common_data._storage = None
    get_trading_provider.cache_clear()
    get_order_event_store().reset()
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        _common_data._storage = None
        get_trading_provider.cache_clear()
        get_order_event_store().reset()
