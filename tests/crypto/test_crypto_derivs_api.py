"""API tests for /crypto-derivs/* endpoints (phaseF.1.13)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient

from quantbot.crypto.crypto_derivs import FundingRate, OpenInterest
from quantbot.web.app import app
from quantbot.crypto.api import crypto_derivs as api_module

client = TestClient(app)


@pytest.fixture
def funding_history() -> list[float]:
    rng = np.random.default_rng(42)
    return rng.normal(0.0001, 0.00005, size=200).tolist()


@pytest.fixture
def etf_flow_history() -> list[float]:
    rng = np.random.default_rng(7)
    return rng.normal(50_000_000, 25_000_000, size=120).tolist()


# --- /snapshot --------------------------------------------------------------


class TestSnapshotEndpoint:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_aggregated(asset: str = "BTC") -> dict[str, Any]:
            ts = datetime(2026, 4, 29, tzinfo=UTC)
            return {
                "asset": asset,
                "timestamp": ts,
                "funding": {
                    "binance": FundingRate(
                        exchange="binance",
                        symbol="BTC",
                        raw_symbol="BTCUSDT",
                        funding_rate=0.0001,
                        next_funding_time=None,
                        timestamp=ts,
                    ),
                    "okx": None,
                },
                "open_interest": {
                    "binance": OpenInterest(
                        exchange="binance",
                        symbol="BTC",
                        raw_symbol="BTCUSDT",
                        open_interest=12345.0,
                        open_interest_value=None,
                        timestamp=ts,
                    ),
                    "okx": None,
                },
                "errors": {"okx_funding": "RuntimeError: simulated"},
            }

        monkeypatch.setattr(api_module, "fetch_aggregated_derivs", fake_aggregated)
        r = client.post("/crypto-derivs/snapshot", json={"asset": "BTC"})
        assert r.status_code == 200
        body = r.json()
        assert body["asset"] == "BTC"
        assert body["funding"]["binance"]["exchange"] == "binance"
        assert body["funding"]["okx"] is None
        assert body["open_interest"]["binance"]["raw_symbol"] == "BTCUSDT"
        assert "okx_funding" in body["errors"]

    def test_default_asset_is_btc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        async def fake(asset: str = "BTC") -> dict[str, Any]:
            captured["asset"] = asset
            return {
                "asset": asset,
                "timestamp": datetime.now(tz=UTC),
                "funding": {"binance": None, "okx": None},
                "open_interest": {"binance": None, "okx": None},
                "errors": {},
            }

        monkeypatch.setattr(api_module, "fetch_aggregated_derivs", fake)
        r = client.post("/crypto-derivs/snapshot", json={})
        assert r.status_code == 200
        assert captured["asset"] == "BTC"

    def test_lowercase_asset_uppercased(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, str] = {}

        async def fake(asset: str = "BTC") -> dict[str, Any]:
            captured["asset"] = asset
            return {
                "asset": asset,
                "timestamp": datetime.now(tz=UTC),
                "funding": {"binance": None, "okx": None},
                "open_interest": {"binance": None, "okx": None},
                "errors": {},
            }

        monkeypatch.setattr(api_module, "fetch_aggregated_derivs", fake)
        r = client.post("/crypto-derivs/snapshot", json={"asset": "eth"})
        assert r.status_code == 200
        assert captured["asset"] == "ETH"

    def test_empty_asset_400(self) -> None:
        r = client.post("/crypto-derivs/snapshot", json={"asset": "  "})
        assert r.status_code == 400


# --- /funding-stats ---------------------------------------------------------


class TestFundingStatsEndpoint:
    def test_history_only_no_signal(self, funding_history: list[float]) -> None:
        r = client.post("/crypto-derivs/funding-stats", json={"history": funding_history})
        assert r.status_code == 200
        body = r.json()
        assert body["signal"] is None
        for k in ("mean", "std", "p5", "p25", "p50", "p75", "p95"):
            assert k in body["stats"]
        assert body["n_samples"] == len(funding_history)

    def test_with_current_returns_signal(self, funding_history: list[float]) -> None:
        # 5σ outlier → contrarian_short
        mu = float(np.mean(funding_history))
        sigma = float(np.std(funding_history, ddof=1))
        r = client.post(
            "/crypto-derivs/funding-stats",
            json={"history": funding_history, "current": mu + 5 * sigma},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["signal"]["signal"] == "contrarian_short"
        assert body["signal"]["z_score"] > 2.0

    def test_too_few_samples_400(self) -> None:
        r = client.post("/crypto-derivs/funding-stats", json={"history": [0.0001] * 20})
        assert r.status_code == 400

    def test_invalid_threshold_422(self, funding_history: list[float]) -> None:
        # pydantic gt=0 验证 → 422
        r = client.post(
            "/crypto-derivs/funding-stats",
            json={"history": funding_history, "z_threshold": 0.0},
        )
        assert r.status_code == 422


# --- /etf-flow-stats --------------------------------------------------------


class TestETFFlowStatsEndpoint:
    def test_history_only(self, etf_flow_history: list[float]) -> None:
        r = client.post("/crypto-derivs/etf-flow-stats", json={"history": etf_flow_history})
        assert r.status_code == 200
        body = r.json()
        assert body["signal"] is None
        assert body["stats"]["n_samples"] == len(etf_flow_history)

    def test_large_inflow_signal(self, etf_flow_history: list[float]) -> None:
        mu = float(np.mean(etf_flow_history))
        sigma = float(np.std(etf_flow_history, ddof=1))
        r = client.post(
            "/crypto-derivs/etf-flow-stats",
            json={"history": etf_flow_history, "current": mu + 5 * sigma},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["signal"]["signal"] == "large_inflow"

    def test_too_few_samples_400(self) -> None:
        # _flow_describe needs >= 10
        r = client.post("/crypto-derivs/etf-flow-stats", json={"history": [1.0, 2.0, 3.0]})
        assert r.status_code == 400

    def test_signal_requires_30(self, etf_flow_history: list[float]) -> None:
        # _flow_describe 给 12 样本能过，但 flow_extreme_signal 要 30 → 400
        small = etf_flow_history[:12]
        r = client.post(
            "/crypto-derivs/etf-flow-stats",
            json={"history": small, "current": 100_000_000.0},
        )
        assert r.status_code == 400
