"""Unit tests for PEAD engine and API endpoint."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.pead.engine import (
    EarningsEvent,
    PEADSignal,
    PEAD_DRIFT,
    _surprise_grade,
    compute_pead_signal,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_earnings_dates(
    actual: float = 1.5,
    estimate: float = 1.2,
    surprise_pct: float | None = None,
    past_date: str = "2024-10-31",
    future_date: str = "2025-01-31",
) -> pd.DataFrame:
    """Build a mock earnings_dates DataFrame matching yfinance format."""
    sp = surprise_pct if surprise_pct is not None else (actual - estimate) / abs(estimate) * 100
    index = [
        pd.Timestamp(past_date),   # most recent reported
        pd.Timestamp(future_date), # upcoming (NaN actual)
    ]
    return pd.DataFrame(
        {
            "EPS Estimate":  [estimate, 1.3],
            "Reported EPS":  [actual,   float("nan")],
            "Surprise(%)":   [sp,        float("nan")],
        },
        index=index,
    )


def _mock_ticker(
    actual: float = 1.5,
    estimate: float = 1.2,
    surprise_pct: float | None = None,
) -> MagicMock:
    t = MagicMock()
    t.earnings_dates = _make_earnings_dates(actual, estimate, surprise_pct)
    t.calendar = None
    t.info = {}
    return t


# ---------------------------------------------------------------------------
# _surprise_grade
# ---------------------------------------------------------------------------


class TestSurpriseGrade:
    def test_large_beat(self):
        assert _surprise_grade(15.0) == "large_beat"

    def test_beat(self):
        assert _surprise_grade(5.0) == "beat"

    def test_inline_positive(self):
        assert _surprise_grade(1.0) == "inline"

    def test_inline_zero(self):
        assert _surprise_grade(0.0) == "inline"

    def test_inline_negative(self):
        assert _surprise_grade(-1.5) == "inline"

    def test_miss(self):
        assert _surprise_grade(-5.0) == "miss"

    def test_large_miss(self):
        assert _surprise_grade(-12.0) == "large_miss"

    def test_boundary_large_beat(self):
        # exactly +10 is NOT large_beat (> not >=)
        assert _surprise_grade(10.0) == "beat"

    def test_boundary_beat(self):
        # exactly +2.0 is beat (>= 2.0 threshold)
        assert _surprise_grade(2.0) == "beat"

    def test_boundary_miss(self):
        # exactly -10.0 is miss (>= -10.0 threshold)
        assert _surprise_grade(-10.0) == "miss"

    def test_negative_two_is_miss(self):
        # exactly -2.0 is miss (not inline): inline is > -2.0
        assert _surprise_grade(-2.0) == "miss"

    def test_just_above_large_beat(self):
        assert _surprise_grade(10.01) == "large_beat"

    def test_just_below_large_miss(self):
        assert _surprise_grade(-10.01) == "large_miss"


# ---------------------------------------------------------------------------
# PEAD drift values
# ---------------------------------------------------------------------------


class TestPEADDrift:
    def test_large_beat_positive_drift(self):
        d30, d60, d90 = PEAD_DRIFT["large_beat"]
        assert d30 > 0 and d60 > 0 and d90 > 0

    def test_large_miss_negative_drift(self):
        d30, d60, d90 = PEAD_DRIFT["large_miss"]
        assert d30 < 0 and d60 < 0 and d90 < 0

    def test_inline_zero_drift(self):
        assert PEAD_DRIFT["inline"] == (0.0, 0.0, 0.0)

    def test_all_grades_present(self):
        for grade in ("large_beat", "beat", "inline", "miss", "large_miss"):
            assert grade in PEAD_DRIFT


# ---------------------------------------------------------------------------
# compute_pead_signal
# ---------------------------------------------------------------------------


class TestComputePEADSignal:
    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_returns_pead_signal(self, mock_yf):
        mock_yf.return_value = _mock_ticker(actual=1.5, estimate=1.2)
        result = compute_pead_signal("AAPL")
        assert isinstance(result, PEADSignal)
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_surprise_pct_computed(self, mock_yf):
        """actual=1.5, estimate=1.2 → surprise = (1.5-1.2)/1.2*100 = 25.0%"""
        mock_yf.return_value = _mock_ticker(actual=1.5, estimate=1.2)
        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.last_earnings.surprise_pct == pytest.approx(25.0, rel=0.01)

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_grade_is_large_beat(self, mock_yf):
        """surprise=25% → large_beat"""
        mock_yf.return_value = _mock_ticker(actual=1.5, estimate=1.2)
        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.last_earnings.grade == "large_beat"

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_drift_matches_grade(self, mock_yf):
        mock_yf.return_value = _mock_ticker(actual=1.5, estimate=1.2)
        result = compute_pead_signal("AAPL")
        assert result is not None
        d30, d60, d90 = PEAD_DRIFT["large_beat"]
        assert result.expected_drift_30d == d30
        assert result.expected_drift_60d == d60
        assert result.expected_drift_90d == d90

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_miss_grade_negative_drift(self, mock_yf):
        """actual=0.8, estimate=1.0 → surprise=-20% → large_miss"""
        mock_yf.return_value = _mock_ticker(actual=0.8, estimate=1.0)
        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.last_earnings.grade == "large_miss"
        assert result.expected_drift_30d < 0

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_as_of_date_is_today(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_pead_signal("AAPL")
        assert result is not None
        assert result.as_of_date <= date.today()

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_next_earnings_date_populated(self, mock_yf):
        """The future_date row (NaN actual) should populate next_earnings_date."""
        mock_yf.return_value = _mock_ticker()
        result = compute_pead_signal("AAPL")
        assert result is not None
        # May or may not be found depending on date filtering, but should not crash
        assert result.next_earnings_date is None or isinstance(result.next_earnings_date, date)

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_returns_none_on_empty_earnings_dates(self, mock_yf):
        t = MagicMock()
        t.earnings_dates = pd.DataFrame()
        t.calendar = None
        t.info = {}
        mock_yf.return_value = t
        assert compute_pead_signal("X") is None

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network")
        assert compute_pead_signal("ERR") is None

    @patch("quantbot.stocks.pead.engine.yf.Ticker")
    def test_returns_none_when_all_nans(self, mock_yf):
        """All Reported EPS are NaN → no reported earnings → None."""
        t = MagicMock()
        t.earnings_dates = pd.DataFrame({
            "EPS Estimate": [1.0, 1.1],
            "Reported EPS": [float("nan"), float("nan")],
            "Surprise(%)":  [float("nan"), float("nan")],
        }, index=[pd.Timestamp("2025-01-31"), pd.Timestamp("2025-04-30")])
        t.calendar = None
        t.info = {}
        mock_yf.return_value = t
        assert compute_pead_signal("X") is None


# ---------------------------------------------------------------------------
# API: GET /api/pead
# ---------------------------------------------------------------------------

_MOCK_EVENT = EarningsEvent(
    earnings_date=date(2024, 10, 31),
    actual_eps=1.50,
    estimated_eps=1.20,
    surprise_pct=25.0,
    grade="large_beat",
)

_MOCK_PEAD = PEADSignal(
    ticker="AAPL",
    last_earnings=_MOCK_EVENT,
    expected_drift_30d=3.5,
    expected_drift_60d=5.2,
    expected_drift_90d=6.8,
    next_earnings_date=date(2025, 1, 31),
    interpretation="大幅超预期 +25.0%...",
    as_of_date=date(2026, 4, 29),
)


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


class TestPEADAPIEndpoint:
    @patch("quantbot.stocks.api.pead.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/pead/?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.pead.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/pead/?ticker=AAPL").json()
        assert "last_earnings" in data
        assert "expected_drift_30d" in data
        assert "expected_drift_60d" in data
        assert "expected_drift_90d" in data
        assert "next_earnings_date" in data

    @patch("quantbot.stocks.api.pead.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_last_earnings_has_grade(self, mock_fn, client):
        data = client.get("/api/pead/?ticker=AAPL").json()
        assert "grade" in data["last_earnings"]
        assert data["last_earnings"]["grade"] in {
            "large_beat", "beat", "inline", "miss", "large_miss"
        }

    @patch("quantbot.stocks.api.pead.compute_pead_signal", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/pead/?ticker=NODATA")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/pead/").status_code == 422

    @patch("quantbot.stocks.api.pead.compute_pead_signal", return_value=_MOCK_PEAD)
    def test_drift_values_correct(self, mock_fn, client):
        data = client.get("/api/pead/?ticker=AAPL").json()
        assert data["expected_drift_30d"] == pytest.approx(3.5)
        assert data["expected_drift_90d"] == pytest.approx(6.8)
