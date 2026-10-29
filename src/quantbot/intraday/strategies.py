"""Completed-session-price features for disabled, preregistered research strategies."""

from collections import deque


class SessionSignals:
    """Maintain session VWAP and the preceding hour without observing future bars."""

    def __init__(self):
        self.notional = 0.0
        self.volume = 0.0
        self.history = deque(maxlen=4)
        self.seen = set()

    def update(self, group, *, eligible):
        opening, close = float(group[0]["open"]), float(group[-1]["close"])
        high = max(float(b["high"]) for b in group)
        low = min(float(b["low"]) for b in group)
        for bar in group:
            volume = float(bar["volume"])
            typical = (float(bar["high"]) + float(bar["low"]) + float(bar["close"])) / 3
            self.notional += typical * volume
            self.volume += volume
        vwap = self.notional / self.volume if self.volume else None
        previous = self.history[-1] if self.history else None
        reclaim = bool(eligible and "reclaim" not in self.seen and previous
                       and previous["vwap"] is not None and vwap is not None
                       and previous["close"] <= previous["vwap"] and close > vwap)
        floor = min(b["low"] for b in self.history) if len(self.history) == 4 else None
        breakdown = bool(eligible and "breakdown" not in self.seen and floor is not None and low < floor)
        if reclaim:
            self.seen.add("reclaim")
        if breakdown:
            self.seen.add("breakdown")
        recent = list(self.history)[-2:]
        changes = [recent[1]["close"] - recent[0]["close"], close - recent[1]["close"]] if len(recent) == 2 else None
        distance = sum(abs(c) for c in changes) if changes is not None else None
        efficiency = (sum(changes) / distance if distance else 0.0) if changes is not None else None
        prior_range = max(b["high"] for b in recent) - min(b["low"] for b in recent) if len(recent) == 2 else None
        values = {
            "signed_efficiency_30m": efficiency,
            "net_move_30m_bps": (close / recent[0]["close"] - 1) * 10000 if len(recent) == 2 else None,
            "prior_range_bps": prior_range / close * 10000 if prior_range is not None else None,
            "close_location": (close - low) / (high - low) if high > low else 0.0,
            "session_vwap": vwap, "vwap_reclaim": reclaim,
            "vwap_rising": bool(previous and previous["vwap"] is not None and vwap is not None
                                and vwap > previous["vwap"]),
            "vwap_extension_bps": (close / vwap - 1) * 10000 if vwap else None,
            "bullish_bar": close > opening,
            "range_breakdown": breakdown, "prior_hour_low": floor,
            "range_reclaimed": floor is not None and close > floor,
            "lower_wick_fraction": (min(opening, close) - low) / (high - low) if high > low else 0.0,
        }
        self.history.append({"close": close, "high": high, "low": low, "vwap": vwap})
        return values


def select_mechanism(factor, value, stress_round_trip_bps):
    if factor == "vwap_reclaim":
        return value["vwap_reclaim"]
    if factor == "vwap_trend_reclaim":
        return (value["vwap_reclaim"] and value["vwap_rising"] and value["bullish_bar"]
                and value["vwap_extension_bps"] <= stress_round_trip_bps)
    if factor == "range_breakdown":
        return value["range_breakdown"]
    if factor == "failed_breakdown_reversal":
        return (value["range_breakdown"] and value["range_reclaimed"] and value["bullish_bar"]
                and value["lower_wick_fraction"] >= 0.5)
    raise ValueError("未知日内机制")
