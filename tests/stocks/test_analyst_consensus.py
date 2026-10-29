"""Tests for analyst_consensus engine and API endpoint.

Covers:
- _analyst_grade boundary thresholds (1-5 scale)
- compute_analyst_consensus happy path (mocked yfinance)
- graceful degradation (no data / exception)
- GET /api/analyst-consensus 200 + 422
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.analyst_consensus.engine import (
    _analyst_grade,
    compute_analyst_consensus,
)


# ---------------------------------------------------------------------------
# AC-4: _analyst_grade — boundary tests
# ---------------------------------------------------------------------------


class TestAnalystGrade:
    def test_1_0_is_strong_buy(self) -> None:
        assert _analyst_grade(1.0) == "strong_buy"

    def test_1_5_is_strong_buy(self) -> None:
        # <=1.5 → strong_buy
        assert _analyst_grade(1.5) == "strong_buy"

    def test_1_6_is_buy(self) -> None:
        # >1.5 and <=2.5 → buy
        assert _analyst_grade(1.6) == "buy"

    def test_2_5_is_buy(self) -> None:
        assert _analyst_grade(2.5) == "buy"

    def test_2_6_is_hold(self) -> None:
        assert _analyst_grade(2.6) == "hold"

    def test_3_5_is_hold(self) -> None:
        assert _analyst_grade(3.5) == "hold"

    def test_3_6_is_sell(self) -> None:
        assert _analyst_grade(3.6) == "sell"

    def test_4_5_is_sell(self) -> None:
        assert _analyst_grade(4.5) == "sell"

    def test_4_6_is_strong_sell(self) -> None:
        assert _analyst_grade(4.6) == "strong_sell"

    def test_5_0_is_strong_sell(self) -> None:
        assert _analyst_grade(5.0) == "strong_sell"

    def test_none_is_no_coverage(self) -> None:
        assert _analyst_grade(None) == "no_coverage"


# ---------------------------------------------------------------------------
# Helpers for mocking yfinance
# ---------------------------------------------------------------------------


def _make_mock_ticker(info: dict) -> MagicMock:
    mock = MagicMock()
    mock.info = info
    return mock


# ---------------------------------------------------------------------------
# AC-4: compute_analyst_consensus — happy path
# ---------------------------------------------------------------------------


class TestComputeAnalystConsensus:
    def test_happy_path_strong_buy(self) -> None:
        mock_ticker = _make_mock_ticker({
            "regularMarketPrice": 200.0,
            "recommendationMean": 1.3,
            "recommendationKey": "strongBuy",
            "numberOfAnalystOpinions": 35,
            "targetMeanPrice": 250.0,
            "targetHighPrice": 300.0,
            "targetLowPrice": 180.0,
        })
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("NVDA")

        assert result.data_available is True
        assert result.ticker == "NVDA"
        assert result.grade == "strong_buy"
        assert result.recommendation_mean == 1.3
        assert result.num_analysts == 35
        assert result.upside_pct is not None
        assert abs(result.upside_pct - 25.0) < 0.1  # (250/200 - 1) × 100
        assert result.as_of_date == date.today()

    def test_happy_path_hold(self) -> None:
        mock_ticker = _make_mock_ticker({
            "regularMarketPrice": 150.0,
            "recommendationMean": 3.0,
            "recommendationKey": "hold",
            "numberOfAnalystOpinions": 20,
            "targetMeanPrice": 155.0,
            "targetHighPrice": 175.0,
            "targetLowPrice": 130.0,
        })
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("MSFT")

        assert result.grade == "hold"
        assert result.upside_pct is not None
        assert result.upside_pct > 0  # 155/150 - 1 > 0

    def test_upside_computed_correctly(self) -> None:
        mock_ticker = _make_mock_ticker({
            "currentPrice": 100.0,
            "recommendationMean": 2.0,
            "numberOfAnalystOpinions": 15,
            "targetMeanPrice": 130.0,
        })
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("AAPL")

        # (130/100 - 1) * 100 = 30%
        assert result.upside_pct is not None
        assert abs(result.upside_pct - 30.0) < 0.1

    def test_ticker_uppercased(self) -> None:
        mock_ticker = _make_mock_ticker({})
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("aapl")

        assert result.ticker == "AAPL"

    def test_no_analyst_data_gives_degraded(self) -> None:
        mock_ticker = _make_mock_ticker({})
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("FAKE")

        assert result.data_available is False
        assert result.grade == "no_coverage"

    def test_recommendation_key_stored(self) -> None:
        mock_ticker = _make_mock_ticker({
            "regularMarketPrice": 50.0,
            "recommendationMean": 2.1,
            "recommendationKey": "buy",
            "numberOfAnalystOpinions": 10,
            "targetMeanPrice": 60.0,
        })
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("XYZ")

        assert result.recommendation_key == "buy"

    def test_strong_sell_grade(self) -> None:
        mock_ticker = _make_mock_ticker({
            "regularMarketPrice": 200.0,
            "recommendationMean": 4.8,
            "numberOfAnalystOpinions": 5,
            "targetMeanPrice": 150.0,
        })
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("DOGE")

        assert result.grade == "strong_sell"
        assert result.upside_pct is not None
        assert result.upside_pct < 0  # target below current


# ---------------------------------------------------------------------------
# AC-4: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_yfinance_exception_gives_degraded(self) -> None:
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", side_effect=Exception("fail")):
            result = compute_analyst_consensus("TSLA")

        assert result.data_available is False
        assert result.ticker == "TSLA"

    def test_never_raises(self) -> None:
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", side_effect=RuntimeError("boom")):
            result = compute_analyst_consensus("META")

        assert result is not None
        assert result.data_available is False

    def test_degraded_has_today_date(self) -> None:
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", side_effect=Exception("err")):
            result = compute_analyst_consensus("AMZN")

        assert result.as_of_date == date.today()

    def test_empty_info_gives_degraded(self) -> None:
        mock_ticker = MagicMock()
        mock_ticker.info = None
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            result = compute_analyst_consensus("EMPTY")

        assert result.data_available is False


# ---------------------------------------------------------------------------
# AC-4: API endpoint — HTTP 200 + 422
# ---------------------------------------------------------------------------


class TestAnalystConsensusAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app

        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/analyst-consensus/")
        assert resp.status_code == 422

    def test_always_returns_200_even_when_degraded(self, client: TestClient) -> None:
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", side_effect=Exception("fail")):
            resp = client.get("/api/analyst-consensus/?ticker=AAPL")

        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_all_fields(self, client: TestClient) -> None:
        mock_ticker = _make_mock_ticker({
            "regularMarketPrice": 200.0,
            "recommendationMean": 1.8,
            "recommendationKey": "buy",
            "numberOfAnalystOpinions": 25,
            "targetMeanPrice": 240.0,
            "targetHighPrice": 280.0,
            "targetLowPrice": 190.0,
        })
        with patch("quantbot.stocks.analyst_consensus.engine.yf.Ticker", return_value=mock_ticker):
            resp = client.get("/api/analyst-consensus/?ticker=AAPL")

        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "recommendation_mean", "recommendation_key", "num_analysts",
            "target_mean_price", "target_high_price", "target_low_price",
            "current_price", "upside_pct", "grade", "interpretation",
            "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"
        assert body["grade"] == "buy"
