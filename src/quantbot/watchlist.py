"""Watchlist entry and exit rules."""

import math
from datetime import date
from typing import Iterable, Mapping, Set

from .regime import Regime


class WatchlistManager:
    def __init__(self, entry_percentile: float = 0.15, exit_percentile: float = 0.25, stop_loss: float = 0.08):
        self.entry_percentile = entry_percentile
        self.exit_percentile = exit_percentile
        self.stop_loss = stop_loss

    def update(
        self,
        scores: Mapping[str, float],
        previous_watchlist: Iterable[str],
        current_returns: Mapping[str, float] = None,
        negative_events: Set[str] = None,
        earnings_blackout: Set[str] = None,
        regime_sharpe_by_ticker: Mapping[str, float] = None,
        min_regime_sharpe: float = 0.80,
        regime: Regime = Regime.R3_SIDEWAYS,
    ):
        if regime in {Regime.R4_BEAR, Regime.R5_CRISIS}:
            return []

        current_returns = current_returns or {}
        negative_events = negative_events or set()
        earnings_blackout = earnings_blackout or set()
        ranked = [ticker for ticker, _score in sorted(scores.items(), key=lambda item: (-item[1], item[0]))]
        entry_count = max(1, int(math.ceil(len(ranked) * self.entry_percentile))) if ranked else 0
        exit_count = max(entry_count, int(math.ceil(len(ranked) * self.exit_percentile))) if ranked else 0

        entry_set = set(ranked[:entry_count]).difference(earnings_blackout)
        if regime_sharpe_by_ticker is not None:
            entry_set = {
                ticker
                for ticker in entry_set
                if float(regime_sharpe_by_ticker.get(ticker, float("-inf"))) >= min_regime_sharpe
            }
        keep_set = set(ranked[:exit_count])
        selected = entry_set.union(set(previous_watchlist).intersection(keep_set))

        selected = {
            ticker
            for ticker in selected
            if current_returns.get(ticker, 0.0) > -self.stop_loss
            and ticker not in negative_events
        }

        return [ticker for ticker in ranked if ticker in selected]


def earnings_blackout_symbols(
    signal_date: date,
    earnings_dates: Mapping[str, date],
    window_days: int = 2,
) -> Set[str]:
    return {
        ticker
        for ticker, earnings_date in earnings_dates.items()
        if abs((earnings_date - signal_date).days) <= window_days
    }


def watchlist_size_for_capital(capital: float, min_size: int = 20, max_size: int = 40) -> int:
    if capital >= 200_000:
        return max_size
    if capital <= 50_000:
        return min_size
    fraction = (capital - 50_000) / 150_000
    return int(round(min_size + fraction * (max_size - min_size)))


def adjusted_exit_percentile_for_turnover(
    base_exit_percentile: float,
    expected_turnover: float,
    target_turnover: float = 4.0,
    max_exit_percentile: float = 0.50,
) -> float:
    if target_turnover <= 0 or expected_turnover <= target_turnover:
        return base_exit_percentile
    scaled = base_exit_percentile * (expected_turnover / target_turnover)
    return min(max_exit_percentile, scaled)
