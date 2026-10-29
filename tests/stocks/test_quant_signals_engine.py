"""Unit tests for quant_signals engine — Beneish M-Score + Russell membership.

Tests are fully mocked (no real yfinance network calls).
"""

from __future__ import annotations

import math
from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.quant_signals.engine import (
    BeneishMScore,
    RussellMembership,
    _estimate_rank_from_cap,
    _risk_level,
    _safe_div,
    compute_beneish_mscore,
    estimate_russell_membership,
)


# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------


def _make_income(
    revenue: tuple[float, float] = (10_000_000, 9_000_000),
    cogs: tuple[float, float] = (6_000_000, 5_400_000),
    sga: tuple[float, float] = (1_000_000, 900_000),
    ni: tuple[float, float] = (1_500_000, 1_200_000),
) -> pd.DataFrame:
    """Two-period income statement: col 0 = current, col 1 = prior."""
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {
            dates[0]: {
                "Total Revenue": revenue[0],
                "Cost Of Revenue": cogs[0],
                "Selling General Administrative": sga[0],
                "Net Income": ni[0],
            },
            dates[1]: {
                "Total Revenue": revenue[1],
                "Cost Of Revenue": cogs[1],
                "Selling General Administrative": sga[1],
                "Net Income": ni[1],
            },
        }
    )


def _make_balance(
    ar: tuple[float, float] = (800_000, 700_000),
    ppe: tuple[float, float] = (3_000_000, 3_200_000),
    ta: tuple[float, float] = (20_000_000, 18_000_000),
    ca: tuple[float, float] = (5_000_000, 4_500_000),
    ltd: tuple[float, float] = (4_000_000, 4_000_000),
    cl: tuple[float, float] = (2_000_000, 1_800_000),
) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {
            dates[0]: {
                "Net Receivables": ar[0],
                "Net PPE": ppe[0],
                "Total Assets": ta[0],
                "Current Assets": ca[0],
                "Long Term Debt": ltd[0],
                "Current Liabilities": cl[0],
            },
            dates[1]: {
                "Net Receivables": ar[1],
                "Net PPE": ppe[1],
                "Total Assets": ta[1],
                "Current Assets": ca[1],
                "Long Term Debt": ltd[1],
                "Current Liabilities": cl[1],
            },
        }
    )


def _make_cashflow(
    dep: tuple[float, float] = (500_000, 480_000),
    cfo: tuple[float, float] = (1_800_000, 1_500_000),
) -> pd.DataFrame:
    dates = [pd.Timestamp("2024-12-31"), pd.Timestamp("2023-12-31")]
    return pd.DataFrame(
        {
            dates[0]: {
                "Depreciation": dep[0],
                "Operating Cash Flow": cfo[0],
            },
            dates[1]: {
                "Depreciation": dep[1],
                "Operating Cash Flow": cfo[1],
            },
        }
    )


def _mock_ticker(income, balance, cashflow):
    t = MagicMock()
    t.financials = income
    t.balance_sheet = balance
    t.cashflow = cashflow
    return t


# ---------------------------------------------------------------------------
# _safe_div
# ---------------------------------------------------------------------------


class TestSafeDiv:
    def test_normal_division(self):
        assert _safe_div(10.0, 4.0) == pytest.approx(2.5)

    def test_zero_denominator_returns_default(self):
        assert _safe_div(10.0, 0.0) == 1.0

    def test_custom_default(self):
        assert _safe_div(5.0, 0.0, default=99.0) == 99.0

    def test_nan_inputs(self):
        assert _safe_div(float("nan"), 2.0) == 1.0
        assert _safe_div(2.0, float("nan")) == 1.0


# ---------------------------------------------------------------------------
# _risk_level
# ---------------------------------------------------------------------------


class TestRiskLevel:
    def test_safe(self):
        assert _risk_level(-3.0) == "safe"

    def test_grey_lower(self):
        assert _risk_level(-2.22) == "grey"

    def test_grey_upper(self):
        assert _risk_level(-1.78) == "grey"

    def test_manipulator(self):
        assert _risk_level(-1.5) == "manipulator"

    def test_boundary_exactly_minus_2_22(self):
        # Exactly -2.22 is NOT < -2.22, so it's grey
        assert _risk_level(-2.22) == "grey"


# ---------------------------------------------------------------------------
# compute_beneish_mscore — happy path
# ---------------------------------------------------------------------------


class TestComputeBeneishMScore:
    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_beneish_mscore_dataclass(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_beneish_mscore("AAPL")
        assert isinstance(result, BeneishMScore)
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_ratios_keys_present(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_beneish_mscore("MSFT")
        assert result is not None
        expected_keys = {"DSRI", "GMI", "AQI", "SGI", "DEPI", "SGAI", "LVGI", "TATA"}
        assert set(result.ratios.keys()) == expected_keys

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_m_score_is_finite(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_beneish_mscore("TSLA")
        assert result is not None
        assert math.isfinite(result.m_score)

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_risk_level_matches_score(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_beneish_mscore("AAPL")
        assert result is not None
        assert result.risk_level in {"safe", "grey", "manipulator"}
        if result.m_score < -2.22:
            assert result.risk_level == "safe"
        elif result.m_score <= -1.78:
            assert result.risk_level == "grey"
        else:
            assert result.risk_level == "manipulator"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_interpretation_non_empty(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_beneish_mscore("NVDA")
        assert result is not None
        assert len(result.interpretation) > 0

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_as_of_date_is_date(self, mock_yf):
        mock_yf.return_value = _mock_ticker(
            _make_income(), _make_balance(), _make_cashflow()
        )
        result = compute_beneish_mscore("GOOG")
        assert result is not None
        assert isinstance(result.as_of_date, date)


# ---------------------------------------------------------------------------
# compute_beneish_mscore — failure / edge cases
# ---------------------------------------------------------------------------


class TestComputeBeneishMScoreEdgeCases:
    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_for_empty_income(self, mock_yf):
        t = MagicMock()
        t.financials = pd.DataFrame()
        t.balance_sheet = _make_balance()
        t.cashflow = _make_cashflow()
        mock_yf.return_value = t
        assert compute_beneish_mscore("FAKE") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_for_single_period(self, mock_yf):
        """Only 1 period — cannot compute ratio changes."""
        one_col = pd.DataFrame({pd.Timestamp("2024-12-31"): {"Total Revenue": 1e7, "Net Income": 1e6}})
        t = MagicMock()
        t.financials = one_col
        t.balance_sheet = _make_balance()
        t.cashflow = _make_cashflow()
        mock_yf.return_value = t
        assert compute_beneish_mscore("X") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network failure")
        assert compute_beneish_mscore("ERR") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_high_tata_produces_manipulator(self, mock_yf):
        """TATA = (NI - CFO) / TA.  Make NI huge vs CFO → TATA large → high M-Score."""
        income = _make_income(ni=(50_000_000, 1_200_000))
        cashflow = _make_cashflow(cfo=(100_000, 1_500_000))  # very low CFO
        mock_yf.return_value = _mock_ticker(income, _make_balance(), cashflow)
        result = compute_beneish_mscore("FRAUD")
        assert result is not None
        assert result.risk_level == "manipulator"


# ---------------------------------------------------------------------------
# _estimate_rank_from_cap
# ---------------------------------------------------------------------------


class TestEstimateRankFromCap:
    def test_mega_cap(self):
        rank = _estimate_rank_from_cap(3_000_000_000_000)  # $3T
        assert rank is not None and 1 <= rank <= 10

    def test_large_cap(self):
        rank = _estimate_rank_from_cap(50_000_000_000)  # $50B
        assert rank is not None and rank < 500

    def test_small_cap(self):
        rank = _estimate_rank_from_cap(500_000_000)  # $500M
        assert rank is not None and 1000 < rank < 3000

    def test_zero_returns_none(self):
        assert _estimate_rank_from_cap(0) is None

    def test_negative_returns_none(self):
        assert _estimate_rank_from_cap(-1) is None


# ---------------------------------------------------------------------------
# estimate_russell_membership — happy path
# ---------------------------------------------------------------------------


class TestEstimateRussellMembership:
    def _make_info(self, market_cap: float) -> dict:
        return {"marketCap": market_cap, "shortName": "Test Corp"}

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_large_cap_russell_1000(self, mock_yf):
        t = MagicMock()
        t.info = self._make_info(50_000_000_000)  # $50B → Russell 1000
        mock_yf.return_value = t
        result = estimate_russell_membership("AAPL")
        assert isinstance(result, RussellMembership)
        assert result.current_index == "Russell 1000"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_small_cap_russell_2000(self, mock_yf):
        t = MagicMock()
        t.info = self._make_info(600_000_000)  # $600M → Russell 2000
        mock_yf.return_value = t
        result = estimate_russell_membership("SMLC")
        assert isinstance(result, RussellMembership)
        assert result.current_index == "Russell 2000"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_micro_cap_outside_russell(self, mock_yf):
        t = MagicMock()
        t.info = self._make_info(50_000_000)  # $50M → Outside
        mock_yf.return_value = t
        result = estimate_russell_membership("TINY")
        assert result is not None
        assert result.current_index == "Outside Russell 3000"

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_proximity_score_in_range(self, mock_yf):
        t = MagicMock()
        t.info = self._make_info(600_000_000)
        mock_yf.return_value = t
        result = estimate_russell_membership("SMLC")
        assert result is not None
        assert 0.0 <= result.proximity_score <= 1.0

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_rebalance_signal_valid_literal(self, mock_yf):
        t = MagicMock()
        t.info = self._make_info(600_000_000)
        mock_yf.return_value = t
        result = estimate_russell_membership("SMLC")
        assert result is not None
        valid = {
            "likely_add_1000", "likely_drop_1000",
            "likely_add_2000", "likely_drop_2000",
            "stable", "unknown",
        }
        assert result.rebalance_signal in valid

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_no_market_cap(self, mock_yf):
        t = MagicMock()
        t.info = {"shortName": "No Cap Corp"}
        mock_yf.return_value = t
        assert estimate_russell_membership("NOCAP") is None

    @patch("quantbot.stocks.quant_signals.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("timeout")
        assert estimate_russell_membership("ERR") is None
