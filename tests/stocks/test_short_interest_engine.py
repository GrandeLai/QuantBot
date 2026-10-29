"""Unit tests for short interest engine."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from quantbot.stocks.short_interest.engine import (
    ShortInterestData,
    _compute_squeeze_score,
    _squeeze_signal,
    compute_short_interest,
)


def _mock_info(
    short_pct: float = 0.10,
    short_ratio: float = 3.5,
    shares_short: int = 50_000_000,
    shares_short_prior: int = 45_000_000,
    float_shares: int = 500_000_000,
    avg_vol: int = 15_000_000,
    current_price: float = 150.0,
    high_52w: float = 180.0,
) -> dict:
    return {
        "shortPercentOfFloat": short_pct,
        "shortRatio": short_ratio,
        "sharesShort": shares_short,
        "sharesShortPriorMonth": shares_short_prior,
        "floatShares": float_shares,
        "averageDailyVolume10Day": avg_vol,
        "currentPrice": current_price,
        "fiftyTwoWeekHigh": high_52w,
    }


# ---------------------------------------------------------------------------
# _compute_squeeze_score
# ---------------------------------------------------------------------------


class TestComputeSqueezeScore:
    def test_high_pct_high_dtc_high_momentum(self):
        # 25% float, 12 DTC, price at 95% of 52w high
        score = _compute_squeeze_score(0.25, 12.0, 0.95)
        assert score >= 0.6

    def test_zero_pct_returns_zero(self):
        assert _compute_squeeze_score(0.0, 5.0, 0.8) == pytest.approx(0.0)

    def test_none_pct_returns_zero(self):
        assert _compute_squeeze_score(None, 5.0, 0.8) == pytest.approx(0.0)

    def test_score_in_range(self):
        for pct in [0, 0.05, 0.15, 0.25, 0.40]:
            for dtc in [1, 5, 10, 20]:
                for pvh in [0.5, 0.7, 0.85, 1.0]:
                    s = _compute_squeeze_score(pct, float(dtc), pvh)
                    assert 0.0 <= s <= 1.0

    def test_low_short_returns_low_score(self):
        score = _compute_squeeze_score(0.02, 1.0, 0.5)
        assert score < 0.2


# ---------------------------------------------------------------------------
# _squeeze_signal
# ---------------------------------------------------------------------------


class TestSqueezeSignal:
    def test_squeeze_setup(self):
        sig = _squeeze_signal(0.25, 0.75, 0.90)
        assert sig == "squeeze_setup"

    def test_high_short(self):
        sig = _squeeze_signal(0.20, 0.40, 0.60)
        assert sig == "high_short"

    def test_moderate(self):
        sig = _squeeze_signal(0.08, 0.20, 0.70)
        assert sig == "moderate"

    def test_low_short(self):
        sig = _squeeze_signal(0.02, 0.05, 0.50)
        assert sig == "low_short"


# ---------------------------------------------------------------------------
# compute_short_interest
# ---------------------------------------------------------------------------


class TestComputeShortInterest:
    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_returns_short_interest_data(self, mock_yf):
        t = MagicMock()
        t.info = _mock_info()
        mock_yf.return_value = t
        result = compute_short_interest("GME")
        assert isinstance(result, ShortInterestData)
        assert result.ticker == "GME"

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_short_pct_float_populated(self, mock_yf):
        t = MagicMock()
        t.info = _mock_info(short_pct=0.12)
        mock_yf.return_value = t
        result = compute_short_interest("AAPL")
        assert result is not None
        assert result.short_pct_float == pytest.approx(0.12)

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_short_change_pct_computed(self, mock_yf):
        t = MagicMock()
        t.info = _mock_info(shares_short=55_000_000, shares_short_prior=50_000_000)
        mock_yf.return_value = t
        result = compute_short_interest("X")
        assert result is not None
        assert result.short_change_pct == pytest.approx(0.10, rel=1e-3)

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_price_vs_52w_high_computed(self, mock_yf):
        t = MagicMock()
        t.info = _mock_info(current_price=160.0, high_52w=200.0)
        mock_yf.return_value = t
        result = compute_short_interest("X")
        assert result is not None
        assert result.price_vs_52w_high == pytest.approx(0.80)

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_signal_is_valid_literal(self, mock_yf):
        t = MagicMock()
        t.info = _mock_info()
        mock_yf.return_value = t
        result = compute_short_interest("X")
        assert result is not None
        assert result.signal in {"squeeze_setup", "high_short", "moderate", "low_short"}

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_as_of_date_is_today(self, mock_yf):
        t = MagicMock()
        t.info = _mock_info()
        mock_yf.return_value = t
        result = compute_short_interest("X")
        assert result is not None
        assert result.as_of_date == date.today()

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_returns_none_when_no_short_pct(self, mock_yf):
        t = MagicMock()
        t.info = {"currentPrice": 100.0}  # missing shortPercentOfFloat
        mock_yf.return_value = t
        assert compute_short_interest("NODATA") is None

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("timeout")
        assert compute_short_interest("ERR") is None

    @patch("quantbot.stocks.short_interest.engine.yf.Ticker")
    def test_squeeze_setup_detected(self, mock_yf):
        """High short float + high DTC + near 52w high → squeeze_setup."""
        t = MagicMock()
        t.info = _mock_info(
            short_pct=0.30,       # 30% of float short
            short_ratio=12.0,     # 12 days to cover
            current_price=190.0,  # 95% of 52w high
            high_52w=200.0,
        )
        mock_yf.return_value = t
        result = compute_short_interest("GME")
        assert result is not None
        assert result.signal == "squeeze_setup"
