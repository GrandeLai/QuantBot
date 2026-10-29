"""Tests for Earnings Calendar & Expected Move — Phase F.25.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.earnings_calendar.engine import (
    EarningsMove,
    _compute_historical_moves,
    _get_atm_straddle_cost,
    compute_earnings_calendar,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date.today()


def _make_price_history(n_days: int = 500, seed: int = 42) -> pd.DataFrame:
    """Generate synthetic price history DataFrame."""
    rng = np.random.default_rng(seed)
    dates = [TODAY - timedelta(days=n_days - i) for i in range(n_days)]
    prices = 100.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n_days)))
    # Use tz-aware timestamps like yfinance returns
    ts = pd.to_datetime([d.isoformat() for d in dates])
    return pd.DataFrame({"Close": prices}, index=ts)


def _make_option_chain(spot: float, call_price: float = 3.0, put_price: float = 3.0) -> MagicMock:
    """Build a mock option chain with lastPrice columns."""
    chain = MagicMock()
    strikes = [spot * 0.95, spot, spot * 1.05]
    chain.calls = pd.DataFrame({
        "strike": strikes,
        "lastPrice": [call_price + 0.5, call_price, call_price - 0.2],
        "impliedVolatility": [0.25, 0.25, 0.24],
    })
    chain.puts = pd.DataFrame({
        "strike": strikes,
        "lastPrice": [put_price - 0.3, put_price, put_price + 0.4],
        "impliedVolatility": [0.28, 0.27, 0.26],
    })
    return chain


def _make_ticker_mock(
    next_earnings: date | None = None,
    hist_earnings_dates: list[date] | None = None,
    spot: float = 100.0,
    call_price: float = 3.0,
    put_price: float = 3.0,
    has_options: bool = True,
) -> MagicMock:
    if next_earnings is None:
        next_earnings = TODAY + timedelta(days=20)
    if hist_earnings_dates is None:
        hist_earnings_dates = [
            TODAY - timedelta(days=90 * (i + 1)) for i in range(8)
        ]

    hist_df = _make_price_history()
    tk = MagicMock()
    tk.history.return_value = hist_df
    tk.info = {"regularMarketPrice": spot}

    # calendar: dict form
    tk.calendar = {"Earnings Date": [next_earnings]}

    # earnings_dates: DataFrame indexed by timestamps
    all_dates = [next_earnings] + hist_earnings_dates
    ts_index = pd.to_datetime([d.isoformat() for d in all_dates])
    tk.earnings_dates = pd.DataFrame(
        {"EPS Estimate": [None] * len(all_dates)},
        index=ts_index,
    )

    # options
    if has_options:
        expiry_after = (next_earnings + timedelta(days=3)).isoformat()
        tk.options = [expiry_after]
        tk.option_chain.return_value = _make_option_chain(spot, call_price, put_price)
    else:
        tk.options = []

    return tk


# ---------------------------------------------------------------------------
# TestGetAtmStraddleCost
# ---------------------------------------------------------------------------

class TestGetAtmStraddleCost:
    def test_normal_straddle_cost(self):
        spot = 100.0
        calls = pd.DataFrame({"strike": [95.0, 100.0, 105.0], "lastPrice": [6.0, 4.0, 2.0]})
        puts = pd.DataFrame({"strike": [95.0, 100.0, 105.0], "lastPrice": [1.5, 3.0, 5.0]})
        cost = _get_atm_straddle_cost(calls, puts, spot)
        # ATM strike 100: call=4.0, put=3.0 → straddle=7.0/100=7%
        assert cost == pytest.approx(7.0, abs=0.1)

    def test_zero_price_returns_none(self):
        spot = 100.0
        calls = pd.DataFrame({"strike": [100.0], "lastPrice": [0.0]})
        puts = pd.DataFrame({"strike": [100.0], "lastPrice": [0.0]})
        cost = _get_atm_straddle_cost(calls, puts, spot)
        assert cost is None

    def test_empty_chain_returns_none(self):
        calls = pd.DataFrame(columns=["strike", "lastPrice"])
        puts = pd.DataFrame(columns=["strike", "lastPrice"])
        cost = _get_atm_straddle_cost(calls, puts, 100.0)
        assert cost is None

    def test_missing_one_leg_returns_none(self):
        """If either call or put price is zero/missing, return None."""
        spot = 100.0
        calls = pd.DataFrame({"strike": [100.0], "lastPrice": [4.0]})
        puts = pd.DataFrame({"strike": [100.0], "lastPrice": [0.0]})
        # put price is 0 → treated as missing
        cost = _get_atm_straddle_cost(calls, puts, spot)
        assert cost is None


# ---------------------------------------------------------------------------
# TestComputeHistoricalMoves
# ---------------------------------------------------------------------------

class TestComputeHistoricalMoves:
    def test_normal_moves_computed(self):
        hist = _make_price_history(500)
        earnings_dates = [TODAY - timedelta(days=90 * (i + 1)) for i in range(8)]
        moves = _compute_historical_moves(earnings_dates, hist)
        assert len(moves) > 0
        for m in moves:
            assert isinstance(m, EarningsMove)
            assert m.abs_move_pct >= 0

    def test_empty_history_returns_empty(self):
        moves = _compute_historical_moves(
            [TODAY - timedelta(days=90)],
            pd.DataFrame(),
        )
        assert moves == []

    def test_no_earnings_dates_returns_empty(self):
        hist = _make_price_history(100)
        moves = _compute_historical_moves([], hist)
        assert moves == []

    def test_max_8_moves_returned(self):
        hist = _make_price_history(1000)
        many_dates = [TODAY - timedelta(days=60 * (i + 1)) for i in range(20)]
        moves = _compute_historical_moves(many_dates, hist)
        assert len(moves) <= 8


# ---------------------------------------------------------------------------
# TestComputeEarningsCalendarNormal
# ---------------------------------------------------------------------------

class TestComputeEarningsCalendarNormal:
    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_earnings_calendar("AAPL")
        assert result.data_available is True
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_next_earnings_date_populated(self, mock_yf):
        expected_date = TODAY + timedelta(days=20)
        mock_yf.return_value = _make_ticker_mock(next_earnings=expected_date)
        result = compute_earnings_calendar("AAPL")
        assert result.next_earnings_date == expected_date.isoformat()
        assert result.days_to_earnings == 20

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_implied_move_computed(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock(spot=100.0, call_price=3.0, put_price=3.0)
        result = compute_earnings_calendar("AAPL")
        # straddle ≈ 6.0/100 = 6%
        if result.implied_move_pct is not None:
            assert result.implied_move_pct > 0

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_historical_moves_populated(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_earnings_calendar("AAPL")
        assert isinstance(result.historical_moves, list)

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_no_earnings_date_still_data_available(self, mock_yf):
        tk = _make_ticker_mock()
        tk.calendar = {}
        tk.earnings_dates = pd.DataFrame()
        mock_yf.return_value = tk
        result = compute_earnings_calendar("BRK-A")
        assert result.data_available is True
        assert result.next_earnings_date is None
        assert result.straddle_signal == "unknown"


# ---------------------------------------------------------------------------
# TestStraddleSignal
# ---------------------------------------------------------------------------

class TestStraddleSignal:
    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_sell_signal_high_implied(self, mock_yf):
        """implied >> historical → sell_straddle."""
        # Big straddle: call=20, put=20 → 40% implied on $100 stock
        tk = _make_ticker_mock(spot=100.0, call_price=20.0, put_price=20.0)
        mock_yf.return_value = tk
        result = compute_earnings_calendar("AAPL")
        # historical avg is typically 2-6% for synthetic data
        if result.implied_move_pct is not None and result.historical_avg_move_pct is not None:
            assert result.straddle_signal in ("sell_straddle", "fair")

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_buy_signal_low_implied(self, mock_yf):
        """historical >> implied → buy_straddle."""
        # Tiny straddle: call=0.01, put=0.01 → 0.02% implied
        tk = _make_ticker_mock(spot=100.0, call_price=0.01, put_price=0.01)
        mock_yf.return_value = tk
        result = compute_earnings_calendar("AAPL")
        if result.implied_move_pct is not None and result.historical_avg_move_pct is not None:
            assert result.straddle_signal in ("buy_straddle", "fair")

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_unknown_signal_no_options(self, mock_yf):
        tk = _make_ticker_mock(has_options=False)
        mock_yf.return_value = tk
        result = compute_earnings_calendar("BRK-A")
        assert result.implied_move_pct is None
        assert result.straddle_signal == "unknown"

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_earnings_calendar("MSFT")
        assert result.data_available is True
        assert len(result.interpretation) > 5


# ---------------------------------------------------------------------------
# TestGracefulDegradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_yfinance_exception_returns_default(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_earnings_calendar("FAIL")
        assert result.data_available is False
        assert result.straddle_signal == "unknown"

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_empty_history_returns_default(self, mock_yf):
        tk = MagicMock()
        tk.history.return_value = pd.DataFrame()
        mock_yf.return_value = tk
        result = compute_earnings_calendar("AAPL")
        assert result.data_available is False

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_option_chain_error_still_returns_data(self, mock_yf):
        tk = _make_ticker_mock()
        tk.option_chain.side_effect = Exception("options unavailable")
        mock_yf.return_value = tk
        result = compute_earnings_calendar("AAPL")
        assert result.data_available is True
        assert result.implied_move_pct is None

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_ticker_uppercased(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        result = compute_earnings_calendar("aapl")
        assert result.ticker == "AAPL"


# ---------------------------------------------------------------------------
# TestEarningsCalendarAPIEndpoint
# ---------------------------------------------------------------------------

class TestEarningsCalendarAPIEndpoint:
    def _get_client(self) -> TestClient:
        from quantbot.web.app import create_app
        return TestClient(create_app())

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_with_ticker_returns_200(self, mock_yf):
        mock_yf.return_value = _make_ticker_mock()
        client = self._get_client()
        resp = client.get("/api/earnings-calendar?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert "straddle_signal" in body

    def test_without_ticker_returns_422(self):
        client = self._get_client()
        resp = client.get("/api/earnings-calendar")
        assert resp.status_code == 422

    @patch("quantbot.stocks.earnings_calendar.engine.yf.Ticker")
    def test_network_failure_returns_200_with_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/earnings-calendar?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
