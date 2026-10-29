"""风控数据契约（RiskManager 类留在量化模块）."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskConfig:
    """风控规则配置."""

    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    max_position_count: int = 10
    max_single_position_pct: float = 0.30
    daily_loss_limit_pct: float | None = None
    max_order_value: float | None = None


@dataclass
class RiskCheckResult:
    """风控检查结果."""

    allowed: bool
    reason: str = ""

    @classmethod
    def ok(cls) -> RiskCheckResult:
        return cls(allowed=True)

    @classmethod
    def reject(cls, reason: str) -> RiskCheckResult:
        return cls(allowed=False, reason=reason)
