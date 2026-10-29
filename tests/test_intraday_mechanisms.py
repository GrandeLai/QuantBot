import pytest

from quantbot.intraday.strategies import SessionSignals, select_mechanism


def group(opening=100, high=101, low=99, close=100, volume=100):
    return [{"open": str(opening), "high": str(high), "low": str(low), "close": str(close),
             "volume": str(volume)} for _ in range(3)]


def warmed():
    signals = SessionSignals()
    for _ in range(4):
        signals.update(group(), eligible=False)
    return signals


def test_vwap_is_volume_weighted_and_never_includes_later_groups():
    signals = warmed()
    now = signals.update(group(opening=100, high=101, low=100, close=100.5, volume=200), eligible=True)
    # 12 old bars at typical 100; 3 new bars at typical 100.5 with double volume.
    assert now["session_vwap"] == pytest.approx((1200 * 100 + 600 * 100.5) / 1800)
    assert now["vwap_reclaim"]
    assert select_mechanism("vwap_trend_reclaim", now, 70)
    saved = now.copy()
    signals.update(group(opening=200, high=201, low=199, close=200), eligible=True)
    assert now == saved


def test_first_vwap_reclaim_is_consumed_even_if_confirmation_fails():
    signals = warmed()
    # Reclaims VWAP on a red bar: valid control event, rejected trend candidate.
    first = signals.update(group(opening=101, high=101, low=99, close=100.5), eligible=True)
    assert select_mechanism("vwap_reclaim", first, 70)
    assert not select_mechanism("vwap_trend_reclaim", first, 70)
    signals.update(group(opening=99, high=100, low=98, close=99), eligible=True)
    later = signals.update(group(opening=100, high=101, low=100, close=100.5), eligible=True)
    assert not later["vwap_reclaim"]


def test_failed_breakdown_requires_recovery_and_rejection_wick():
    now = warmed().update(group(opening=99, high=100, low=98, close=99.8), eligible=True)
    assert now["prior_hour_low"] == 99
    assert now["lower_wick_fraction"] == 0.5
    assert select_mechanism("range_breakdown", now, 70)
    assert select_mechanism("failed_breakdown_reversal", now, 70)
    falling = warmed().update(group(opening=99, high=100, low=98, close=98.5), eligible=True)
    assert select_mechanism("range_breakdown", falling, 70)
    assert not select_mechanism("failed_breakdown_reversal", falling, 70)


def test_earlier_session_events_do_not_consume_the_eligible_signal():
    signals = warmed()
    first = signals.update(group(opening=99, high=100, low=98, close=99.8), eligible=False)
    assert not first["range_breakdown"]
    later = signals.update(group(opening=98, high=99, low=97, close=98.5), eligible=True)
    assert later["range_breakdown"]
    repeated = signals.update(group(opening=97, high=98, low=96, close=97.5), eligible=True)
    assert not repeated["range_breakdown"]


def test_zero_volume_is_not_a_vwap_or_a_reclaim():
    now = SessionSignals().update(group(volume=0), eligible=True)
    assert now["session_vwap"] is None and not now["vwap_reclaim"]
    assert not select_mechanism("vwap_trend_reclaim", now, 70)


def test_cost_extension_filter_uses_the_same_worst_case_boundary():
    now = warmed().update(group(opening=100, high=102, low=100, close=102), eligible=True)
    assert now["vwap_reclaim"] and now["vwap_extension_bps"] > 70
    assert not select_mechanism("vwap_trend_reclaim", now, 70)


def test_new_strategy_attribution_does_not_require_an_opening_range_breakout():
    import pandas as pd

    from quantbot.intraday.attribution import explain_signals

    opening = pd.Timestamp("2025-01-02T00:00Z")
    at = opening + pd.Timedelta(minutes=75)
    bar = {"open": "100", "high": "101", "low": "99", "close": "100"}
    prepared = {"2025-01-02": {"opening": opening, "closing": opening + pd.Timedelta(days=1),
        "bars": [(t, {"BTC/USDT": bar}) for t in pd.date_range(opening, periods=30, freq="5min")],
        "signals": {at: {"BTC/USDT": {"base": False, "close": 100, "opening_high": 101,
                                     "relative_volume": 1.5, "session_vwap": 99.8}}}}}
    rows = [{"symbol": "BTC/USDT", "signal_at": at.isoformat(),
             "entry_at": (at + pd.Timedelta(minutes=5)).isoformat(),
             "exit_at": (at + pd.Timedelta(minutes=20)).isoformat(), "entry_reference": "100"}]
    explain_signals(rows, prepared, "vwap_trend_reclaim")
    assert rows[0]["confirmation_delay_minutes"] is None
    assert rows[0]["signal_reference"] == "signal_session_vwap"
    assert rows[0]["signal_reference_price"] == 99.8
