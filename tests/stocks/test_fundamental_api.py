"""基本面信号 API 端点测试（Phase F.4.2）."""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from quantbot.stocks.api.fundamental import router
from quantbot.stocks.fundamental.engine import (
    EarningsSurprise,
    PEADSignal,
    PiotroskiScore,
)

app = FastAPI()
app.include_router(router)
app.include_router(router, prefix="/api")

client = TestClient(app)


# ---------------------------------------------------------------------------
# 测试固件
# ---------------------------------------------------------------------------

_MOCK_SURPRISE = EarningsSurprise(
    ticker="AAPL",
    quarter=date(2024, 12, 31),
    eps_actual=1.85,
    eps_estimate=1.77,
    eps_difference=0.08,
    surprise_pct=0.045,
)

_MOCK_PEAD = PEADSignal(
    ticker="AAPL",
    latest_surprise=_MOCK_SURPRISE,
    surprise_magnitude="beat",
    historical_drift_7d=1.2,
    historical_drift_30d=3.8,
    historical_drift_60d=6.1,
    signal_strength=0.55,
)

_MOCK_PIOTROSKI = PiotroskiScore(
    ticker="AAPL",
    score=7,
    grade="strong",
    signals={
        "F1_roa_positive": True,
        "F2_operating_cashflow_positive": True,
        "F3_roa_improving": True,
        "F4_accruals_low": True,
        "F5_leverage_decreasing": True,
        "F6_current_ratio_improving": False,
        "F7_no_dilution": True,
        "F8_gross_margin_improving": False,
        "F9_asset_turnover_improving": True,
    },
    as_of_date=date(2025, 1, 15),
    interpretation="财务健康优质（F-Score 7/9）",
)


# ---------------------------------------------------------------------------
# GET /fundamental/pead
# ---------------------------------------------------------------------------


class TestPEADEndpoint:
    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_pead_returns_200(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/pead?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_pead_response_shape(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/pead?ticker=AAPL")
        data = resp.json()
        assert data["ticker"] == "AAPL"
        assert data["surprise_magnitude"] == "beat"
        assert "historical_drift_30d" in data
        assert "latest_surprise" in data

    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=None)
    def test_pead_404_when_no_data(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/pead?ticker=ZZZZZ")
        assert resp.status_code == 404

    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_pead_via_api_prefix(self, _: MagicMock) -> None:
        resp = client.get("/api/fundamental/pead?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_pead_signal_strength_range(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/pead?ticker=AAPL")
        s = resp.json()["signal_strength"]
        assert 0.0 <= s <= 1.0


# ---------------------------------------------------------------------------
# GET /fundamental/piotroski
# ---------------------------------------------------------------------------


class TestPiotroskiEndpoint:
    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=_MOCK_PIOTROSKI)
    def test_piotroski_returns_200(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/piotroski?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=_MOCK_PIOTROSKI)
    def test_piotroski_score_and_grade(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/piotroski?ticker=AAPL")
        data = resp.json()
        assert data["score"] == 7
        assert data["grade"] == "strong"
        assert len(data["signals"]) == 9

    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=None)
    def test_piotroski_404_when_no_data(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/piotroski?ticker=ZZZZZ")
        assert resp.status_code == 404

    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=_MOCK_PIOTROSKI)
    def test_piotroski_interpretation_present(self, _: MagicMock) -> None:
        resp = client.get("/fundamental/piotroski?ticker=AAPL")
        data = resp.json()
        assert "interpretation" in data
        assert len(data["interpretation"]) > 0


# ---------------------------------------------------------------------------
# GET /fundamental/summary
# ---------------------------------------------------------------------------


class TestSummaryEndpoint:
    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=_MOCK_PEAD)
    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=_MOCK_PIOTROSKI)
    def test_summary_returns_both(self, _p: MagicMock, _pead: MagicMock) -> None:
        resp = client.get("/fundamental/summary?ticker=AAPL")
        assert resp.status_code == 200
        data = resp.json()
        assert "pead" in data
        assert "piotroski" in data
        assert data["ticker"] == "AAPL"

    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=None)
    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=None)
    def test_summary_404_when_both_fail(self, _p: MagicMock, _pead: MagicMock) -> None:
        resp = client.get("/fundamental/summary?ticker=ZZZZZ")
        assert resp.status_code == 404

    @patch("quantbot.stocks.api.fundamental.compute_pead_signal", return_value=_MOCK_PEAD)
    @patch("quantbot.stocks.api.fundamental.compute_piotroski_fscore", return_value=None)
    def test_summary_partial_ok(self, _p: MagicMock, _pead: MagicMock) -> None:
        """piotroski 失败时 pead 仍然返回，不报错."""
        resp = client.get("/fundamental/summary?ticker=AAPL")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pead"] is not None
        assert data["piotroski"] is None
