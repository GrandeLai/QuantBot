"""Dependency-free subset of Alpha158-style technical factors."""

from collections import defaultdict
from typing import Any, Dict, Iterable, List, Sequence


PriceRow = Dict[str, Any]


class Alpha158Lite:
    """Compute a small Alpha158-compatible technical factor subset.

    This is not a replacement for Qlib Alpha158. It provides the same category
    surface for local development: momentum, volatility, and volume/price
    relationship factors computed from adjusted prices.
    """

    def __init__(
        self,
        momentum_windows: Sequence[int] = (5, 10, 20, 60),
        volatility_windows: Sequence[int] = (5, 20),
        earnings_season_months: Sequence[int] = (1, 4, 7, 10),
    ):
        self.momentum_windows = tuple(momentum_windows)
        self.volatility_windows = tuple(volatility_windows)
        self.earnings_season_months = tuple(earnings_season_months)

    def compute(self, history: Iterable[PriceRow]) -> Dict[str, Dict[str, float]]:
        grouped: Dict[str, List[PriceRow]] = defaultdict(list)
        for row in history:
            grouped[row["ticker"]].append(dict(row))

        factors: Dict[str, Dict[str, float]] = {}
        for ticker, rows in grouped.items():
            rows.sort(key=lambda row: row["date"])
            prices = [float(row["adjusted_close"]) for row in rows]
            volumes = [float(row.get("volume", 0)) for row in rows]
            ticker_factors: Dict[str, float] = {}
            ticker_factors["earnings_season"] = _is_earnings_season(
                rows[-1].get("date"),
                self.earnings_season_months,
            )

            for window in self.momentum_windows:
                if len(prices) > window and prices[-window - 1] != 0:
                    ticker_factors["momentum_%d" % window] = prices[-1] / prices[-window - 1] - 1

            for window in self.volatility_windows:
                if len(prices) > window:
                    returns = _window_returns(prices, window)
                    ticker_factors["volatility_%d" % window] = _stddev(returns)

                    recent_volume = volumes[-window:]
                    average_volume = sum(recent_volume) / len(recent_volume)
                    ticker_factors["volume_ratio_%d" % window] = (
                        volumes[-1] / average_volume if average_volume else 0.0
                    )

                    recent_prices = prices[-window:]
                    moving_average = sum(recent_prices) / len(recent_prices)
                    price_std = _stddev(recent_prices)
                    ticker_factors["ma_deviation_%d" % window] = (
                        prices[-1] / moving_average - 1 if moving_average else 0.0
                    )
                    ticker_factors["rsi_%d" % window] = _rsi(prices, window)
                    ticker_factors["bollinger_position_%d" % window] = (
                        (prices[-1] - moving_average) / (2 * price_std)
                        if price_std
                        else 0.0
                    )

            factors[ticker] = ticker_factors

        return factors


def _window_returns(prices: Sequence[float], window: int) -> List[float]:
    start = len(prices) - window - 1
    values = prices[start:]
    return [
        values[index] / values[index - 1] - 1
        for index in range(1, len(values))
        if values[index - 1] != 0
    ]


def _stddev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return variance ** 0.5


def _is_earnings_season(value, earnings_season_months: Sequence[int]) -> float:
    month = getattr(value, "month", None)
    return 1.0 if month in earnings_season_months else 0.0


def _rsi(prices: Sequence[float], window: int) -> float:
    if len(prices) <= window:
        return 50.0
    recent = prices[-window - 1 :]
    gains = []
    losses = []
    for index in range(1, len(recent)):
        change = recent[index] - recent[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    average_gain = sum(gains) / len(gains)
    average_loss = sum(losses) / len(losses)
    if average_loss == 0 and average_gain == 0:
        return 50.0
    if average_loss == 0:
        return 100.0
    relative_strength = average_gain / average_loss
    return 100 - 100 / (1 + relative_strength)
