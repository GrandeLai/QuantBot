from datetime import UTC, datetime, timedelta

import pytest

from quantbot.intraday.research import select_signal


T = datetime(2025, 1, 2, 1, tzinfo=UTC)


def signal(**changes):
    return dict(base=False, retest=False, relative_volume=2.0,
                opening_high=100.0, opening_low=99.0, close=100.2, **changes)


def test_cost_room_uses_same_stress_cost_boundary_in_every_scenario():
    value = signal()
    value["base"] = True
    assert select_signal("volume_cost_room", value, "SPY", T, {}, 70)
    for changes in ({"opening_low": 99.5}, {"close": 100.51}, {"relative_volume": 1.49}):
        assert not select_signal("volume_cost_room", value | changes, "SPY", T, {}, 70)
    # Structural half-range edge; not estimated from profits or changed for cheaper scenarios.
    assert select_signal("volume_cost_room", value | {"close": 100.5}, "SPY", T, {}, 70)


def test_persistence_waits_for_next_completed_bar_and_consumes_failure():
    pending = {}
    assert not select_signal("retest_persistence", signal() | {"base": True}, "SPY", T, pending, 70)
    retest_at = T + timedelta(minutes=15)
    assert not select_signal("retest_persistence", signal() | {"retest": True}, "SPY", retest_at, pending, 70)
    assert not select_signal("retest_persistence", signal() | {"close": 100.1}, "SPY",
                             T + timedelta(minutes=30), pending, 70)
    # A later recovery cannot replace the failed next-bar confirmation.
    assert not select_signal("retest_persistence", signal() | {"retest": True, "close": 101}, "SPY",
                             T + timedelta(minutes=45), pending, 70)


def test_persistence_can_confirm_without_touching_breakout_again():
    pending = {}
    select_signal("retest_persistence", signal() | {"base": True}, "SPY", T, pending, 70)
    select_signal("retest_persistence", signal() | {"retest": True}, "SPY", T + timedelta(minutes=30), pending, 70)
    assert select_signal("retest_persistence", signal(), "SPY", T + timedelta(minutes=45), pending, 70)
    assert "SPY" not in pending


def test_unknown_factor_cannot_silently_turn_into_volume_filter():
    with pytest.raises(ValueError, match="未知"):
        select_signal("mistyped", signal(), "SPY", T, {}, 70)
