"""Regime-aware multi-factor scoring and watchlist construction."""

from typing import Dict, Iterable, Mapping, Set

from .regime import Regime


def default_regime_weight_config() -> Dict[Regime, Dict[str, float]]:
    return {
        Regime.R1_BULL_LOW_VOL: {
            "momentum": 0.35,
            "quality": 0.25,
            "small_cap": 0.15,
            "low_volatility": -0.10,
            "reversal": -0.10,
        },
        Regime.R2_BULL_HIGH_VOL: {
            "momentum": 0.30,
            "growth": 0.25,
            "quality": 0.15,
            "value": -0.10,
            "high_beta": -0.15,
        },
        Regime.R3_SIDEWAYS: {
            "value": 0.30,
            "reversal": 0.25,
            "quality": 0.20,
            "momentum": -0.15,
        },
        Regime.R4_BEAR: {
            "low_volatility": 0.30,
            "quality": 0.25,
            "short_interest_pressure": 0.20,
            "momentum": -0.25,
            "high_beta": -0.15,
        },
        Regime.R5_CRISIS: {
            "defensive_cash": 1.0,
            "momentum": -0.30,
            "high_beta": -0.30,
        },
    }


class TradingAgent:
    def __init__(
        self,
        candidate_factors: Iterable[str],
        weight_config: Mapping[Regime, Mapping[str, float]],
    ):
        self.candidate_factors = tuple(candidate_factors)
        self.weight_config = {
            regime: dict(weights)
            for regime, weights in weight_config.items()
        }

    def compute_composite_scores(
        self,
        factor_matrix: Mapping[str, Mapping[str, float]],
        regime: Regime,
    ) -> Dict[str, float]:
        weights = self.weight_config.get(regime, {})
        raw_scores: Dict[str, float] = {}
        for ticker, factors in factor_matrix.items():
            raw_scores[ticker] = sum(
                float(factors.get(factor, 0.0)) * float(weights.get(factor, 0.0))
                for factor in self.candidate_factors
            )
        return _zscore(raw_scores)

    def update_watchlist(
        self,
        scores: Mapping[str, float],
        top_n: int = 30,
        min_adv: float = 5_000_000,
        average_dollar_volume: Mapping[str, float] = None,
        earnings_window: Set[str] = None,
        exclude_earnings_week: bool = True,
    ):
        average_dollar_volume = average_dollar_volume or {}
        earnings_window = earnings_window or set()
        candidates = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[: top_n * 2]

        selected = []
        for ticker, _score in candidates:
            if average_dollar_volume.get(ticker, 0.0) < min_adv:
                continue
            if exclude_earnings_week and ticker in earnings_window:
                continue
            selected.append(ticker)
            if len(selected) == top_n:
                break
        return selected


def _zscore(scores: Mapping[str, float]) -> Dict[str, float]:
    if not scores:
        return {}
    values = list(scores.values())
    mean = sum(values) / len(values)
    std = (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5
    if std == 0:
        return {ticker: 0.0 for ticker in scores}
    return {ticker: (value - mean) / std for ticker, value in scores.items()}
