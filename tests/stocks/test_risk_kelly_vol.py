"""Unit tests for quantbot.stocks.risk (Kelly + Vol Target)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from quantbot.stocks.risk import (
    capped_kelly,
    fractional_kelly,
    kelly_fraction_binary,
    kelly_fraction_from_returns,
    realized_volatility,
    regime_classify,
    vol_target_position_size,
    vol_target_recommendation,
)


# --- kelly_fraction_binary ---------------------------------------------------


class TestKellyBinary:
    def test_classic_50_50_payoff_2_gives_quarter(self) -> None:
        # p=0.5, b=2 → f* = 0.5 - 0.5/2 = 0.25
        assert math.isclose(kelly_fraction_binary(0.5, 2.0), 0.25)

    def test_high_edge_60_pct_even_money(self) -> None:
        # p=0.6, b=1 → f* = 0.6 - 0.4 = 0.20
        assert math.isclose(kelly_fraction_binary(0.6, 1.0), 0.20)

    def test_negative_ev_clipped_to_zero(self) -> None:
        # p=0.4, b=1 → f* = 0.4 - 0.6 = -0.2 → 0
        assert kelly_fraction_binary(0.4, 1.0) == 0.0

    def test_invalid_win_rate_raises(self) -> None:
        with pytest.raises(ValueError):
            kelly_fraction_binary(1.5, 2.0)
        with pytest.raises(ValueError):
            kelly_fraction_binary(-0.1, 2.0)

    def test_invalid_payoff_raises(self) -> None:
        with pytest.raises(ValueError):
            kelly_fraction_binary(0.5, 0.0)
        with pytest.raises(ValueError):
            kelly_fraction_binary(0.5, -1.0)


# --- kelly_fraction_from_returns ---------------------------------------------


class TestKellyFromReturns:
    def test_positive_drift_gives_positive_fraction(self) -> None:
        rng = np.random.default_rng(42)
        # 日化均值 0.001、波动 0.01：理想 Kelly = 0.001 / 0.0001 = 10（被原始公式不截顶）
        returns = rng.normal(0.001, 0.01, size=500)
        f = kelly_fraction_from_returns(returns)
        assert f > 0.0

    def test_zero_variance_returns_zero(self) -> None:
        constant = np.full(20, 0.001, dtype=np.float64)
        assert kelly_fraction_from_returns(constant) == 0.0

    def test_negative_drift_clipped(self) -> None:
        rng = np.random.default_rng(7)
        returns = rng.normal(-0.002, 0.01, size=200)
        assert kelly_fraction_from_returns(returns) == 0.0

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            kelly_fraction_from_returns(np.array([0.01, 0.02, -0.01]))


# --- fractional_kelly --------------------------------------------------------


class TestFractionalKelly:
    def test_quarter_of_full(self) -> None:
        assert math.isclose(fractional_kelly(0.40, 0.25), 0.10)

    def test_default_is_quarter(self) -> None:
        assert math.isclose(fractional_kelly(0.40), 0.10)

    def test_negative_full_kelly_floored_to_zero(self) -> None:
        assert fractional_kelly(-0.5, 0.25) == 0.0

    def test_invalid_fraction_raises(self) -> None:
        with pytest.raises(ValueError):
            fractional_kelly(0.5, 0.0)
        with pytest.raises(ValueError):
            fractional_kelly(0.5, 1.5)


# --- capped_kelly ------------------------------------------------------------


class TestCappedKelly:
    def test_below_cap_passes_through(self) -> None:
        assert math.isclose(capped_kelly(0.10, 0.25), 0.10)

    def test_above_cap_clipped(self) -> None:
        assert math.isclose(capped_kelly(0.50, 0.25), 0.25)

    def test_negative_floored(self) -> None:
        assert capped_kelly(-0.10) == 0.0

    def test_invalid_cap_raises(self) -> None:
        with pytest.raises(ValueError):
            capped_kelly(0.10, 0.0)
        with pytest.raises(ValueError):
            capped_kelly(0.10, 1.5)


# --- realized_volatility -----------------------------------------------------


class TestRealizedVolatility:
    def test_annualize_daily(self) -> None:
        # 日波动 1% → 年化约 15.87%
        rng = np.random.default_rng(42)
        returns = rng.normal(0.0, 0.01, size=2520)
        sigma = realized_volatility(returns, annualize=True)
        assert 0.13 < sigma < 0.18

    def test_no_annualize_returns_raw_std(self) -> None:
        returns = np.array([0.01, -0.01, 0.02, -0.02, 0.0])
        raw = realized_volatility(returns, annualize=False)
        assert math.isclose(raw, float(np.std(returns, ddof=1)))

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            realized_volatility(np.array([0.01]))

    def test_weekly_annualization(self) -> None:
        rng = np.random.default_rng(11)
        returns = rng.normal(0.0, 0.02, size=200)
        annual = realized_volatility(returns, annualize=True, periods_per_year=52)
        raw = realized_volatility(returns, annualize=False)
        assert math.isclose(annual, raw * math.sqrt(52))


# --- vol_target_position_size ------------------------------------------------


class TestVolTargetPositionSize:
    def test_realized_above_target_scales_down(self) -> None:
        # realized 30%、target 15% → 0.5
        assert math.isclose(vol_target_position_size(0.30, 0.15), 0.5)

    def test_realized_below_target_capped_at_one(self) -> None:
        # 不放大杠杆 — realized 5% / target 15% → 1.0
        assert vol_target_position_size(0.05, 0.15) == 1.0

    def test_invalid_realized_raises(self) -> None:
        with pytest.raises(ValueError):
            vol_target_position_size(0.0, 0.15)
        with pytest.raises(ValueError):
            vol_target_position_size(-0.1, 0.15)

    def test_invalid_target_raises(self) -> None:
        with pytest.raises(ValueError):
            vol_target_position_size(0.20, 0.0)


# --- regime_classify ---------------------------------------------------------


class TestRegimeClassify:
    def test_low_regime(self) -> None:
        assert regime_classify(0.05) == "low"

    def test_normal_regime(self) -> None:
        assert regime_classify(0.18) == "normal"

    def test_high_regime(self) -> None:
        assert regime_classify(0.35) == "high"

    def test_crisis_regime(self) -> None:
        assert regime_classify(0.60) == "crisis"

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError):
            regime_classify(-0.01)

    def test_inverted_thresholds_raise(self) -> None:
        with pytest.raises(ValueError):
            regime_classify(0.10, low_threshold=0.30, high_threshold=0.10)


# --- vol_target_recommendation ----------------------------------------------


class TestVolTargetRecommendation:
    def test_returns_full_dict(self) -> None:
        rng = np.random.default_rng(2)
        returns = rng.normal(0.0, 0.012, size=252)
        rec = vol_target_recommendation(returns, target_vol=0.15)
        assert set(rec.keys()) == {"realized_vol", "target_vol", "scale_factor", "regime"}
        assert rec["target_vol"] == 0.15
        assert isinstance(rec["realized_vol"], float)
        assert isinstance(rec["scale_factor"], float)
        assert rec["regime"] in {"low", "normal", "high", "crisis"}

    def test_high_vol_period_classified_high(self) -> None:
        rng = np.random.default_rng(99)
        # 日波动 2.5% → 年化 ~40%
        returns = rng.normal(0.0, 0.025, size=252)
        rec = vol_target_recommendation(returns)
        assert rec["regime"] in {"high", "crisis"}
        assert isinstance(rec["scale_factor"], float)
        assert rec["scale_factor"] < 1.0  # 减仓

    def test_low_vol_period_classified_low_no_leverage(self) -> None:
        rng = np.random.default_rng(123)
        # 日波动 0.3% → 年化 ~4.8%
        returns = rng.normal(0.0, 0.003, size=252)
        rec = vol_target_recommendation(returns)
        assert rec["regime"] == "low"
        assert rec["scale_factor"] == 1.0  # 不加杠杆
