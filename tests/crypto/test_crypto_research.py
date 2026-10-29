"""API tests for /crypto/research/* endpoints (phaseF.assistant-advisor-backend)."""
from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

import quantbot.crypto.api.crypto_research as research_module
from quantbot.web.app import app

client = TestClient(app)

# All supported regime labels
_ALL_REGIMES = [
    "overheated_bull",
    "bull_trending",
    "ranging",
    "bear_ranging",
    "bear_trending",
]


# ---------- helpers ----------------------------------------------------------


def _make_regime(regime: str, funding: float = 0.0001, pct: float = 50.0):
    async def fake_detect(asset: str) -> tuple[str, float, float]:
        return regime, funding, pct

    return fake_detect


# ---------- /crypto/research/latest ------------------------------------------


class TestResearchLatest:
    def test_happy_path_ranging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("ranging"))
        r = client.get("/crypto/research/latest?symbol=BTC-USDT&base_timeframe=15m")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "BTC-USDT"
        assert body["base_timeframe"] == "15m"
        assert body["market_regime"] == "ranging"
        assert isinstance(body["recommended_strategy_ids"], list)
        assert len(body["recommended_strategy_ids"]) > 0
        assert isinstance(body["recommended_timeframes"], list)
        assert body["parameter_search_ready"] is True
        assert "generated_at" in body

    @pytest.mark.parametrize("regime", _ALL_REGIMES)
    def test_all_regimes_return_strategies(
        self, monkeypatch: pytest.MonkeyPatch, regime: str
    ) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime(regime))
        r = client.get("/crypto/research/latest?symbol=ETH-USDT")
        assert r.status_code == 200
        body = r.json()
        assert body["market_regime"] == regime
        assert len(body["recommended_strategy_ids"]) > 0
        assert len(body["recommended_timeframes"]) > 0

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("bull_trending"))
        r = client.get("/api/crypto/research/latest?symbol=BTC-USDT")
        assert r.status_code == 200

    def test_overheated_bull_strategies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            research_module, "_detect_regime", _make_regime("overheated_bull", pct=95.0)
        )
        r = client.get("/crypto/research/latest?symbol=BTC-USDT")
        body = r.json()
        assert "mean_reversion" in body["recommended_strategy_ids"]


# ---------- /crypto/research/optimize ----------------------------------------


class TestResearchOptimize:
    def test_happy_path_returns_params(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("bull_trending"))
        r = client.post(
            "/crypto/research/optimize",
            json={"symbol": "BTC-USDT", "strategy_id": "vwap_ema_trend"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "BTC-USDT"
        assert body["strategy_id"] == "vwap_ema_trend"
        assert body["regime"] == "bull_trending"
        params = body["best_params"]
        assert "fast_period" in params
        assert "slow_period" in params
        assert "trailing_stop_pct" in params
        assert "max_hold_bars" in params

    @pytest.mark.parametrize("regime", _ALL_REGIMES)
    def test_all_regimes_return_params(
        self, monkeypatch: pytest.MonkeyPatch, regime: str
    ) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime(regime))
        r = client.post("/crypto/research/optimize", json={"symbol": "ETH-USDT"})
        assert r.status_code == 200
        assert r.json()["regime"] == regime
        assert "fast_period" in r.json()["best_params"]

    def test_method_field_is_regime_based(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("ranging"))
        r = client.post("/crypto/research/optimize", json={"symbol": "BTC-USDT"})
        assert r.json()["method"] == "regime_based_defaults"

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("ranging"))
        r = client.post("/api/crypto/research/optimize", json={"symbol": "BTC-USDT"})
        assert r.status_code == 200


# ---------- /crypto/research/optimize/latest ---------------------------------


class TestResearchOptimizeLatest:
    def test_happy_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("bear_trending"))
        r = client.get(
            "/crypto/research/optimize/latest"
            "?symbol=BTC-USDT&base_timeframe=15m&strategy_id=vwap_ema_trend"
        )
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "BTC-USDT"
        assert body["strategy_id"] == "vwap_ema_trend"
        assert body["regime"] == "bear_trending"
        assert "fast_period" in body["best_params"]

    def test_default_params_structure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("ranging"))
        r = client.get("/crypto/research/optimize/latest?symbol=ETH-USDT")
        assert r.status_code == 200
        params = r.json()["best_params"]
        # All 5 expected keys present
        for key in ["fast_period", "slow_period", "vwap_window", "trailing_stop_pct", "max_hold_bars"]:
            assert key in params, f"missing key: {key}"

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research_module, "_detect_regime", _make_regime("bull_trending"))
        r = client.get("/api/crypto/research/optimize/latest?symbol=BTC-USDT")
        assert r.status_code == 200
