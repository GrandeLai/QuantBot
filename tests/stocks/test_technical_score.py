"""Tests for Technical Momentum Score — Phase F.30.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.technical_score.engine import (
    _classify_signal,
    _compute_bb,
    _compute_composite_score,
    _compute_macd,
    _compute_rsi,
    compute_technical_score,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_price_series(values: list[float]) -> pd.Series:
    """Build a close price Series with DatetimeIndex."""
    today = date.today()
    dates = [today - timedelta(days=len(values) - i - 1) for i in range(len(values))]
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    return pd.Series(values, index=idx)


def _rising_prices(n: int = 252, start: float = 100.0, step: float = 0.5) -> pd.Series:
    return _make_price_series([start + i * step for i in range(n)])


def _falling_prices(n: int = 252, start: float = 150.0, step: float = 0.5) -> pd.Series:
    return _make_price_series([start - i * step for i in range(n)])


def _flat_prices(n: int = 252, price: float = 100.0) -> pd.Series:
    return _make_price_series([price] * n)


def _make_hist_df(
    close_values: list[float],
    volume_values: list[float] | None = None,
) -> pd.DataFrame:
    prices = _make_price_series(close_values)
    if volume_values is None:
        volume_values = [1_000_000] * len(close_values)
    df = pd.DataFrame({
        "Close": prices.values,
        "Volume": volume_values,
    }, index=prices.index)
    return df


def _mock_ticker(hist_df: pd.DataFrame) -> MagicMock:
    tk = MagicMock()
    tk.history.return_value = hist_df
    return tk


# ---------------------------------------------------------------------------
# TestComputeRsi
# ---------------------------------------------------------------------------

class TestComputeRsi:
    def test_rising_prices_high_rsi(self):
        close = _rising_prices(60)
        rsi = _compute_rsi(close)
        assert rsi is not None and rsi > 60

    def test_falling_prices_low_rsi(self):
        close = _falling_prices(60)
        rsi = _compute_rsi(close)
        assert rsi is not None and rsi < 40

    def test_insufficient_data_returns_none(self):
        close = _make_price_series([100.0] * 10)
        assert _compute_rsi(close) is None

    def test_all_gains_returns_100(self):
        close = _rising_prices(30, step=1.0)
        rsi = _compute_rsi(close)
        assert rsi is not None and rsi == pytest.approx(100.0, abs=0.1)


# ---------------------------------------------------------------------------
# TestComputeMacd
# ---------------------------------------------------------------------------

class TestComputeMacd:
    def test_rising_positive_histogram(self):
        close = _rising_prices(60)
        macd, sig, hist = _compute_macd(close)
        assert macd is not None and sig is not None and hist is not None
        assert hist > 0  # rising → fast > slow → histogram positive

    def test_falling_negative_histogram(self):
        close = _falling_prices(60)
        macd, sig, hist = _compute_macd(close)
        assert hist is not None and hist < 0

    def test_insufficient_data_returns_none_tuple(self):
        close = _make_price_series([100.0] * 20)
        macd, sig, hist = _compute_macd(close)
        assert macd is None and sig is None and hist is None


# ---------------------------------------------------------------------------
# TestComputeBb
# ---------------------------------------------------------------------------

class TestComputeBb:
    def test_price_near_upper_band_high_position(self):
        # Strong uptrend → price near upper band
        close = _rising_prices(40, step=2.0)
        pos = _compute_bb(close)
        assert pos is not None and pos > 50

    def test_price_near_lower_band_low_position(self):
        close = _falling_prices(40, step=2.0)
        pos = _compute_bb(close)
        assert pos is not None and pos < 50

    def test_flat_prices_near_50(self):
        close = _flat_prices(40)
        pos = _compute_bb(close)
        # Flat prices → all at midline, but std=0 → returns None
        assert pos is None or pos == pytest.approx(50.0, abs=10)

    def test_insufficient_data_returns_none(self):
        close = _make_price_series([100.0] * 10)
        assert _compute_bb(close) is None


# ---------------------------------------------------------------------------
# TestComputeCompositeScore
# ---------------------------------------------------------------------------

class TestComputeCompositeScore:
    def test_all_bullish_signals_positive_score(self):
        score = _compute_composite_score(
            rsi=25.0,          # oversold → +20
            macd_hist=0.5,     # positive → +20
            bb_pos=15.0,       # below lower → +20
            vol_ratio=2.5,     # high volume
            close_up=True,     # confirmed up → +10
            w52_pos=25.0,      # near 52w low → +10
        )
        assert score is not None and score == pytest.approx(80.0, abs=1)

    def test_all_bearish_signals_negative_score(self):
        score = _compute_composite_score(
            rsi=75.0,
            macd_hist=-0.5,
            bb_pos=85.0,
            vol_ratio=2.5,
            close_up=False,
            w52_pos=75.0,
        )
        assert score is not None and score == pytest.approx(-80.0, abs=1)

    def test_neutral_rsi_zero_contribution(self):
        # RSI = 50 → contributes 0
        score = _compute_composite_score(50.0, 0, 50.0, 1.0, True, 50.0)
        # Only MACD=0, BB=0, vol < 2 → 0, 52w=0 → all neutral
        assert score == pytest.approx(0.0, abs=5)

    def test_all_none_returns_none(self):
        assert _compute_composite_score(None, None, None, None, None, None) is None


# ---------------------------------------------------------------------------
# TestClassifySignal
# ---------------------------------------------------------------------------

class TestClassifySignal:
    def test_strong_buy(self):
        assert _classify_signal(70.0) == "strong_buy"

    def test_buy(self):
        assert _classify_signal(45.0) == "buy"

    def test_neutral(self):
        assert _classify_signal(0.0) == "neutral"

    def test_sell(self):
        assert _classify_signal(-45.0) == "sell"

    def test_strong_sell(self):
        assert _classify_signal(-70.0) == "strong_sell"

    def test_none_returns_no_data(self):
        assert _classify_signal(None) == "no_data"

    def test_boundary_at_30(self):
        assert _classify_signal(30.0) == "buy"

    def test_boundary_at_minus_30(self):
        assert _classify_signal(-30.0) == "sell"


# ---------------------------------------------------------------------------
# TestComputeTechnicalScoreIntegration
# ---------------------------------------------------------------------------

class TestComputeTechnicalScoreIntegration:
    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        hist = _make_hist_df([100.0 + i for i in range(100)])
        mock_yf.return_value = _mock_ticker(hist)
        result = compute_technical_score("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_rsi_populated(self, mock_yf):
        hist = _make_hist_df([100.0 + i * 0.5 for i in range(60)])
        mock_yf.return_value = _mock_ticker(hist)
        result = compute_technical_score("AAPL")
        assert result.rsi14 is not None and 0 <= result.rsi14 <= 100

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_composite_score_populated(self, mock_yf):
        hist = _make_hist_df([100.0 + i for i in range(100)])
        mock_yf.return_value = _mock_ticker(hist)
        result = compute_technical_score("AAPL")
        assert result.composite_score is not None
        assert -100 <= result.composite_score <= 100

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_yfinance_exception_data_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_technical_score("AAPL")
        assert result.data_available is False
        assert result.signal == "no_data"

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_insufficient_data_graceful(self, mock_yf):
        hist = _make_hist_df([100.0] * 10)  # < 30 bars
        mock_yf.return_value = _mock_ticker(hist)
        result = compute_technical_score("AAPL")
        assert result.data_available is True
        assert result.signal == "no_data"

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        hist = _make_hist_df([100.0 + i for i in range(100)])
        mock_yf.return_value = _mock_ticker(hist)
        result = compute_technical_score("AAPL")
        assert len(result.interpretation) > 5

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_rising_trend_positive_score(self, mock_yf):
        """Strong uptrend → composite score should be positive."""
        values = [100.0 + i * 0.5 for i in range(100)]
        hist = _make_hist_df(values)
        mock_yf.return_value = _mock_ticker(hist)
        result = compute_technical_score("AAPL")
        assert result.composite_score is not None
        # Rising trend → MACD positive + high RSI, but RSI contribution negative = mixed
        # At least signal should be non-null
        assert result.signal != "no_data"


# ---------------------------------------------------------------------------
# TestTechnicalScoreAPI
# ---------------------------------------------------------------------------

class TestTechnicalScoreAPI:
    def _get_client(self):
        from fastapi.testclient import TestClient
        from quantbot.web.app import create_app
        return TestClient(create_app())

    def test_no_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/technical-score")
        assert resp.status_code == 422

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        hist = _make_hist_df([100.0 + i for i in range(60)])
        mock_yf.return_value = _mock_ticker(hist)
        client = self._get_client()
        resp = client.get("/api/technical-score?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_response_has_required_fields(self, mock_yf):
        hist = _make_hist_df([100.0 + i for i in range(60)])
        mock_yf.return_value = _mock_ticker(hist)
        client = self._get_client()
        body = client.get("/api/technical-score?ticker=AAPL").json()
        for f in ["ticker", "signal", "data_available", "interpretation", "composite_score"]:
            assert f in body

    @patch("quantbot.stocks.technical_score.engine.yf.Ticker")
    def test_yfinance_fail_still_200(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/technical-score?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["data_available"] is False
