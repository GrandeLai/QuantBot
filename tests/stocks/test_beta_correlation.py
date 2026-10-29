"""Tests for Correlation & Beta Monitor — Phase F.31.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.beta_correlation.engine import (
    _classify_signal,
    _compute_beta,
    _compute_correlation,
    _compute_idio_vol,
    _compute_r_squared,
    compute_beta_correlation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_returns(values: list[float]) -> pd.Series:
    """Build a daily returns Series with DatetimeIndex."""
    today = date.today()
    dates = [today - timedelta(days=len(values) - i - 1) for i in range(len(values))]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.Series(values, index=idx)


def _make_price_df(n: int, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """Build a price history DataFrame."""
    prices = [start + i * step for i in range(n)]
    today = date.today()
    dates = [today - timedelta(days=n - i - 1) for i in range(n)]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.DataFrame({"Close": prices}, index=idx)


def _mock_side_effect(stock_df: pd.DataFrame, spy_df: pd.DataFrame, qqq_df: pd.DataFrame):
    """Return a side_effect for yf.Ticker that returns different hist per symbol."""
    def _mock_ticker(symbol: str):
        tk = MagicMock()
        if symbol == "SPY":
            tk.history.return_value = spy_df
        elif symbol == "QQQ":
            tk.history.return_value = qqq_df
        else:
            tk.history.return_value = stock_df
        return tk

    return _mock_ticker


# ---------------------------------------------------------------------------
# TestComputeBeta
# ---------------------------------------------------------------------------

class TestComputeBeta:
    def test_perfectly_correlated_returns_one(self):
        ret = _make_returns([0.01] * 60)
        beta = _compute_beta(ret, ret)
        assert beta == pytest.approx(1.0, abs=0.01)

    def test_double_returns_beta_two(self):
        bench = _make_returns([0.01] * 60)
        stock = _make_returns([0.02] * 60)
        beta = _compute_beta(stock, bench)
        # Cov(2x, x) / Var(x) = 2
        assert beta == pytest.approx(2.0, abs=0.1)

    def test_insufficient_data_returns_none(self):
        short_ret = _make_returns([0.01] * 10)
        assert _compute_beta(short_ret, short_ret) is None

    def test_constant_bench_returns_none(self):
        # var(bench) = 0 → should return None
        bench = _make_returns([0.0] * 60)
        stock = _make_returns([0.01] * 60)
        result = _compute_beta(stock, bench)
        assert result is None

    def test_negative_beta(self):
        bench = _make_returns([0.01] * 60)
        stock = _make_returns([-0.01] * 60)
        beta = _compute_beta(stock, bench)
        assert beta is not None and beta < 0


# ---------------------------------------------------------------------------
# TestComputeCorrelation
# ---------------------------------------------------------------------------

class TestComputeCorrelation:
    def test_identical_series_correlation_one(self):
        ret = _make_returns([0.01, -0.01, 0.02] * 20)
        corr = _compute_correlation(ret, ret)
        assert corr == pytest.approx(1.0, abs=0.01)

    def test_opposite_series_correlation_minus_one(self):
        a = _make_returns([0.01, -0.01] * 30)
        b = _make_returns([-0.01, 0.01] * 30)
        corr = _compute_correlation(a, b)
        assert corr is not None and corr < -0.9

    def test_insufficient_data_returns_none(self):
        short = _make_returns([0.01] * 10)
        assert _compute_correlation(short, short) is None


# ---------------------------------------------------------------------------
# TestComputeRSquared
# ---------------------------------------------------------------------------

class TestComputeRSquared:
    def test_corr_one_r2_one(self):
        assert _compute_r_squared(1.0) == pytest.approx(1.0)

    def test_corr_half_r2_quarter(self):
        assert _compute_r_squared(0.5) == pytest.approx(0.25)

    def test_none_returns_none(self):
        assert _compute_r_squared(None) is None


# ---------------------------------------------------------------------------
# TestComputeIdioVol
# ---------------------------------------------------------------------------

class TestComputeIdioVol:
    def test_idio_vol_positive(self):
        bench = _make_returns([0.01, -0.01] * 40)
        # stock with extra noise
        stock = _make_returns([0.01 + 0.02, -0.01 + 0.02] * 40)
        idio = _compute_idio_vol(stock, bench, beta=1.0)
        assert idio is not None and idio >= 0


# ---------------------------------------------------------------------------
# TestClassifySignal
# ---------------------------------------------------------------------------

class TestClassifySignal:
    def test_high_beta(self):
        assert _classify_signal(1.8) == "high_beta"

    def test_moderate_beta(self):
        assert _classify_signal(1.2) == "moderate_beta"

    def test_low_beta(self):
        assert _classify_signal(0.7) == "low_beta"

    def test_defensive(self):
        assert _classify_signal(0.3) == "defensive"

    def test_none_returns_no_data(self):
        assert _classify_signal(None) == "no_data"

    def test_boundary_exactly_1_5(self):
        assert _classify_signal(1.5) == "high_beta"

    def test_boundary_exactly_1_0(self):
        assert _classify_signal(1.0) == "moderate_beta"

    def test_boundary_exactly_0_5(self):
        assert _classify_signal(0.5) == "low_beta"


# ---------------------------------------------------------------------------
# TestComputeBetaCorrelationIntegration
# ---------------------------------------------------------------------------

class TestComputeBetaCorrelationIntegration:
    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        df = _make_price_df(100)
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        result = compute_beta_correlation("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_beta_1y_populated(self, mock_yf):
        df = _make_price_df(100)
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        result = compute_beta_correlation("AAPL")
        assert result.beta_1y is not None

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_corr_fields_populated(self, mock_yf):
        df = _make_price_df(100)
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        result = compute_beta_correlation("AAPL")
        assert result.corr_spy_1y is not None
        assert result.r_squared_1y is not None

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        df = _make_price_df(100)
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        result = compute_beta_correlation("AAPL")
        assert len(result.interpretation) > 5

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_yfinance_exception_data_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_beta_correlation("AAPL")
        assert result.data_available is False
        assert result.signal == "no_data"

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_insufficient_data_graceful(self, mock_yf):
        df = _make_price_df(10)  # < 30 bars
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        result = compute_beta_correlation("AAPL")
        assert result.data_available is True
        assert result.signal == "no_data"

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_high_beta_signal(self, mock_yf):
        """Stock with 2x market returns → high beta."""
        bench_prices = [100.0 + i * 0.5 for i in range(100)]
        # Stock moves 2x the market
        stock_prices = [100.0 + i * 1.0 for i in range(100)]
        bench_df = pd.DataFrame({"Close": bench_prices}, index=pd.date_range("2024-01-01", periods=100))
        stock_df = pd.DataFrame({"Close": stock_prices}, index=pd.date_range("2024-01-01", periods=100))
        mock_yf.side_effect = _mock_side_effect(stock_df, bench_df, bench_df)
        result = compute_beta_correlation("TEST")
        assert result.beta_1y is not None
        assert result.beta_1y > 1.0


# ---------------------------------------------------------------------------
# TestBetaCorrelationAPI
# ---------------------------------------------------------------------------

class TestBetaCorrelationAPI:
    def _get_client(self):
        from fastapi.testclient import TestClient
        from quantbot.web.app import create_app
        return TestClient(create_app())

    def test_no_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/beta-correlation")
        assert resp.status_code == 422

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        df = _make_price_df(60)
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        client = self._get_client()
        resp = client.get("/api/beta-correlation?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_response_has_required_fields(self, mock_yf):
        df = _make_price_df(60)
        mock_yf.side_effect = _mock_side_effect(df, df, df)
        client = self._get_client()
        body = client.get("/api/beta-correlation?ticker=AAPL").json()
        for f in ["ticker", "beta_1y", "signal", "data_available", "interpretation"]:
            assert f in body

    @patch("quantbot.stocks.beta_correlation.engine.yf.Ticker")
    def test_yfinance_fail_still_200(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/beta-correlation?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["data_available"] is False
