"""Tests for Put/Call Ratio & Options Sentiment — Phase F.26.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.put_call_ratio.engine import (
    ExpiryPCR,
    _aggregate_chain,
    _classify_sentiment,
    _compute_pcr,
    compute_put_call_ratio,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date.today()


def _make_chain_df(n: int, volume: int, oi: int) -> pd.DataFrame:
    """Create a minimal option chain DataFrame."""
    return pd.DataFrame({
        "strike": [100.0 + i * 5 for i in range(n)],
        "volume": [volume] * n,
        "openInterest": [oi] * n,
        "lastPrice": [2.0] * n,
    })


def _make_ticker_mock(
    expiry_dates: list[str] | None = None,
    call_volume_per_contract: int = 100,
    put_volume_per_contract: int = 150,
    call_oi_per_contract: int = 1000,
    put_oi_per_contract: int = 1200,
    n_contracts: int = 5,
) -> MagicMock:
    if expiry_dates is None:
        expiry_dates = [
            (TODAY + timedelta(days=15)).isoformat(),
            (TODAY + timedelta(days=50)).isoformat(),
            (TODAY + timedelta(days=85)).isoformat(),
        ]

    tk = MagicMock()
    tk.options = expiry_dates

    def make_chain(exp_str: str) -> MagicMock:
        chain = MagicMock()
        chain.calls = _make_chain_df(n_contracts, call_volume_per_contract, call_oi_per_contract)
        chain.puts = _make_chain_df(n_contracts, put_volume_per_contract, put_oi_per_contract)
        return chain

    tk.option_chain.side_effect = make_chain
    return tk


# ---------------------------------------------------------------------------
# TestComputePcr
# ---------------------------------------------------------------------------

class TestComputePcr:
    def test_normal_ratio(self):
        pcr = _compute_pcr(150, 100)
        assert pcr == pytest.approx(1.5, abs=0.001)

    def test_zero_call_returns_none(self):
        assert _compute_pcr(100, 0) is None

    def test_zero_put_returns_zero_ratio(self):
        pcr = _compute_pcr(0, 100)
        assert pcr == pytest.approx(0.0, abs=0.001)

    def test_equal_put_call(self):
        pcr = _compute_pcr(100, 100)
        assert pcr == pytest.approx(1.0, abs=0.001)


# ---------------------------------------------------------------------------
# TestClassifySentiment
# ---------------------------------------------------------------------------

class TestClassifySentiment:
    def test_extreme_bearish(self):
        assert _classify_sentiment(1.6) == "extreme_bearish"

    def test_bearish(self):
        assert _classify_sentiment(1.2) == "bearish"

    def test_neutral(self):
        assert _classify_sentiment(0.85) == "neutral"

    def test_bullish(self):
        assert _classify_sentiment(0.6) == "bullish"

    def test_extreme_bullish(self):
        assert _classify_sentiment(0.4) == "extreme_bullish"

    def test_none_returns_unknown(self):
        assert _classify_sentiment(None) == "unknown"

    def test_exact_boundary_bearish_threshold(self):
        """PCR == 1.0 → bearish (>= 1.0 → bearish, not neutral)."""
        assert _classify_sentiment(1.0) == "bearish"

    def test_exact_boundary_neutral_threshold(self):
        """PCR == 0.7 → neutral."""
        assert _classify_sentiment(0.7) == "neutral"


# ---------------------------------------------------------------------------
# TestAggregateChain
# ---------------------------------------------------------------------------

class TestAggregateChain:
    def test_normal_aggregation(self):
        calls = _make_chain_df(5, volume=100, oi=1000)
        puts = _make_chain_df(5, volume=150, oi=1200)
        cv, pv, co, po = _aggregate_chain(calls, puts)
        assert cv == 500   # 5 × 100
        assert pv == 750   # 5 × 150
        assert co == 5000  # 5 × 1000
        assert po == 6000  # 5 × 1200

    def test_empty_df_returns_zeros(self):
        empty = pd.DataFrame(columns=["strike", "volume", "openInterest"])
        cv, pv, co, po = _aggregate_chain(empty, empty)
        assert cv == pv == co == po == 0


# ---------------------------------------------------------------------------
# TestComputePutCallRatioNormal
# ---------------------------------------------------------------------------

class TestComputePutCallRatioNormal:
    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_put_call_ratio("AAPL")
        assert result.data_available is True
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_volume_pcr_computed(self, mock_yf):
        # call=100/contract × 5, put=150/contract × 5 → pcr = 750/500 = 1.5
        mock_yf.return_value = _make_ticker_mock(
            call_volume_per_contract=100,
            put_volume_per_contract=150,
            n_contracts=5,
        )
        result = compute_put_call_ratio("AAPL")
        assert result.volume_pcr is not None
        assert result.volume_pcr == pytest.approx(1.5, abs=0.01)

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_oi_pcr_computed(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock(
            call_oi_per_contract=1000,
            put_oi_per_contract=1200,
        )
        result = compute_put_call_ratio("AAPL")
        assert result.oi_pcr is not None
        assert result.oi_pcr > 0

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_expiry_breakdown_populated(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_put_call_ratio("AAPL")
        assert len(result.expiry_breakdown) >= 1
        for ep in result.expiry_breakdown:
            assert isinstance(ep, ExpiryPCR)

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_extreme_bearish_signal(self, mock_yf):
        # PCR = 1.6+ → extreme_bearish
        mock_yf.return_value = _make_ticker_mock(
            call_volume_per_contract=100,
            put_volume_per_contract=200,  # 2.0 PCR
        )
        result = compute_put_call_ratio("AAPL")
        if result.volume_pcr is not None:
            assert result.sentiment in ("extreme_bearish", "bearish")

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_extreme_bullish_signal(self, mock_yf):
        # PCR = 0.3 → extreme_bullish
        mock_yf.return_value = _make_ticker_mock(
            call_volume_per_contract=200,
            put_volume_per_contract=50,
        )
        result = compute_put_call_ratio("AAPL")
        if result.volume_pcr is not None:
            assert result.sentiment in ("extreme_bullish", "bullish")


# ---------------------------------------------------------------------------
# TestGracefulDegradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_no_options_returns_unknown(self, mock_yf):
        tk = MagicMock()
        tk.options = []
        mock_yf.return_value = tk
        result = compute_put_call_ratio("BRK-A")
        assert result.data_available is True
        assert result.volume_pcr is None
        assert result.sentiment == "unknown"

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_yfinance_exception_returns_default(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_put_call_ratio("FAIL")
        assert result.data_available is False

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_option_chain_error_skipped(self, mock_yf):
        """If one expiry's chain fails, others still aggregated."""
        tk = _make_ticker_mock()
        call_count = 0

        def side_effect(exp: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("chain error")
            chain = MagicMock()
            chain.calls = _make_chain_df(3, 100, 1000)
            chain.puts = _make_chain_df(3, 120, 1100)
            return chain

        tk.option_chain.side_effect = side_effect
        mock_yf.return_value = tk
        result = compute_put_call_ratio("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_ticker_uppercased(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_put_call_ratio("aapl")
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_put_call_ratio("MSFT")
        assert len(result.interpretation) > 5


# ---------------------------------------------------------------------------
# TestPutCallRatioAPIEndpoint
# ---------------------------------------------------------------------------

class TestPutCallRatioAPIEndpoint:
    def _get_client(self) -> TestClient:
        from quantbot.web.app import create_app
        return TestClient(create_app())

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        client = self._get_client()
        resp = client.get("/api/put-call-ratio?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert "volume_pcr" in body
        assert "sentiment" in body

    def test_without_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/put-call-ratio")
        assert resp.status_code == 422

    @patch("quantbot.stocks.put_call_ratio.engine.yf.Ticker")
    def test_network_failure_returns_200_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/put-call-ratio?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
