"""Tests for sector_momentum engine and API endpoint.

Covers:
- _sector_grade boundary thresholds
- _pct_return calculation
- compute_sector_momentum happy path (mocked yf.download)
- graceful degradation (yfinance unavailable)
- GET /api/sector-momentum 200
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.sector_momentum.engine import (
    SECTOR_ETFS,
    SectorMomentumData,
    _pct_return,
    _sector_grade,
    compute_sector_momentum,
)


# ---------------------------------------------------------------------------
# AC-4: _sector_grade — boundary tests
# ---------------------------------------------------------------------------


class TestSectorGrade:
    def test_leading_when_vs_spy_above_2(self) -> None:
        assert _sector_grade(5.0, 2.1) == "leading"

    def test_in_line_when_vs_spy_exactly_2(self) -> None:
        # vs_spy > 2.0 → leading; == 2.0 is NOT > 2.0 → in_line
        assert _sector_grade(5.0, 2.0) == "in_line"

    def test_in_line_when_close(self) -> None:
        assert _sector_grade(3.0, 1.0) == "in_line"

    def test_lagging_when_vs_spy_below_minus_2(self) -> None:
        assert _sector_grade(0.0, -2.1) == "lagging"

    def test_in_line_when_vs_spy_exactly_minus_2(self) -> None:
        assert _sector_grade(0.0, -2.0) == "in_line"

    def test_zero_vs_spy_is_in_line(self) -> None:
        assert _sector_grade(0.0, 0.0) == "in_line"


# ---------------------------------------------------------------------------
# AC-4: _pct_return
# ---------------------------------------------------------------------------


class TestPctReturn:
    def _make_series(self, values: list[float]) -> pd.Series:
        return pd.Series(values, dtype=float)

    def test_positive_return(self) -> None:
        # Last = 110, 21 bars back = 100 → +10%
        prices = self._make_series([100.0] * 21 + [110.0])
        result = _pct_return(prices, 21)
        assert abs(result - 10.0) < 0.01

    def test_negative_return(self) -> None:
        prices = self._make_series([100.0] * 21 + [90.0])
        result = _pct_return(prices, 21)
        assert abs(result - (-10.0)) < 0.01

    def test_insufficient_data_returns_zero(self) -> None:
        prices = self._make_series([100.0, 110.0])
        result = _pct_return(prices, 21)
        assert result == 0.0

    def test_zero_start_price_returns_zero(self) -> None:
        prices = self._make_series([0.0] * 21 + [100.0])
        result = _pct_return(prices, 21)
        assert result == 0.0


# ---------------------------------------------------------------------------
# Helpers for mocking yf.download
# ---------------------------------------------------------------------------


def _make_mock_download(tickers: list[str], n_rows: int = 130) -> pd.DataFrame:
    """Build a mock multi-column Close DataFrame as yf.download returns."""

    index = pd.date_range(end=date.today(), periods=n_rows, freq="B")
    data = {}
    for t in tickers:
        base = 100.0 if t == "SPY" else 50.0 + hash(t) % 200
        # Slightly different returns per ticker
        prices = base * (1 + 0.001 * (hash(t) % 5)) ** pd.RangeIndex(n_rows)
        data[t] = prices.values

    close_df = pd.DataFrame(data, index=index)
    # Wrap in a MultiIndex as yfinance does
    close_df.columns = pd.MultiIndex.from_tuples(
        [("Close", t) for t in tickers]
    )
    return close_df


# ---------------------------------------------------------------------------
# AC-4: compute_sector_momentum — happy path
# ---------------------------------------------------------------------------


class TestComputeSectorMomentum:
    def test_happy_path_returns_all_sectors(self) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch("quantbot.stocks.sector_momentum.engine.yf.download", return_value=mock_df):
            result = compute_sector_momentum()

        assert isinstance(result, SectorMomentumData)
        assert result.data_available is True
        assert len(result.sectors) == len(SECTOR_ETFS)
        assert result.as_of_date == date.today()

    def test_top3_and_bottom3_populated(self) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch("quantbot.stocks.sector_momentum.engine.yf.download", return_value=mock_df):
            result = compute_sector_momentum()

        assert len(result.top3) == 3
        assert len(result.bottom3) == 3

    def test_top3_sorted_by_1m_return(self) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch("quantbot.stocks.sector_momentum.engine.yf.download", return_value=mock_df):
            result = compute_sector_momentum()

        if len(result.top3) >= 2:
            assert result.top3[0].return_1m >= result.top3[1].return_1m

    def test_each_sector_has_required_fields(self) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch("quantbot.stocks.sector_momentum.engine.yf.download", return_value=mock_df):
            result = compute_sector_momentum()

        for s in result.sectors:
            assert s.ticker in SECTOR_ETFS
            assert s.sector_name != ""
            assert isinstance(s.return_1m, float)
            assert isinstance(s.return_3m, float)
            assert isinstance(s.return_6m, float)
            assert s.grade in ("leading", "in_line", "lagging")

    def test_spy_return_computed(self) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch("quantbot.stocks.sector_momentum.engine.yf.download", return_value=mock_df):
            result = compute_sector_momentum()

        # SPY had monotonic growth → positive 1M return
        assert isinstance(result.spy_return_1m, float)


# ---------------------------------------------------------------------------
# AC-4: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_empty_download_gives_degraded(self) -> None:
        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            return_value=pd.DataFrame(),
        ):
            result = compute_sector_momentum()

        assert result.data_available is False

    def test_exception_gives_degraded(self) -> None:
        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            side_effect=Exception("network fail"),
        ):
            result = compute_sector_momentum()

        assert result.data_available is False

    def test_never_raises(self) -> None:
        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            side_effect=RuntimeError("boom"),
        ):
            result = compute_sector_momentum()

        assert result is not None
        assert result.data_available is False

    def test_degraded_has_today_date(self) -> None:
        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            side_effect=Exception("err"),
        ):
            result = compute_sector_momentum()

        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# AC-4: API endpoint — GET /api/sector-momentum
# ---------------------------------------------------------------------------


class TestSectorMomentumAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app

        return TestClient(app)

    def test_returns_200_always(self, client: TestClient) -> None:
        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            side_effect=Exception("fail"),
        ):
            resp = client.get("/api/sector-momentum/")

        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False

    def test_happy_path_returns_all_fields(self, client: TestClient) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            return_value=mock_df,
        ):
            resp = client.get("/api/sector-momentum/")

        assert resp.status_code == 200
        body = resp.json()
        for field in ["sectors", "top3", "bottom3", "spy_return_1m", "as_of_date", "data_available"]:
            assert field in body, f"Missing field: {field}"
        assert len(body["sectors"]) == len(SECTOR_ETFS)

    def test_sector_entries_have_grade(self, client: TestClient) -> None:
        tickers = ["SPY"] + list(SECTOR_ETFS.keys())
        mock_df = _make_mock_download(tickers)

        with patch(
            "quantbot.stocks.sector_momentum.engine.yf.download",
            return_value=mock_df,
        ):
            resp = client.get("/api/sector-momentum/")

        body = resp.json()
        for s in body["sectors"]:
            assert s["grade"] in ("leading", "in_line", "lagging")
