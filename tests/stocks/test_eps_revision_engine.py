"""Unit tests for EPS revision momentum engine.

All yfinance calls are mocked; no real network requests.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from quantbot.stocks.eps_revision.engine import (
    AnalystTargets,
    EpsRevisionMomentum,
    EpsRevisionPeriod,
    _revision_direction,
    _revision_score,
    _safe_float,
    _safe_int,
    compute_eps_revision_momentum,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_revisions_df(
    up_7d: dict[str, int] | None = None,
    down_7d: dict[str, int] | None = None,
    up_30d: dict[str, int] | None = None,
    down_30d: dict[str, int] | None = None,
) -> pd.DataFrame:
    """Build a mock eps_revisions DataFrame."""
    periods = ["0q", "+1q", "0y", "+1y"]
    u7  = up_7d   or {p: 3 for p in periods}
    d7  = down_7d  or {p: 1 for p in periods}
    u30 = up_30d  or {p: 7 for p in periods}
    d30 = down_30d or {p: 2 for p in periods}
    return pd.DataFrame(
        {
            "upLast7days":   u7,
            "downLast7days": d7,
            "upLast30days":  u30,
            "downLast30days":d30,
        }
    ).T  # index = row labels, columns = period codes


def _mock_ticker(revisions_df, info: dict | None = None):
    t = MagicMock()
    t.eps_revisions = revisions_df
    t.info = info or {
        "currentPrice": 175.0,
        "targetMeanPrice": 210.0,
        "targetMedianPrice": 205.0,
        "targetHighPrice": 250.0,
        "targetLowPrice": 150.0,
    }
    return t


# ---------------------------------------------------------------------------
# _revision_score
# ---------------------------------------------------------------------------


class TestRevisionScore:
    def test_all_up(self):
        assert _revision_score(10, 0) == pytest.approx(1.0)

    def test_all_down(self):
        assert _revision_score(0, 10) == pytest.approx(-1.0)

    def test_balanced(self):
        assert _revision_score(5, 5) == pytest.approx(0.0)

    def test_zero_total_returns_zero(self):
        # up=0, down=0 → score = (0-0)/max(1,0) = 0
        assert _revision_score(0, 0) == pytest.approx(0.0)

    def test_skewed_up(self):
        score = _revision_score(7, 3)
        assert score == pytest.approx((7 - 3) / 10)


# ---------------------------------------------------------------------------
# _revision_direction
# ---------------------------------------------------------------------------


class TestRevisionDirection:
    def test_strong_upgrade(self):
        assert _revision_direction(0.8) == "strong_upgrade"

    def test_upgrade(self):
        assert _revision_direction(0.3) == "upgrade"

    def test_neutral(self):
        assert _revision_direction(0.0) == "neutral"

    def test_downgrade(self):
        assert _revision_direction(-0.3) == "downgrade"

    def test_strong_downgrade(self):
        assert _revision_direction(-0.9) == "strong_downgrade"


# ---------------------------------------------------------------------------
# _safe_float / _safe_int
# ---------------------------------------------------------------------------


class TestSafeHelpers:
    def test_safe_float_normal(self):
        assert _safe_float(3.14) == pytest.approx(3.14)

    def test_safe_float_none(self):
        assert _safe_float(None) is None

    def test_safe_float_nan(self):
        assert _safe_float(float("nan")) is None

    def test_safe_int_normal(self):
        assert _safe_int(5) == 5

    def test_safe_int_none_returns_default(self):
        assert _safe_int(None) == 0

    def test_safe_int_float_str(self):
        assert _safe_int("3.0") == 3


# ---------------------------------------------------------------------------
# compute_eps_revision_momentum — happy path
# ---------------------------------------------------------------------------


class TestComputeEpsRevisionMomentum:
    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_returns_eps_revision_momentum(self, mock_yf):
        mock_yf.return_value = _mock_ticker(_make_revisions_df())
        result = compute_eps_revision_momentum("AAPL")
        assert isinstance(result, EpsRevisionMomentum)
        assert result.ticker == "AAPL"

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_periods_are_populated(self, mock_yf):
        mock_yf.return_value = _mock_ticker(_make_revisions_df())
        result = compute_eps_revision_momentum("AAPL")
        assert result is not None
        assert len(result.periods) > 0

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_period_has_required_fields(self, mock_yf):
        mock_yf.return_value = _mock_ticker(_make_revisions_df())
        result = compute_eps_revision_momentum("AAPL")
        assert result is not None
        p = result.periods[0]
        assert isinstance(p, EpsRevisionPeriod)
        assert -1.0 <= p.revision_score_7d <= 1.0
        assert -1.0 <= p.revision_score_30d <= 1.0
        assert p.direction in {
            "strong_upgrade", "upgrade", "neutral", "downgrade", "strong_downgrade"
        }

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_overall_direction_valid(self, mock_yf):
        mock_yf.return_value = _mock_ticker(_make_revisions_df())
        result = compute_eps_revision_momentum("MSFT")
        assert result is not None
        assert result.overall_direction in {
            "strong_upgrade", "upgrade", "neutral", "downgrade", "strong_downgrade"
        }

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_targets_populated(self, mock_yf):
        mock_yf.return_value = _mock_ticker(_make_revisions_df())
        result = compute_eps_revision_momentum("AAPL")
        assert result is not None
        assert result.targets is not None
        assert isinstance(result.targets, AnalystTargets)
        assert result.targets.target_mean == pytest.approx(210.0)

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_upside_pct_computed(self, mock_yf):
        info = {
            "currentPrice": 175.0,
            "targetMeanPrice": 210.0,
        }
        mock_yf.return_value = _mock_ticker(_make_revisions_df(), info)
        result = compute_eps_revision_momentum("AAPL")
        assert result is not None and result.targets is not None
        assert result.targets.upside_pct == pytest.approx((210 - 175) / 175, rel=1e-3)

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_as_of_date_is_today(self, mock_yf):
        mock_yf.return_value = _mock_ticker(_make_revisions_df())
        result = compute_eps_revision_momentum("NVDA")
        assert result is not None
        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_returns_none_for_empty_revisions(self, mock_yf):
        t = MagicMock()
        t.eps_revisions = pd.DataFrame()
        t.info = {}
        mock_yf.return_value = t
        assert compute_eps_revision_momentum("FAKE") is None

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_returns_none_on_exception(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network failure")
        assert compute_eps_revision_momentum("ERR") is None

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_handles_none_revisions(self, mock_yf):
        t = MagicMock()
        t.eps_revisions = None
        t.info = {}
        mock_yf.return_value = t
        assert compute_eps_revision_momentum("NONE") is None

    @patch("quantbot.stocks.eps_revision.engine.yf.Ticker")
    def test_strong_upgrade_when_all_up(self, mock_yf):
        df = _make_revisions_df(
            up_7d={"0q": 10}, down_7d={"0q": 0},
            up_30d={"0q": 20}, down_30d={"0q": 0},
        )
        mock_yf.return_value = _mock_ticker(df)
        result = compute_eps_revision_momentum("HOT")
        assert result is not None
        # At least one period should be strong_upgrade
        directions = {p.direction for p in result.periods}
        assert "strong_upgrade" in directions
