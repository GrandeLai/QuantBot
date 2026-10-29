"""Central configuration values from the design document."""

from dataclasses import dataclass
from typing import Tuple

from .risk import RiskConfig


@dataclass(frozen=True)
class MarketConfig:
    exchange: Tuple[str, ...]
    benchmark: str
    target_alpha_annual: float
    target_sharpe: float
    signal_frequency: str


@dataclass(frozen=True)
class FactorPoolConfig:
    min_size: int
    max_size: int
    alpha158_share: float
    kronos_share: float
    rd_agent_share: float
    ic_threshold: float
    icir_threshold: float


@dataclass(frozen=True)
class RegimeConfig:
    n_states: int
    min_confidence: float
    walk_forward_step_months: int


@dataclass(frozen=True)
class ExecutionConfig:
    primary_broker: str
    backup_broker: str
    min_trade_delta: float
    target_turnover_annual: float


@dataclass(frozen=True)
class SystemConfig:
    market: MarketConfig
    factor_pool: FactorPoolConfig
    regime: RegimeConfig
    risk: RiskConfig
    execution: ExecutionConfig

    @classmethod
    def default(cls):
        return cls(
            market=MarketConfig(
                exchange=("NYSE", "NASDAQ"),
                benchmark="SPY",
                target_alpha_annual=0.05,
                target_sharpe=1.2,
                signal_frequency="daily_after_close",
            ),
            factor_pool=FactorPoolConfig(
                min_size=20,
                max_size=40,
                alpha158_share=0.40,
                kronos_share=0.30,
                rd_agent_share=0.30,
                ic_threshold=0.025,
                icir_threshold=0.35,
            ),
            regime=RegimeConfig(
                n_states=5,
                min_confidence=0.60,
                walk_forward_step_months=3,
            ),
            risk=RiskConfig(),
            execution=ExecutionConfig(
                primary_broker="Alpaca",
                backup_broker="IBKR",
                min_trade_delta=0.005,
                target_turnover_annual=4.0,
            ),
        )
