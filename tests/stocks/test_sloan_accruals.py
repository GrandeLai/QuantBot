"""Unit tests for Sloan Accruals engine and API endpoint."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.quant_signals.engine import (
    SloanAccruals,
    _sloan_grade,
    _sloan_interpretation,
    compute_sloan_accruals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_income(net_income: float = 5_000_000_000) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {dates[0]: {"Net Income": net_income},
         dates[1]: {"Net Income": net_income * 0.9}},
    )


def _make_cashflow(ocf: float = 7_000_000_000) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {dates[0]: {"Operating Cash Flow": ocf},
         dates[1]: {"Operating Cash Flow": ocf * 0.9}},
    )


def _make_balance(total_assets: float = 50_000_000_000) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {dates[0]: {"Total Assets": total_assets},
         dates[1]: {"Total Assets": total_assets * 0.9}},
    )


def _mock_ticker(ni: float = 5e9, ocf: float = 7e9, ta: float = 50e9) -> MagicMock:
    t = MagicMock()
    t.financials = _make_income(ni)
    t.cashflow = _make_cashflow(ocf)
    t.balance_sheet = _make_balance(ta)
    t.info = {}
    return t


# ---------------------------------------------------------------------------
# _sloan_grade
# ---------------------------------------------------------------------------


class TestSloanGrade:
    def test_low_accrual(self):
        assert _sloan_grade(-0.15) == "low_accrual"

    def test_normal_negative(self):
        assert _sloan_grade(-0.05) == "normal"

    def test_normal_zero(self):
        assert _sloan_grade(0.0) == "normal"

    def test_elevated_accrual(self):
        assert _sloan_grade(0.07) == "elevated_accrual"

    def test_high_accrual(self):
        assert _sloan_grade(0.15) == "high_accrual"

    def test_boundary_low(self):
        assert _sloan_grade(-0.10) == "normal"   # exactly -0.10 is normal (< not <=)

    def test_boundary_elevated(self):
        assert _sloan_grade(0.05) == "elevated_accrual"  # 0.05 is elevated

    def test_boundary_high(self):
        assert _sloan_grade(0.10) == "high_accrual"      # 0.10 is high


# ---------------------------------------------------------------------------
# _sloan_interpretation
# ---------------------------------------------------------------------------


class TestSloanInterpretation:
    def test_low_accrual_interpretation(self):
        interp = _sloan_interpretation(-0.15, "low_accrual")
        assert "应计率" in interp
        assert "低_accrual" not in interp  # should be Chinese

    def test_high_accrual_has_warning(self):
        interp = _sloan_interpretation(0.12, "high_accrual")
        assert "⚠" in interp or "高" in interp


# ---------------------------------------------------------------------------
# compute_sloan_accruals
# ---------------------------------------------------------------------------


class TestComputeSloanAccruals:
    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_sloan_accruals(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_sloan_accruals("AAPL")
        assert isinstance(result, SloanAccruals)
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_accrual_ratio_computed(self, mock_yf):
        """ni=5B, ocf=7B, avg_ta=(50+45)/2=47.5B → ratio=(5-7)/47.5 ≈ -0.0421"""
        mock_yf.return_value = _mock_ticker(ni=5e9, ocf=7e9, ta=50e9)
        result = compute_sloan_accruals("AAPL")
        assert result is not None
        # avg_ta = (50e9 + 45e9) / 2 = 47.5e9
        expected = (5e9 - 7e9) / 47.5e9
        assert result.accrual_ratio == pytest.approx(expected, rel=1e-3)

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_grade_is_valid_literal(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_sloan_accruals("AAPL")
        assert result is not None
        assert result.grade in {"low_accrual", "normal", "elevated_accrual", "high_accrual"}

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_high_accrual_detected(self, mock_yf):
        """ni=10B, ocf=2B, avg_ta=20B → ratio=8/20=0.40 → high_accrual"""
        mock_yf.return_value = _mock_ticker(ni=10e9, ocf=2e9, ta=20e9)
        result = compute_sloan_accruals("AAPL")
        assert result is not None
        assert result.grade == "high_accrual"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_low_accrual_detected(self, mock_yf):
        """ni=1B, ocf=15B, avg_ta=20B → ratio=(1-15)/18.5 ≈ -0.76 → low_accrual"""
        mock_yf.return_value = _mock_ticker(ni=1e9, ocf=15e9, ta=20e9)
        result = compute_sloan_accruals("AAPL")
        assert result is not None
        assert result.grade == "low_accrual"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_as_of_date_is_today(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_sloan_accruals("AAPL")
        assert result is not None
        assert result.as_of_date <= date.today()

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_on_empty_cashflow(self, mock_yf):
        t = MagicMock()
        t.financials = _make_income()
        t.cashflow = pd.DataFrame()
        t.balance_sheet = _make_balance()
        mock_yf.return_value = t
        assert compute_sloan_accruals("X") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network")
        assert compute_sloan_accruals("ERR") is None


# ---------------------------------------------------------------------------
# API: GET /api/quant-signals/sloan
# ---------------------------------------------------------------------------

_MOCK_SLOAN = SloanAccruals(
    ticker="AAPL",
    accrual_ratio=-0.042,
    grade="normal",
    net_income=5_000_000_000.0,
    operating_cash_flow=7_000_000_000.0,
    avg_total_assets=47_500_000_000.0,
    interpretation="应计率 -0.042：正常区间，盈利质量健康。",
    as_of_date=date(2026, 4, 29),
)


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


class TestSloanAPIEndpoint:
    @patch("quantbot.stocks.api.quant_signals.compute_sloan_accruals", return_value=_MOCK_SLOAN)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/quant-signals/sloan?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.quant_signals.compute_sloan_accruals", return_value=_MOCK_SLOAN)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/quant-signals/sloan?ticker=AAPL").json()
        assert "accrual_ratio" in data
        assert "grade" in data
        assert "interpretation" in data
        assert "net_income" in data

    @patch("quantbot.stocks.api.quant_signals.compute_sloan_accruals", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/quant-signals/sloan?ticker=NODATA")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/quant-signals/sloan").status_code == 422

    @patch("quantbot.stocks.api.quant_signals.compute_sloan_accruals", return_value=_MOCK_SLOAN)
    def test_grade_is_valid(self, mock_fn, client):
        data = client.get("/api/quant-signals/sloan?ticker=AAPL").json()
        assert data["grade"] in {"low_accrual", "normal", "elevated_accrual", "high_accrual"}

    @patch("quantbot.stocks.api.quant_signals.compute_sloan_accruals", return_value=_MOCK_SLOAN)
    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=None)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=None)
    def test_sloan_in_summary(self, mock_r, mock_b, mock_s, client):
        data = client.get("/api/quant-signals/summary?ticker=AAPL").json()
        assert "sloan" in data
        assert data["sloan"] is not None
        assert data["sloan"]["accrual_ratio"] == pytest.approx(-0.042)
