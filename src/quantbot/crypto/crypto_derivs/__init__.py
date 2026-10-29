"""Crypto derivatives — Binance/OKX collectors + basis/funding analytics."""
from quantbot.crypto.crypto_derivs.analytics import (
    compute_basis,
    funding_extreme_signal,
    funding_percentile_stats,
    oi_momentum,
)
from quantbot.crypto.crypto_derivs.collector import (
    fetch_aggregated_derivs,
    fetch_binance_funding,
    fetch_binance_funding_history,
    fetch_binance_open_interest,
    fetch_okx_funding,
    fetch_okx_funding_history,
    fetch_okx_open_interest,
)
from quantbot.crypto.crypto_derivs.etf_flow import (
    BTC_SPOT_ETF_TICKERS,
    ETH_SPOT_ETF_TICKERS,
    aggregate_daily_flows,
    flow_aum_velocity,
    flow_extreme_signal,
    flow_zscore,
)
from quantbot.crypto.crypto_derivs.models import (
    ETFFlowSnapshot,
    FundingRate,
    OpenInterest,
)

__all__ = [
    "BTC_SPOT_ETF_TICKERS",
    "ETFFlowSnapshot",
    "ETH_SPOT_ETF_TICKERS",
    "FundingRate",
    "OpenInterest",
    "aggregate_daily_flows",
    "compute_basis",
    "fetch_aggregated_derivs",
    "fetch_binance_funding",
    "fetch_binance_funding_history",
    "fetch_binance_open_interest",
    "fetch_okx_funding",
    "fetch_okx_funding_history",
    "fetch_okx_open_interest",
    "flow_aum_velocity",
    "flow_extreme_signal",
    "flow_zscore",
    "funding_extreme_signal",
    "funding_percentile_stats",
    "oi_momentum",
]
