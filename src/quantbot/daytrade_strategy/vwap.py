"""Session VWAP and hysteresis calculations with prefix-only semantics."""

from __future__ import annotations

from math import isfinite
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

from .domain import aware, finite_number, non_negative_number, side_value


EASTERN = ZoneInfo("America/New_York")


def session_cumulative_vwap(bars: Iterable[object]) -> Mapping[tuple[str, object], float]:
    """Return VWAP at each event using only that event and its session prefix."""

    ordered = sorted(tuple(bars), key=lambda bar: (bar.timestamp, str(bar.symbol).upper()))
    states: dict[tuple[str, object], tuple[float, float]] = {}
    result: dict[tuple[str, object], float] = {}
    for bar in ordered:
        timestamp = aware(bar.timestamp, "bar.timestamp")
        symbol = str(bar.symbol).strip().upper()
        if not symbol:
            raise ValueError("bar.symbol must be non-empty")
        event_key = (symbol, timestamp)
        if event_key in result:
            raise ValueError("duplicate symbol/timestamp minute bar")
        price = _bar_vwap_price(bar)
        volume = non_negative_number(bar.volume, "bar.volume")
        session_key = (symbol, timestamp.astimezone(EASTERN).date())
        previous_vwap, previous_volume = states.get(session_key, (price, 0.0))
        if volume == 0:
            current_vwap = previous_vwap
            total_volume = previous_volume
        else:
            total_volume = previous_volume + volume
            if not isfinite(total_volume):
                raise ValueError("session cumulative volume overflow")
            weight = volume / total_volume
            current_vwap = previous_vwap + (price - previous_vwap) * weight
            if not isfinite(current_vwap):
                raise ValueError("session cumulative VWAP overflow")
        states[session_key] = (current_vwap, total_volume)
        result[event_key] = current_vwap
    return result


def update_vwap_hysteresis(
    *,
    previous_below_count: int,
    close: float,
    session_vwap: float,
    hysteresis_bps: float,
    required_bars: int,
    side: str,
) -> tuple[int, bool]:
    if isinstance(previous_below_count, bool) or previous_below_count < 0:
        raise ValueError("previous_below_count must be a non-negative integer")
    if isinstance(required_bars, bool) or not isinstance(required_bars, int) or required_bars < 0:
        raise ValueError("required_bars must be a non-negative integer")
    if required_bars == 0:
        return 0, False
    close_value = finite_number(close, "close")
    vwap_value = finite_number(session_vwap, "session_vwap")
    if vwap_value <= 0:
        raise ValueError("session_vwap must be positive")
    hysteresis = non_negative_number(hysteresis_bps, "hysteresis_bps") / 10_000.0
    direction = side_value(side)
    adverse = (
        close_value < vwap_value * (1.0 - hysteresis)
        if direction == "long"
        else close_value > vwap_value * (1.0 + hysteresis)
    )
    count = previous_below_count + 1 if adverse else 0
    return count, count >= required_bars


def _bar_vwap_price(bar: object) -> float:
    supplied = getattr(bar, "vwap", None)
    if supplied is not None:
        price = finite_number(supplied, "bar.vwap")
    else:
        price = sum(
            finite_number(getattr(bar, name), f"bar.{name}")
            for name in ("high", "low", "close")
        ) / 3.0
    if price <= 0:
        raise ValueError("bar VWAP price must be positive")
    return price

