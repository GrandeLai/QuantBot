"""Unit tests for quantbot.crypto.crypto_derivs.analytics."""
from __future__ import annotations

import math

import numpy as np
import pytest

from quantbot.crypto.crypto_derivs import (
    compute_basis,
    funding_extreme_signal,
    funding_percentile_stats,
    oi_momentum,
)


# --- compute_basis -----------------------------------------------------------


class TestComputeBasis:
    def test_perp_premium(self) -> None:
        # spot 50000, perp 50050 → basis 50, ~10 bps; funding 0.01%/8h → APR ~10.95%
        result = compute_basis(50000.0, 50050.0, 0.0001)
        assert math.isclose(result["basis_abs"], 50.0)
        assert math.isclose(result["basis_bps"], 10.0)
        # 0.0001 × 1095 = 0.1095
        assert math.isclose(result["funding_apr"], 0.1095, rel_tol=1e-9)

    def test_perp_discount(self) -> None:
        result = compute_basis(50000.0, 49975.0, -0.00005)
        assert result["basis_abs"] == -25.0
        assert math.isclose(result["basis_bps"], -5.0)
        assert result["funding_apr"] < 0.0

    def test_custom_fundings_per_year(self) -> None:
        # 用 1095 年化 vs 365（每日 funding） → 不同 APR
        r_8h = compute_basis(100.0, 101.0, 0.0001)
        r_daily = compute_basis(100.0, 101.0, 0.0001, fundings_per_year=365)
        assert r_daily["funding_apr"] < r_8h["funding_apr"]

    def test_invalid_prices_raise(self) -> None:
        with pytest.raises(ValueError):
            compute_basis(0.0, 50050.0, 0.0001)
        with pytest.raises(ValueError):
            compute_basis(50000.0, -50050.0, 0.0001)


# --- funding_percentile_stats -----------------------------------------------


class TestFundingPercentileStats:
    def test_full_dict_keys(self) -> None:
        rng = np.random.default_rng(42)
        history = rng.normal(0.0001, 0.00005, size=300).tolist()
        stats = funding_percentile_stats(history)
        for key in ("mean", "std", "p5", "p25", "p50", "p75", "p95"):
            assert key in stats
            assert isinstance(stats[key], float)

    def test_percentile_ordering(self) -> None:
        rng = np.random.default_rng(7)
        history = rng.normal(0.0, 0.0002, size=500).tolist()
        s = funding_percentile_stats(history)
        assert s["p5"] < s["p25"] < s["p50"] < s["p75"] < s["p95"]

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            funding_percentile_stats([0.0001] * 20)


# --- funding_extreme_signal --------------------------------------------------


class TestFundingExtremeSignal:
    def test_high_z_triggers_contrarian_short(self) -> None:
        rng = np.random.default_rng(0)
        history = rng.normal(0.0001, 0.00005, size=200).tolist()
        # 当前 = mean + 5σ → 远超 +2σ
        result = funding_extreme_signal(0.0001 + 5 * 0.00005, history)
        assert result["signal"] == "contrarian_short"
        assert isinstance(result["z_score"], float)
        assert result["z_score"] > 2.0

    def test_low_z_triggers_contrarian_long(self) -> None:
        rng = np.random.default_rng(1)
        history = rng.normal(0.0001, 0.00005, size=200).tolist()
        result = funding_extreme_signal(0.0001 - 5 * 0.00005, history)
        assert result["signal"] == "contrarian_long"
        assert isinstance(result["z_score"], float)
        assert result["z_score"] < -2.0

    def test_neutral(self) -> None:
        rng = np.random.default_rng(2)
        history = rng.normal(0.0001, 0.00005, size=200).tolist()
        result = funding_extreme_signal(0.0001, history)
        assert result["signal"] == "neutral"

    def test_zero_std_history_returns_neutral(self) -> None:
        history = [0.0001] * 50
        # std=0 → z_score=0 → neutral
        result = funding_extreme_signal(0.0005, history)
        assert result["signal"] == "neutral"
        assert result["z_score"] == 0.0

    def test_custom_threshold(self) -> None:
        rng = np.random.default_rng(3)
        history = rng.normal(0.0, 1.0, size=200).tolist()
        # z=1.5 < default 2.0 → neutral
        # 但当 z_threshold=1.0 时应触发
        result_default = funding_extreme_signal(1.5, history)
        result_strict = funding_extreme_signal(1.5, history, z_threshold=1.0)
        assert result_default["signal"] == "neutral"
        assert result_strict["signal"] == "contrarian_short"

    def test_invalid_threshold_raises(self) -> None:
        with pytest.raises(ValueError):
            funding_extreme_signal(0.0001, [0.0001] * 50, z_threshold=0.0)

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            funding_extreme_signal(0.0001, [0.0001] * 20)


# --- oi_momentum -------------------------------------------------------------


class TestOIMomentum:
    def test_up(self) -> None:
        result = oi_momentum(11000.0, 10000.0)
        assert result["direction"] == "up"
        assert math.isclose(result["oi_change_pct"], 10.0)

    def test_down(self) -> None:
        result = oi_momentum(9000.0, 10000.0)
        assert result["direction"] == "down"
        assert math.isclose(result["oi_change_pct"], -10.0)

    def test_flat(self) -> None:
        result = oi_momentum(10050.0, 10000.0)
        # +0.5% < 1% threshold → flat
        assert result["direction"] == "flat"

    def test_zero_prior_raises(self) -> None:
        with pytest.raises(ValueError):
            oi_momentum(10000.0, 0.0)

    def test_negative_current_raises(self) -> None:
        with pytest.raises(ValueError):
            oi_momentum(-1.0, 10000.0)
