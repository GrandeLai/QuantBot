"""Bounded, point-in-time-safe DayTrade hypothesis components."""

from .domain import (
    DayTradeHypothesisConfig,
    DayTradeRiskSizingConfig,
    EntryFilterDecision,
    IntradayFeatureSnapshot,
    IntradayPositionSize,
    PortfolioHeatState,
)
from .config import hypothesis_config_from_runtime
from .filters import evaluate_entry_filters, risk_scaled_stop
from .features import (
    DEFAULT_DAYTRADE_SECTOR_PROXIES,
    IntradayFeatureBook,
    IntradayFeatureBuildConfig,
    IntradayFeatureRequirements,
    build_intraday_feature_snapshots,
    intraday_feature_config_to_payload,
)
from .experiments import (
    preregistered_daytrade_experiments,
    preregistered_daytrade_followup_experiments,
    preregistered_daytrade_h3_experiments,
    preregistered_daytrade_h6_experiments,
    preregistered_daytrade_h7_experiments,
    preregistered_daytrade_h8_experiments,
    preregistered_daytrade_h9_experiments,
    preregistered_daytrade_global_experiments,
)
from .risk import size_intraday_position
from .universe import DailyUniverseSnapshot
from .vwap import session_cumulative_vwap, update_vwap_hysteresis


__all__ = (
    "DayTradeHypothesisConfig",
    "DayTradeRiskSizingConfig",
    "DailyUniverseSnapshot",
    "EntryFilterDecision",
    "IntradayFeatureSnapshot",
    "IntradayFeatureBook",
    "IntradayFeatureBuildConfig",
    "IntradayFeatureRequirements",
    "IntradayPositionSize",
    "PortfolioHeatState",
    "evaluate_entry_filters",
    "build_intraday_feature_snapshots",
    "DEFAULT_DAYTRADE_SECTOR_PROXIES",
    "hypothesis_config_from_runtime",
    "intraday_feature_config_to_payload",
    "preregistered_daytrade_experiments",
    "preregistered_daytrade_followup_experiments",
    "preregistered_daytrade_h3_experiments",
    "preregistered_daytrade_h6_experiments",
    "preregistered_daytrade_h7_experiments",
    "preregistered_daytrade_h8_experiments",
    "preregistered_daytrade_h9_experiments",
    "preregistered_daytrade_global_experiments",
    "risk_scaled_stop",
    "session_cumulative_vwap",
    "size_intraday_position",
    "update_vwap_hysteresis",
)
