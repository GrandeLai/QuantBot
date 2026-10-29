"""Unit tests for Price Momentum engine and API endpoint."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.momentum.engine import (
    MomentumSignal,
    _momentum_grade,
    compute_momentum_signal,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _make_history(
    n_bars: int = 280,
    start_price: float = 100.0,
    end_price: float = 130.0,
) -> pd.DataFrame:
    """Build a synthetic price history DataFrame."""
    prices = np.linspace(start_price, end_price, n_bars)
    index = pd.bdate_range(end="2024-12-31", periods=n_bars)
    return pd.DataFrame({"Close": prices}, index=index)


def _mock_ticker(
    n_bars: int = 280,
    start_price: float = 100.0,
    end_price: float = 130.0,
) -> MagicMock:
    t = MagicMock()
    t.history.return_value = _make_history(n_bars, start_price, end_price)
    t.info = {}
    return t


# ---------------------------------------------------------------------------
# _momentum_grade
# ---------------------------------------------------------------------------


class TestMomentumGrade:
    def test_strong_momentum(self):
        assert _momentum_grade(25.0) == "strong_momentum"

    def test_momentum(self):
        assert _momentum_grade(10.0) == "momentum"

    def test_neutral_positive(self):
        assert _momentum_grade(2.0) == "neutral"

    def test_neutral_zero(self):
        assert _momentum_grade(0.0) == "neutral"

    def test_neutral_negative(self):
        assert _momentum_grade(-3.0) == "neutral"

    def test_reversal_risk(self):
        assert _momentum_grade(-10.0) == "reversal_risk"

    def test_strong_reversal(self):
        assert _momentum_grade(-25.0) == "strong_reversal"

    def test_boundary_strong_momentum(self):
        # exactly +20 is momentum (> not >=)
        assert _momentum_grade(20.0) == "momentum"

    def test_boundary_above_strong(self):
        assert _momentum_grade(20.01) == "strong_momentum"

    def test_boundary_momentum(self):
        # exactly +5 is neutral (> not >=)
        assert _momentum_grade(5.0) == "neutral"

    def test_boundary_neutral_low(self):
        # exactly -5 is neutral (>= -5)
        assert _momentum_grade(-5.0) == "neutral"

    def test_boundary_reversal_risk(self):
        # exactly -20 is reversal_risk (>= -20)
        assert _momentum_grade(-20.0) == "reversal_risk"

    def test_boundary_strong_reversal(self):
        assert _momentum_grade(-20.01) == "strong_reversal"


# ---------------------------------------------------------------------------
# compute_momentum_signal
# ---------------------------------------------------------------------------


class TestComputeMomentumSignal:
    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_returns_momentum_signal(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_momentum_signal("AAPL")
        assert isinstance(result, MomentumSignal)
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_all_returns_are_floats(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_momentum_signal("AAPL")
        assert result is not None
        for attr in ("momentum_12_1", "return_1m", "return_3m", "return_6m"):
            assert isinstance(getattr(result, attr), float), f"{attr} should be float"

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_grade_valid_literal(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_momentum_signal("AAPL")
        assert result is not None
        assert result.grade in {
            "strong_momentum", "momentum", "neutral", "reversal_risk", "strong_reversal"
        }

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_rising_price_positive_momentum(self, mock_yf):
        """start=100 end=150 → positive 12-1m momentum → momentum or strong_momentum."""
        mock_yf.return_value = _mock_ticker(start_price=100.0, end_price=150.0)
        result = compute_momentum_signal("AAPL")
        assert result is not None
        assert result.momentum_12_1 > 0
        assert result.grade in {"strong_momentum", "momentum", "neutral"}

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_falling_price_negative_momentum(self, mock_yf):
        """start=150 end=100 → negative momentum → reversal_risk or strong_reversal."""
        mock_yf.return_value = _mock_ticker(start_price=150.0, end_price=100.0)
        result = compute_momentum_signal("AAPL")
        assert result is not None
        assert result.momentum_12_1 < 0
        assert result.grade in {"reversal_risk", "strong_reversal", "neutral"}

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_proximity_52w_high_in_range(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_momentum_signal("AAPL")
        assert result is not None
        assert 0.0 < result.proximity_52w_high <= 1.0

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_proximity_1_at_52w_high(self, mock_yf):
        """If end_price is max, proximity should be 1.0."""
        mock_yf.return_value = _mock_ticker(start_price=80.0, end_price=130.0)
        result = compute_momentum_signal("AAPL")
        assert result is not None
        # current is the max, so proximity should be ~1.0
        assert result.proximity_52w_high == pytest.approx(1.0, abs=0.01)

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_52w_high_ge_52w_low(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_momentum_signal("AAPL")
        assert result is not None
        assert result.high_52w >= result.low_52w

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_as_of_date_is_today(self, mock_yf):
        mock_yf.return_value = _mock_ticker()
        result = compute_momentum_signal("AAPL")
        assert result is not None
        assert result.as_of_date <= date.today()

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_returns_none_on_insufficient_data(self, mock_yf):
        """Only 10 bars → insufficient → None."""
        t = MagicMock()
        t.history.return_value = _make_history(n_bars=10)
        t.info = {}
        mock_yf.return_value = t
        assert compute_momentum_signal("X") is None

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_returns_none_on_empty_history(self, mock_yf):
        t = MagicMock()
        t.history.return_value = pd.DataFrame()
        t.info = {}
        mock_yf.return_value = t
        assert compute_momentum_signal("X") is None

    @patch("quantbot.stocks.momentum.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network")
        assert compute_momentum_signal("ERR") is None


# ---------------------------------------------------------------------------
# API: GET /api/momentum
# ---------------------------------------------------------------------------

_MOCK_MOMENTUM = MomentumSignal(
    ticker="AAPL",
    momentum_12_1=22.5,
    return_1m=3.2,
    return_3m=8.1,
    return_6m=15.4,
    high_52w=200.0,
    low_52w=140.0,
    current_price=195.0,
    proximity_52w_high=0.975,
    grade="strong_momentum",
    interpretation="12-1月动量 +22.5%：强动量信号...",
    as_of_date=date(2026, 4, 29),
)


@pytest.fixture
def client():
    from quantbot.web.app import app
    return TestClient(app)


class TestMomentumAPIEndpoint:
    @patch("quantbot.stocks.api.momentum.compute_momentum_signal", return_value=_MOCK_MOMENTUM)
    def test_returns_200(self, mock_fn, client):
        resp = client.get("/api/momentum/?ticker=AAPL")
        assert resp.status_code == 200

    @patch("quantbot.stocks.api.momentum.compute_momentum_signal", return_value=_MOCK_MOMENTUM)
    def test_has_required_fields(self, mock_fn, client):
        data = client.get("/api/momentum/?ticker=AAPL").json()
        for field in ("momentum_12_1", "return_1m", "return_3m", "return_6m",
                      "grade", "high_52w", "low_52w", "current_price",
                      "proximity_52w_high", "interpretation"):
            assert field in data, f"Missing field: {field}"

    @patch("quantbot.stocks.api.momentum.compute_momentum_signal", return_value=_MOCK_MOMENTUM)
    def test_grade_valid(self, mock_fn, client):
        data = client.get("/api/momentum/?ticker=AAPL").json()
        assert data["grade"] in {
            "strong_momentum", "momentum", "neutral", "reversal_risk", "strong_reversal"
        }

    @patch("quantbot.stocks.api.momentum.compute_momentum_signal", return_value=None)
    def test_returns_404_when_no_data(self, mock_fn, client):
        resp = client.get("/api/momentum/?ticker=NODATA")
        assert resp.status_code == 404

    def test_missing_ticker_returns_422(self, client):
        assert client.get("/api/momentum/").status_code == 422

    @patch("quantbot.stocks.api.momentum.compute_momentum_signal", return_value=_MOCK_MOMENTUM)
    def test_momentum_value_correct(self, mock_fn, client):
        data = client.get("/api/momentum/?ticker=AAPL").json()
        assert data["momentum_12_1"] == pytest.approx(22.5)
        assert data["proximity_52w_high"] == pytest.approx(0.975)
