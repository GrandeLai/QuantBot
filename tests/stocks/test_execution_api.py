"""执行 API 端点测试（Phase F.3.5）."""
from __future__ import annotations


import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantbot.stocks.api.execution import router

app = FastAPI()
app.include_router(router)
app.include_router(router, prefix="/api")

client = TestClient(app)

_START = "2025-01-15T09:30:00Z"
_END = "2025-01-15T11:00:00Z"   # 90 分钟后


# ---------------------------------------------------------------------------
# POST /execution/twap
# ---------------------------------------------------------------------------


class TestTWAPEndpoint:
    def test_twap_returns_6_slices(self) -> None:
        """90 分钟 / 15 分钟 = 6 切片."""
        body = {
            "ticker": "AAPL",
            "total_quantity": 600.0,
            "start_time": _START,
            "end_time": _END,
        }
        resp = client.post("/execution/twap", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["child_orders"]) == 6
        assert data["algo"] == "TWAP"
        assert data["ticker"] == "AAPL"

    def test_twap_via_api_prefix(self) -> None:
        body = {"ticker": "AAPL", "total_quantity": 100.0, "start_time": _START, "end_time": _END}
        resp = client.post("/api/execution/twap", json=body)
        assert resp.status_code == 200

    def test_twap_num_slices_override(self) -> None:
        body = {
            "ticker": "MSFT",
            "total_quantity": 500.0,
            "start_time": _START,
            "end_time": _END,
            "num_slices": 5,
        }
        resp = client.post("/execution/twap", json=body)
        assert len(resp.json()["child_orders"]) == 5

    def test_twap_invalid_end_before_start(self) -> None:
        body = {"ticker": "AAPL", "total_quantity": 100.0, "start_time": _END, "end_time": _START}
        resp = client.post("/execution/twap", json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /execution/vwap
# ---------------------------------------------------------------------------


class TestVWAPEndpoint:
    def test_vwap_basic(self) -> None:
        body = {
            "ticker": "SPY",
            "total_quantity": 300.0,
            "start_time": _START,
            "end_time": _END,
            "num_slices": 3,
        }
        resp = client.post("/execution/vwap", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["algo"] == "VWAP"
        assert len(data["child_orders"]) == 3

    def test_vwap_weighted_profile(self) -> None:
        body = {
            "ticker": "QQQ",
            "total_quantity": 400.0,
            "start_time": _START,
            "end_time": _END,
            "volume_profile": [1.0, 2.0, 1.0],
            "num_slices": 3,
        }
        resp = client.post("/execution/vwap", json=body)
        orders = resp.json()["child_orders"]
        # 中间片（index=1）数量应约为总量的 50%
        assert orders[1]["quantity"] == pytest.approx(200.0, abs=0.01)

    def test_vwap_profile_mismatch_returns_422(self) -> None:
        body = {
            "ticker": "SPY",
            "total_quantity": 100.0,
            "start_time": _START,
            "end_time": _END,
            "volume_profile": [1.0, 2.0],   # 2 个但 num_slices=3
            "num_slices": 3,
        }
        resp = client.post("/execution/vwap", json=body)
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /execution/tca
# ---------------------------------------------------------------------------


class TestTCAEndpoint:
    def test_tca_positive_slippage(self) -> None:
        body = {
            "ticker": "AAPL",
            "arrival_price": 100.0,
            "executed_avg_price": 100.1,
            "total_quantity": 1000.0,
            "algo": "TWAP",
        }
        resp = client.post("/execution/tca", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["slippage_bps"] == pytest.approx(10.0, abs=0.01)

    def test_tca_zero_slippage(self) -> None:
        body = {"arrival_price": 50.0, "executed_avg_price": 50.0}
        resp = client.post("/execution/tca", json=body)
        assert resp.json()["slippage_bps"] == pytest.approx(0.0, abs=1e-6)

    def test_tca_optional_fields(self) -> None:
        body = {
            "arrival_price": 200.0,
            "executed_avg_price": 199.5,
            "vwap_price": 199.8,
            "close_price": 200.5,
            "algo": "VWAP",
        }
        resp = client.post("/execution/tca", json=body)
        data = resp.json()
        assert data["vwap_price"] == 199.8
        assert data["close_price"] == 200.5


# ---------------------------------------------------------------------------
# GET /execution/adv-check
# ---------------------------------------------------------------------------


class TestADVCheckEndpoint:
    def test_needs_slicing(self) -> None:
        resp = client.get("/execution/adv-check?ticker=AAPL&quantity=10000&adv=1000000")
        assert resp.status_code == 200
        data = resp.json()
        assert data["needs_slicing"] is True
        assert data["recommended_slices"] >= 2

    def test_no_slicing_needed(self) -> None:
        resp = client.get("/execution/adv-check?ticker=AAPL&quantity=100&adv=1000000")
        data = resp.json()
        assert data["needs_slicing"] is False
        assert data["recommended_slices"] == 1

    def test_custom_threshold(self) -> None:
        resp = client.get(
            "/execution/adv-check?ticker=SPY&quantity=5000&adv=1000000&threshold_pct=0.01"
        )
        data = resp.json()
        assert data["needs_slicing"] is False  # 5000/1M = 0.5% < 1%
