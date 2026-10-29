"""Kronos time-series feature extraction fallback.

External Chronos/TimesFM models can be attached behind this boundary later.
The local implementation converts recent return context into the three
distribution features specified by the design: mean, volatility, and skew.
"""

from typing import Dict, Iterable, List


class KronosFeatureExtractor:
    def __init__(self, context_length: int = 60):
        if context_length <= 0:
            raise ValueError("context_length must be positive")
        self.context_length = context_length

    def extract(self, returns: Iterable[float]) -> Dict[str, float]:
        context = [float(value) for value in returns][-self.context_length :]
        if not context:
            return {"kronos_mean": 0.0, "kronos_vol": 0.0, "kronos_skew": 0.0}

        mean = sum(context) / len(context)
        volatility = _stddev(context, mean)
        skew = _skew(context, mean, volatility)
        return {
            "kronos_mean": mean,
            "kronos_vol": volatility,
            "kronos_skew": skew,
        }


def _stddev(values: List[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5


def _skew(values: List[float], mean: float, volatility: float) -> float:
    if len(values) < 3 or volatility == 0:
        return 0.0
    third_moment = sum((value - mean) ** 3 for value in values) / len(values)
    return third_moment / (volatility ** 3)
