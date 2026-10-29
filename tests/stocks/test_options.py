"""期权 Greeks 测试 — T-3.3 验收."""
from __future__ import annotations

import math

import pytest

from quantbot.stocks.options.greeks import BlackScholes, GreeksResult


class TestBlackScholes:
    """验收：Black-Scholes Greeks 计算正确性."""

    @pytest.fixture
    def atm_call(self) -> BlackScholes:
        """平值看涨期权: S=K=100, T=1yr, r=5%, σ=20%."""
        return BlackScholes(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20, option_type="call")

    @pytest.fixture
    def atm_put(self) -> BlackScholes:
        return BlackScholes(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20, option_type="put")

    def test_call_price_positive(self, atm_call: BlackScholes) -> None:
        result = atm_call.compute()
        assert result.price > 0

    def test_put_call_parity(self, atm_call: BlackScholes, atm_put: BlackScholes) -> None:
        """C - P = S - K*exp(-rT)."""
        c = atm_call.compute()
        p = atm_put.compute()
        K, r, T, S = 100.0, 0.05, 1.0, 100.0
        parity = S - K * math.exp(-r * T)
        assert abs((c.price - p.price) - parity) < 0.01

    def test_call_delta_between_0_and_1(self, atm_call: BlackScholes) -> None:
        result = atm_call.compute()
        assert 0.0 < result.delta < 1.0

    def test_put_delta_between_minus1_and_0(self, atm_put: BlackScholes) -> None:
        result = atm_put.compute()
        assert -1.0 < result.delta < 0.0

    def test_gamma_positive(self, atm_call: BlackScholes) -> None:
        result = atm_call.compute()
        assert result.gamma > 0

    def test_vega_positive(self, atm_call: BlackScholes) -> None:
        result = atm_call.compute()
        assert result.vega > 0

    def test_atm_delta_near_half(self, atm_call: BlackScholes) -> None:
        """ATM call delta ≈ 0.5 (approximately, before drift)."""
        result = atm_call.compute()
        assert 0.45 < result.delta < 0.65

    def test_implied_vol(self) -> None:
        """Round-trip: compute price from σ=0.2, then recover σ."""
        bs = BlackScholes(S=100.0, K=100.0, T=1.0, r=0.05, sigma=0.20, option_type="call")
        price = bs.compute().price
        iv = BlackScholes.implied_vol(price, S=100.0, K=100.0, T=1.0, r=0.05, option_type="call")
        assert abs(iv - 0.20) < 0.001


class TestGreeksResult:
    def test_has_all_fields(self) -> None:
        r = GreeksResult(price=10.0, delta=0.6, gamma=0.02, theta=-0.05, vega=0.3, rho=0.4)
        assert r.price == 10.0
        assert r.delta == 0.6
