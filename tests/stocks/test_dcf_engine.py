"""Unit tests for DCF + Monte Carlo engine.

All yfinance calls are mocked. No real network access.
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.dcf.engine import (
    DCFResult,
    WACCComponents,
    _npv,
    _valuation_label,
    compute_dcf,
    compute_wacc,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_income(
    interest: float = 200_000_000,
    pretax: float = 5_000_000_000,
    tax: float = 1_000_000_000,
) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {dates[0]: {"Interest Expense": -interest, "Pretax Income": pretax, "Tax Provision": tax},
         dates[1]: {"Interest Expense": -interest, "Pretax Income": pretax * 0.9, "Tax Provision": tax * 0.9}},
    )


def _make_balance(debt: float = 4_000_000_000) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {dates[0]: {"Total Debt": debt, "Total Assets": 50_000_000_000},
         dates[1]: {"Total Debt": debt, "Total Assets": 45_000_000_000}},
    )


def _make_cashflow(fcf: float = 3_000_000_000) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31"),
             pd.Timestamp("2022-12-31"), pd.Timestamp("2021-12-31")]
    return pd.DataFrame(
        {d: {"Free Cash Flow": fcf * (0.9 ** i)} for i, d in enumerate(dates)}
    )


def _mock_ticker(
    income, balance, cashflow,
    info: dict | None = None,
):
    t = MagicMock()
    t.financials = income
    t.balance_sheet = balance
    t.cashflow = cashflow
    t.info = info or {
        "beta": 1.2,
        "marketCap": 3_000_000_000_000,
        "currentPrice": 185.0,
        "sharesOutstanding": 15_000_000_000,
        "fiftyTwoWeekHigh": 200.0,
        "earningsGrowth": 0.08,
    }
    return t


# ---------------------------------------------------------------------------
# _npv
# ---------------------------------------------------------------------------


class TestNpv:
    def test_single_cashflow(self):
        # 100 discounted 1 year at 10% = 90.91
        assert _npv([100.0], 0.10) == pytest.approx(100 / 1.10)

    def test_zero_rate(self):
        assert _npv([100.0, 100.0], 0.0) == pytest.approx(200.0)

    def test_multiple_cashflows(self):
        cfs = [110.0, 121.0]  # 10% growth
        expected = 110 / 1.10 + 121 / 1.21
        assert _npv(cfs, 0.10) == pytest.approx(expected, rel=1e-6)


# ---------------------------------------------------------------------------
# _valuation_label
# ---------------------------------------------------------------------------


class TestValuationLabel:
    def test_deep_value(self):
        assert _valuation_label(0.35) == "deep_value"

    def test_undervalued(self):
        assert _valuation_label(0.20) == "undervalued"

    def test_fair(self):
        assert _valuation_label(0.0) == "fair"

    def test_overvalued(self):
        assert _valuation_label(-0.20) == "overvalued"

    def test_overheated(self):
        assert _valuation_label(-0.50) == "overheated"


# ---------------------------------------------------------------------------
# compute_wacc
# ---------------------------------------------------------------------------


class TestComputeWacc:
    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_returns_wacc_components(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_wacc("AAPL")
        assert isinstance(result, WACCComponents)

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_wacc_is_finite_positive(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_wacc("AAPL")
        assert r is not None
        assert 0 < r.wacc < 1

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_weights_sum_to_one(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_wacc("AAPL")
        assert r is not None
        assert r.debt_weight + r.equity_weight == pytest.approx(1.0, abs=1e-4)

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_returns_none_for_empty_statements(self, mock_yf):
        t = MagicMock()
        t.financials = pd.DataFrame()
        t.balance_sheet = _make_balance()
        t.info = {"marketCap": 1e12}
        mock_yf.return_value = t
        assert compute_wacc("X") is None

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("timeout")
        assert compute_wacc("ERR") is None


# ---------------------------------------------------------------------------
# compute_dcf
# ---------------------------------------------------------------------------


class TestComputeDcf:
    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_returns_dcf_result(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_dcf("AAPL")
        assert isinstance(result, DCFResult)

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_fair_values_ordered(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_dcf("AAPL")
        assert r is not None
        assert r.fair_value_p5 <= r.fair_value_p50 <= r.fair_value_p95

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_fair_values_positive(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_dcf("AAPL")
        assert r is not None
        assert r.fair_value_p5 > 0
        assert r.fair_value_p50 > 0

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_projected_fcfs_length(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_dcf("AAPL", projection_years=5)
        assert r is not None
        assert len(r.projected_fcfs) == 5

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_margin_of_safety_is_finite(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_dcf("AAPL")
        assert r is not None
        assert math.isfinite(r.margin_of_safety)

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_valuation_label_valid(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_dcf("AAPL")
        assert r is not None
        assert r.valuation in {
            "deep_value", "undervalued", "fair", "overvalued", "overheated"
        }

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_returns_none_no_cashflow(self, mock_yf):
        t = MagicMock()
        t.cashflow = pd.DataFrame()
        t.info = {"marketCap": 1e12, "currentPrice": 100.0, "sharesOutstanding": 1e9}
        t.financials = _make_income()
        t.balance_sheet = _make_balance()
        mock_yf.return_value = t
        assert compute_dcf("NOCF") is None

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network failure")
        assert compute_dcf("ERR") is None

    @patch("quantbot.stocks.dcf.engine.yf.Ticker")
    def test_as_of_date_is_today(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        r = compute_dcf("AAPL")
        assert r is not None
        assert r.as_of_date == date.today()
