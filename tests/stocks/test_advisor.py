"""API tests for /advisor/* endpoints (phaseF.assistant-advisor-backend)."""
from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import quantbot.stocks.api.advisor as advisor_module
from quantbot.web.app import app

client = TestClient(app)


# ---------- helpers ----------------------------------------------------------


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


def _stock_bull_evidence() -> dict[str, Any]:
    return {
        "technical": {"signal": "buy", "score": 45.0, "available": True, "summary": "技术偏多"},
        "relative_strength": {"signal": "outperformer", "score": 72.0, "available": True, "summary": "跑赢 SPY"},
        "eps_revision": {"signal": "upgrade", "available": True, "summary": "EPS 上修"},
        "analyst": {"signal": "buy", "upside_pct": 12.0, "available": True, "summary": "分析师买入"},
    }


def _stock_bear_evidence() -> dict[str, Any]:
    return {
        "technical": {"signal": "sell", "score": -45.0, "available": True, "summary": "技术偏空"},
        "relative_strength": {"signal": "underperformer", "score": 25.0, "available": True, "summary": "跑输 SPY"},
        "eps_revision": {"signal": "downgrade", "available": True, "summary": "EPS 下修"},
        "analyst": {"signal": "sell", "upside_pct": -8.0, "available": True, "summary": "分析师卖出"},
    }


def _stock_neutral_evidence() -> dict[str, Any]:
    return {
        "technical": {"signal": "neutral", "score": 0.0, "available": True, "summary": "技术中性"},
        "relative_strength": {"signal": "neutral", "score": 50.0, "available": True, "summary": "相对中性"},
        "eps_revision": {"signal": "neutral", "available": True, "summary": "EPS 中性"},
        "analyst": {"signal": "hold", "upside_pct": 2.0, "available": True, "summary": "分析师持有"},
    }


# ---------- /advisor/overview ------------------------------------------------


class TestAdvisorOverview:
    def test_returns_200_and_schema(self) -> None:
        r = client.get("/advisor/overview")
        assert r.status_code == 200
        body = r.json()
        assert "net_worth" in body
        assert "cash_ratio" in body
        assert "positions" in body
        assert "generated_at" in body
        assert isinstance(body["net_worth"], float | int)
        assert 0.0 <= body["cash_ratio"] <= 1.0

    def test_positions_is_list(self) -> None:
        r = client.get("/advisor/overview")
        assert isinstance(r.json()["positions"], list)

    def test_api_alias_works(self) -> None:
        r = client.get("/api/advisor/overview")
        assert r.status_code == 200


# ---------- /advisor/opportunities -------------------------------------------


class TestStockOpportunities:
    def test_stock_opportunity_uses_multi_factor_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_evidence(symbol: str) -> dict[str, Any]:
            assert symbol == "NVDA"
            return _stock_bull_evidence()

        monkeypatch.setattr(advisor_module, "_get_stock_evidence", fake_evidence)
        r = client.get("/advisor/opportunities?symbols=NVDA")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["type"] == "stock_long_opportunity"
        assert items[0]["subject"] == "NVDA"
        assert len(items[0]["evidence"]) >= 3
        assert items[0]["confidence"] > 0.5

    def test_stock_opportunity_filters_neutral_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(advisor_module, "_get_stock_evidence", lambda symbol: _stock_neutral_evidence())
        r = client.get("/advisor/opportunities?symbols=AAPL")
        assert r.status_code == 200
        assert r.json()["items"] == []

    def test_stock_opportunity_api_alias_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(advisor_module, "_get_stock_evidence", lambda symbol: _stock_bull_evidence())
        r = client.get("/api/advisor/opportunities?symbols=MSFT")
        assert r.status_code == 200
        assert r.json()["items"][0]["subject"] == "MSFT"


# ---------- /advisor/risks ----------------------------------------------------


class TestStockRisks:
    def test_stock_risk_uses_multi_factor_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(advisor_module, "_get_stock_evidence", lambda symbol: _stock_bear_evidence())
        r = client.get("/advisor/risks?symbols=TSLA")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["type"] == "stock_risk"
        assert items[0]["subject"] == "TSLA"
        assert len(items[0]["risk_notes"]) >= 1

    def test_stock_risk_filters_neutral_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(advisor_module, "_get_stock_evidence", lambda symbol: _stock_neutral_evidence())
        r = client.get("/advisor/risks?symbols=AAPL")
        assert r.status_code == 200
        assert r.json()["items"] == []


