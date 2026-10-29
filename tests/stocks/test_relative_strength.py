"""Tests for Relative Strength Score — Phase F.33.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.relative_strength.engine import (
    _classify_signal,
    _compute_period_returns,
    _period_to_rs_score,
    compute_rs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_price_df(n: int, start: float = 100.0, step: float = 0.5) -> pd.DataFrame:
    """Build a daily price DataFrame."""
    prices = [start + i * step for i in range(n)]
    idx = pd.date_range("2023-01-01", periods=n, freq="B")
    return pd.DataFrame({"Close": prices}, index=idx)


def _mock_side_effect(
    stock_df: pd.DataFrame,
    spy_df: pd.DataFrame,
) -> Callable[[str], MagicMock]:
    """Return a side_effect that dispatches by symbol."""
    def _inner(symbol: str) -> MagicMock:
        tk = MagicMock()
        if symbol == "SPY":
            tk.history.return_value = spy_df
        else:
            tk.history.return_value = stock_df
        return tk
    return _inner


# ---------------------------------------------------------------------------
# TestComputePeriodReturns
# ---------------------------------------------------------------------------

class TestComputePeriodReturns:
    def test_positive_return(self):
        """Stock rising from 100 to 110 over 21 bars → ~10% return."""
        prices = [100.0 + i * (10.0 / 21) for i in range(23)]
        close = pd.Series(prices)
        ret = _compute_period_returns(close, 21)
        assert ret is not None
        assert ret == pytest.approx(10.0 / 100.0, abs=0.005)

    def test_negative_return(self):
        """Stock falling from 100 to 90 → ~-10% return."""
        prices = [100.0 - i * (10.0 / 21) for i in range(23)]
        close = pd.Series(prices)
        ret = _compute_period_returns(close, 21)
        assert ret is not None
        assert ret < 0

    def test_flat_returns_zero(self):
        prices = [100.0] * 30
        close = pd.Series(prices)
        ret = _compute_period_returns(close, 21)
        assert ret == pytest.approx(0.0, abs=1e-9)

    def test_insufficient_data_returns_none(self):
        close = pd.Series([100.0] * 10)
        assert _compute_period_returns(close, 21) is None

    def test_exactly_n_bars_returns_none(self):
        """Need at least n+1 bars (start + n periods)."""
        close = pd.Series([100.0] * 21)
        assert _compute_period_returns(close, 21) is None

    def test_n_plus_one_bars_succeeds(self):
        close = pd.Series([100.0] * 22)
        assert _compute_period_returns(close, 21) is not None


# ---------------------------------------------------------------------------
# TestPeriodToRsScore
# ---------------------------------------------------------------------------

class TestPeriodToRsScore:
    def test_zero_relative_return_is_50(self):
        assert _period_to_rs_score(0.0) == pytest.approx(50.0)

    def test_plus_50pct_is_100(self):
        assert _period_to_rs_score(0.50) == pytest.approx(100.0)

    def test_minus_50pct_is_0(self):
        assert _period_to_rs_score(-0.50) == pytest.approx(0.0)

    def test_clipped_above_50pct(self):
        assert _period_to_rs_score(1.0) == pytest.approx(100.0)

    def test_clipped_below_minus_50pct(self):
        assert _period_to_rs_score(-1.0) == pytest.approx(0.0)

    def test_positive_relative_above_50(self):
        assert _period_to_rs_score(0.25) > 50.0


# ---------------------------------------------------------------------------
# TestClassifySignal
# ---------------------------------------------------------------------------

class TestClassifySignal:
    def test_strong_outperformer(self):
        assert _classify_signal(85.0) == "strong_outperformer"

    def test_outperformer(self):
        assert _classify_signal(70.0) == "outperformer"

    def test_neutral(self):
        assert _classify_signal(50.0) == "neutral"

    def test_underperformer(self):
        assert _classify_signal(30.0) == "underperformer"

    def test_strong_underperformer(self):
        assert _classify_signal(10.0) == "strong_underperformer"

    def test_none_returns_no_data(self):
        assert _classify_signal(None) == "no_data"

    def test_boundary_80_is_strong(self):
        assert _classify_signal(80.0) == "strong_outperformer"

    def test_boundary_60_is_outperformer(self):
        assert _classify_signal(60.0) == "outperformer"

    def test_boundary_40_is_neutral(self):
        assert _classify_signal(40.0) == "neutral"

    def test_boundary_20_is_underperformer(self):
        assert _classify_signal(20.0) == "underperformer"


# ---------------------------------------------------------------------------
# TestComputeRSIntegration
# ---------------------------------------------------------------------------

class TestComputeRSIntegration:
    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        result = compute_rs("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_periods_populated(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        result = compute_rs("AAPL")
        assert len(result.periods) > 0

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_rs_score_in_range(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        result = compute_rs("AAPL")
        assert 0.0 <= result.rs_score <= 100.0

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_equal_performance_neutral(self, mock_yf):
        """Stock and SPY have identical prices → RS ≈ 50, signal neutral."""
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        result = compute_rs("AAPL")
        assert result.rs_score == pytest.approx(50.0, abs=0.1)
        assert result.signal == "neutral"

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_outperforming_stock_higher_score(self, mock_yf):
        """Stock rising faster than SPY → RS > 50."""
        stock_df = _make_price_df(300, start=100, step=1.0)   # +3/day approx
        spy_df = _make_price_df(300, start=100, step=0.3)     # +0.9/day approx
        mock_yf.side_effect = _mock_side_effect(stock_df, spy_df)
        result = compute_rs("AAPL")
        assert result.rs_score > 50.0

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_underperforming_stock_lower_score(self, mock_yf):
        """Stock flat while SPY rises → RS < 50."""
        stock_df = _make_price_df(300, start=100, step=0.0)
        spy_df = _make_price_df(300, start=100, step=0.5)
        mock_yf.side_effect = _mock_side_effect(stock_df, spy_df)
        result = compute_rs("AAPL")
        assert result.rs_score < 50.0

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_interpretation_nonempty(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        result = compute_rs("AAPL")
        assert len(result.interpretation) > 10

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_insufficient_data_graceful(self, mock_yf):
        """< 20 trading days → data_available=True, signal=no_data."""
        df = _make_price_df(10)
        mock_yf.side_effect = _mock_side_effect(df, df)
        result = compute_rs("AAPL")
        assert result.data_available is True
        assert result.signal == "no_data"

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_yfinance_exception_data_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_rs("AAPL")
        assert result.data_available is False
        assert result.signal == "no_data"


# ---------------------------------------------------------------------------
# TestRelativeStrengthAPI
# ---------------------------------------------------------------------------

class TestRelativeStrengthAPI:
    def _get_client(self):
        from fastapi.testclient import TestClient
        from quantbot.web.app import create_app
        return TestClient(create_app())

    def test_no_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/relative-strength")
        assert resp.status_code == 422

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        client = self._get_client()
        resp = client.get("/api/relative-strength?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_response_has_required_fields(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        client = self._get_client()
        body = client.get("/api/relative-strength?ticker=AAPL").json()
        for f in ["ticker", "periods", "rs_score", "signal", "data_available", "interpretation"]:
            assert f in body

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_yfinance_fail_still_200(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/relative-strength?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["data_available"] is False

    @patch("quantbot.stocks.relative_strength.engine.yf.Ticker")
    def test_period_fields_present(self, mock_yf):
        df = _make_price_df(300)
        mock_yf.side_effect = _mock_side_effect(df, df)
        client = self._get_client()
        body = client.get("/api/relative-strength?ticker=AAPL").json()
        if body["periods"]:
            p = body["periods"][0]
            assert "period" in p
            assert "stock_return" in p
            assert "spy_return" in p
            assert "relative_return" in p
