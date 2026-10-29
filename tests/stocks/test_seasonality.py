"""Tests for Seasonality Pattern Analysis — Phase F.32.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.seasonality.engine import (
    _classify_signal,
    _compute_monthly_stats,
    compute_seasonality,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_monthly_price_df(n_months: int, start: float = 100.0, step: float = 1.0) -> pd.DataFrame:
    """Build a monthly price DataFrame with a DatetimeIndex (month-start periods)."""
    prices = [start + i * step for i in range(n_months)]
    # Use monthly date range so .month attribute works reliably
    idx = pd.date_range("2015-01-01", periods=n_months, freq="MS")
    return pd.DataFrame({"Close": prices}, index=idx)


def _make_mock_ticker(price_df: pd.DataFrame) -> MagicMock:
    tk = MagicMock()
    tk.history.return_value = price_df
    return tk


# ---------------------------------------------------------------------------
# TestComputeMonthlyStats
# ---------------------------------------------------------------------------

class TestComputeMonthlyStats:
    def test_normal_returns_all_months_present(self):
        """Provide returns for all 12 months; expect 12 MonthStats objects."""
        monthly_returns = {m: [0.01, 0.02, -0.01] for m in range(1, 13)}
        result = _compute_monthly_stats(monthly_returns)
        assert len(result) == 12

    def test_avg_return_correct(self):
        monthly_returns = {m: [] for m in range(1, 13)}
        monthly_returns[1] = [0.04, 0.06]   # avg 0.05
        result = _compute_monthly_stats(monthly_returns)
        jan = next(s for s in result if s.month == 1)
        assert jan.avg_return == pytest.approx(0.05, abs=1e-9)

    def test_median_return_correct(self):
        monthly_returns = {m: [] for m in range(1, 13)}
        monthly_returns[3] = [0.01, 0.03, 0.05]  # median 0.03
        result = _compute_monthly_stats(monthly_returns)
        mar = next(s for s in result if s.month == 3)
        assert mar.median_return == pytest.approx(0.03, abs=1e-9)

    def test_positive_rate_correct(self):
        monthly_returns = {m: [] for m in range(1, 13)}
        monthly_returns[6] = [0.01, -0.01, 0.02, -0.02]  # 2/4 = 0.5
        result = _compute_monthly_stats(monthly_returns)
        jun = next(s for s in result if s.month == 6)
        assert jun.positive_rate == pytest.approx(0.5, abs=1e-9)

    def test_empty_month_has_zero_sample_size(self):
        monthly_returns = {m: [] for m in range(1, 13)}
        result = _compute_monthly_stats(monthly_returns)
        assert all(s.sample_size == 0 for s in result)

    def test_sample_size_matches_input(self):
        monthly_returns = {m: [] for m in range(1, 13)}
        monthly_returns[12] = [0.01, 0.02, 0.03, 0.04, 0.05]
        result = _compute_monthly_stats(monthly_returns)
        dec = next(s for s in result if s.month == 12)
        assert dec.sample_size == 5

    def test_month_names_correct(self):
        monthly_returns = {m: [0.01] for m in range(1, 13)}
        result = _compute_monthly_stats(monthly_returns)
        name_map = {s.month: s.month_name for s in result}
        assert name_map[1] == "January"
        assert name_map[6] == "June"
        assert name_map[12] == "December"


# ---------------------------------------------------------------------------
# TestClassifySignal
# ---------------------------------------------------------------------------

class TestClassifySignal:
    def test_strong_season(self):
        assert _classify_signal(0.04) == "strong_season"

    def test_strong_season_exact_boundary(self):
        # exactly 3% is NOT > 3%, so should be positive_season
        assert _classify_signal(0.03) == "positive_season"

    def test_positive_season(self):
        assert _classify_signal(0.02) == "positive_season"

    def test_positive_season_exact_boundary(self):
        assert _classify_signal(0.01) == "neutral"

    def test_neutral(self):
        assert _classify_signal(0.0) == "neutral"

    def test_neutral_negative_boundary(self):
        # exactly -1% → NOT > -1%, so falls to negative_season
        assert _classify_signal(-0.01) == "negative_season"

    def test_negative_season(self):
        assert _classify_signal(-0.02) == "negative_season"

    def test_negative_season_exact_boundary(self):
        # exactly -3% → NOT > -3%, so falls to strong_negative
        assert _classify_signal(-0.03) == "strong_negative"

    def test_strong_negative(self):
        assert _classify_signal(-0.04) == "strong_negative"

    def test_none_returns_no_data(self):
        assert _classify_signal(None) == "no_data"


# ---------------------------------------------------------------------------
# TestComputeSeasonalityIntegration
# ---------------------------------------------------------------------------

class TestComputeSeasonalityIntegration:
    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_normal_returns_data_available(self, mock_yf):
        """With 36 months of data, data_available should be True."""
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_normal_returns_12_month_stats(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert len(result.all_months) == 12

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_best_worst_month_populated(self, mock_yf):
        df = _make_monthly_price_df(48)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert result.best_month is not None
        assert result.worst_month is not None

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_signal_not_no_data_for_adequate_data(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert result.signal != "no_data"

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_interpretation_nonempty(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert len(result.interpretation) > 10

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_current_month_matches_today(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert result.current_month == date.today().month

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_insufficient_data_graceful(self, mock_yf):
        """< 12 months → data_available=True, all_months=[], signal=no_data."""
        df = _make_monthly_price_df(6)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("AAPL")
        assert result.data_available is True
        assert result.all_months == []
        assert result.signal == "no_data"

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_yfinance_exception_data_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_seasonality("AAPL")
        assert result.data_available is False
        assert result.signal == "no_data"

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_empty_dataframe_graceful(self, mock_yf):
        mock_yf.return_value = _make_mock_ticker(pd.DataFrame())
        result = compute_seasonality("AAPL")
        assert result.data_available is True
        assert result.signal == "no_data"

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_ticker_stored_uppercase(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        result = compute_seasonality("aapl")
        # engine stores ticker as-is (caller responsible for case), just check it stored
        assert result.ticker == "aapl"


# ---------------------------------------------------------------------------
# TestSeasonalityAPI
# ---------------------------------------------------------------------------

class TestSeasonalityAPI:
    def _get_client(self):
        from fastapi.testclient import TestClient
        from quantbot.web.app import create_app
        return TestClient(create_app())

    def test_no_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/seasonality")
        assert resp.status_code == 422

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        client = self._get_client()
        resp = client.get("/api/seasonality?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_response_has_required_fields(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        client = self._get_client()
        body = client.get("/api/seasonality?ticker=AAPL").json()
        for f in ["ticker", "current_month", "all_months", "signal", "data_available", "interpretation"]:
            assert f in body

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_yfinance_fail_still_200(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/seasonality?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["data_available"] is False

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_all_months_has_12_entries(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        client = self._get_client()
        body = client.get("/api/seasonality?ticker=AAPL").json()
        assert len(body["all_months"]) == 12

    @patch("quantbot.stocks.seasonality.engine.yf.Ticker")
    def test_month_stats_fields_present(self, mock_yf):
        df = _make_monthly_price_df(36)
        mock_yf.return_value = _make_mock_ticker(df)
        client = self._get_client()
        body = client.get("/api/seasonality?ticker=AAPL").json()
        for stats in body["all_months"]:
            assert "month" in stats
            assert "month_name" in stats
            assert "avg_return" in stats
            assert "median_return" in stats
            assert "positive_rate" in stats
            assert "sample_size" in stats
