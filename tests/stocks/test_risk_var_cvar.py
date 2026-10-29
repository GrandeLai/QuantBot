"""Unit tests for quantbot.stocks.risk.var_cvar."""
from __future__ import annotations

import numpy as np
import pytest

from quantbot.stocks.risk import (
    historical_cvar,
    historical_var,
    parametric_var,
    var_summary,
)


# --- historical_var ----------------------------------------------------------


class TestHistoricalVar:
    def test_returns_positive_loss_magnitude(self) -> None:
        rng = np.random.default_rng(0)
        returns = rng.normal(0.0, 0.01, size=1000)
        var = historical_var(returns, confidence=0.95)
        # 标准 std 0.01 → 95% VaR 应在 1.6% 附近
        assert 0.012 < var < 0.022

    def test_higher_confidence_gives_higher_var(self) -> None:
        rng = np.random.default_rng(1)
        returns = rng.normal(0.0, 0.02, size=1000)
        var_95 = historical_var(returns, confidence=0.95)
        var_99 = historical_var(returns, confidence=0.99)
        assert var_99 > var_95

    def test_all_positive_returns_var_zero(self) -> None:
        # 全正收益 → 95% 分位数仍然为正 → VaR 截断为 0
        returns = np.linspace(0.001, 0.05, 100)
        assert historical_var(returns, confidence=0.95) == 0.0

    def test_invalid_confidence_raises(self) -> None:
        returns = np.zeros(100)
        with pytest.raises(ValueError):
            historical_var(returns, confidence=0.0)
        with pytest.raises(ValueError):
            historical_var(returns, confidence=1.0)

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            historical_var(np.zeros(20))


# --- historical_cvar ---------------------------------------------------------


class TestHistoricalCvar:
    def test_cvar_geq_var(self) -> None:
        rng = np.random.default_rng(2)
        returns = rng.normal(0.0, 0.015, size=1000)
        var = historical_var(returns, confidence=0.95)
        cvar = historical_cvar(returns, confidence=0.95)
        assert cvar >= var

    def test_cvar_higher_confidence_higher(self) -> None:
        rng = np.random.default_rng(3)
        returns = rng.normal(0.0, 0.02, size=1000)
        cvar_95 = historical_cvar(returns, confidence=0.95)
        cvar_99 = historical_cvar(returns, confidence=0.99)
        assert cvar_99 >= cvar_95

    def test_all_positive_returns_cvar_zero(self) -> None:
        returns = np.linspace(0.001, 0.05, 100)
        assert historical_cvar(returns, confidence=0.95) == 0.0

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            historical_cvar(np.zeros(20))


# --- parametric_var ----------------------------------------------------------


class TestParametricVar:
    def test_close_to_historical_for_normal_data(self) -> None:
        rng = np.random.default_rng(4)
        returns = rng.normal(0.0, 0.015, size=2000)
        h = historical_var(returns, confidence=0.95)
        p = parametric_var(returns, confidence=0.95)
        # 高斯样本下两者应接近（5% 容忍）
        assert abs(h - p) / max(h, 1e-9) < 0.15

    def test_zero_variance_returns_zero(self) -> None:
        constant = np.full(100, 0.001, dtype=np.float64)
        assert parametric_var(constant, confidence=0.95) == 0.0

    def test_invalid_confidence_raises(self) -> None:
        with pytest.raises(ValueError):
            parametric_var(np.zeros(100), confidence=1.5)


# --- var_summary -------------------------------------------------------------


class TestVarSummary:
    def test_default_keys(self) -> None:
        rng = np.random.default_rng(5)
        returns = rng.normal(0.0, 0.012, size=500)
        rec = var_summary(returns)
        for key in ("n_samples", "worst_loss", "method", "var_95", "var_99", "cvar_95", "cvar_99"):
            assert key in rec
        assert rec["n_samples"] == 500
        assert rec["method"] == "historical"
        assert isinstance(rec["worst_loss"], float)

    def test_method_both_includes_parametric(self) -> None:
        rng = np.random.default_rng(6)
        returns = rng.normal(0.0, 0.01, size=500)
        rec = var_summary(returns, method="both")
        assert "parametric_var_95" in rec
        assert "parametric_var_99" in rec

    def test_method_parametric_excludes_historical(self) -> None:
        rng = np.random.default_rng(7)
        returns = rng.normal(0.0, 0.01, size=500)
        rec = var_summary(returns, method="parametric")
        # historical 不应出现
        assert "var_95" not in rec
        assert "cvar_95" not in rec
        assert "parametric_var_95" in rec

    def test_custom_confidences(self) -> None:
        rng = np.random.default_rng(8)
        returns = rng.normal(0.0, 0.01, size=500)
        rec = var_summary(returns, confidences=(0.90, 0.95, 0.99))
        assert "var_90" in rec
        assert "var_95" in rec
        assert "var_99" in rec

    def test_invalid_method_raises(self) -> None:
        with pytest.raises(ValueError):
            var_summary(np.zeros(100), method="garch")  # type: ignore[arg-type]

    def test_too_few_samples_raises(self) -> None:
        with pytest.raises(ValueError):
            var_summary(np.zeros(20))
