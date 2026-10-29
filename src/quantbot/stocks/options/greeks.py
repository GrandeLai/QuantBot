"""Black-Scholes 期权定价与 Greeks 计算."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from scipy.stats import norm


@dataclass
class GreeksResult:
    """期权 Greeks 计算结果."""

    price: float
    delta: float
    gamma: float
    theta: float    # 每日 theta（除以 365）
    vega: float     # 每 1% 波动率变动对应的价格变动
    rho: float      # 每 1% 利率变动对应的价格变动


class BlackScholes:
    """Black-Scholes 欧式期权定价模型.

    参数:
        S: 标的资产现价
        K: 行权价格
        T: 到期时间（年）
        r: 年化无风险利率（如 0.05 = 5%）
        sigma: 年化波动率（如 0.20 = 20%）
        option_type: 'call' 或 'put'
    """

    def __init__(
        self,
        S: float,
        K: float,
        T: float,
        r: float,
        sigma: float,
        option_type: Literal["call", "put"] = "call",
    ) -> None:
        self.S = S
        self.K = K
        self.T = T
        self.r = r
        self.sigma = sigma
        self.option_type = option_type

    def _d1_d2(self) -> tuple[float, float]:
        d1 = (math.log(self.S / self.K) + (self.r + 0.5 * self.sigma ** 2) * self.T) / (
            self.sigma * math.sqrt(self.T)
        )
        d2 = d1 - self.sigma * math.sqrt(self.T)
        return d1, d2

    def compute(self) -> GreeksResult:
        """计算期权价格和所有 Greeks."""
        S, K, T, r, sigma = self.S, self.K, self.T, self.r, self.sigma
        d1, d2 = self._d1_d2()
        sqrtT = math.sqrt(T)
        Nd1 = norm.cdf(d1)
        Nd2 = norm.cdf(d2)
        nd1 = norm.pdf(d1)

        if self.option_type == "call":
            price = S * Nd1 - K * math.exp(-r * T) * Nd2
            delta = Nd1
            rho = K * T * math.exp(-r * T) * Nd2 / 100.0
            theta = (
                -S * nd1 * sigma / (2 * sqrtT)
                - r * K * math.exp(-r * T) * Nd2
            ) / 365.0
        else:
            Nnd1 = norm.cdf(-d1)
            Nnd2 = norm.cdf(-d2)
            price = K * math.exp(-r * T) * Nnd2 - S * Nnd1
            delta = Nd1 - 1.0
            rho = -K * T * math.exp(-r * T) * Nnd2 / 100.0
            theta = (
                -S * nd1 * sigma / (2 * sqrtT)
                + r * K * math.exp(-r * T) * Nnd2
            ) / 365.0

        gamma = nd1 / (S * sigma * sqrtT)
        vega = S * nd1 * sqrtT / 100.0   # per 1% vol move

        return GreeksResult(
            price=price, delta=delta, gamma=gamma,
            theta=theta, vega=vega, rho=rho,
        )

    @staticmethod
    def implied_vol(
        market_price: float,
        S: float,
        K: float,
        T: float,
        r: float,
        option_type: Literal["call", "put"] = "call",
        tol: float = 1e-6,
        max_iter: int = 100,
    ) -> float:
        """牛顿法求隐含波动率."""
        sigma = 0.20  # initial guess
        for _ in range(max_iter):
            bs = BlackScholes(S=S, K=K, T=T, r=r, sigma=sigma, option_type=option_type)
            result = bs.compute()
            diff = result.price - market_price
            if abs(diff) < tol:
                return sigma
            vega_full = result.vega * 100  # un-scale from /100
            if abs(vega_full) < 1e-10:
                break
            sigma -= diff / vega_full
            sigma = max(0.001, min(sigma, 10.0))  # clamp
        return sigma
