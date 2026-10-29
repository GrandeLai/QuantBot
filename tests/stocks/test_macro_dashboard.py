"""Tests for Macro Dashboard — Phase F.27.

All network calls are mocked. Target ≥ 16 tests.
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.macro_dashboard.engine import (
    _classify_regime,
    _safe_last,
    _vix_percentile,
    compute_macro_dashboard,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hist(values: list[float], days_back: int = 260) -> pd.DataFrame:
    """Build a minimal history DataFrame with Close column."""
    dates = [date.today() - timedelta(days=days_back - i) for i in range(len(values))]
    ts = pd.to_datetime([d.isoformat() for d in dates])
    return pd.DataFrame({"Close": values}, index=ts)


def _vix_hist(current: float, n: int = 252) -> pd.DataFrame:
    """Generate a year of VIX-like history with a known current value."""
    # Random values between 10 and 40
    rng = np.random.default_rng(42)
    values = rng.uniform(10, 40, n - 1).tolist() + [current]
    return _make_hist(values, days_back=n)


def _side_effect_factory(symbol_map: dict[str, pd.DataFrame]) -> object:
    """Return a side_effect callable that returns different DataFrames per symbol."""
    class _MockTicker:
        def __init__(self, symbol: str):
            self._symbol = symbol

        def history(self, period: str = "1y") -> pd.DataFrame:
            return symbol_map.get(self._symbol, pd.DataFrame())

    return _MockTicker


def _patch_all_symbols(
    vix: float = 18.0,
    yield_10y: float = 4.25,
    yield_3m: float = 5.10,
    dxy: float = 103.5,
    gold: float = 2400.0,
    oil: float = 78.5,
) -> dict[str, pd.DataFrame]:
    return {
        "^VIX": _vix_hist(vix),
        "^TNX": _make_hist([yield_10y], 5),
        "^IRX": _make_hist([yield_3m], 5),
        "DX-Y.NYB": _make_hist([dxy], 5),
        "GC=F": _make_hist([gold], 5),
        "CL=F": _make_hist([oil], 5),
    }


# ---------------------------------------------------------------------------
# TestSafeLast
# ---------------------------------------------------------------------------

class TestSafeLast:
    def test_returns_last_close(self):
        df = _make_hist([100.0, 105.0, 103.0], 3)
        assert _safe_last(df) == pytest.approx(103.0)

    def test_empty_df_returns_none(self):
        assert _safe_last(pd.DataFrame()) is None

    def test_missing_col_returns_none(self):
        df = pd.DataFrame({"Open": [100.0]})
        assert _safe_last(df) is None


# ---------------------------------------------------------------------------
# TestVixPercentile
# ---------------------------------------------------------------------------

class TestVixPercentile:
    def test_high_vix_gives_high_percentile(self):
        """VIX at max of series → percentile ~100."""
        hist = _vix_hist(current=45.0)
        pct = _vix_percentile(hist)
        assert pct is not None and pct > 80

    def test_low_vix_gives_low_percentile(self):
        """VIX at very low value → percentile ~0."""
        hist = _vix_hist(current=5.0)
        pct = _vix_percentile(hist)
        assert pct is not None and pct < 20

    def test_empty_hist_returns_none(self):
        assert _vix_percentile(pd.DataFrame()) is None


# ---------------------------------------------------------------------------
# TestClassifyRegime
# ---------------------------------------------------------------------------

class TestClassifyRegime:
    def test_extreme_risk_off_high_vix(self):
        assert _classify_regime(vix=35.0, vix_pct=85.0, yield_spread=-0.5, inverted=True) == "extreme_risk_off"

    def test_extreme_risk_off_vix_above_30(self):
        assert _classify_regime(vix=31.0, vix_pct=50.0, yield_spread=0.5, inverted=False) == "extreme_risk_off"

    def test_risk_off_moderate_vix(self):
        assert _classify_regime(vix=22.0, vix_pct=55.0, yield_spread=-0.2, inverted=True) == "risk_off"

    def test_risk_off_by_percentile(self):
        assert _classify_regime(vix=19.0, vix_pct=65.0, yield_spread=0.1, inverted=False) == "risk_off"

    def test_risk_on(self):
        result = _classify_regime(vix=12.0, vix_pct=20.0, yield_spread=0.5, inverted=False)
        assert result == "risk_on"

    def test_neutral(self):
        result = _classify_regime(vix=17.0, vix_pct=45.0, yield_spread=0.2, inverted=False)
        assert result == "neutral"

    def test_unknown_when_all_none(self):
        assert _classify_regime(None, None, None, False) == "unknown"

    def test_inverted_curve_flags(self):
        """Inverted curve + high VIX pct → extreme_risk_off."""
        result = _classify_regime(vix=19.0, vix_pct=75.0, yield_spread=-0.1, inverted=True)
        assert result == "extreme_risk_off"


# ---------------------------------------------------------------------------
# TestComputeMacroDashboardNormal
# ---------------------------------------------------------------------------

class TestComputeMacroDashboardNormal:
    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_data_available_true(self, mock_yf):
        symbol_map = _patch_all_symbols()
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.data_available is True

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_vix_populated(self, mock_yf):
        symbol_map = _patch_all_symbols(vix=18.0)
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.vix == pytest.approx(18.0, abs=0.1)

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_yield_spread_and_inversion(self, mock_yf):
        """yield_10y < yield_3m → inverted."""
        symbol_map = _patch_all_symbols(yield_10y=4.0, yield_3m=5.0)
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.yield_curve_inverted is True
        assert result.yield_spread is not None and result.yield_spread < 0

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_normal_yield_curve(self, mock_yf):
        symbol_map = _patch_all_symbols(yield_10y=4.5, yield_3m=4.0)
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.yield_curve_inverted is False
        assert result.yield_spread is not None and result.yield_spread > 0

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_extreme_risk_off_high_vix(self, mock_yf):
        symbol_map = _patch_all_symbols(vix=32.0)
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.regime == "extreme_risk_off"

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_interpretation_populated(self, mock_yf):
        symbol_map = _patch_all_symbols()
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert len(result.interpretation) > 5


# ---------------------------------------------------------------------------
# TestGracefulDegradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_all_symbols_fail_returns_default(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        result = compute_macro_dashboard()
        assert result.data_available is False
        assert result.regime == "unknown"

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_single_symbol_fail_still_data_available(self, mock_yf):
        """If VIX fails but other symbols succeed, data_available=True."""
        symbol_map = _patch_all_symbols()
        # VIX returns empty DataFrame, others succeed
        symbol_map["^VIX"] = pd.DataFrame()

        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.data_available is True
        assert result.vix is None  # VIX failed
        # At least one other field should have data
        assert result.dxy is not None or result.gold is not None

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_yield_spread_none_when_either_missing(self, mock_yf):
        """If TNX is missing, yield_spread should be None."""
        symbol_map = _patch_all_symbols()
        symbol_map["^TNX"] = pd.DataFrame()  # no 10yr data
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        result = compute_macro_dashboard()
        assert result.yield_10y is None
        assert result.yield_spread is None


# ---------------------------------------------------------------------------
# TestMacroDashboardAPIEndpoint
# ---------------------------------------------------------------------------

class TestMacroDashboardAPIEndpoint:
    def _get_client(self) -> TestClient:
        from quantbot.web.app import create_app
        return TestClient(create_app())

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_no_params_returns_200(self, mock_yf):
        symbol_map = _patch_all_symbols()
        mock_yf.side_effect = _side_effect_factory(symbol_map)
        client = self._get_client()
        resp = client.get("/api/macro-dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert "vix" in body
        assert "regime" in body
        assert "yield_curve_inverted" in body

    @patch("quantbot.stocks.macro_dashboard.engine.yf.Ticker")
    def test_all_fail_returns_200_unavailable(self, mock_yf):
        mock_yf.side_effect = RuntimeError("network error")
        client = self._get_client()
        resp = client.get("/api/macro-dashboard")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
