"""Cross-sectional factor normalization helpers."""

from typing import Dict, Mapping


def cross_sectional_winsorized_zscore(
    factor_matrix: Mapping[str, Mapping[str, float]],
    lower_quantile: float = 0.05,
    upper_quantile: float = 0.95,
) -> Dict[str, Dict[str, float]]:
    normalized = {ticker: {} for ticker in factor_matrix}
    factor_names = sorted(
        {
            factor_name
            for factors in factor_matrix.values()
            for factor_name in factors
        }
    )

    for factor_name in factor_names:
        tickers = [
            ticker
            for ticker, factors in factor_matrix.items()
            if factor_name in factors
        ]
        values = [float(factor_matrix[ticker][factor_name]) for ticker in tickers]
        lower = _quantile(values, lower_quantile)
        upper = _quantile(values, upper_quantile)
        clipped = [min(max(value, lower), upper) for value in values]
        zscores = _zscore(clipped)
        for ticker, value in zip(tickers, zscores):
            normalized[ticker][factor_name] = value

    return normalized


def sector_neutralize(
    factor_matrix: Mapping[str, Mapping[str, float]],
    sectors: Mapping[str, str],
) -> Dict[str, Dict[str, float]]:
    neutral = {ticker: dict(factors) for ticker, factors in factor_matrix.items()}
    factor_names = sorted(
        {
            factor_name
            for factors in factor_matrix.values()
            for factor_name in factors
        }
    )
    for factor_name in factor_names:
        sector_values: Dict[str, list] = {}
        for ticker, factors in factor_matrix.items():
            if factor_name in factors:
                sector = sectors.get(ticker, "Unknown")
                sector_values.setdefault(sector, []).append(float(factors[factor_name]))
        sector_means = {
            sector: sum(values) / len(values)
            for sector, values in sector_values.items()
        }
        for ticker, factors in factor_matrix.items():
            if factor_name in factors:
                sector = sectors.get(ticker, "Unknown")
                neutral[ticker][factor_name] = float(factors[factor_name]) - sector_means[sector]
    return neutral


def market_cap_bucket_neutralize(
    factor_matrix: Mapping[str, Mapping[str, float]],
    market_caps: Mapping[str, float],
    buckets: int = 5,
) -> Dict[str, Dict[str, float]]:
    neutral = {ticker: dict(factors) for ticker, factors in factor_matrix.items()}
    bucket_by_ticker = _market_cap_buckets(factor_matrix, market_caps, buckets)
    factor_names = sorted(
        {
            factor_name
            for factors in factor_matrix.values()
            for factor_name in factors
        }
    )

    for factor_name in factor_names:
        bucket_values: Dict[int, list] = {}
        for ticker, factors in factor_matrix.items():
            if factor_name in factors:
                bucket_values.setdefault(bucket_by_ticker[ticker], []).append(float(factors[factor_name]))
        bucket_means = {
            bucket: sum(values) / len(values)
            for bucket, values in bucket_values.items()
        }
        for ticker, factors in factor_matrix.items():
            if factor_name in factors:
                bucket = bucket_by_ticker[ticker]
                neutral[ticker][factor_name] = float(factors[factor_name]) - bucket_means[bucket]

    return neutral


def _quantile(values, quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(ordered) - 1)
    fraction = position - lower_index
    return ordered[lower_index] * (1 - fraction) + ordered[upper_index] * fraction


def _zscore(values):
    if not values:
        return []
    mean = sum(values) / len(values)
    std = (sum((value - mean) ** 2 for value in values) / len(values)) ** 0.5
    if std == 0:
        return [0.0 for _value in values]
    return [(value - mean) / std for value in values]


def _market_cap_buckets(factor_matrix, market_caps, buckets: int) -> Dict[str, int]:
    tickers = sorted(
        factor_matrix,
        key=lambda ticker: (float(market_caps.get(ticker, 0.0)), ticker),
    )
    if not tickers:
        return {}
    bucket_count = max(1, buckets)
    return {
        ticker: min(bucket_count - 1, index * bucket_count // len(tickers))
        for index, ticker in enumerate(tickers)
    }
