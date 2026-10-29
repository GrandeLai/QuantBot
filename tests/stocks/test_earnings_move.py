"""Tests for earnings_move engine and API endpoint.

Covers:
- _earnings_move_grade boundary thresholds
- compute_earnings_move happy path (mocked yfinance)
- graceful degradation (no options / no calendar / exception)
- GET /api/earnings-move 200 + 422
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.earnings_move.engine import (
    _atm_straddle,
    _earnings_move_grade,
    _find_nearest_expiry_after,
    compute_earnings_move,
)


# ---------------------------------------------------------------------------
# AC-4: _earnings_move_grade — boundary tests
# ---------------------------------------------------------------------------


class TestEarningsMoveGrade:
    def test_above_10_is_large(self) -> None:
        assert _earnings_move_grade(10.1) == "large_expected"

    def test_exactly_10_is_medium(self) -> None:
        # >10 = large; 10.0 is NOT >10 → medium
        assert _earnings_move_grade(10.0) == "medium_expected"

    def test_above_5_is_medium(self) -> None:
        assert _earnings_move_grade(7.5) == "medium_expected"

    def test_exactly_5_is_small(self) -> None:
        assert _earnings_move_grade(5.0) == "small_expected"

    def test_zero_is_small(self) -> None:
        assert _earnings_move_grade(0.0) == "small_expected"

    def test_large_pct(self) -> None:
        assert _earnings_move_grade(25.0) == "large_expected"


# ---------------------------------------------------------------------------
# AC-4: _find_nearest_expiry_after
# ---------------------------------------------------------------------------


class TestFindNearestExpiry:
    def test_finds_exact_match(self) -> None:
        expiries = ["2025-01-17", "2025-02-21", "2025-03-21"]
        assert _find_nearest_expiry_after(expiries, "2025-02-21") == "2025-02-21"

    def test_finds_next_after(self) -> None:
        expiries = ["2025-01-17", "2025-02-21", "2025-03-21"]
        assert _find_nearest_expiry_after(expiries, "2025-02-01") == "2025-02-21"

    def test_returns_none_if_all_before(self) -> None:
        expiries = ["2025-01-17", "2025-02-21"]
        result = _find_nearest_expiry_after(expiries, "2025-12-01")
        assert result is None

    def test_unsorted_input(self) -> None:
        expiries = ["2025-03-21", "2025-01-17", "2025-02-21"]
        assert _find_nearest_expiry_after(expiries, "2025-02-15") == "2025-02-21"


# ---------------------------------------------------------------------------
# AC-4: _atm_straddle
# ---------------------------------------------------------------------------


def _make_chain_df(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestAtmStraddle:
    def test_finds_atm_strike(self) -> None:
        calls = _make_chain_df([
            {"strike": 195.0, "lastPrice": 8.0, "bid": 7.8, "ask": 8.2},
            {"strike": 200.0, "lastPrice": 5.0, "bid": 4.8, "ask": 5.2},
            {"strike": 205.0, "lastPrice": 2.5, "bid": 2.3, "ask": 2.7},
        ])
        puts = _make_chain_df([
            {"strike": 195.0, "lastPrice": 2.0, "bid": 1.8, "ask": 2.2},
            {"strike": 200.0, "lastPrice": 4.8, "bid": 4.6, "ask": 5.0},
            {"strike": 205.0, "lastPrice": 7.5, "bid": 7.3, "ask": 7.7},
        ])
        strike, call_p, put_p = _atm_straddle(calls, puts, 200.0)
        assert strike == 200.0
        assert call_p == 5.0
        assert put_p == 4.8

    def test_empty_chains_return_none(self) -> None:
        result = _atm_straddle(pd.DataFrame(), pd.DataFrame(), 200.0)
        assert result == (None, None, None)

    def test_no_strike_column_returns_none(self) -> None:
        df = _make_chain_df([{"lastPrice": 5.0}])
        result = _atm_straddle(df, df, 200.0)
        assert result == (None, None, None)


# ---------------------------------------------------------------------------
# Helpers for mocking yfinance
# ---------------------------------------------------------------------------


def _make_yf_ticker(
    current_price: float | None,
    earnings_str: str | None,
    expiries: list[str],
    calls_df: pd.DataFrame,
    puts_df: pd.DataFrame,
) -> MagicMock:
    mock_chain = MagicMock()
    mock_chain.calls = calls_df
    mock_chain.puts = puts_df

    mock_ticker = MagicMock()
    mock_ticker.info = {
        "regularMarketPrice": current_price,
        "currentPrice": current_price,
    }

    if earnings_str:
        mock_ticker.calendar = {"Earnings Date": [earnings_str]}
    else:
        mock_ticker.calendar = {}

    mock_ticker.options = expiries if expiries else None
    mock_ticker.option_chain.return_value = mock_chain
    return mock_ticker


# ---------------------------------------------------------------------------
# AC-4: compute_earnings_move — happy path
# ---------------------------------------------------------------------------


class TestComputeEarningsMove:
    def _future_earnings(self, days: int = 14) -> str:
        return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")

    def _future_expiry(self, days: int = 21) -> str:
        return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")

    def test_happy_path_large_expected(self) -> None:
        earnings = self._future_earnings(14)
        expiry = self._future_expiry(21)
        calls = _make_chain_df([
            {"strike": 200.0, "lastPrice": 12.0, "bid": 11.5, "ask": 12.5},
        ])
        puts = _make_chain_df([
            {"strike": 200.0, "lastPrice": 11.0, "bid": 10.5, "ask": 11.5},
        ])
        mock_ticker = _make_yf_ticker(200.0, earnings, [expiry], calls, puts)

        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("AAPL")

        assert result.data_available is True
        assert result.ticker == "AAPL"
        assert result.expected_move_pct is not None
        # (12 + 11) / 200 * 100 = 11.5%
        assert abs(result.expected_move_pct - 11.5) < 0.1
        assert result.grade == "large_expected"
        assert result.days_to_earnings == 14
        assert result.as_of_date == date.today()

    def test_happy_path_small_expected(self) -> None:
        earnings = self._future_earnings(7)
        expiry = self._future_expiry(14)
        calls = _make_chain_df([
            {"strike": 100.0, "lastPrice": 2.0, "bid": 1.8, "ask": 2.2},
        ])
        puts = _make_chain_df([
            {"strike": 100.0, "lastPrice": 1.5, "bid": 1.3, "ask": 1.7},
        ])
        mock_ticker = _make_yf_ticker(100.0, earnings, [expiry], calls, puts)

        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("MSFT")

        # (2 + 1.5) / 100 * 100 = 3.5%
        assert result.grade == "small_expected"
        assert result.expected_move_pct is not None
        assert abs(result.expected_move_pct - 3.5) < 0.1

    def test_ticker_uppercased(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("aapl")
        assert result.ticker == "AAPL"

    def test_no_current_price_gives_degraded(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("XYZ")

        assert result.data_available is False
        assert result.grade == "no_data"

    def test_no_earnings_date_gives_degraded(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = {"regularMarketPrice": 150.0}
        mock_ticker.calendar = {}
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("FAKE")

        assert result.data_available is False

    def test_no_options_gives_degraded(self) -> None:
        earnings = self._future_earnings(7)
        mock_ticker = _make_yf_ticker(150.0, earnings, [], pd.DataFrame(), pd.DataFrame())
        mock_ticker.options = None
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("SPY")

        assert result.data_available is False
        assert result.next_earnings_date == earnings

    def test_straddle_price_stored(self) -> None:
        earnings = self._future_earnings(10)
        expiry = self._future_expiry(14)
        calls = _make_chain_df([
            {"strike": 500.0, "lastPrice": 15.0, "bid": 14.5, "ask": 15.5},
        ])
        puts = _make_chain_df([
            {"strike": 500.0, "lastPrice": 14.0, "bid": 13.5, "ask": 14.5},
        ])
        mock_ticker = _make_yf_ticker(500.0, earnings, [expiry], calls, puts)

        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_earnings_move("NVDA")

        assert result.straddle_price is not None
        assert abs(result.straddle_price - 29.0) < 0.1
        assert result.atm_strike == 500.0


# ---------------------------------------------------------------------------
# AC-4: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_yfinance_exception_gives_degraded(self) -> None:
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", side_effect=Exception("fail")):
            result = compute_earnings_move("TSLA")

        assert result.data_available is False
        assert result.ticker == "TSLA"

    def test_never_raises(self) -> None:
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", side_effect=RuntimeError("boom")):
            result = compute_earnings_move("META")

        assert result is not None
        assert result.data_available is False

    def test_degraded_has_today_date(self) -> None:
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_earnings_move("AMZN")

        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# AC-4: API endpoint — HTTP 200 + 422
# ---------------------------------------------------------------------------


class TestEarningsMoveAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app

        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/earnings-move/")
        assert resp.status_code == 422

    def test_always_returns_200_even_when_degraded(self, client: TestClient) -> None:
        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", side_effect=Exception("fail")):
            resp = client.get("/api/earnings-move/?ticker=AAPL")

        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_200_with_all_fields(self, client: TestClient) -> None:
        earnings = (date.today() + timedelta(days=14)).strftime("%Y-%m-%d")
        expiry = (date.today() + timedelta(days=21)).strftime("%Y-%m-%d")
        calls = _make_chain_df([
            {"strike": 200.0, "lastPrice": 10.0, "bid": 9.5, "ask": 10.5},
        ])
        puts = _make_chain_df([
            {"strike": 200.0, "lastPrice": 9.5, "bid": 9.0, "ask": 10.0},
        ])
        mock_ticker = _make_yf_ticker(200.0, earnings, [expiry], calls, puts)

        with patch("quantbot.stocks.earnings_move.engine.yf.Ticker", return_value=mock_ticker):
            resp = client.get("/api/earnings-move/?ticker=NVDA")

        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "next_earnings_date", "days_to_earnings",
            "expected_move_pct", "atm_strike", "straddle_price", "current_price",
            "grade", "interpretation", "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"
