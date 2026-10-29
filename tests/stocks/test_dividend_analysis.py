"""Tests for Dividend Analysis — Phase F.28.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.dividend_analysis.engine import (
    _annual_dividends,
    _classify_capture,
    _classify_safety,
    _compute_consecutive_growth,
    _compute_dividend_growth,
    _parse_ex_date,
    compute_dividend_analysis,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dividend_series(year_amounts: dict[int, list[float]]) -> pd.Series:
    """Build a dividend Series from {year: [q1, q2, q3, q4]} mapping."""
    records: list[tuple] = []
    for year, amounts in sorted(year_amounts.items()):
        for month_offset, amount in zip([2, 5, 8, 11], amounts):
            dt = pd.Timestamp(year=year, month=month_offset, day=15, tz="UTC")
            records.append((dt, amount))
    if not records:
        return pd.Series(dtype=float)
    idx, vals = zip(*records)
    return pd.Series(list(vals), index=pd.DatetimeIndex(list(idx)))


def _mock_ticker(
    info: dict | None = None,
    dividends: pd.Series | None = None,
) -> MagicMock:
    """Build a mock yfinance Ticker."""
    tk = MagicMock()
    tk.info = info if info is not None else {}
    tk.dividends = dividends if dividends is not None else pd.Series(dtype=float)
    return tk


def _normal_info(
    div_yield: float = 0.02,
    div_rate: float = 2.0,
    payout_ratio: float = 0.40,
    ex_date: int | None = None,
) -> dict:
    if ex_date is None:
        # Ex-Date 3 days from now
        ex_date = int((date.today() + timedelta(days=3)).strftime("%s") if hasattr(date.today(), "strftime") else
                      (date.today() + timedelta(days=3)).toordinal())
        # Use proper unix timestamp
        from datetime import timezone
        from datetime import datetime as dt_cls
        d = date.today() + timedelta(days=3)
        ex_date = int(dt_cls(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
    return {
        "dividendYield": div_yield,
        "dividendRate": div_rate,
        "payoutRatio": payout_ratio,
        "exDividendDate": ex_date,
    }


def _no_dividend_info() -> dict:
    return {
        "dividendYield": 0.0,
        "dividendRate": 0.0,
        "payoutRatio": None,
        "exDividendDate": None,
    }


# ---------------------------------------------------------------------------
# TestAnnualDividends
# ---------------------------------------------------------------------------

class TestAnnualDividends:
    def test_aggregates_by_year(self):
        series = _make_dividend_series({2022: [0.5, 0.5, 0.5, 0.5], 2023: [0.6, 0.6, 0.6, 0.6]})
        annual = _annual_dividends(series)
        assert annual[2022] == pytest.approx(2.0)
        assert annual[2023] == pytest.approx(2.4)

    def test_empty_series_returns_empty(self):
        assert _annual_dividends(pd.Series(dtype=float)) == {}

    def test_none_series_returns_empty(self):
        assert _annual_dividends(None) == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# TestComputeDividendGrowth
# ---------------------------------------------------------------------------

class TestComputeDividendGrowth:
    def test_positive_growth_cagr(self):
        annual = {2019: 1.0, 2020: 1.1, 2021: 1.21, 2022: 1.33, 2023: 1.46}
        # 4 yr CAGR ≈ 10%
        cagr = _compute_dividend_growth(annual)
        assert cagr is not None
        assert cagr == pytest.approx(0.10, abs=0.01)

    def test_insufficient_data_returns_none(self):
        # Only 1 year of data
        assert _compute_dividend_growth({2023: 2.0}) is None

    def test_zero_start_returns_none(self):
        assert _compute_dividend_growth({2019: 0.0, 2023: 2.0}) is None

    def test_two_year_data_works(self):
        # Simple 1-yr CAGR
        cagr = _compute_dividend_growth({2022: 2.0, 2023: 2.2})
        assert cagr == pytest.approx(0.10, abs=0.005)


# ---------------------------------------------------------------------------
# TestComputeConsecutiveGrowth
# ---------------------------------------------------------------------------

class TestComputeConsecutiveGrowth:
    def test_consecutive_growth_years(self):
        annual = {2019: 1.0, 2020: 1.1, 2021: 1.2, 2022: 1.3, 2023: 1.4}
        assert _compute_consecutive_growth(annual) == 4

    def test_interrupted_growth_stops(self):
        # 2021 < 2020 breaks the streak
        annual = {2019: 1.0, 2020: 1.1, 2021: 1.05, 2022: 1.2, 2023: 1.3}
        # Looking back from 2023: 2023>2022 (count=1), 2022>2021 (count=2), 2021<2020 (stop)
        assert _compute_consecutive_growth(annual) == 2

    def test_single_year_returns_zero(self):
        assert _compute_consecutive_growth({2023: 1.0}) == 0

    def test_empty_returns_zero(self):
        assert _compute_consecutive_growth({}) == 0


# ---------------------------------------------------------------------------
# TestClassifySafety
# ---------------------------------------------------------------------------

class TestClassifySafety:
    def test_safe(self):
        assert _classify_safety(0.40, 0.02) == "safe"

    def test_watch(self):
        assert _classify_safety(0.80, 0.02) == "watch"

    def test_danger(self):
        assert _classify_safety(1.10, 0.02) == "danger"

    def test_no_dividend_zero_yield(self):
        assert _classify_safety(0.40, 0.0) == "no_dividend"

    def test_no_dividend_none_yield(self):
        assert _classify_safety(0.40, None) == "no_dividend"

    def test_no_dividend_none_payout(self):
        assert _classify_safety(None, 0.02) == "no_dividend"

    def test_boundary_safe_exactly_075(self):
        # 0.75 → watch (not safe)
        assert _classify_safety(0.75, 0.02) == "watch"

    def test_boundary_danger_exactly_1(self):
        # 1.0 → danger
        assert _classify_safety(1.0, 0.02) == "danger"


# ---------------------------------------------------------------------------
# TestClassifyCapture
# ---------------------------------------------------------------------------

class TestClassifyCapture:
    def test_capture_within_window(self):
        assert _classify_capture(3) == "capture_opportunity"

    def test_capture_on_zero_days(self):
        assert _classify_capture(0) == "capture_opportunity"

    def test_not_applicable_beyond_window(self):
        assert _classify_capture(10) == "not_applicable"

    def test_past_ex_date_not_applicable(self):
        assert _classify_capture(-2) == "not_applicable"

    def test_none_days_returns_unknown(self):
        assert _classify_capture(None) == "unknown"


# ---------------------------------------------------------------------------
# TestParseExDate
# ---------------------------------------------------------------------------

class TestParseExDate:
    def test_unix_timestamp(self):
        from datetime import datetime, timezone
        d = date(2025, 6, 15)
        ts = int(datetime(2025, 6, 15, tzinfo=timezone.utc).timestamp())
        result = _parse_ex_date(ts)
        assert result == d

    def test_none_returns_none(self):
        assert _parse_ex_date(None) is None

    def test_string_iso(self):
        result = _parse_ex_date("2025-06-15")
        assert result == date(2025, 6, 15)

    def test_date_object(self):
        d = date(2025, 6, 15)
        assert _parse_ex_date(d) == d


# ---------------------------------------------------------------------------
# TestComputeDividendAnalysisNormal
# ---------------------------------------------------------------------------

class TestComputeDividendAnalysisNormal:
    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        divs = _make_dividend_series({2019: [0.5]*4, 2020: [0.55]*4, 2021: [0.6]*4,
                                      2022: [0.65]*4, 2023: [0.70]*4})
        mock_yf.return_value = _mock_ticker(info=_normal_info(), dividends=divs)
        result = compute_dividend_analysis("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_dividend_yield_populated(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_normal_info(div_yield=0.015))
        result = compute_dividend_analysis("AAPL")
        assert result.dividend_yield == pytest.approx(0.015, rel=1e-4)

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_payout_ratio_safe(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_normal_info(payout_ratio=0.35))
        result = compute_dividend_analysis("AAPL")
        assert result.safety == "safe"

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_payout_ratio_danger(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_normal_info(payout_ratio=1.20))
        result = compute_dividend_analysis("AAPL")
        assert result.safety == "danger"

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_capture_signal_within_window(self, mock_yf):
        from datetime import datetime, timezone
        d = date.today() + timedelta(days=3)
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        mock_yf.return_value = _mock_ticker(info=_normal_info(ex_date=ts))
        result = compute_dividend_analysis("AAPL")
        assert result.capture_signal == "capture_opportunity"

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_capture_signal_not_applicable(self, mock_yf):
        from datetime import datetime, timezone
        d = date.today() + timedelta(days=30)
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())
        mock_yf.return_value = _mock_ticker(info=_normal_info(ex_date=ts))
        result = compute_dividend_analysis("AAPL")
        assert result.capture_signal == "not_applicable"

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_five_yr_growth_rate_computed(self, mock_yf):
        divs = _make_dividend_series({2019: [0.5]*4, 2020: [0.55]*4, 2021: [0.6]*4,
                                      2022: [0.65]*4, 2023: [0.70]*4})
        mock_yf.return_value = _mock_ticker(info=_normal_info(), dividends=divs)
        result = compute_dividend_analysis("AAPL")
        assert result.five_yr_growth_rate is not None
        assert result.five_yr_growth_rate > 0

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_consecutive_growth_years(self, mock_yf):
        divs = _make_dividend_series({2020: [0.5]*4, 2021: [0.55]*4, 2022: [0.60]*4, 2023: [0.65]*4})
        mock_yf.return_value = _mock_ticker(info=_normal_info(), dividends=divs)
        result = compute_dividend_analysis("AAPL")
        assert result.consecutive_growth_years == 3

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_normal_info())
        result = compute_dividend_analysis("AAPL")
        assert len(result.interpretation) > 5

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_historical_annual_list(self, mock_yf):
        divs = _make_dividend_series({2021: [0.5]*4, 2022: [0.55]*4, 2023: [0.60]*4})
        mock_yf.return_value = _mock_ticker(info=_normal_info(), dividends=divs)
        result = compute_dividend_analysis("AAPL")
        assert isinstance(result.historical_annual, list)
        assert len(result.historical_annual) >= 1
        for entry in result.historical_annual:
            assert "year" in entry and "total" in entry


# ---------------------------------------------------------------------------
# TestNoDividend
# ---------------------------------------------------------------------------

class TestNoDividend:
    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_no_dividend_safety(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_no_dividend_info())
        result = compute_dividend_analysis("TSLA")
        assert result.safety == "no_dividend"
        assert result.data_available is True

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_no_dividend_capture_signal(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_no_dividend_info())
        result = compute_dividend_analysis("TSLA")
        assert result.capture_signal in ("not_applicable", "unknown")


# ---------------------------------------------------------------------------
# TestGracefulDegradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_yfinance_exception_data_available_false(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_dividend_analysis("AAPL")
        assert result.data_available is False
        assert result.safety == "no_dividend"

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_missing_payout_ratio_still_returns(self, mock_yf):
        info = _normal_info()
        info.pop("payoutRatio", None)
        info["payoutRatio"] = None
        mock_yf.return_value = _mock_ticker(info=info)
        result = compute_dividend_analysis("AAPL")
        assert result.data_available is True
        assert result.payout_ratio is None


# ---------------------------------------------------------------------------
# TestDividendAnalysisAPI
# ---------------------------------------------------------------------------

class TestDividendAnalysisAPI:
    def _get_client(self):
        from fastapi.testclient import TestClient
        from quantbot.web.app import create_app
        return TestClient(create_app())

    def test_no_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/dividend-analysis")
        assert resp.status_code == 422

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_normal_info())
        client = self._get_client()
        resp = client.get("/api/dividend-analysis?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_response_has_required_fields(self, mock_yf):
        mock_yf.return_value = _mock_ticker(info=_normal_info())
        client = self._get_client()
        body = client.get("/api/dividend-analysis?ticker=AAPL").json()
        for field in ["ticker", "safety", "capture_signal", "data_available", "interpretation"]:
            assert field in body

    @patch("quantbot.stocks.dividend_analysis.engine.yf.Ticker")
    def test_yfinance_fail_still_200(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/dividend-analysis?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["data_available"] is False
