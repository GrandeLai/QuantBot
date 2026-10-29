"""Unit tests for quantbot.stocks.risk.sharpe_decay."""
from __future__ import annotations

import math

import numpy as np
import pytest

from quantbot.stocks.risk import (
    analyze_strategy_decay,
    decay_alert_level,
    rolling_sharpe,
    sharpe_z_score,
)


# --- rolling_sharpe ----------------------------------------------------------


class TestRollingSharpe:
    def test_output_length_matches_input(self) -> None:
        rng = np.random.default_rng(0)
        returns = rng.normal(0.001, 0.01, size=200)
        out = rolling_sharpe(returns, window=63)
        assert out.shape == (200,)

    def test_first_window_minus_one_are_nan(self) -> None:
        rng = np.random.default_rng(1)
        returns = rng.normal(0.001, 0.01, size=200)
        out = rolling_sharpe(returns, window=63)
        assert np.all(np.isnan(out[:62]))
        assert not np.any(np.isnan(out[62:]))

    def test_positive_drift_produces_positive_sharpe(self) -> None:
        rng = np.random.default_rng(2)
        # 强 drift：日均 0.5%，日波动 1% → 任一 63-day 窗口的 Sharpe 都应远 > 0
        returns = rng.normal(0.005, 0.01, size=300)
        out = rolling_sharpe(returns, window=63)
        valid = out[~np.isnan(out)]
        # 95% 以上的滚动窗口 Sharpe 应为正
        assert float(np.mean(valid > 0)) > 0.95

    def test_zero_variance_window_returns_zero(self) -> None:
        constant = np.full(100, 0.001, dtype=np.float64)
        out = rolling_sharpe(constant, window=63)
        # 后半段应是 0（不是 NaN，不是 inf）
        assert math.isclose(float(out[-1]), 0.0)

    def test_window_too_small_raises(self) -> None:
        with pytest.raises(ValueError):
            rolling_sharpe(np.ones(50), window=5)

    def test_returns_too_short_raises(self) -> None:
        with pytest.raises(ValueError):
            rolling_sharpe(np.ones(20), window=63)


# --- sharpe_z_score ----------------------------------------------------------


class TestSharpeZScore:
    def test_above_mean_positive_z(self) -> None:
        baseline = np.array([0.5, 0.6, 0.7, 0.4, 0.5, 0.55, 0.6, 0.5, 0.65, 0.5])
        z = sharpe_z_score(1.5, baseline)
        assert z > 5.0

    def test_below_mean_negative_z(self) -> None:
        baseline = np.array([0.5, 0.6, 0.7, 0.4, 0.5, 0.55, 0.6, 0.5, 0.65, 0.5])
        z = sharpe_z_score(-1.0, baseline)
        assert z < -5.0

    def test_baseline_with_nans_filtered(self) -> None:
        baseline = np.concatenate([
            np.full(50, np.nan),
            np.array([0.5, 0.6, 0.7, 0.4, 0.5, 0.55, 0.6, 0.5, 0.65, 0.5]),
        ])
        z = sharpe_z_score(0.5, baseline)
        # 不应崩溃，应得到接近 0 的 z（0.5 接近 baseline 均值）
        assert -2.0 < z < 2.0

    def test_baseline_too_short_raises(self) -> None:
        with pytest.raises(ValueError):
            sharpe_z_score(0.5, np.array([0.5, 0.6, 0.7]))

    def test_zero_std_baseline_returns_zero(self) -> None:
        baseline = np.full(20, 0.5)
        assert sharpe_z_score(0.5, baseline) == 0.0


# --- decay_alert_level -------------------------------------------------------


class TestDecayAlertLevel:
    def test_green_zone(self) -> None:
        assert decay_alert_level(0.5) == "green"
        assert decay_alert_level(-0.5) == "green"
        assert decay_alert_level(-1.0) == "green"  # boundary

    def test_yellow_zone(self) -> None:
        assert decay_alert_level(-1.5) == "yellow"
        assert decay_alert_level(-2.0) == "yellow"  # boundary

    def test_red_zone(self) -> None:
        assert decay_alert_level(-2.5) == "red"
        assert decay_alert_level(-10.0) == "red"


# --- analyze_strategy_decay --------------------------------------------------


class TestAnalyzeStrategyDecay:
    def test_full_dict_keys(self) -> None:
        rng = np.random.default_rng(42)
        returns = rng.normal(0.001, 0.01, size=400)
        rec = analyze_strategy_decay(returns, recent_window=63, baseline_window=252)
        assert set(rec.keys()) == {
            "recent_sharpe",
            "baseline_mean",
            "baseline_std",
            "z_score",
            "alert_level",
            "n_baseline_samples",
        }
        assert rec["alert_level"] in {"green", "yellow", "red"}
        assert isinstance(rec["recent_sharpe"], float)
        assert isinstance(rec["n_baseline_samples"], int)

    def test_decaying_strategy_triggers_yellow_or_red(self) -> None:
        rng = np.random.default_rng(7)
        # 前 350 个样本：高 drift；后 63 个样本：drift 翻负
        good = rng.normal(0.0015, 0.01, size=350)
        bad = rng.normal(-0.002, 0.01, size=63)
        returns = np.concatenate([good, bad])
        # 注意：min_required = 63 + 252 + 10 = 325
        rec = analyze_strategy_decay(returns, recent_window=63, baseline_window=252)
        assert rec["alert_level"] in {"yellow", "red"}
        assert isinstance(rec["z_score"], float)
        assert rec["z_score"] < -1.0

    def test_stable_strategy_stays_green(self) -> None:
        rng = np.random.default_rng(100)
        returns = rng.normal(0.001, 0.01, size=500)
        rec = analyze_strategy_decay(returns)
        assert rec["alert_level"] == "green"

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            analyze_strategy_decay(np.ones(100), recent_window=63, baseline_window=252)
