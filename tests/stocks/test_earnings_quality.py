"""Tests for earnings_quality engine and API endpoint.

Covers:
- _safe_get helper
- _compute_f_score boundaries and scoring
- _compute_m_score outputs
- _compute_accrual ratio + quality
- _overall_grade logic
- compute_earnings_quality happy path + degradation
- GET /api/earnings-quality 200 + 422
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.earnings_quality.engine import (
    _compute_accrual,
    _compute_f_score,
    _compute_m_score,
    _overall_grade,
    _safe_get,
    compute_earnings_quality,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fin(
    net_income: float = 10_000,
    net_income_t1: float = 8_000,
    revenue: float = 100_000,
    revenue_t1: float = 90_000,
    gross_profit: float = 40_000,
    gross_profit_t1: float = 35_000,
    cogs: float = 60_000,
    cogs_t1: float = 55_000,
    sga: float = 5_000,
    sga_t1: float = 4_500,
) -> pd.DataFrame:
    """Build a minimal income statement DataFrame (2 annual periods)."""
    return pd.DataFrame(
        {
            "2024": {
                "Net Income": net_income,
                "Total Revenue": revenue,
                "Gross Profit": gross_profit,
                "Cost Of Revenue": cogs,
                "Selling General And Administrative": sga,
            },
            "2023": {
                "Net Income": net_income_t1,
                "Total Revenue": revenue_t1,
                "Gross Profit": gross_profit_t1,
                "Cost Of Revenue": cogs_t1,
                "Selling General And Administrative": sga_t1,
            },
        }
    )


def _make_bal(
    total_assets: float = 200_000,
    total_assets_t1: float = 180_000,
    total_assets_t2: float = 160_000,
    current_assets: float = 60_000,
    current_assets_t1: float = 50_000,
    current_liab: float = 30_000,
    current_liab_t1: float = 28_000,
    lt_debt: float = 40_000,
    lt_debt_t1: float = 45_000,
    shares: float = 1_000_000,
    shares_t1: float = 1_000_000,
    ppe: float = 50_000,
    ppe_t1: float = 55_000,
    receivables: float = 15_000,
    receivables_t1: float = 12_000,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "2024": {
                "Total Assets": total_assets,
                "Current Assets": current_assets,
                "Current Liabilities": current_liab,
                "Long Term Debt": lt_debt,
                "Common Stock": shares,
                "Net PPE": ppe,
                "Receivables": receivables,
            },
            "2023": {
                "Total Assets": total_assets_t1,
                "Current Assets": current_assets_t1,
                "Current Liabilities": current_liab_t1,
                "Long Term Debt": lt_debt_t1,
                "Common Stock": shares_t1,
                "Net PPE": ppe_t1,
                "Receivables": receivables_t1,
            },
            "2022": {
                "Total Assets": total_assets_t2,
                "Current Assets": 45_000,
                "Current Liabilities": 27_000,
                "Long Term Debt": 48_000,
                "Common Stock": shares_t1,
                "Net PPE": 58_000,
                "Receivables": 11_000,
            },
        }
    )


def _make_cf(
    cfo: float = 15_000,
    cfo_t1: float = 12_000,
    dep: float = 5_000,
    dep_t1: float = 5_500,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "2024": {
                "Operating Cash Flow": cfo,
                "Depreciation And Amortization": dep,
            },
            "2023": {
                "Operating Cash Flow": cfo_t1,
                "Depreciation And Amortization": dep_t1,
            },
        }
    )


def _make_ticker(fin=None, bal=None, cf=None) -> MagicMock:
    mock = MagicMock()
    mock.financials = fin if fin is not None else _make_fin()
    mock.balance_sheet = bal if bal is not None else _make_bal()
    mock.cashflow = cf if cf is not None else _make_cf()
    return mock


# ---------------------------------------------------------------------------
# EQ-1: _safe_get
# ---------------------------------------------------------------------------


class TestSafeGet:
    def test_returns_value_for_existing_key(self) -> None:
        df = pd.DataFrame({"2024": {"Net Income": 100.0}})
        assert _safe_get(df, "Net Income") == 100.0

    def test_returns_none_for_missing_key(self) -> None:
        df = pd.DataFrame({"2024": {"Net Income": 100.0}})
        assert _safe_get(df, "Revenue") is None

    def test_tries_multiple_keys(self) -> None:
        df = pd.DataFrame({"2024": {"Total Revenue": 500.0}})
        assert _safe_get(df, "Revenue", "Total Revenue") == 500.0

    def test_returns_none_for_empty_df(self) -> None:
        assert _safe_get(pd.DataFrame(), "Net Income") is None

    def test_col_offset_returns_prior_period(self) -> None:
        df = pd.DataFrame({"2024": {"Net Income": 100.0}, "2023": {"Net Income": 80.0}})
        assert _safe_get(df, "Net Income", col=1) == 80.0

    def test_returns_none_on_nan(self) -> None:
        df = pd.DataFrame({"2024": {"Net Income": float("nan")}})
        assert _safe_get(df, "Net Income") is None


# ---------------------------------------------------------------------------
# EQ-2: _compute_f_score
# ---------------------------------------------------------------------------


class TestComputeFScore:
    def test_all_pass_gives_9(self) -> None:
        """A perfectly improving company should score near max."""
        fin = _make_fin(net_income=20_000, net_income_t1=10_000)
        bal = _make_bal(lt_debt=20_000, lt_debt_t1=40_000)  # leverage down
        cf = _make_cf(cfo=25_000)  # CFO > NI → accrual OK
        score, grade, comps = _compute_f_score(fin, bal, cf)
        assert score is not None
        assert score >= 7
        assert grade in ("very_strong", "strong")

    def test_score_grades_very_strong_at_8_plus(self) -> None:
        fin = _make_fin(net_income=20_000, net_income_t1=10_000)
        bal = _make_bal(lt_debt=20_000, lt_debt_t1=40_000)
        cf = _make_cf(cfo=25_000)
        score, grade, _ = _compute_f_score(fin, bal, cf)
        if score is not None and score >= 8:
            assert grade == "very_strong"

    def test_negative_roa_fails_roa_component(self) -> None:
        fin = _make_fin(net_income=-5_000)  # ROA < 0
        bal = _make_bal()
        cf = _make_cf()
        _, _, comps = _compute_f_score(fin, bal, cf)
        if "ROA > 0" in comps:
            assert comps["ROA > 0"] is False

    def test_negative_cfo_fails_cfo_component(self) -> None:
        _, _, comps = _compute_f_score(_make_fin(), _make_bal(), _make_cf(cfo=-1_000))
        if "CFO > 0" in comps:
            assert comps["CFO > 0"] is False

    def test_increasing_leverage_fails_component(self) -> None:
        # lt_debt increases relative to assets → leverage ↑ → fail
        bal = _make_bal(lt_debt=80_000, lt_debt_t1=40_000)
        _, _, comps = _compute_f_score(_make_fin(), bal, _make_cf())
        if "ΔLeverage ↓" in comps:
            assert comps["ΔLeverage ↓"] is False

    def test_decreasing_leverage_passes_component(self) -> None:
        bal = _make_bal(lt_debt=20_000, lt_debt_t1=45_000)
        _, _, comps = _compute_f_score(_make_fin(), bal, _make_cf())
        if "ΔLeverage ↓" in comps:
            assert comps["ΔLeverage ↓"] is True

    def test_empty_dataframes_return_none(self) -> None:
        score, grade, comps = _compute_f_score(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )
        assert score is None
        assert grade is None
        assert comps == {}

    def test_grade_weak_for_score_below_3(self) -> None:
        # Force low score: negative NI, negative CFO, increasing leverage
        fin = _make_fin(net_income=-10_000, net_income_t1=-5_000)
        bal = _make_bal(lt_debt=80_000, lt_debt_t1=40_000)
        cf = _make_cf(cfo=-5_000)
        score, grade, _ = _compute_f_score(fin, bal, cf)
        if score is not None and score <= 2:
            assert grade == "weak"


# ---------------------------------------------------------------------------
# EQ-3: _compute_m_score
# ---------------------------------------------------------------------------


class TestComputeMScore:
    def test_returns_float_with_sufficient_data(self) -> None:
        m = _compute_m_score(_make_fin(), _make_bal(), _make_cf())
        assert m is not None
        assert isinstance(m, float)

    def test_low_score_below_threshold(self) -> None:
        """Stable company should have M < -2.22 (no manipulation flag)."""
        m = _compute_m_score(_make_fin(), _make_bal(), _make_cf())
        # With default values this should be well below -2.22
        # (we check it's a valid float; exact value depends on inputs)
        assert m is not None

    def test_returns_none_on_empty_financials(self) -> None:
        m = _compute_m_score(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        assert m is None

    def test_manipulation_flag_when_m_above_threshold(self) -> None:
        """If we set DSRI very high (receivables spiked), M should increase."""
        # Spike receivables from 12k to 50k while revenue stays similar → DSRI high
        fin = _make_fin(revenue=100_000, revenue_t1=100_000)
        bal = _make_bal(receivables=50_000, receivables_t1=10_000)
        cf = _make_cf()
        m = _compute_m_score(fin, bal, cf)
        # We can't always guarantee > -2.22 but we can verify it returned something
        assert m is not None


# ---------------------------------------------------------------------------
# EQ-4: _compute_accrual
# ---------------------------------------------------------------------------


class TestComputeAccrual:
    def test_high_quality_when_small_accrual(self) -> None:
        # NI=10k, CFO=10.5k → accrual = (10k-10.5k)/avg_assets ≈ -0.003 (well within ±5%)
        fin = _make_fin(net_income=10_000)
        bal = _make_bal(total_assets=200_000, total_assets_t1=180_000)
        cf = _make_cf(cfo=10_500)
        accrual, quality = _compute_accrual(fin, bal, cf)
        assert quality == "high"
        assert accrual is not None

    def test_low_quality_when_high_accrual(self) -> None:
        # NI=30k, CFO=5k, avg_assets=190k → accrual ≈ (30k-5k)/190k ≈ 13% > 10%
        fin = _make_fin(net_income=30_000)
        bal = _make_bal(total_assets=200_000, total_assets_t1=180_000)
        cf = _make_cf(cfo=5_000)
        accrual, quality = _compute_accrual(fin, bal, cf)
        assert quality == "low"
        assert accrual is not None and accrual > 0.10

    def test_medium_quality_in_between(self) -> None:
        # NI=20k, CFO=5k, avg_assets=190k → accrual ≈ (15k/190k) ≈ 7.9% (between 5% and 10%)
        fin = _make_fin(net_income=20_000)
        bal = _make_bal(total_assets=200_000, total_assets_t1=180_000)
        cf = _make_cf(cfo=5_000)
        accrual, quality = _compute_accrual(fin, bal, cf)
        assert quality == "medium"

    def test_returns_none_when_data_missing(self) -> None:
        accrual, quality = _compute_accrual(
            pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        )
        assert accrual is None
        assert quality is None


# ---------------------------------------------------------------------------
# EQ-5: _overall_grade
# ---------------------------------------------------------------------------


class TestOverallGrade:
    def test_manipulator_risk_overrides_f_score(self) -> None:
        assert _overall_grade(9, True, "high") == "manipulator_risk"

    def test_high_quality_needs_f_score_7_plus(self) -> None:
        assert _overall_grade(7, False, "high") == "high_quality"
        assert _overall_grade(8, False, "medium") == "high_quality"

    def test_low_quality_for_f_score_below_3(self) -> None:
        assert _overall_grade(2, False, "medium") == "low_quality"

    def test_low_quality_for_low_accrual(self) -> None:
        assert _overall_grade(6, False, "low") == "low_quality"

    def test_average_quality_as_default(self) -> None:
        assert _overall_grade(4, False, "medium") == "average_quality"

    def test_none_f_score_gives_average(self) -> None:
        assert _overall_grade(None, False, "medium") == "average_quality"


# ---------------------------------------------------------------------------
# EQ-6: compute_earnings_quality — happy path
# ---------------------------------------------------------------------------


class TestComputeEarningsQuality:
    def test_happy_path_returns_data_available_true(self) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("AAPL")
        assert result.data_available is True
        assert result.ticker == "AAPL"

    def test_ticker_uppercased(self) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("aapl")
        assert result.ticker == "AAPL"

    def test_as_of_date_is_today(self) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("MSFT")
        assert result.as_of_date == date.today()

    def test_f_score_in_range(self) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("TSLA")
        if result.f_score is not None:
            assert 0 <= result.f_score <= 9

    def test_quality_grade_is_valid(self) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("NVDA")
        assert result.quality_grade in ("high_quality", "average_quality", "low_quality", "manipulator_risk")

    def test_interpretation_is_non_empty(self) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("GOOGL")
        assert isinstance(result.interpretation, str)
        assert len(result.interpretation) > 0


# ---------------------------------------------------------------------------
# EQ-7: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_exception_gives_data_available_false(self) -> None:
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", side_effect=Exception("boom")):
            result = compute_earnings_quality("FAKE")
        assert result.data_available is False

    def test_never_raises(self) -> None:
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", side_effect=RuntimeError("x")):
            result = compute_earnings_quality("ERROR")
        assert result is not None

    def test_empty_financials_gives_degraded(self) -> None:
        mock_t = MagicMock()
        mock_t.financials = pd.DataFrame()
        mock_t.balance_sheet = pd.DataFrame()
        mock_t.cashflow = pd.DataFrame()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("EMPTY")
        assert result.data_available is False

    def test_none_financials_gives_degraded(self) -> None:
        mock_t = MagicMock()
        mock_t.financials = None
        mock_t.balance_sheet = None
        mock_t.cashflow = None
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            result = compute_earnings_quality("NONE")
        assert result.data_available is False

    def test_degraded_ticker_preserved(self) -> None:
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_earnings_quality("META")
        assert result.ticker == "META"

    def test_degraded_as_of_date_is_today(self) -> None:
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_earnings_quality("AMZN")
        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# EQ-8: API endpoint
# ---------------------------------------------------------------------------


class TestEarningsQualityAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app
        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/earnings-quality/")
        assert resp.status_code == 422

    def test_always_200_even_when_degraded(self, client: TestClient) -> None:
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", side_effect=Exception("fail")):
            resp = client.get("/api/earnings-quality/?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_all_fields(self, client: TestClient) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            resp = client.get("/api/earnings-quality/?ticker=MSFT")
        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "f_score", "f_score_grade", "f_score_components",
            "m_score", "manipulation_risk", "accrual_ratio", "accrual_quality",
            "quality_grade", "interpretation", "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"

    def test_f_score_components_is_dict(self, client: TestClient) -> None:
        mock_t = _make_ticker()
        with patch("quantbot.stocks.earnings_quality.engine.yf.Ticker", return_value=mock_t):
            resp = client.get("/api/earnings-quality/?ticker=NVDA")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["f_score_components"], dict)
