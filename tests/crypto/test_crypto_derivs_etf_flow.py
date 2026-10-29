"""Unit tests for quantbot.crypto.crypto_derivs.etf_flow."""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest

from quantbot.crypto.crypto_derivs import (
    BTC_SPOT_ETF_TICKERS,
    ETFFlowSnapshot,
    ETH_SPOT_ETF_TICKERS,
    aggregate_daily_flows,
    flow_aum_velocity,
    flow_extreme_signal,
    flow_zscore,
)


def _snap(d: str, ticker: str, flow: float, aum: float | None = None) -> ETFFlowSnapshot:
    return ETFFlowSnapshot(
        date=date.fromisoformat(d), ticker=ticker, net_flow_usd=flow, aum_usd=aum
    )


# --- Constants ---------------------------------------------------------------


class TestTickerConstants:
    def test_btc_tickers_includes_ibit_fbtc(self) -> None:
        assert "IBIT" in BTC_SPOT_ETF_TICKERS
        assert "FBTC" in BTC_SPOT_ETF_TICKERS
        assert "GBTC" in BTC_SPOT_ETF_TICKERS

    def test_eth_tickers_includes_etha(self) -> None:
        assert "ETHA" in ETH_SPOT_ETF_TICKERS
        assert "FETH" in ETH_SPOT_ETF_TICKERS

    def test_no_overlap_between_btc_and_eth(self) -> None:
        # GBTC 仅 BTC，ETHA 仅 ETH
        assert set(BTC_SPOT_ETF_TICKERS).isdisjoint(set(ETH_SPOT_ETF_TICKERS))


# --- aggregate_daily_flows --------------------------------------------------


class TestAggregateDailyFlows:
    def test_no_filter_sums_all_tickers(self) -> None:
        snaps = [
            _snap("2026-04-01", "IBIT", 100_000_000),
            _snap("2026-04-01", "FBTC", 50_000_000),
            _snap("2026-04-02", "IBIT", -20_000_000),
        ]
        result = aggregate_daily_flows(snaps)
        assert math.isclose(result["2026-04-01"], 150_000_000)
        assert math.isclose(result["2026-04-02"], -20_000_000)

    def test_filter_btc_only(self) -> None:
        snaps = [
            _snap("2026-04-01", "IBIT", 100_000_000),
            _snap("2026-04-01", "ETHA", 30_000_000),  # ETH ETF
            _snap("2026-04-01", "FBTC", 50_000_000),
        ]
        result = aggregate_daily_flows(snaps, tickers=BTC_SPOT_ETF_TICKERS)
        assert math.isclose(result["2026-04-01"], 150_000_000)

    def test_case_insensitive_filter(self) -> None:
        snaps = [_snap("2026-04-01", "ibit", 100_000_000)]
        result = aggregate_daily_flows(snaps, tickers=("IBIT",))
        assert math.isclose(result["2026-04-01"], 100_000_000)

    def test_empty_input_returns_empty(self) -> None:
        assert aggregate_daily_flows([]) == {}


# --- flow_zscore -------------------------------------------------------------


class TestFlowZscore:
    def test_output_length_matches(self) -> None:
        rng = np.random.default_rng(0)
        flows = rng.normal(50_000_000, 30_000_000, size=100).tolist()
        out = flow_zscore(flows, window=30)
        assert len(out) == 100

    def test_first_window_minus_one_are_nan(self) -> None:
        rng = np.random.default_rng(1)
        flows = rng.normal(0.0, 1.0, size=100).tolist()
        out = flow_zscore(flows, window=30)
        assert all(math.isnan(x) for x in out[:29])
        assert not math.isnan(out[29])

    def test_constant_window_returns_zero(self) -> None:
        flows = [50_000_000.0] * 60
        out = flow_zscore(flows, window=30)
        # 后半段 std=0 → z=0
        assert math.isclose(out[-1], 0.0)

    def test_window_too_small_raises(self) -> None:
        with pytest.raises(ValueError):
            flow_zscore([1.0] * 100, window=3)

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            flow_zscore([1.0, 2.0, 3.0], window=30)


# --- flow_extreme_signal -----------------------------------------------------


class TestFlowExtremeSignal:
    def test_large_inflow(self) -> None:
        rng = np.random.default_rng(2)
        history = rng.normal(50_000_000, 20_000_000, size=200).tolist()
        # 5σ 异常正流入
        result = flow_extreme_signal(50_000_000 + 5 * 20_000_000, history)
        assert result["signal"] == "large_inflow"
        assert isinstance(result["z_score"], float) and result["z_score"] > 2.0

    def test_large_outflow(self) -> None:
        rng = np.random.default_rng(3)
        history = rng.normal(50_000_000, 20_000_000, size=200).tolist()
        result = flow_extreme_signal(50_000_000 - 5 * 20_000_000, history)
        assert result["signal"] == "large_outflow"

    def test_neutral(self) -> None:
        rng = np.random.default_rng(4)
        history = rng.normal(50_000_000, 20_000_000, size=200).tolist()
        result = flow_extreme_signal(50_000_000, history)
        assert result["signal"] == "neutral"

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            flow_extreme_signal(0.0, [1.0] * 20)

    def test_invalid_threshold_raises(self) -> None:
        with pytest.raises(ValueError):
            flow_extreme_signal(0.0, [1.0] * 50, z_threshold=0.0)


# --- flow_aum_velocity -------------------------------------------------------


class TestFlowAumVelocity:
    def test_normal_velocity(self) -> None:
        # $500M flow / $20B AUM → 2.5%
        v = flow_aum_velocity(500_000_000, 20_000_000_000)
        assert math.isclose(v, 2.5)

    def test_negative_flow_negative_velocity(self) -> None:
        v = flow_aum_velocity(-1_000_000_000, 20_000_000_000)
        assert math.isclose(v, -5.0)

    def test_zero_aum_raises(self) -> None:
        with pytest.raises(ValueError):
            flow_aum_velocity(1.0, 0.0)
