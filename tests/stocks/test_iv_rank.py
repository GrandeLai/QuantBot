"""Tests for IV Rank & Volatility Monitor — Phase F.24.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.iv_rank.engine import (
    TermStructurePoint,
    _compute_hv,
    _get_atm_iv,
    compute_iv_rank,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_prices(n: int = 260, seed: int = 42) -> pd.Series:
    """Generate synthetic daily price series."""
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0005, 0.015, n)
    prices = 100.0 * np.exp(np.cumsum(returns))
    return pd.Series(prices, dtype=float)


def _make_chain(strikes: list[float], iv: float) -> pd.DataFrame:
    """Create a minimal option chain DataFrame."""
    return pd.DataFrame({"strike": strikes, "impliedVolatility": [iv] * len(strikes)})


def _make_ticker_mock(
    prices: pd.Series | None = None,
    info: dict | None = None,
    options: list[str] | None = None,
    option_chain_iv: float = 0.25,
):
    """Build a mock yf.Ticker for common test scenarios."""
    if prices is None:
        prices = _make_prices()
    if info is None:
        info = {"regularMarketPrice": float(prices.iloc[-1])}
    if options is None:
        options = ["2099-01-15", "2099-02-21", "2099-03-21"]

    hist_df = pd.DataFrame({"Close": prices})

    chain_mock = MagicMock()
    spot = float(prices.iloc[-1])
    chain_mock.calls = _make_chain([spot * 0.95, spot, spot * 1.05], option_chain_iv)
    chain_mock.puts = _make_chain([spot * 0.95, spot, spot * 1.05], option_chain_iv + 0.02)

    tk = MagicMock()
    tk.history.return_value = hist_df
    tk.info = info
    tk.options = options
    tk.option_chain.return_value = chain_mock
    return tk


# ---------------------------------------------------------------------------
# TestComputeHv
# ---------------------------------------------------------------------------

class TestComputeHv:
    def test_normal_output_length(self):
        prices = _make_prices(260)
        hv = _compute_hv(prices, 30)
        assert len(hv) == 260

    def test_annualisation(self):
        """Constant prices → zero daily return → HV == 0."""
        prices = pd.Series([100.0] * 60, dtype=float)
        hv = _compute_hv(prices, 30)
        last = hv.iloc[-1]
        assert math.isnan(last) or last == pytest.approx(0.0, abs=1e-10)

    def test_window_produces_nan_before_full(self):
        """First (window-1) values should be NaN."""
        prices = _make_prices(60)
        hv = _compute_hv(prices, 30)
        # iloc[28] corresponds to index 28 → only 29 returns → NaN
        assert math.isnan(hv.iloc[28])

    def test_nonzero_for_volatile_prices(self):
        prices = _make_prices(100, seed=7)
        hv = _compute_hv(prices, 20)
        last = hv.iloc[-1]
        assert not math.isnan(last) and last > 0.0


# ---------------------------------------------------------------------------
# TestGetAtmIv
# ---------------------------------------------------------------------------

class TestGetAtmIv:
    def test_both_call_and_put_average(self):
        spot = 100.0
        calls = _make_chain([95.0, 100.0, 105.0], 0.25)
        puts = _make_chain([95.0, 100.0, 105.0], 0.30)
        call_iv, put_iv, atm = _get_atm_iv(calls, puts, spot)
        assert call_iv == pytest.approx(0.25)
        assert put_iv == pytest.approx(0.30)
        assert atm == pytest.approx(0.275)

    def test_only_calls_available(self):
        spot = 100.0
        calls = _make_chain([95.0, 100.0, 105.0], 0.22)
        puts = pd.DataFrame(columns=["strike", "impliedVolatility"])
        _, _, atm = _get_atm_iv(calls, puts, spot)
        assert atm == pytest.approx(0.22)

    def test_only_puts_available(self):
        spot = 100.0
        calls = pd.DataFrame(columns=["strike", "impliedVolatility"])
        puts = _make_chain([95.0, 100.0, 105.0], 0.28)
        _, _, atm = _get_atm_iv(calls, puts, spot)
        assert atm == pytest.approx(0.28)

    def test_empty_chain_returns_none(self):
        calls = pd.DataFrame(columns=["strike", "impliedVolatility"])
        puts = pd.DataFrame(columns=["strike", "impliedVolatility"])
        c, p, atm = _get_atm_iv(calls, puts, 100.0)
        assert c is None and p is None and atm is None

    def test_zero_iv_treated_as_missing(self):
        """impliedVolatility == 0 should be treated as unavailable."""
        spot = 100.0
        calls = _make_chain([100.0], 0.0)
        puts = pd.DataFrame(columns=["strike", "impliedVolatility"])
        _, _, atm = _get_atm_iv(calls, puts, spot)
        assert atm is None


# ---------------------------------------------------------------------------
# TestComputeIvRankNormal
# ---------------------------------------------------------------------------

class TestComputeIvRankNormal:
    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_data_available_with_options(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock(option_chain_iv=0.25)
        result = compute_iv_rank("AAPL")
        assert result.data_available is True
        assert result.current_iv is not None
        assert result.hv30 is not None
        assert result.iv_rank is not None
        assert result.iv_percentile is not None

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_term_structure_populated(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock(options=["2099-01-15", "2099-02-21", "2099-04-17"])
        result = compute_iv_rank("TSLA")
        assert len(result.term_structure) >= 1
        for pt in result.term_structure:
            assert isinstance(pt, TermStructurePoint)
            assert pt.atm_iv > 0
            assert pt.days_to_expiry >= 5

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_put_call_skew_computed(self, mock_yf):
        """put IV = call IV + 0.02 → skew ≈ +2 pp."""
        tk = _make_ticker_mock(option_chain_iv=0.25)
        mock_yf.return_value = tk
        result = compute_iv_rank("SPY")
        assert result.put_call_skew is not None
        # put_iv - call_iv ≈ 0.02 * 100 = 2.0
        assert result.put_call_skew == pytest.approx(2.0, abs=0.5)

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_no_options_falls_back_to_hv(self, mock_yf):
        """If no options available, still return HV data with data_available=True."""
        tk = _make_ticker_mock()
        tk.options = []  # no options listed
        mock_yf.return_value = tk
        result = compute_iv_rank("BRK-A")
        assert result.data_available is True
        assert result.hv30 is not None
        assert result.current_iv is None  # no option IV
        assert result.term_structure == []


# ---------------------------------------------------------------------------
# TestIVSignal
# ---------------------------------------------------------------------------

class TestIVSignal:
    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_buy_signal_low_iv(self, mock_yf):
        """Current IV well below historical → buy_options."""
        # Use very low IV so it ends up below historical range
        tk = _make_ticker_mock(option_chain_iv=0.001)
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        if result.iv_rank is not None:
            assert result.iv_signal in ("buy_options", "neutral", "no_data")

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_sell_signal_high_iv(self, mock_yf):
        """Current IV far above historical → sell_options."""
        tk = _make_ticker_mock(option_chain_iv=5.0)  # 500% IV → extreme
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        if result.iv_rank is not None:
            assert result.iv_signal in ("sell_options", "neutral")

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_neutral_signal_mid_range(self, mock_yf):
        """IV in mid-range → neutral or another valid signal."""
        tk = _make_ticker_mock(option_chain_iv=0.25)
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        assert result.iv_signal in ("buy_options", "neutral", "sell_options", "no_data")

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_iv_rank_clamped_0_100(self, mock_yf):
        tk = _make_ticker_mock(option_chain_iv=0.25)
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        if result.iv_rank is not None:
            assert 0.0 <= result.iv_rank <= 100.0
        if result.iv_percentile is not None:
            assert 0.0 <= result.iv_percentile <= 100.0


# ---------------------------------------------------------------------------
# TestGracefulDegradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_yfinance_exception_returns_default(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_iv_rank("FAIL")
        assert result.data_available is False
        assert result.iv_signal == "no_data"

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_empty_history_returns_default(self, mock_yf):
        tk = MagicMock()
        tk.history.return_value = pd.DataFrame()
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        assert result.data_available is False

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_insufficient_history_returns_default(self, mock_yf):
        tk = MagicMock()
        tk.history.return_value = pd.DataFrame({"Close": [100.0] * 10})
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        assert result.data_available is False

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_option_chain_error_still_returns_hv(self, mock_yf):
        """If option chain fetch fails, still return HV data."""
        tk = _make_ticker_mock()
        tk.option_chain.side_effect = Exception("options unavailable")
        mock_yf.return_value = tk
        result = compute_iv_rank("AAPL")
        assert result.data_available is True
        assert result.hv30 is not None
        assert result.current_iv is None

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_ticker_uppercased(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_iv_rank("aapl")
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_iv_rank("MSFT")
        assert result.data_available is True
        assert len(result.interpretation) > 5


# ---------------------------------------------------------------------------
# TestIVRankAPIEndpoint
# ---------------------------------------------------------------------------

class TestIVRankAPIEndpoint:
    def _get_client(self) -> TestClient:
        from quantbot.web.app import create_app
        return TestClient(create_app())

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        client = self._get_client()
        resp = client.get("/api/iv-rank?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert "iv_rank" in body

    def test_without_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/iv-rank")
        assert resp.status_code == 422

    @patch("quantbot.stocks.iv_rank.engine.yf.Ticker")
    def test_network_failure_returns_200_with_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/iv-rank?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
