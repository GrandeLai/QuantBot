"""Regime feature engineering without dataframe dependencies."""

from typing import Any, Dict, Iterable, List


MarketRow = Dict[str, Any]


def build_regime_features(
    market_data: Iterable[MarketRow],
    vix_ma_window: int = 60,
    vol_window: int = 21,
    spy_mom_1m_window: int = 21,
    spy_mom_3m_window: int = 63,
    ma_short_window: int = 50,
    ma_long_window: int = 200,
    flow_window: int = 10,
    earnings_season_months=(1, 4, 7, 10),
) -> List[Dict[str, float]]:
    rows = sorted([dict(row) for row in market_data], key=lambda row: row["date"])
    features = []

    for index, row in enumerate(rows):
        spy_prices = [float(item["SPY"]) for item in rows[: index + 1]]
        vix_values = [float(item["VIX"]) for item in rows[: index + 1]]
        flows = [float(item.get("SPY_NET_FLOW", 0.0)) for item in rows[: index + 1]]

        feature = {
            "date": row["date"],
            "vix_level": float(row["VIX"]),
            "vix_ma_ratio": _safe_div(float(row["VIX"]), _rolling_mean(vix_values, vix_ma_window)),
            "vol_regime": _annualized_volatility(spy_prices, vol_window),
            "spy_mom_1m": _momentum(spy_prices, spy_mom_1m_window),
            "spy_mom_3m": _momentum(spy_prices, spy_mom_3m_window),
            "ma_signal": 1 if _rolling_mean(spy_prices, ma_short_window) > _rolling_mean(spy_prices, ma_long_window) else 0,
            "yield_curve": float(row["US10Y"]) - float(row["US2Y"]),
            "credit_spread": float(row["HY_SPREAD"]) - float(row["IG_SPREAD"]),
            "real_rate": float(row["US10Y"]) - float(row["INFLATION_1Y"]),
            "breadth": _safe_div(float(row["NYSE_ADV"]), float(row["NYSE_ADV"]) + float(row["NYSE_DEC"])),
            "new_highs": _safe_div(float(row["NYSE_NH"]), float(row["NYSE_NH"]) + float(row["NYSE_NL"])),
            "spy_flow": _rolling_mean(flows, flow_window),
            "earnings_season": _is_earnings_season(row["date"], earnings_season_months),
        }
        features.append(feature)

    return features


def _rolling_mean(values: List[float], window: int) -> float:
    if not values:
        return 0.0
    window_values = values[-window:]
    return sum(window_values) / len(window_values)


def _momentum(prices: List[float], window: int) -> float:
    if len(prices) <= window:
        return 0.0
    return _safe_div(prices[-1], prices[-window - 1]) - 1


def _annualized_volatility(prices: List[float], window: int) -> float:
    if len(prices) <= 1:
        return 0.0
    returns = [
        prices[index] / prices[index - 1] - 1
        for index in range(1, len(prices))
        if prices[index - 1] != 0
    ][-window:]
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    variance = sum((value - mean) ** 2 for value in returns) / len(returns)
    return (variance ** 0.5) * (252 ** 0.5)


def _safe_div(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


def _is_earnings_season(value, earnings_season_months) -> float:
    return 1.0 if getattr(value, "month", None) in earnings_season_months else 0.0
