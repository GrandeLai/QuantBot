"""Tests for unusual_options engine and API endpoint.

Covers:
- _options_grade boundaries
- volume_oi_ratio threshold logic
- compute_unusual_options happy path (mocked yfinance)
- graceful degradation (no options / exception)
- GET /api/unusual-options 200 + 422
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.unusual_options.engine import (
    UnusualOptionsData,
    _options_grade,
    _parse_chain,
    compute_unusual_options,
)


# ---------------------------------------------------------------------------
# AC-4: _options_grade — boundary tests
# ---------------------------------------------------------------------------


class TestOptionsGrade:
    def test_no_unusual_is_neutral(self) -> None:
        assert _options_grade(0, 0) == "neutral"

    def test_calls_dominate_bullish(self) -> None:
        # unusual_calls > 2 * unusual_puts AND >= 2
        assert _options_grade(4, 1) == "bullish_unusual"

    def test_puts_dominate_bearish(self) -> None:
        # unusual_puts > 2 * unusual_calls AND >= 2
        assert _options_grade(1, 4) == "bearish_unusual"

    def test_mixed_when_balanced_and_sufficient(self) -> None:
        # Neither side dominates, total >= 3
        assert _options_grade(3, 3) == "mixed_unusual"

    def test_single_call_is_neutral(self) -> None:
        # unusual_calls=1, unusual_puts=0: calls > 2*0 but calls < 2 → neutral
        assert _options_grade(1, 0) == "neutral"

    def test_two_calls_zero_puts_bullish(self) -> None:
        # 2 > 2*0 and 2 >= 2 → bullish
        assert _options_grade(2, 0) == "bullish_unusual"

    def test_two_puts_zero_calls_bearish(self) -> None:
        assert _options_grade(0, 2) == "bearish_unusual"

    def test_calls_barely_not_dominant(self) -> None:
        # 4 calls, 3 puts: 4 > 2*3 is False (4 <= 6) → neither dominant
        assert _options_grade(4, 3) == "mixed_unusual"


# ---------------------------------------------------------------------------
# AC-4: _parse_chain — volume/OI flagging
# ---------------------------------------------------------------------------


def _make_options_df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


class TestParseChain:
    def test_high_ratio_flagged_as_unusual(self) -> None:
        calls_df = _make_options_df([
            {
                "strike": 200.0,
                "volume": 3000,
                "openInterest": 100,
                "impliedVolatility": 0.30,
                "inTheMoney": False,
            }
        ])
        uc, up = _parse_chain("AAPL", "2025-01-17", calls_df, pd.DataFrame())
        assert len(uc) == 1
        assert uc[0].is_unusual is True
        assert uc[0].volume_oi_ratio == 30.0

    def test_low_ratio_not_flagged(self) -> None:
        calls_df = _make_options_df([
            {
                "strike": 200.0,
                "volume": 100,
                "openInterest": 500,
                "impliedVolatility": 0.25,
                "inTheMoney": True,
            }
        ])
        uc, up = _parse_chain("AAPL", "2025-01-17", calls_df, pd.DataFrame())
        assert len(uc) == 0  # ratio = 0.2 < 3.0

    def test_zero_volume_not_flagged(self) -> None:
        calls_df = _make_options_df([
            {
                "strike": 150.0,
                "volume": 0,
                "openInterest": 0,
                "impliedVolatility": 0.20,
                "inTheMoney": False,
            }
        ])
        uc, up = _parse_chain("TSLA", "2025-01-17", calls_df, pd.DataFrame())
        assert len(uc) == 0

    def test_put_chain_flagged_correctly(self) -> None:
        puts_df = _make_options_df([
            {
                "strike": 180.0,
                "volume": 5000,
                "openInterest": 200,
                "impliedVolatility": 0.45,
                "inTheMoney": True,
            }
        ])
        uc, up = _parse_chain("SPY", "2025-01-17", pd.DataFrame(), puts_df)
        assert len(up) == 1
        assert up[0].option_type == "put"
        assert up[0].is_unusual is True

    def test_empty_dataframes_returns_empty_lists(self) -> None:
        uc, up = _parse_chain("MSFT", "2025-01-17", pd.DataFrame(), pd.DataFrame())
        assert uc == []
        assert up == []


# ---------------------------------------------------------------------------
# AC-4: compute_unusual_options — happy path
# ---------------------------------------------------------------------------


def _mock_yf_ticker(expiries: list[str], chain_calls: pd.DataFrame, chain_puts: pd.DataFrame) -> MagicMock:
    """Build a mock yf.Ticker object."""
    mock_chain = MagicMock()
    mock_chain.calls = chain_calls
    mock_chain.puts = chain_puts

    mock_ticker = MagicMock()
    mock_ticker.options = expiries
    mock_ticker.option_chain.return_value = mock_chain
    return mock_ticker


class TestComputeUnusualOptions:
    def test_happy_path_bullish_unusual(self) -> None:
        calls_df = _make_options_df([
            {"strike": 200.0, "volume": 5000, "openInterest": 100, "impliedVolatility": 0.30, "inTheMoney": False},
            {"strike": 205.0, "volume": 4000, "openInterest": 50,  "impliedVolatility": 0.35, "inTheMoney": False},
        ])
        puts_df = _make_options_df([
            {"strike": 190.0, "volume": 200, "openInterest": 500, "impliedVolatility": 0.25, "inTheMoney": False},
        ])
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, puts_df)

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("AAPL")

        assert isinstance(result, UnusualOptionsData)
        assert result.data_available is True
        assert result.ticker == "AAPL"
        assert result.total_unusual_calls >= 2
        assert result.total_unusual_puts == 0
        assert result.grade == "bullish_unusual"
        assert result.as_of_date == date.today()

    def test_happy_path_bearish_unusual(self) -> None:
        calls_df = _make_options_df([
            {"strike": 200.0, "volume": 50, "openInterest": 1000, "impliedVolatility": 0.20, "inTheMoney": True},
        ])
        puts_df = _make_options_df([
            {"strike": 190.0, "volume": 6000, "openInterest": 100, "impliedVolatility": 0.40, "inTheMoney": False},
            {"strike": 185.0, "volume": 4500, "openInterest": 80,  "impliedVolatility": 0.45, "inTheMoney": False},
        ])
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, puts_df)

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("SPY")

        assert result.total_unusual_puts >= 2
        assert result.grade == "bearish_unusual"

    def test_no_unusual_returns_neutral(self) -> None:
        calls_df = _make_options_df([
            {"strike": 200.0, "volume": 100, "openInterest": 5000, "impliedVolatility": 0.20, "inTheMoney": True},
        ])
        puts_df = _make_options_df([
            {"strike": 190.0, "volume": 80,  "openInterest": 3000, "impliedVolatility": 0.22, "inTheMoney": False},
        ])
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, puts_df)

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("MSFT")

        assert result.grade == "neutral"
        assert result.total_unusual_calls == 0
        assert result.total_unusual_puts == 0

    def test_top_unusual_capped_at_5(self) -> None:
        rows = [
            {"strike": float(i * 5 + 100), "volume": 10000, "openInterest": 10, "impliedVolatility": 0.30, "inTheMoney": False}
            for i in range(10)
        ]
        calls_df = _make_options_df(rows)
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, pd.DataFrame())

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("NVDA")

        assert len(result.top_unusual) <= 5

    def test_ticker_uppercased(self) -> None:
        mock_ticker = _mock_yf_ticker([], pd.DataFrame(), pd.DataFrame())

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("aapl")

        assert result.ticker == "AAPL"

    def test_put_call_ratio_computed(self) -> None:
        calls_df = _make_options_df([
            {"strike": 200.0, "volume": 1000, "openInterest": 500, "impliedVolatility": 0.20, "inTheMoney": True},
        ])
        puts_df = _make_options_df([
            {"strike": 190.0, "volume": 2000, "openInterest": 500, "impliedVolatility": 0.25, "inTheMoney": False},
        ])
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, puts_df)

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("AMD")

        # put_vol=2000, call_vol=1000 → ratio=2.0
        assert abs(result.put_call_ratio - 2.0) < 0.01


# ---------------------------------------------------------------------------
# AC-4: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_no_options_data_gives_degraded(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.options = None

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_unusual_options("FAKE")

        assert result.data_available is False
        assert result.grade == "neutral"

    def test_yfinance_exception_gives_degraded(self) -> None:
        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", side_effect=Exception("network fail")):
            result = compute_unusual_options("GME")

        assert result.data_available is False
        assert result.ticker == "GME"

    def test_degraded_never_raises(self) -> None:
        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", side_effect=RuntimeError("boom")):
            result = compute_unusual_options("AMC")

        assert result is not None
        assert result.data_available is False

    def test_degraded_has_today_date(self) -> None:
        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_unusual_options("XYZ")

        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# AC-4: API endpoint — HTTP 200 + 422
# ---------------------------------------------------------------------------


class TestUnusualOptionsAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app

        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/unusual-options/")
        assert resp.status_code == 422

    def test_always_returns_200_even_when_degraded(self, client: TestClient) -> None:
        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", side_effect=Exception("fail")):
            resp = client.get("/api/unusual-options/?ticker=AAPL")

        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_200_with_all_fields(self, client: TestClient) -> None:
        calls_df = _make_options_df([
            {"strike": 200.0, "volume": 5000, "openInterest": 100, "impliedVolatility": 0.30, "inTheMoney": False},
        ])
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, pd.DataFrame())

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            resp = client.get("/api/unusual-options/?ticker=NVDA")

        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "total_unusual_calls", "total_unusual_puts",
            "total_call_volume", "total_put_volume", "put_call_ratio",
            "grade", "top_unusual", "interpretation", "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"

    def test_top_unusual_contracts_have_correct_fields(self, client: TestClient) -> None:
        calls_df = _make_options_df([
            {"strike": 200.0, "volume": 5000, "openInterest": 50, "impliedVolatility": 0.30, "inTheMoney": False},
        ])
        mock_ticker = _mock_yf_ticker(["2025-01-17"], calls_df, pd.DataFrame())

        with patch("quantbot.stocks.unusual_options.engine.yf.Ticker", return_value=mock_ticker):
            resp = client.get("/api/unusual-options/?ticker=META")

        body = resp.json()
        if body["top_unusual"]:
            contract = body["top_unusual"][0]
            for f in ["ticker", "expiry", "strike", "option_type", "volume", "open_interest",
                       "volume_oi_ratio", "implied_volatility", "in_the_money", "is_unusual"]:
                assert f in contract, f"Contract missing field: {f}"
