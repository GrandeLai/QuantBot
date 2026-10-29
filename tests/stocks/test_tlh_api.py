"""TLH API 端点测试（Phase F.3.2）."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantbot.stocks.api.tlh import router

app = FastAPI()
app.include_router(router)
app.include_router(router, prefix="/api")

client = TestClient(app)

# 使用实际今天的日期，确保测试不依赖硬编码年份
_TODAY = date.today()


def _scan_body(
    days_held: int = 60,
    ticker: str = "AAPL",
    qty: float = 100.0,
    cost: float = 150.0,
    price: float = 120.0,
    recent_purchases: dict | None = None,
) -> dict:
    return {
        "lots": [
            {
                "ticker": ticker,
                "quantity": qty,
                "cost_basis": cost,
                "acquisition_date": str(_TODAY - timedelta(days=days_held)),
                "lot_id": "L001",
            }
        ],
        "current_prices": {ticker: price},
        "recent_purchases": recent_purchases or {},
    }


# ---------------------------------------------------------------------------
# POST /tlh/scan
# ---------------------------------------------------------------------------


class TestTLHScan:
    def test_scan_returns_candidate(self) -> None:
        body = _scan_body(days_held=60)  # short-term lot
        resp = client.post("/tlh/scan", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["candidates"]) == 1
        c = data["candidates"][0]
        assert c["ticker"] == "AAPL"
        assert c["unrealized_pnl"] == pytest.approx(-3000.0, abs=0.01)
        assert c["wash_sale_risk"] is False

    def test_scan_returns_estimated_saving(self) -> None:
        body = _scan_body(days_held=60)   # short-term: rate 0.37
        resp = client.post("/tlh/scan", json=body)
        data = resp.json()
        # 短期亏损 $3000 × 37% ≈ $1110
        assert data["estimated_tax_saving"] == pytest.approx(1110.0, abs=0.01)

    def test_scan_no_candidates_above_threshold(self) -> None:
        """没有满足条件的 lot 时返回空列表."""
        body = _scan_body(days_held=60, price=148.0)  # 跌幅仅 1.3%
        resp = client.post("/tlh/scan", json=body)
        assert resp.status_code == 200
        assert resp.json()["candidates"] == []

    def test_scan_wash_sale_flag(self) -> None:
        """近 30 天内有买入记录时应标记 wash_sale_risk."""
        recent = {"AAPL": str(_TODAY - timedelta(days=10))}
        body = _scan_body(days_held=60, recent_purchases=recent)
        resp = client.post("/tlh/scan", json=body)
        c = resp.json()["candidates"][0]
        assert c["wash_sale_risk"] is True

    def test_scan_generated_at_present(self) -> None:
        resp = client.post("/tlh/scan", json=_scan_body())
        assert "generated_at" in resp.json()

    def test_scan_via_api_prefix(self) -> None:
        resp = client.post("/api/tlh/scan", json=_scan_body())
        assert resp.status_code == 200

    def test_scan_long_term_lot(self) -> None:
        """持有 400 天的 lot 应被标记为长期."""
        body = _scan_body(days_held=400)
        resp = client.post("/tlh/scan", json=body)
        assert resp.json()["candidates"][0]["is_long_term"] is True

    def test_scan_short_term_lot(self) -> None:
        """持有 60 天应标记为短期."""
        body = _scan_body(days_held=60)
        resp = client.post("/tlh/scan", json=body)
        assert resp.json()["candidates"][0]["is_long_term"] is False


# ---------------------------------------------------------------------------
# GET /tlh/replacement
# ---------------------------------------------------------------------------


class TestTLHReplacement:
    def test_spy_replacement(self) -> None:
        resp = client.get("/tlh/replacement?ticker=SPY")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticker"] == "SPY"
        assert "VOO" in data["replacements"]

    def test_unknown_ticker_empty_list(self) -> None:
        resp = client.get("/tlh/replacement?ticker=ZZZZZ")
        assert resp.status_code == 200
        assert resp.json()["replacements"] == []

    def test_lowercase_ticker_normalized(self) -> None:
        resp = client.get("/tlh/replacement?ticker=qqq")
        assert resp.json()["ticker"] == "QQQ"
        assert len(resp.json()["replacements"]) > 0


# ---------------------------------------------------------------------------
# POST /tlh/estimate-saving
# ---------------------------------------------------------------------------


class TestTLHEstimateSaving:
    def _get_candidates(self, days_held: int = 60) -> list[dict]:
        resp = client.post("/tlh/scan", json=_scan_body(days_held=days_held))
        return resp.json()["candidates"]

    def test_estimate_saving_short_term(self) -> None:
        candidates = self._get_candidates(days_held=60)   # short-term
        body = {
            "candidates": candidates,
            "short_term_rate": 0.30,
            "long_term_rate": 0.15,
        }
        resp = client.post("/tlh/estimate-saving", json=body)
        assert resp.status_code == 200
        data = resp.json()
        # 短期亏损 $3000 × 30% = $900
        assert data["tax_saving_usd"] == pytest.approx(900.0, abs=0.01)
        assert len(data["details"]) == 1

    def test_estimate_saving_long_term(self) -> None:
        candidates = self._get_candidates(days_held=400)  # long-term
        body = {
            "candidates": candidates,
            "short_term_rate": 0.37,
            "long_term_rate": 0.20,
        }
        resp = client.post("/tlh/estimate-saving", json=body)
        data = resp.json()
        # 长期亏损 $3000 × 20% = $600
        assert data["tax_saving_usd"] == pytest.approx(600.0, abs=0.01)

    def test_estimate_saving_returns_details(self) -> None:
        candidates = self._get_candidates()
        body = {"candidates": candidates, "short_term_rate": 0.37, "long_term_rate": 0.20}
        resp = client.post("/tlh/estimate-saving", json=body)
        detail = resp.json()["details"][0]
        assert "ticker" in detail
        assert "saving_usd" in detail
        assert "tax_rate" in detail

    def test_estimate_saving_empty_candidates(self) -> None:
        body = {"candidates": [], "short_term_rate": 0.37, "long_term_rate": 0.20}
        resp = client.post("/tlh/estimate-saving", json=body)
        assert resp.status_code == 200
        assert resp.json()["tax_saving_usd"] == 0.0
