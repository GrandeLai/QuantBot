"""Market Regime Switching primitives."""

from dataclasses import dataclass
from enum import Enum


class Regime(str, Enum):
    R1_BULL_LOW_VOL = "R1_BULL_LOW_VOL"
    R2_BULL_HIGH_VOL = "R2_BULL_HIGH_VOL"
    R3_SIDEWAYS = "R3_SIDEWAYS"
    R4_BEAR = "R4_BEAR"
    R5_CRISIS = "R5_CRISIS"


@dataclass(frozen=True)
class RegimeSnapshot:
    vix: float
    spy_1m: float
    yield_curve: float


@dataclass(frozen=True)
class RegimePrediction:
    regime: Regime
    confidence: float = 1.0
    source: str = "rule"


class RuleBasedRegimeDetector:
    """Explicit validation rules that mirror the design document."""

    def predict(self, snapshot: RegimeSnapshot) -> RegimePrediction:
        if snapshot.vix > 35:
            regime = Regime.R5_CRISIS
        elif snapshot.vix > 25 and snapshot.spy_1m < -0.05:
            regime = Regime.R4_BEAR
        elif snapshot.vix < 15 and snapshot.spy_1m > 0.02:
            regime = Regime.R1_BULL_LOW_VOL
        elif 15 <= snapshot.vix <= 25 and snapshot.spy_1m > 0:
            regime = Regime.R2_BULL_HIGH_VOL
        else:
            regime = Regime.R3_SIDEWAYS
        return RegimePrediction(regime=regime, confidence=1.0, source="rule")


class StableRegimeDetector:
    """Hold the previous regime when model confidence is below threshold."""

    def __init__(self, min_confidence: float = 0.60, previous_regime=None):
        self.min_confidence = min_confidence
        self.previous_regime = previous_regime

    def stabilize(self, prediction: RegimePrediction) -> RegimePrediction:
        if (
            prediction.confidence < self.min_confidence
            and self.previous_regime is not None
        ):
            return RegimePrediction(
                regime=self.previous_regime,
                confidence=prediction.confidence,
                source="%s:held_previous" % prediction.source,
            )
        self.previous_regime = prediction.regime
        return prediction
