"""Tests for index_rebalance engine and API endpoint.

Covers:
- _assess_sp500_risk boundary logic
- _assess_nq100_risk boundary logic
- _assess_russell_risk
- compute_index_rebalance happy path (mocked yfinance + Wikipedia)
- graceful degradation
- GET /api/index-rebalance 200 + 422
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.index_rebalance.engine import (
    _assess_nq100_risk,
    _assess_russell_risk,
    _assess_sp500_risk,
    compute_index_rebalance,
)


# ---------------------------------------------------------------------------
# IR-1: _assess_sp500_risk
# ---------------------------------------------------------------------------


class TestAssessSP500Risk:
    def test_member_low_cap_high_deletion_risk(self) -> None:
        assert _assess_sp500_risk("XYZ", 8.0, True, None, None) == "high_deletion_risk"

    def test_member_stable_cap(self) -> None:
        assert _assess_sp500_risk("AAPL", 3000.0, True, None, None) == "stable"

    def test_non_member_large_cap_high_addition(self) -> None:
        # > 1.5× min cap → high addition risk
        result = _assess_sp500_risk("BIG", 30.0, False, None, None)
        assert result == "high_addition_risk"

    def test_non_member_near_threshold_moderate(self) -> None:
        result = _assess_sp500_risk("MID", 15.0, False, None, None)
        assert result in ("moderate_addition_risk", "high_addition_risk")

    def test_non_member_small_cap_stable(self) -> None:
        result = _assess_sp500_risk("SMALL", 1.0, False, None, None)
        assert result == "stable"

    def test_none_market_cap_unknown(self) -> None:
        assert _assess_sp500_risk("UNK", None, False, None, None) == "unknown"

    def test_member_moderate_deletion_risk(self) -> None:
        # Between deletion threshold and min cap
        result = _assess_sp500_risk("MID", 12.0, True, None, None)
        assert result in ("moderate_deletion_risk", "stable")


# ---------------------------------------------------------------------------
# IR-2: _assess_nq100_risk
# ---------------------------------------------------------------------------


class TestAssessNQ100Risk:
    def test_non_member_large_cap_high_addition(self) -> None:
        assert _assess_nq100_risk(15.0, False) == "high_addition_risk"

    def test_non_member_near_threshold_moderate(self) -> None:
        result = _assess_nq100_risk(5.5, False)
        assert result in ("moderate_addition_risk", "high_addition_risk")

    def test_non_member_small_cap_stable(self) -> None:
        assert _assess_nq100_risk(1.0, False) == "stable"

    def test_member_below_threshold_deletion(self) -> None:
        assert _assess_nq100_risk(3.0, True) == "high_deletion_risk"

    def test_member_stable(self) -> None:
        assert _assess_nq100_risk(50.0, True) == "stable"

    def test_none_cap_unknown(self) -> None:
        assert _assess_nq100_risk(None, False) == "unknown"


# ---------------------------------------------------------------------------
# IR-3: _assess_russell_risk
# ---------------------------------------------------------------------------


class TestAssessRussellRisk:
    def test_in_range_is_stable(self) -> None:
        assert _assess_russell_risk(1.0) == "stable"

    def test_above_max_is_deletion(self) -> None:
        assert _assess_russell_risk(10.0) == "moderate_deletion_risk"

    def test_below_min_is_deletion(self) -> None:
        assert _assess_russell_risk(0.1) == "high_deletion_risk"

    def test_none_is_unknown(self) -> None:
        assert _assess_russell_risk(None) == "unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_ticker(market_cap: float = 3_000_000_000_000, price: float = 200.0) -> MagicMock:
    mock = MagicMock()
    mock.info = {
        "marketCap": market_cap,
        "regularMarketPrice": price,
        "floatShares": 1_000_000_000,
        "trailingEps": 6.5,
    }
    return mock


# ---------------------------------------------------------------------------
# IR-4: compute_index_rebalance — happy path
# ---------------------------------------------------------------------------


class TestComputeIndexRebalance:
    def test_sp500_member_detected(self) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value={"AAPL", "MSFT"}),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value={"AAPL", "GOOGL"}),
        ):
            result = compute_index_rebalance("AAPL")

        sp500 = next(i for i in result.indices if "S&P 500" in i.index_name)
        assert sp500.status == "member"

    def test_sp500_non_member_detected(self) -> None:
        mock_t = _make_mock_ticker(market_cap=500_000_000)  # small cap
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value={"AAPL", "MSFT"}),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("SMALL")

        sp500 = next(i for i in result.indices if "S&P 500" in i.index_name)
        assert sp500.status == "non_member"

    def test_nq100_member_detected(self) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value={"NVDA"}),
        ):
            result = compute_index_rebalance("NVDA")

        nq100 = next(i for i in result.indices if "NASDAQ" in i.index_name)
        assert nq100.status == "member"

    def test_ticker_uppercased(self) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("aapl")
        assert result.ticker == "AAPL"

    def test_as_of_date_is_today(self) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("MSFT")
        assert result.as_of_date == date.today()

    def test_market_cap_b_computed(self) -> None:
        mock_t = _make_mock_ticker(market_cap=3_000_000_000_000)
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("AAPL")
        assert result.market_cap_b is not None
        assert abs(result.market_cap_b - 3000.0) < 1.0

    def test_interpretation_non_empty(self) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value={"AAPL"}),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("AAPL")
        assert len(result.interpretation) > 0

    def test_three_indices_returned(self) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("TSLA")
        assert len(result.indices) == 3

    def test_high_addition_risk_for_large_non_member(self) -> None:
        mock_t = _make_mock_ticker(market_cap=50_000_000_000)  # $50B non-member
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("BIG")
        sp500 = next(i for i in result.indices if "S&P 500" in i.index_name)
        assert sp500.rebalance_risk == "high_addition_risk"


# ---------------------------------------------------------------------------
# IR-5: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_exception_gives_data_available_false(self) -> None:
        with patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", side_effect=Exception("fail")):
            result = compute_index_rebalance("CRASH")
        assert result.data_available is False

    def test_never_raises(self) -> None:
        with patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", side_effect=RuntimeError("boom")):
            result = compute_index_rebalance("ERROR")
        assert result is not None

    def test_degraded_ticker_preserved(self) -> None:
        with patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_index_rebalance("AMZN")
        assert result.ticker == "AMZN"

    def test_degraded_date_is_today(self) -> None:
        with patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_index_rebalance("META")
        assert result.as_of_date == date.today()

    def test_empty_info_gives_degraded(self) -> None:
        mock_t = MagicMock()
        mock_t.info = {}
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            result = compute_index_rebalance("EMPTY")
        assert result.data_available is False


# ---------------------------------------------------------------------------
# IR-6: API endpoint
# ---------------------------------------------------------------------------


class TestIndexRebalanceAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app
        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/index-rebalance/")
        assert resp.status_code == 422

    def test_always_200_even_when_degraded(self, client: TestClient) -> None:
        with patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", side_effect=Exception("fail")):
            resp = client.get("/api/index-rebalance/?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_all_fields(self, client: TestClient) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value={"MSFT"}),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value={"MSFT"}),
        ):
            resp = client.get("/api/index-rebalance/?ticker=MSFT")
        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "market_cap", "market_cap_b", "float_shares", "price",
            "eps_ttm", "indices", "interpretation", "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"
        assert isinstance(body["indices"], list)
        assert len(body["indices"]) == 3

    def test_indices_have_status_and_risk(self, client: TestClient) -> None:
        mock_t = _make_mock_ticker()
        with (
            patch("quantbot.stocks.index_rebalance.engine.yf.Ticker", return_value=mock_t),
            patch("quantbot.stocks.index_rebalance.engine._fetch_sp500_tickers", return_value=set()),
            patch("quantbot.stocks.index_rebalance.engine._fetch_nq100_tickers", return_value=set()),
        ):
            resp = client.get("/api/index-rebalance/?ticker=NVDA")
        body = resp.json()
        for idx in body["indices"]:
            assert "status" in idx
            assert "rebalance_risk" in idx
            assert idx["status"] in ("member", "non_member", "unknown")
