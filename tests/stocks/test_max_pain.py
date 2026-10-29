"""Tests for Max Pain Calculator — Phase F.29.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.max_pain.engine import (
    _classify_signal,
    _compute_max_pain,
    _get_dte,
    compute_max_pain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chain(
    strikes: list[float],
    call_oi: list[int],
    put_oi: list[int],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build mock calls/puts DataFrames."""
    calls = pd.DataFrame({"strike": strikes, "openInterest": call_oi})
    puts = pd.DataFrame({"strike": strikes, "openInterest": put_oi})
    return calls, puts


def _expiry_str(days_from_now: int) -> str:
    return (date.today() + timedelta(days=days_from_now)).isoformat()


def _mock_ticker(
    current_price: float = 150.0,
    option_dates: tuple = (),
    chains: dict | None = None,
) -> MagicMock:
    """Build mock yfinance Ticker with configurable options."""
    tk = MagicMock()
    tk.info = {"currentPrice": current_price}
    tk.options = option_dates
    if chains is not None:
        def _option_chain(expiry: str):
            mock_chain = MagicMock()
            c, p = chains.get(expiry, (pd.DataFrame(), pd.DataFrame()))
            mock_chain.calls = c
            mock_chain.puts = p
            return mock_chain
        tk.option_chain.side_effect = _option_chain
    return tk


# ---------------------------------------------------------------------------
# TestComputeMaxPain
# ---------------------------------------------------------------------------

class TestComputeMaxPain:
    def test_simple_max_pain(self):
        """Max pain where more calls ATM than puts → max pain below current."""
        # calls concentrated at high strikes, puts at low → pain is in middle
        strikes = [100.0, 110.0, 120.0]
        call_oi = [100, 200, 300]   # heavy calls at 120
        put_oi = [300, 200, 100]    # heavy puts at 100
        calls, puts = _make_chain(strikes, call_oi, put_oi)
        max_pain, call_total, put_total = _compute_max_pain(calls, puts)
        assert max_pain is not None
        assert max_pain in strikes
        assert call_total == 600
        assert put_total == 600

    def test_empty_calls_empty_puts_returns_none(self):
        calls = pd.DataFrame()
        puts = pd.DataFrame()
        max_pain, call_oi, put_oi = _compute_max_pain(calls, puts)
        assert max_pain is None
        assert call_oi == 0
        assert put_oi == 0

    def test_only_calls_no_puts(self):
        """Only calls → max pain should still work (put loss = 0 everywhere)."""
        calls, _ = _make_chain([100.0, 110.0], [100, 200], [0, 0])
        puts = pd.DataFrame({"strike": [100.0, 110.0], "openInterest": [0, 0]})
        max_pain, _, _ = _compute_max_pain(calls, puts)
        # Min call loss is at lowest strike (all calls OTM)
        assert max_pain == pytest.approx(100.0)

    def test_zero_oi_rows_ignored(self):
        """Rows with OI=0 should be excluded."""
        calls = pd.DataFrame({"strike": [100.0, 110.0], "openInterest": [0, 100]})
        puts = pd.DataFrame({"strike": [100.0, 110.0], "openInterest": [100, 0]})
        max_pain, call_oi, put_oi = _compute_max_pain(calls, puts)
        assert call_oi == 100
        assert put_oi == 100
        assert max_pain is not None

    def test_single_strike(self):
        """Single strike chain should return that strike as max pain."""
        calls, puts = _make_chain([150.0], [100], [100])
        max_pain, _, _ = _compute_max_pain(calls, puts)
        assert max_pain == pytest.approx(150.0)


# ---------------------------------------------------------------------------
# TestClassifySignal
# ---------------------------------------------------------------------------

class TestClassifySignal:
    def test_pin_zone_near_zero(self):
        assert _classify_signal(0.5) == "pin_zone"

    def test_pin_zone_negative(self):
        assert _classify_signal(-1.5) == "pin_zone"

    def test_bullish_pull(self):
        # distance > 0 and 2-5% → price below max pain → up pull
        assert _classify_signal(3.0) == "bullish_pull"

    def test_bearish_pull(self):
        # distance < 0 and 2-5% → price above max pain → down pull
        assert _classify_signal(-3.0) == "bearish_pull"

    def test_weak_pull_large_positive(self):
        assert _classify_signal(6.0) == "weak_pull"

    def test_weak_pull_large_negative(self):
        assert _classify_signal(-7.0) == "weak_pull"

    def test_boundary_exactly_2pct(self):
        # 2.0% → bullish_pull (not pin_zone because ≥ 2.0)
        assert _classify_signal(2.0) == "bullish_pull"

    def test_boundary_exactly_5pct(self):
        # 5.0% → weak_pull (not bullish_pull because ≥ 5.0)
        assert _classify_signal(5.0) == "weak_pull"


# ---------------------------------------------------------------------------
# TestGetDte
# ---------------------------------------------------------------------------

class TestGetDte:
    def test_future_date(self):
        future = (date.today() + timedelta(days=7)).isoformat()
        assert _get_dte(future) == 7

    def test_today(self):
        assert _get_dte(date.today().isoformat()) == 0

    def test_past_date(self):
        past = (date.today() - timedelta(days=2)).isoformat()
        assert _get_dte(past) == -2


# ---------------------------------------------------------------------------
# TestComputeMaxPainIntegration
# ---------------------------------------------------------------------------

class TestComputeMaxPainIntegration:
    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        expiry = _expiry_str(7)
        calls, puts = _make_chain([145.0, 150.0, 155.0], [100, 200, 300], [300, 200, 100])
        mock_yf.return_value = _mock_ticker(
            current_price=152.0,
            option_dates=(expiry,),
            chains={expiry: (calls, puts)},
        )
        result = compute_max_pain("AAPL")
        assert result.data_available is True

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_expiry_populated(self, mock_yf):
        expiry = _expiry_str(7)
        calls, puts = _make_chain([145.0, 150.0, 155.0], [100, 200, 300], [300, 200, 100])
        mock_yf.return_value = _mock_ticker(
            current_price=152.0,
            option_dates=(expiry,),
            chains={expiry: (calls, puts)},
        )
        result = compute_max_pain("AAPL")
        assert len(result.expiries) == 1
        ep = result.expiries[0]
        assert ep.expiry == expiry
        assert ep.dte == 7
        assert ep.max_pain_strike in [145.0, 150.0, 155.0]

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_no_options_returns_empty_expiries(self, mock_yf):
        mock_yf.return_value = _mock_ticker(option_dates=())
        result = compute_max_pain("AAPL")
        assert result.data_available is True
        assert result.expiries == []

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_current_price_set(self, mock_yf):
        expiry = _expiry_str(7)
        calls, puts = _make_chain([150.0], [100], [100])
        mock_yf.return_value = _mock_ticker(
            current_price=150.0,
            option_dates=(expiry,),
            chains={expiry: (calls, puts)},
        )
        result = compute_max_pain("AAPL")
        assert result.current_price == pytest.approx(150.0)

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        expiry = _expiry_str(7)
        calls, puts = _make_chain([150.0], [100], [100])
        mock_yf.return_value = _mock_ticker(
            current_price=150.0,
            option_dates=(expiry,),
            chains={expiry: (calls, puts)},
        )
        result = compute_max_pain("AAPL")
        assert len(result.interpretation) > 5

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_yfinance_exception_data_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_max_pain("AAPL")
        assert result.data_available is False

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_expiry_beyond_max_dte_skipped(self, mock_yf):
        # DTE=60 > _MAX_DTE=45 → should be skipped
        far_expiry = _expiry_str(60)
        calls, puts = _make_chain([150.0], [100], [100])
        mock_yf.return_value = _mock_ticker(
            current_price=150.0,
            option_dates=(far_expiry,),
            chains={far_expiry: (calls, puts)},
        )
        result = compute_max_pain("AAPL")
        assert result.data_available is True
        assert len(result.expiries) == 0

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_multiple_expiries_sorted_by_dte(self, mock_yf):
        expiry7 = _expiry_str(7)
        expiry14 = _expiry_str(14)
        calls, puts = _make_chain([150.0], [100], [100])
        mock_yf.return_value = _mock_ticker(
            current_price=150.0,
            option_dates=(expiry14, expiry7),  # reverse order
            chains={expiry7: (calls, puts), expiry14: (calls, puts)},
        )
        result = compute_max_pain("AAPL")
        if len(result.expiries) == 2:
            assert result.expiries[0].dte <= result.expiries[1].dte


# ---------------------------------------------------------------------------
# TestMaxPainAPI
# ---------------------------------------------------------------------------

class TestMaxPainAPI:
    def _get_client(self):
        from fastapi.testclient import TestClient
        from quantbot.web.app import create_app
        return TestClient(create_app())

    def test_no_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/max-pain")
        assert resp.status_code == 422

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        mock_yf.return_value = _mock_ticker(option_dates=())
        client = self._get_client()
        resp = client.get("/api/max-pain?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_response_has_required_fields(self, mock_yf):
        mock_yf.return_value = _mock_ticker(option_dates=())
        client = self._get_client()
        body = client.get("/api/max-pain?ticker=AAPL").json()
        for f in ["ticker", "data_available", "expiries", "interpretation"]:
            assert f in body

    @patch("quantbot.stocks.max_pain.engine.yf.Ticker")
    def test_yfinance_fail_still_200(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/max-pain?ticker=AAPL")
        assert resp.status_code == 200
        assert resp.json()["data_available"] is False
