"""Tests for smart_money engine and API endpoint.

Covers:
- _classify_bars logic (large/small threshold, buy/sell direction)
- _buy_pressure calculation
- _compute_signal_from_pressure boundaries
- _build_interpretation for each signal
- compute_smart_money happy path + graceful degradation
- GET /api/smart-money 200 + 422
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.smart_money.engine import (
    _build_interpretation,
    _buy_pressure,
    _classify_bars,
    _compute_signal_from_pressure,
    compute_smart_money,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bar_df(rows: list[dict]) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame for testing _classify_bars."""
    records = []
    base = pd.Timestamp("2024-01-02 09:30:00", tz="America/New_York")
    for i, row in enumerate(rows):
        records.append({
            "Open": row.get("open", 100.0),
            "High": row.get("high", 101.0),
            "Low": row.get("low", 99.0),
            "Close": row.get("close", 100.0),
            "Volume": row.get("vol", 1000),
        })
    df = pd.DataFrame(records, index=[base + pd.Timedelta(minutes=i) for i in range(len(records))])
    df.index.name = "Datetime"
    return df


def _make_5d_ohlcv(
    days: int = 5,
    bars_per_day: int = 10,
    base_price: float = 100.0,
    base_vol: int = 1000,
    today_buy_vol_mult: float = 1.0,
) -> pd.DataFrame:
    """Build a 5-day 1-minute OHLCV DataFrame."""
    rows = []
    base_date = date.today() - timedelta(days=days)

    for d in range(days):
        trade_date = base_date + timedelta(days=d)
        # Skip weekends
        if trade_date.weekday() >= 5:
            continue
        for m in range(bars_per_day):
            ts = pd.Timestamp(
                f"{trade_date} 09:30:00", tz="America/New_York"
            ) + pd.Timedelta(minutes=m)
            is_today = d == days - 1
            vol_mult = today_buy_vol_mult if is_today else 1.0
            rows.append({
                "Open": base_price,
                "High": base_price + 1,
                "Low": base_price - 1,
                "Close": base_price + 0.5,  # bullish bar
                "Volume": int(base_vol * vol_mult),
                "Datetime": ts,
            })

    df = pd.DataFrame(rows).set_index("Datetime")
    df.index.name = "Datetime"
    return df


# ---------------------------------------------------------------------------
# SM-1: _classify_bars
# ---------------------------------------------------------------------------


class TestClassifyBars:
    def test_bullish_large_bar_counted_as_buy(self) -> None:
        # close > open → buy; dollar vol > threshold
        df = _make_bar_df([{"open": 100.0, "close": 101.0, "vol": 10_000}])
        buy, sell, count = _classify_bars(df, threshold_usd=500.0)
        assert buy > 0
        assert sell == 0.0
        assert count == 1

    def test_bearish_large_bar_counted_as_sell(self) -> None:
        df = _make_bar_df([{"open": 101.0, "close": 100.0, "vol": 10_000}])
        buy, sell, count = _classify_bars(df, threshold_usd=500.0)
        assert sell > 0
        assert buy == 0.0
        assert count == 1

    def test_doji_bar_not_counted(self) -> None:
        df = _make_bar_df([{"open": 100.0, "close": 100.0, "vol": 10_000}])
        buy, sell, count = _classify_bars(df, threshold_usd=500.0)
        assert count == 1  # counted but not buy or sell
        assert buy == 0.0
        assert sell == 0.0

    def test_small_bar_excluded(self) -> None:
        # dollar vol = (100+100+100)/3 × 1 = 100 < threshold 1000
        df = _make_bar_df([{"open": 100.0, "close": 100.5, "vol": 1}])
        buy, sell, count = _classify_bars(df, threshold_usd=1_000.0)
        assert count == 0
        assert buy == 0.0
        assert sell == 0.0

    def test_multiple_bars_aggregated(self) -> None:
        df = _make_bar_df([
            {"open": 100.0, "close": 101.0, "vol": 10_000},  # buy
            {"open": 101.0, "close": 100.0, "vol": 10_000},  # sell
            {"open": 100.0, "close": 101.0, "vol": 10_000},  # buy
        ])
        buy, sell, count = _classify_bars(df, threshold_usd=500.0)
        assert count == 3
        assert buy > sell

    def test_empty_df_returns_zeros(self) -> None:
        buy, sell, count = _classify_bars(pd.DataFrame(), threshold_usd=500.0)
        assert buy == 0.0
        assert sell == 0.0
        assert count == 0


# ---------------------------------------------------------------------------
# SM-2: _buy_pressure
# ---------------------------------------------------------------------------


class TestBuyPressure:
    def test_all_buy_returns_100(self) -> None:
        assert _buy_pressure(1000.0, 0.0) == 100.0

    def test_all_sell_returns_0(self) -> None:
        assert _buy_pressure(0.0, 1000.0) == 0.0

    def test_equal_returns_50(self) -> None:
        assert _buy_pressure(500.0, 500.0) == 50.0

    def test_zero_total_returns_none(self) -> None:
        assert _buy_pressure(0.0, 0.0) is None

    def test_60pct_buy(self) -> None:
        assert abs(_buy_pressure(600.0, 400.0) - 60.0) < 0.01


# ---------------------------------------------------------------------------
# SM-3: _compute_signal_from_pressure
# ---------------------------------------------------------------------------


class TestComputeSignalFromPressure:
    def test_strong_buy_signal(self) -> None:
        # today > 60 and delta >= 10
        assert _compute_signal_from_pressure(70.0, 55.0) == "smart_money_buy"

    def test_strong_sell_signal(self) -> None:
        # today < 40 and delta <= -10
        assert _compute_signal_from_pressure(30.0, 45.0) == "smart_money_sell"

    def test_accumulation_55_to_60(self) -> None:
        assert _compute_signal_from_pressure(58.0, 55.0) == "accumulation"

    def test_distribution_40_to_45(self) -> None:
        assert _compute_signal_from_pressure(42.0, 45.0) == "distribution"

    def test_neutral_in_middle(self) -> None:
        assert _compute_signal_from_pressure(50.0, 50.0) == "neutral"

    def test_none_today_is_no_data(self) -> None:
        assert _compute_signal_from_pressure(None, 50.0) == "no_data"

    def test_strong_buy_needs_delta_10(self) -> None:
        # today = 65, avg = 60 → delta = 5 < 10 → not strong buy
        result = _compute_signal_from_pressure(65.0, 60.0)
        assert result != "smart_money_buy"

    def test_no_avg_does_not_crash(self) -> None:
        # avg_5d is None → delta defaults to 0
        result = _compute_signal_from_pressure(70.0, None)
        # With delta=0, today=70>60 but delta<10 → accumulation
        assert result in ("smart_money_buy", "accumulation")


# ---------------------------------------------------------------------------
# SM-4: _build_interpretation
# ---------------------------------------------------------------------------


class TestBuildInterpretation:
    def test_buy_signal_mentions_buy(self) -> None:
        interp = _build_interpretation("smart_money_buy", 70.0, 55.0, 50_000.0)
        assert "买压" in interp or "建仓" in interp

    def test_sell_signal_mentions_sell(self) -> None:
        interp = _build_interpretation("smart_money_sell", 30.0, 45.0, 50_000.0)
        assert "卖压" in interp or "派发" in interp

    def test_no_data_interpretation(self) -> None:
        interp = _build_interpretation("no_data", None, None, None)
        assert len(interp) > 0

    def test_neutral_interpretation(self) -> None:
        interp = _build_interpretation("neutral", 50.0, 50.0, 50_000.0)
        assert len(interp) > 0


# ---------------------------------------------------------------------------
# SM-5: compute_smart_money — happy path
# ---------------------------------------------------------------------------


class TestComputeSmartMoney:
    def test_happy_path_data_available(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("AAPL")
        assert result.data_available is True
        assert result.ticker == "AAPL"

    def test_ticker_uppercased(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("aapl")
        assert result.ticker == "AAPL"

    def test_as_of_date_is_today(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("MSFT")
        assert result.as_of_date == date.today()

    def test_signal_is_valid(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("NVDA")
        valid = {"smart_money_buy", "smart_money_sell", "accumulation", "distribution", "neutral", "no_data"}
        assert result.signal in valid

    def test_daily_flows_populated(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("TSLA")
        assert len(result.daily_flows) >= 1

    def test_interpretation_non_empty(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("GOOGL")
        assert isinstance(result.interpretation, str)
        assert len(result.interpretation) > 0

    def test_buy_pressure_in_range(self) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            result = compute_smart_money("AMZN")
        if result.today_buy_pressure_pct is not None:
            assert 0.0 <= result.today_buy_pressure_pct <= 100.0


# ---------------------------------------------------------------------------
# SM-6: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_exception_gives_data_available_false(self) -> None:
        with patch("quantbot.stocks.smart_money.engine.yf.download", side_effect=Exception("fail")):
            result = compute_smart_money("FAKE")
        assert result.data_available is False

    def test_never_raises(self) -> None:
        with patch("quantbot.stocks.smart_money.engine.yf.download", side_effect=RuntimeError("boom")):
            result = compute_smart_money("ERROR")
        assert result is not None

    def test_empty_df_gives_degraded(self) -> None:
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=pd.DataFrame()):
            result = compute_smart_money("EMPTY")
        assert result.data_available is False

    def test_degraded_as_of_date_is_today(self) -> None:
        with patch("quantbot.stocks.smart_money.engine.yf.download", side_effect=Exception("err")):
            result = compute_smart_money("META")
        assert result.as_of_date == date.today()


# ---------------------------------------------------------------------------
# SM-7: API endpoint
# ---------------------------------------------------------------------------


class TestSmartMoneyAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app
        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/smart-money/")
        assert resp.status_code == 422

    def test_always_200_even_when_degraded(self, client: TestClient) -> None:
        with patch("quantbot.stocks.smart_money.engine.yf.download", side_effect=Exception("fail")):
            resp = client.get("/api/smart-money/?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_all_fields(self, client: TestClient) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            resp = client.get("/api/smart-money/?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "signal", "today_buy_pressure_pct", "avg_5d_buy_pressure_pct",
            "large_threshold_usd", "daily_flows", "interpretation", "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"

    def test_daily_flows_is_list(self, client: TestClient) -> None:
        mock_df = _make_5d_ohlcv()
        with patch("quantbot.stocks.smart_money.engine.yf.download", return_value=mock_df):
            resp = client.get("/api/smart-money/?ticker=MSFT")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["daily_flows"], list)
