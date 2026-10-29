"""API tests for crypto-only advisor endpoints."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import quantbot.crypto.api.advisor as advisor_module
from quantbot.web.app import app

client = TestClient(app)


def _neutral_signal() -> dict[str, Any]:
    return {"signal": "neutral", "z_score": 0.0, "percentile": 50.0, "funding_rate": 0.0001}


def _contrarian_long_signal() -> dict[str, Any]:
    return {"signal": "contrarian_long", "z_score": -2.5, "percentile": 3.0, "funding_rate": -0.0002}


def _contrarian_short_signal() -> dict[str, Any]:
    return {"signal": "contrarian_short", "z_score": 2.8, "percentile": 97.0, "funding_rate": 0.0008}


def _low_funding_signal() -> dict[str, Any]:
    return {"signal": "neutral", "z_score": -0.8, "percentile": 20.0, "funding_rate": 0.00005}


def _high_funding_signal() -> dict[str, Any]:
    return {"signal": "neutral", "z_score": 0.9, "percentile": 75.0, "funding_rate": 0.0003}


class TestAdvisorOverview:
    """Overview endpoint smoke tests."""

    def test_returns_200_and_schema(self) -> None:
        """Overview returns the portfolio shell expected by the UI."""
        response = client.get("/advisor/overview")
        assert response.status_code == 200
        body = response.json()
        assert "net_worth" in body
        assert "cash_ratio" in body
        assert "positions" in body
        assert "generated_at" in body
        assert isinstance(body["net_worth"], float | int)
        assert 0.0 <= body["cash_ratio"] <= 1.0

    def test_api_alias_works(self) -> None:
        """The /api alias is registered for frontend proxy compatibility."""
        response = client.get("/api/advisor/overview")
        assert response.status_code == 200


class TestCryptoOpportunities:
    """Crypto opportunity endpoint tests."""

    def test_neutral_signal_no_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neutral funding should not emit an opportunity."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _neutral_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/opportunities?symbol=BTC-USDT")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_contrarian_long_produces_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Very negative funding creates a contrarian long opportunity."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _contrarian_long_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/opportunities?symbol=BTC-USDT")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["type"] == "long_opportunity"
        assert item["subject"] == "BTC-USDT"
        assert 0.5 < item["confidence"] <= 0.92
        assert len(item["evidence"]) >= 1
        assert len(item["risk_notes"]) >= 1

    def test_low_funding_neutral_produces_basis_item(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Low but non-extreme funding creates a basis carry candidate."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _low_funding_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/opportunities?symbol=ETH-USDT")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["type"] == "basis_opportunity"
        assert item["subject"] == "ETH-USDT"

    def test_contrarian_short_no_opportunity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Crowded long funding should not be classified as an opportunity."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _contrarian_short_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/opportunities?symbol=BTC-USDT")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The /api alias is registered."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _neutral_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/api/advisor/crypto/opportunities?symbol=BTC-USDT")
        assert response.status_code == 200

    def test_default_symbol_is_btc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """BTC is the default crypto advisor asset."""
        captured: list[str] = []

        async def fake_signal(asset: str) -> dict[str, Any]:
            captured.append(asset)
            return _neutral_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        client.get("/advisor/crypto/opportunities")
        assert captured == ["BTC"]


class TestCryptoRisks:
    """Crypto risk endpoint tests."""

    def test_neutral_signal_no_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Neutral funding should not emit a risk item."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _neutral_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/risks?symbol=BTC-USDT")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_contrarian_short_produces_risk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Very positive funding creates a crowded-long risk item."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _contrarian_short_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/risks?symbol=BTC-USDT")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["type"] == "crowded_long_risk"
        assert item["subject"] == "BTC-USDT"
        assert 0.5 < item["confidence"] <= 0.92
        assert len(item["evidence"]) >= 1
        assert any("%" in note for note in item["risk_notes"])

    def test_high_funding_neutral_produces_elevated_risk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """High but non-extreme funding still surfaces as an elevated risk."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _high_funding_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/risks?symbol=ETH-USDT")
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["type"] == "elevated_funding_risk"

    def test_contrarian_long_no_risk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative funding should not be classified as a crowded-long risk."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _contrarian_long_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/advisor/crypto/risks?symbol=BTC-USDT")
        assert response.status_code == 200
        assert response.json()["items"] == []

    def test_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The /api alias is registered."""
        async def fake_signal(asset: str) -> dict[str, Any]:
            return _neutral_signal()

        monkeypatch.setattr(advisor_module, "_get_funding_signal", fake_signal)
        response = client.get("/api/advisor/crypto/risks?symbol=BTC-USDT")
        assert response.status_code == 200
