"""价格行为生产政策。

政策只定义系统不可逾越的边界，不参与信号计算：价格行为可以产生方向，
风险和执行层只能限制或降低已有风险。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from ..config import validate_config
from ..domain.decision import DecisionSource


class PolicyViolation(ValueError):
    """交易决策违反价格行为宗旨。"""


class ProductionPolicyError(PolicyViolation):
    """配置不允许进入生产阶段。"""

    def __init__(self, errors: list[str] | tuple[str, ...]):
        self.errors = tuple(errors)
        super().__init__("生产价格行为政策校验失败：\n- " + "\n- ".join(self.errors))


@dataclass(frozen=True)
class ExposureChange:
    """一次目标持仓变化；正数为多头，负数为空头。"""

    symbol: str
    before_shares: float
    after_shares: float
    source: DecisionSource
    reason: str
    evidence_id: str | None = None

    @property
    def reduces_exposure(self) -> bool:
        return abs(self.after_shares) < abs(self.before_shares) \
            and not self.reverses_direction

    @property
    def increases_exposure(self) -> bool:
        return abs(self.after_shares) > abs(self.before_shares)

    @property
    def reverses_direction(self) -> bool:
        return self.before_shares * self.after_shares < 0

    @property
    def creates_direction(self) -> bool:
        return self.before_shares == 0 and self.after_shares != 0


def _finite_shares(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PolicyViolation(f"{name} 必须是有限数值")
    number = float(value)
    if not math.isfinite(number):
        raise PolicyViolation(f"{name} 必须是有限数值")
    return number


def validate_exposure_change(
    *,
    symbol: str,
    before_shares: int | float,
    after_shares: int | float,
    source: DecisionSource | str,
    reason: str,
    evidence_id: str | None = None,
) -> ExposureChange:
    """验证并返回一次持仓变化。

    新建、反向或增加绝对暴露只能由 ``PRICE_ACTION`` 产生，并必须关联
    当时落盘的价格行为证据。``RISK_OVERRIDE`` 只能在不反向的前提下严格
    减仓；执行安全层同样不能创造或扩大方向。
    """
    if not isinstance(symbol, str) or not symbol.strip():
        raise PolicyViolation("symbol 必须是非空字符串")
    if not isinstance(reason, str) or not reason.strip():
        raise PolicyViolation("reason 必须是非空字符串")
    try:
        if isinstance(source, DecisionSource):
            normalized_source = source
        elif isinstance(source, str):
            try:
                normalized_source = DecisionSource(source)
            except ValueError:
                normalized_source = DecisionSource[source.upper()]
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise PolicyViolation(f"未知决策来源: {source!r}") from exc

    change = ExposureChange(
        symbol=symbol.strip().upper(),
        before_shares=_finite_shares(before_shares, "before_shares"),
        after_shares=_finite_shares(after_shares, "after_shares"),
        source=normalized_source,
        reason=reason.strip(),
        evidence_id=evidence_id.strip() if isinstance(evidence_id, str) else None,
    )

    creates_or_expands = change.creates_direction or change.reverses_direction \
        or change.increases_exposure
    if creates_or_expands and change.source is not DecisionSource.PRICE_ACTION:
        raise PolicyViolation("新建、反向或增加暴露只能来自 PRICE_ACTION")
    if change.source is DecisionSource.PRICE_ACTION \
            and change.before_shares != change.after_shares and not change.evidence_id:
        raise PolicyViolation("价格行为持仓变化必须关联 evidence_id")
    if change.source is DecisionSource.RISK_OVERRIDE and not change.reduces_exposure:
        raise PolicyViolation("RISK_OVERRIDE 只能在不反向的前提下严格减仓")
    if change.source is DecisionSource.EXECUTION_RECOVERY \
            and (change.increases_exposure or change.reverses_direction
                 or change.creates_direction):
        raise PolicyViolation("EXECUTION_RECOVERY 不得创造、反向或增加暴露")
    return change


def validate_production_config(cfg: Mapping[str, Any]) -> Mapping[str, Any]:
    """拒绝非价格行为输入和已经证伪的生产模块，成功时原样返回配置。"""
    validate_config(cfg)
    errors: list[str] = []
    strategy = cfg.get("strategy", {})
    if int(strategy.get("earnings_avoid_days", 0) or 0) > 0:
        errors.append("strategy.earnings_avoid_days 必须为 0（财报不是价格行为信号）")
    if int(strategy.get("time_stop_days", 0) or 0) > 0:
        errors.append("strategy.time_stop_days 必须为 0（时间本身不是价格结构破坏）")

    for section in ("shorts", "base_position", "leverage"):
        module = cfg.get(section, {}) or {}
        if bool(module.get("enabled", False)):
            errors.append(f"{section}.enabled 必须为 false（该研究模块已被证伪）")

    max_cache_age = int(
        (cfg.get("live", {}) or {}).get("reconcile_max_cache_age_days", 4)
    )
    if max_cache_age > 4:
        errors.append(
            "live.reconcile_max_cache_age_days 不得大于 4（禁止放宽对账新鲜度掩盖漏跑）"
        )

    if errors:
        raise ProductionPolicyError(errors)
    return cfg
