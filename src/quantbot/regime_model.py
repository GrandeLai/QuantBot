"""Adapters for probabilistic Regime models such as HMMs."""

from dataclasses import dataclass
from datetime import date
from typing import Iterable, List, Sequence

from .regime import Regime, RegimePrediction


class HMMRegimeAdapter:
    def __init__(self, model, scaler, regime_order: Sequence[Regime]):
        self.model = model
        self.scaler = scaler
        self.regime_order = list(regime_order)

    def predict(self, feature_rows: Iterable[dict]) -> RegimePrediction:
        rows = list(feature_rows)
        scaled = self.scaler.transform(rows)
        probabilities = self.model.predict_proba(scaled)
        latest = list(probabilities[-1])
        best_index = max(range(len(latest)), key=lambda index: latest[index])
        return RegimePrediction(
            regime=self.regime_order[best_index],
            confidence=float(latest[best_index]),
            source="hmm",
        )


class RegimeConsensusValidator:
    def __init__(self, confidence_boost: float = 0.10, conflict_penalty: float = 0.50):
        self.confidence_boost = confidence_boost
        self.conflict_penalty = conflict_penalty

    def combine(self, model_prediction, rule_prediction) -> RegimePrediction:
        if model_prediction.regime == rule_prediction.regime:
            return RegimePrediction(
                regime=model_prediction.regime,
                confidence=min(1.0, float(model_prediction.confidence) + self.confidence_boost),
                source="%s+%s" % (model_prediction.source, rule_prediction.source),
            )
        return RegimePrediction(
            regime=model_prediction.regime,
            confidence=max(0.0, float(model_prediction.confidence) * self.conflict_penalty),
            source="%s:rule_conflict" % model_prediction.source,
        )


@dataclass(frozen=True)
class RegimeAccuracyReport:
    passed: bool
    accuracy: float
    observations: int


@dataclass(frozen=True)
class HMMOOSWindowReport:
    passed: bool
    reasons: List[str]


def evaluate_regime_accuracy(
    predicted: Sequence[Regime],
    actual: Sequence[Regime],
    min_accuracy: float = 0.70,
) -> RegimeAccuracyReport:
    observations = min(len(predicted), len(actual))
    if observations == 0:
        return RegimeAccuracyReport(False, 0.0, 0)
    hits = sum(
        1
        for predicted_regime, actual_regime in zip(predicted[:observations], actual[:observations])
        if predicted_regime == actual_regime
    )
    accuracy = hits / observations
    return RegimeAccuracyReport(
        passed=accuracy >= min_accuracy,
        accuracy=accuracy,
        observations=observations,
    )


def validate_hmm_oos_window(
    train_start: date,
    train_end: date,
    prediction_start: date,
    min_train_start: date = date(2010, 1, 1),
    max_train_end: date = date(2018, 12, 31),
    min_prediction_start: date = date(2019, 1, 1),
) -> HMMOOSWindowReport:
    reasons = []
    if train_start > min_train_start:
        reasons.append("insufficient_training_history")
    if train_end > max_train_end:
        reasons.append("train_window_after_2018")
    if prediction_start < min_prediction_start:
        reasons.append("prediction_before_2019")
    if train_end >= prediction_start:
        reasons.append("lookahead_overlap")
    return HMMOOSWindowReport(passed=not reasons, reasons=reasons)
