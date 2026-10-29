"""Risk controls and target weight construction."""

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping

from .regime import Regime


@dataclass(frozen=True)
class RiskConfig:
    max_portfolio_vol_annual: float = 0.18
    max_single_stock_weight: float = 0.05
    max_sector_weight: float = 0.25
    max_factor_beta: float = 0.3
    max_drawdown_halt: float = 0.15
    stop_loss_per_stock: float = 0.08


@dataclass(frozen=True)
class DrawdownAction:
    manual_review: bool
    halt_trading: bool
    position_scale: float
    reason: str


POSITION_SCALE = {
    Regime.R1_BULL_LOW_VOL: 1.0,
    Regime.R2_BULL_HIGH_VOL: 0.80,
    Regime.R3_SIDEWAYS: 0.65,
    Regime.R4_BEAR: 0.30,
    Regime.R5_CRISIS: 0.05,
}


class RiskManager:
    def __init__(self, config: RiskConfig = None):
        self.config = config or RiskConfig()

    def build_target_weights(
        self,
        watchlist: Iterable[str],
        sectors: Mapping[str, str],
        regime: Regime,
    ) -> Dict[str, float]:
        tickers = list(watchlist)
        if not tickers:
            return {}

        target_exposure = POSITION_SCALE[regime]
        equal_weight = target_exposure / len(tickers)
        capped_weight = min(equal_weight, self.config.max_single_stock_weight)
        weights = {ticker: capped_weight for ticker in tickers}

        self._enforce_sector_caps(weights, sectors)
        return weights

    def should_halt_for_drawdown(self, drawdown: float) -> bool:
        return drawdown >= self.config.max_drawdown_halt

    def drawdown_action(
        self,
        drawdown: float,
        halt_multiplier: float = 1.5,
    ) -> DrawdownAction:
        if drawdown >= self.config.max_drawdown_halt * halt_multiplier:
            return DrawdownAction(
                manual_review=True,
                halt_trading=True,
                position_scale=0.0,
                reason="drawdown_halt",
            )
        if drawdown >= self.config.max_drawdown_halt:
            return DrawdownAction(
                manual_review=True,
                halt_trading=False,
                position_scale=0.5,
                reason="drawdown_review",
            )
        return DrawdownAction(
            manual_review=False,
            halt_trading=False,
            position_scale=1.0,
            reason="within_limit",
        )

    def scale_for_factor_beta(
        self,
        weights: Mapping[str, float],
        factor_betas: Mapping[str, float],
    ) -> Dict[str, float]:
        exposure = sum(
            float(weight) * float(factor_betas.get(ticker, 0.0))
            for ticker, weight in weights.items()
        )
        if abs(exposure) <= self.config.max_factor_beta or exposure == 0:
            return dict(weights)
        scale = self.config.max_factor_beta / abs(exposure)
        return {
            ticker: float(weight) * scale
            for ticker, weight in weights.items()
        }

    def scale_for_portfolio_volatility(
        self,
        weights: Mapping[str, float],
        annual_volatility: float,
    ) -> Dict[str, float]:
        if annual_volatility <= 0 or annual_volatility <= self.config.max_portfolio_vol_annual:
            return dict(weights)
        scale = self.config.max_portfolio_vol_annual / float(annual_volatility)
        return {
            ticker: float(weight) * scale
            for ticker, weight in weights.items()
        }

    def scale_for_regime_confidence(
        self,
        weights: Mapping[str, float],
        prediction,
        min_confidence: float = 0.60,
        degraded_scale: float = 0.50,
    ) -> Dict[str, float]:
        source = str(getattr(prediction, "source", ""))
        confidence = float(getattr(prediction, "confidence", 1.0))
        if confidence >= min_confidence and "rule_conflict" not in source:
            return dict(weights)
        return {
            ticker: float(weight) * degraded_scale
            for ticker, weight in weights.items()
        }

    def _enforce_sector_caps(self, weights: Dict[str, float], sectors: Mapping[str, str]) -> None:
        sector_to_tickers: Dict[str, list] = {}
        for ticker in weights:
            sector_to_tickers.setdefault(sectors.get(ticker, "Unknown"), []).append(ticker)

        for tickers in sector_to_tickers.values():
            sector_weight = sum(weights[ticker] for ticker in tickers)
            if sector_weight <= self.config.max_sector_weight:
                continue
            scale = self.config.max_sector_weight / sector_weight
            for ticker in tickers:
                weights[ticker] *= scale
