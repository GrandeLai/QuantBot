"""Unit tests for Piotroski F-Score engine and API endpoint."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.quant_signals.engine import (
    PiotroskiCriteria,
    PiotroskiScore,
    _piotroski_grade,
    _piotroski_interpretation,
    compute_piotroski_score,
)


# ---------------------------------------------------------------------------
# Mock data helpers
# ---------------------------------------------------------------------------

def _col(year: int = 2024) -> pd.Timestamp:
    return pd.Timestamp(f"{year}-12-31")


def _make_income(
    ni_t: float = 5e9,
    ni_t1: float = 4e9,
    rev_t: float = 30e9,
    rev_t1: float = 28e9,
    gp_t: float = 12e9,
    gp_t1: float = 10e9,
) -> pd.DataFrame:
    c0, c1 = _col(2024), _col(2023)
    return pd.DataFrame({
        c0: {"Net Income": ni_t, "Total Revenue": rev_t, "Gross Profit": gp_t},
        c1: {"Net Income": ni_t1, "Total Revenue": rev_t1, "Gross Profit": gp_t1},
    })


def _make_balance(
    ta_t: float = 50e9,
    ta_t1: float = 45e9,
    ltd_t: float = 5e9,
    ltd_t1: float = 7e9,
    ca_t: float = 15e9,
    ca_t1: float = 12e9,
    cl_t: float = 8e9,
    cl_t1: float = 7e9,
    shares_t: float = 1e9,
    shares_t1: float = 1e9,
) -> pd.DataFrame:
    c0, c1 = _col(2024), _col(2023)
    return pd.DataFrame({
        c0: {
            "Total Assets": ta_t,
            "Long Term Debt": ltd_t,
            "Current Assets": ca_t,
            "Current Liabilities": cl_t,
            "Common Stock Shares Outstanding": shares_t,
        },
        c1: {
            "Total Assets": ta_t1,
            "Long Term Debt": ltd_t1,
            "Current Assets": ca_t1,
            "Current Liabilities": cl_t1,
            "Common Stock Shares Outstanding": shares_t1,
        },
    })


def _make_cashflow(ocf: float = 8e9) -> pd.DataFrame:
    c0, c1 = _col(2024), _col(2023)
    return pd.DataFrame({
        c0: {"Operating Cash Flow": ocf},
        c1: {"Operating Cash Flow": ocf * 0.9},
    })


def _mock_ticker(
    ni_t: float = 5e9,
    ni_t1: float = 4e9,
    ocf: float = 8e9,
    ta_t: float = 50e9,
    ta_t1: float = 45e9,
    ltd_t: float = 5e9,
    ltd_t1: float = 7e9,
) -> MagicMock:
    t = MagicMock()
    t.financials = _make_income(ni_t=ni_t, ni_t1=ni_t1)
    t.cashflow = _make_cashflow(ocf=ocf)
    t.balance_sheet = _make_balance(ta_t=ta_t, ta_t1=ta_t1, ltd_t=ltd_t, ltd_t1=ltd_t1)
    t.info = {}
    return t


# ---------------------------------------------------------------------------
# _piotroski_grade
# ---------------------------------------------------------------------------


class TestPiotroskiGrade:
    def test_strong_9(self):
        assert _piotroski_grade(9) == "strong"

    def test_strong_7(self):
        assert _piotroski_grade(7) == "strong"

    def test_neutral_6(self):
        assert _piotroski_grade(6) == "neutral"

    def test_neutral_4(self):
        assert _piotroski_grade(4) == "neutral"

    def test_weak_3(self):
        assert _piotroski_grade(3) == "weak"

    def test_weak_0(self):
        assert _piotroski_grade(0) == "weak"

    def test_boundary_strong(self):
        # exactly 7 is strong
        assert _piotroski_grade(7) == "strong"

    def test_boundary_weak(self):
        # exactly 3 is weak
        assert _piotroski_grade(3) == "weak"

    def test_boundary_neutral_low(self):
        # exactly 4 is neutral
        assert _piotroski_grade(4) == "neutral"


# ---------------------------------------------------------------------------
# _piotroski_interpretation
# ---------------------------------------------------------------------------


class TestPiotroskiInterpretation:
    def test_strong_mentions_score(self):
        interp = _piotroski_interpretation(8, "strong")
        assert "8/9" in interp

    def test_weak_has_warning(self):
        interp = _piotroski_interpretation(2, "weak")
        assert "⚠" in interp or "弱" in interp or "低" in interp

    def test_neutral_text(self):
        interp = _piotroski_interpretation(5, "neutral")
        assert "5/9" in interp or "中性" in interp


# ---------------------------------------------------------------------------
# compute_piotroski_score
# ---------------------------------------------------------------------------


class TestComputePiotroskiScore:
    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_piotroski_score(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_piotroski_score("AAPL")
        assert isinstance(result, PiotroskiScore)
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_f_score_in_range(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert 0 <= result.f_score <= 9

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_grade_valid_literal(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert result.grade in {"strong", "neutral", "weak"}

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_criteria_are_bool(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_piotroski_score("AAPL")
        assert result is not None
        c = result.criteria
        for attr in (
            "roa_positive", "cfo_positive", "roa_improving", "accruals_ok",
            "leverage_ok", "liquidity_ok", "no_dilution", "margin_ok", "turnover_ok",
        ):
            assert isinstance(getattr(c, attr), bool), f"{attr} should be bool"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_f_score_equals_sum_of_criteria(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_piotroski_score("AAPL")
        assert result is not None
        c = result.criteria
        expected = sum([
            c.roa_positive, c.cfo_positive, c.roa_improving, c.accruals_ok,
            c.leverage_ok, c.liquidity_ok, c.no_dilution, c.margin_ok, c.turnover_ok,
        ])
        assert result.f_score == expected

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_roa_positive_true_when_ni_positive(self, mock_yf):
        """NI > 0 and TA > 0 → ROA > 0 → F1 = True."""
        mock_yf.return_value = _mock_ticker(ni_t=5e9, ta_t=50e9)
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert result.criteria.roa_positive is True

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_roa_positive_false_when_ni_negative(self, mock_yf):
        """NI < 0 → ROA < 0 → F1 = False."""
        mock_yf.return_value = _mock_ticker(ni_t=-1e9, ta_t=50e9)
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert result.criteria.roa_positive is False

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_leverage_ok_when_ltd_ratio_decreased(self, mock_yf):
        """ltd_t=5B < ltd_t1=7B (both on ~47.5B avg TA) → leverage improved → F5 = True."""
        mock_yf.return_value = _mock_ticker(ltd_t=5e9, ltd_t1=7e9, ta_t=50e9, ta_t1=45e9)
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert result.criteria.leverage_ok is True

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_leverage_bad_when_ltd_ratio_increased(self, mock_yf):
        """ltd_t=10B > ltd_t1=5B (ratio increased) → F5 = False."""
        mock_yf.return_value = _mock_ticker(ltd_t=10e9, ltd_t1=5e9, ta_t=50e9, ta_t1=45e9)
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert result.criteria.leverage_ok is False

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_as_of_date_is_reasonable(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_piotroski_score("AAPL")
        assert result is not None
        assert result.as_of_date <= date.today()

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_on_empty_balance(self, mock_yf):
        t = MagicMock()
        t.financials = _make_income()
        t.cashflow = _make_cashflow()
        t.balance_sheet = pd.DataFrame()
        t.info = {}
        mock_yf.return_value = t
        assert compute_piotroski_score("X") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network")
        assert compute_piotroski_score("ERR") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_when_only_one_period(self, mock_yf):
        """Only 1 column of balance sheet → cannot compute prior-year comparisons."""
        t = MagicMock()
        t.financials = _make_income()
        t.cashflow = _make_cashflow()
        # Single-column balance sheet
        t.balance_sheet = pd.DataFrame({
            _col(2024): {"Total Assets": 50e9},
        })
        t.info = {}
        mock_yf.return_value = t
        assert compute_piotroski_score("X") is None


# ---------------------------------------------------------------------------
# API: GET /api/quant-signals/piotroski
# ---------------------------------------------------------------------------

_MOCK_CRITERIA = PiotroskiCriteria(
    roa_positive=True,
    cfo_positive=True,
    roa_improving=True,
    accruals_ok=True,
    leverage_ok=True,
    liquidity_ok=True,
    no_dilution=True,
    margin_ok=False,
    turnover_ok=False,
)

_MOCK_PIOTROSKI = PiotroskiScore(
    ticker="AAPL",
    f_score=7,
    grade="strong",
    criteria=_MOCK_CRITERIA,
    interpretation="F-Score 7/9（强）：...",
    as_of_date=date(2026, 4, 29),
)


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


class TestPiotroskiAPIEndpoint:
    @patch("quantbot.stocks.api.quant_signals.compute_piotroski_score", return_value=_MOCK_PIOTROSKI)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/quant-signals/piotroski?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.quant_signals.compute_piotroski_score", return_value=_MOCK_PIOTROSKI)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/quant-signals/piotroski?ticker=AAPL").json()
        assert "f_score" in data
        assert "grade" in data
        assert "criteria" in data
        assert "interpretation" in data

    @patch("quantbot.stocks.api.quant_signals.compute_piotroski_score", return_value=_MOCK_PIOTROSKI)
    def test_criteria_has_all_9_fields(self, mock_fn, client):
        data = client.get("/api/quant-signals/piotroski?ticker=AAPL").json()
        criteria = data["criteria"]
        for field in (
            "roa_positive", "cfo_positive", "roa_improving", "accruals_ok",
            "leverage_ok", "liquidity_ok", "no_dilution", "margin_ok", "turnover_ok",
        ):
            assert field in criteria, f"Missing criteria field: {field}"

    @patch("quantbot.stocks.api.quant_signals.compute_piotroski_score", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/quant-signals/piotroski?ticker=NODATA")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/quant-signals/piotroski").status_code == 422

    @patch("quantbot.stocks.api.quant_signals.compute_piotroski_score", return_value=_MOCK_PIOTROSKI)
    def test_grade_is_valid(self, mock_fn, client):
        data = client.get("/api/quant-signals/piotroski?ticker=AAPL").json()
        assert data["grade"] in {"strong", "neutral", "weak"}

    @patch("quantbot.stocks.api.quant_signals.compute_piotroski_score", return_value=_MOCK_PIOTROSKI)
    @patch("quantbot.stocks.api.quant_signals.compute_beneish_mscore", return_value=None)
    @patch("quantbot.stocks.api.quant_signals.estimate_russell_membership", return_value=None)
    @patch("quantbot.stocks.api.quant_signals.compute_sloan_accruals", return_value=None)
    def test_piotroski_in_summary(self, mock_s, mock_r, mock_b, mock_p, client):
        data = client.get("/api/quant-signals/summary?ticker=AAPL").json()
        assert "piotroski" in data
        assert data["piotroski"] is not None
        assert data["piotroski"]["f_score"] == 7
        assert data["piotroski"]["grade"] == "strong"
