"""价格行为决策领域约束。"""

import pandas as pd
import pytest

from quantbot.price_action.domain.decision import (
    DecisionIntent,
    DecisionSource,
    ExposureEffect,
    evidence_from_feature_row,
)
from quantbot.price_action.strategy.trend_breakout import evaluate_entry_signal


def test_exposure_increase_requires_price_action_evidence():
    with pytest.raises(ValueError, match="价格行为"):
        DecisionIntent(
            symbol="TST",
            intent="entry",
            source=DecisionSource.RISK_OVERRIDE,
            exposure_effect=ExposureEffect.INCREASE,
            reason_code="manual",
        )


def test_risk_override_may_only_reduce_exposure():
    decision = DecisionIntent(
        symbol="TST",
        intent="emergency_exit",
        source=DecisionSource.RISK_OVERRIDE,
        exposure_effect=ExposureEffect.DECREASE,
        reason_code="unknown_order_hold",
    )
    assert decision.source == DecisionSource.RISK_OVERRIDE


def test_evidence_is_point_in_time_and_json_safe():
    row = pd.Series({
        "close": 101.0,
        "atr": 2.5,
        "uptrend": True,
        "sh1": float("nan"),
        "not_allowed": "ignored",
    })
    evidence = evidence_from_feature_row(
        "2026-08-07", row, regime_level=2, bench_momentum=0.12,
        extra={"trigger": "breakout"},
    )
    assert evidence.signal_bar_date == "2026-08-07"
    assert evidence.values["close"] == 101.0
    assert evidence.values["sh1"] is None
    assert evidence.values["trigger"] == "breakout"
    assert "not_allowed" not in evidence.values


def test_entry_evaluation_preserves_rejection_reason():
    row = pd.Series({
        "hh_break": 100.0,
        "atr": 2.0,
        "mom": 0.1,
        "uptrend": False,
    })
    result = evaluate_entry_signal(row, 2, {})
    assert result.signal is None
    assert result.reason_code == "not_uptrend"
