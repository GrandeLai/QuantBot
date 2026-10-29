"""Tests for social_sentiment engine and API endpoint.

Covers:
- _sentiment_grade boundary thresholds
- _pump_risk_score formula
- compute_social_sentiment happy path (mocked HTTP)
- compute_social_sentiment graceful degradation (API unreachable)
- GET /api/social-sentiment 200 + 422
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.social_sentiment.engine import (
    SocialSentimentData,
    _pump_risk_level,
    _pump_risk_score,
    _sentiment_grade,
    compute_social_sentiment,
)


# ---------------------------------------------------------------------------
# AC-4: _sentiment_grade — boundary tests
# ---------------------------------------------------------------------------


class TestSentimentGrade:
    def test_above_70_is_very_bullish(self) -> None:
        assert _sentiment_grade(0.71) == "very_bullish"

    def test_exactly_70_is_bullish(self) -> None:
        # >0.70 → very_bullish; 0.70 is NOT > 0.70 → bullish
        assert _sentiment_grade(0.70) == "bullish"

    def test_above_55_is_bullish(self) -> None:
        assert _sentiment_grade(0.56) == "bullish"

    def test_exactly_55_is_neutral(self) -> None:
        assert _sentiment_grade(0.55) == "neutral"

    def test_above_45_is_neutral(self) -> None:
        assert _sentiment_grade(0.50) == "neutral"

    def test_exactly_45_is_bearish(self) -> None:
        assert _sentiment_grade(0.45) == "bearish"

    def test_above_30_is_bearish(self) -> None:
        assert _sentiment_grade(0.31) == "bearish"

    def test_exactly_30_is_very_bearish(self) -> None:
        assert _sentiment_grade(0.30) == "very_bearish"

    def test_zero_is_very_bearish(self) -> None:
        assert _sentiment_grade(0.0) == "very_bearish"

    def test_one_is_very_bullish(self) -> None:
        assert _sentiment_grade(1.0) == "very_bullish"


# ---------------------------------------------------------------------------
# AC-4: _pump_risk_score — formula correctness
# ---------------------------------------------------------------------------


class TestPumpRiskScore:
    def test_max_score_full_bullish_high_volume(self) -> None:
        # bullish_ratio=1.0 → extremity=1.0; count=20 → volume_factor=1.0
        # score = 0.7 * 1.0 + 0.3 * 1.0 = 1.0
        score = _pump_risk_score(1.0, 20)
        assert abs(score - 1.0) < 1e-4

    def test_zero_score_at_50pct_ratio_no_messages(self) -> None:
        # extremity = max(0, (0.5 - 0.5) * 2) = 0; volume = min(1, 0/20) = 0
        score = _pump_risk_score(0.5, 0)
        assert score == 0.0

    def test_below_50_ratio_contributes_zero_extremity(self) -> None:
        # extremity is clamped to 0 for ratios < 0.5
        score_low = _pump_risk_score(0.3, 0)
        assert score_low == 0.0

    def test_volume_factor_saturates_at_20(self) -> None:
        score_20 = _pump_risk_score(0.5, 20)
        score_40 = _pump_risk_score(0.5, 40)
        assert abs(score_20 - score_40) < 1e-6

    def test_score_in_0_1_range(self) -> None:
        for ratio in [0.0, 0.25, 0.5, 0.75, 1.0]:
            for count in [0, 5, 10, 20, 50]:
                s = _pump_risk_score(ratio, count)
                assert 0.0 <= s <= 1.0, f"Out of range: ratio={ratio}, count={count}, score={s}"

    def test_high_bullish_high_volume_gives_high_score(self) -> None:
        score = _pump_risk_score(0.9, 20)
        assert score > 0.6  # should be "high" risk

    def test_moderate_bullish_low_volume(self) -> None:
        # extremity = (0.7 - 0.5) * 2 = 0.4; volume_factor = 5/20 = 0.25
        # expected = 0.7 * 0.4 + 0.3 * 0.25 = 0.28 + 0.075 = 0.355
        score = _pump_risk_score(0.7, 5)
        assert abs(score - 0.355) < 1e-4


# ---------------------------------------------------------------------------
# AC-4: _pump_risk_level
# ---------------------------------------------------------------------------


class TestPumpRiskLevel:
    def test_high_above_06(self) -> None:
        assert _pump_risk_level(0.61) == "high"

    def test_elevated_above_03(self) -> None:
        assert _pump_risk_level(0.31) == "elevated"

    def test_low_at_03(self) -> None:
        assert _pump_risk_level(0.30) == "low"

    def test_low_at_zero(self) -> None:
        assert _pump_risk_level(0.0) == "low"


# ---------------------------------------------------------------------------
# Helpers for mocking StockTwits HTTP response
# ---------------------------------------------------------------------------


def _make_messages(bullish: int, bearish: int, unlabeled: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for _ in range(bullish):
        msgs.append({"entities": {"sentiment": {"basic": "Bullish"}}})
    for _ in range(bearish):
        msgs.append({"entities": {"sentiment": {"basic": "Bearish"}}})
    for _ in range(unlabeled):
        msgs.append({"entities": {"sentiment": None}})
    return msgs


def _mock_resp(status: int, messages: list[dict[str, Any]]) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = {"messages": messages}
    return mock


# ---------------------------------------------------------------------------
# AC-4: compute_social_sentiment — happy path
# ---------------------------------------------------------------------------


class TestComputeSocialSentiment:
    def _mock_client(self, status: int, messages: list[dict[str, Any]]) -> MagicMock:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.get.return_value = _mock_resp(status, messages)
        return cm

    def test_happy_path_returns_correct_fields(self) -> None:
        msgs = _make_messages(bullish=15, bearish=5, unlabeled=5)
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = self._mock_client(200, msgs)
            result = compute_social_sentiment("AAPL")

        assert isinstance(result, SocialSentimentData)
        assert result.ticker == "AAPL"
        assert result.api_accessible is True
        assert result.total_messages == 25
        assert result.bullish_count == 15
        assert result.bearish_count == 5
        # bullish_ratio = 15 / 20 = 0.75
        assert abs(result.bullish_ratio - 0.75) < 1e-3
        assert result.sentiment_grade == "very_bullish"
        assert result.pump_risk_level == "high"
        assert result.as_of_date == date.today()

    def test_all_bearish_gives_very_bearish_grade(self) -> None:
        msgs = _make_messages(bullish=0, bearish=10, unlabeled=0)
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = self._mock_client(200, msgs)
            result = compute_social_sentiment("GME")

        assert result.sentiment_grade == "very_bearish"
        assert result.bullish_ratio == 0.0
        assert result.api_accessible is True

    def test_unlabeled_only_gives_neutral_ratio(self) -> None:
        msgs = _make_messages(bullish=0, bearish=0, unlabeled=10)
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = self._mock_client(200, msgs)
            result = compute_social_sentiment("TSLA")

        # labeled=0 → bullish_ratio defaults to 0.5 (neutral)
        assert result.bullish_ratio == 0.5
        assert result.sentiment_grade == "neutral"

    def test_empty_messages_returns_neutral(self) -> None:
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = self._mock_client(200, [])
            result = compute_social_sentiment("NVDA")

        assert result.total_messages == 0
        assert result.bullish_ratio == 0.5
        assert result.api_accessible is True

    def test_ticker_uppercased(self) -> None:
        msgs = _make_messages(5, 5, 0)
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = self._mock_client(200, msgs)
            result = compute_social_sentiment("aapl")

        assert result.ticker == "AAPL"


# ---------------------------------------------------------------------------
# AC-4: graceful degradation — API unreachable
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_rate_limit_429_gives_degraded(self) -> None:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.get.return_value = _mock_resp(429, [])
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = cm
            result = compute_social_sentiment("GME")

        assert result.api_accessible is False
        assert result.sentiment_grade == "neutral"
        assert result.pump_risk_score == 0.0

    def test_500_error_gives_degraded(self) -> None:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=cm)
        cm.__exit__ = MagicMock(return_value=False)
        cm.get.return_value = _mock_resp(500, [])
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            mock_cls.return_value = cm
            result = compute_social_sentiment("GME")

        assert result.api_accessible is False

    def test_timeout_gives_degraded(self) -> None:
        import httpx

        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get.side_effect = httpx.TimeoutException("timeout")
            mock_cls.return_value = cm
            result = compute_social_sentiment("MSFT")

        assert result.api_accessible is False
        assert result.ticker == "MSFT"

    def test_connect_error_gives_degraded(self) -> None:
        import httpx

        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get.side_effect = httpx.ConnectError("refused")
            mock_cls.return_value = cm
            result = compute_social_sentiment("META")

        assert result.api_accessible is False

    def test_degraded_never_raises(self) -> None:
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get.side_effect = RuntimeError("unexpected")
            mock_cls.return_value = cm
            result = compute_social_sentiment("AMZN")

        assert result.api_accessible is False
        assert result is not None

    def test_degraded_has_today_date(self) -> None:
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get.side_effect = Exception("boom")
            mock_cls.return_value = cm
            result = compute_social_sentiment("GOOGL")

        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# AC-4: API endpoint — HTTP 200 + 422
# ---------------------------------------------------------------------------


class TestSocialSentimentAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app

        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/social-sentiment/")
        assert resp.status_code == 422

    def test_always_returns_200_even_when_api_down(self, client: TestClient) -> None:
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get.side_effect = Exception("network failure")
            mock_cls.return_value = cm
            resp = client.get("/api/social-sentiment/?ticker=AAPL")

        assert resp.status_code == 200
        body = resp.json()
        assert body["api_accessible"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_200_with_all_fields(self, client: TestClient) -> None:
        msgs = _make_messages(bullish=10, bearish=5, unlabeled=5)
        with patch("quantbot.stocks.social_sentiment.engine.httpx.Client") as mock_cls:
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.get.return_value = _mock_resp(200, msgs)
            mock_cls.return_value = cm
            resp = client.get("/api/social-sentiment/?ticker=NVDA")

        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker",
            "total_messages",
            "bullish_count",
            "bearish_count",
            "bullish_ratio",
            "sentiment_grade",
            "pump_risk_level",
            "pump_risk_score",
            "interpretation",
            "as_of_date",
            "api_accessible",
        ]:
            assert field in body, f"Missing field: {field}"
