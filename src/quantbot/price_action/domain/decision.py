"""价格行为决策的领域契约。

这里把项目宗旨变成可执行约束：任何增加市场风险暴露的行为，都必须
关联一份在信号日已经可知的价格行为证据。风险与执行覆盖只能降低风险，
不能借“安全处理”之名创造新的方向或扩大仓位。
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Mapping

import pandas as pd


class DecisionSource(str, Enum):
    """决策来源。"""

    PRICE_ACTION = "price_action"
    RISK_OVERRIDE = "risk_override"
    EXECUTION_RECOVERY = "execution_recovery"


class ExposureEffect(str, Enum):
    """决策对绝对风险暴露的影响。"""

    INCREASE = "increase"
    DECREASE = "decrease"
    NONE = "none"


@dataclass(frozen=True)
class PriceActionEvidence:
    """某个已完成 K 线时点上的价格行为证据快照。"""

    signal_bar_date: str
    values: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DecisionIntent:
    """策略、风控或执行层产生的一条可审计决策。"""

    symbol: str
    intent: str
    source: DecisionSource
    exposure_effect: ExposureEffect
    reason_code: str
    evidence: PriceActionEvidence | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.exposure_effect == ExposureEffect.INCREASE:
            if self.source != DecisionSource.PRICE_ACTION or self.evidence is None:
                raise ValueError("增加风险暴露必须来自价格行为并携带证据")
        if self.source != DecisionSource.PRICE_ACTION \
                and self.exposure_effect == ExposureEffect.INCREASE:
            raise ValueError("风险或执行覆盖不得增加风险暴露")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["source"] = self.source.value
        data["exposure_effect"] = self.exposure_effect.value
        return data


_EVIDENCE_FIELDS = (
    "open", "high", "low", "close", "volume",
    "atr", "hh_break", "ll_break", "ll_exit", "hh_exit",
    "sh1", "sh2", "sl1", "sl2",
    "uptrend", "downtrend", "weekly_uptrend",
    "pullback", "pullback_anchor", "pullback_depth_atr",
    "bullish_engulfing", "hammer", "inside_bar", "ib_breakout", "reversal",
    "mom", "rank_mom", "ann_vol", "adv",
)


def _json_scalar(value: Any) -> Any:
    """把 pandas/numpy 标量转成可持久化的 JSON 标量。"""
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def evidence_from_feature_row(
    signal_bar_date: str | pd.Timestamp,
    row: Mapping[str, Any],
    *,
    regime_level: int,
    bench_momentum: float | None,
    extra: Mapping[str, Any] | None = None,
) -> PriceActionEvidence:
    """从共享特征行生成决策时证据；只复制白名单内的价格派生字段。"""
    values = {
        name: _json_scalar(row[name])
        for name in _EVIDENCE_FIELDS
        if name in row
    }
    values["regime_level"] = int(regime_level)
    values["bench_momentum"] = _json_scalar(bench_momentum)
    if extra:
        values.update({str(k): _json_scalar(v) for k, v in extra.items()})
    date = pd.Timestamp(signal_bar_date).date().isoformat()
    return PriceActionEvidence(signal_bar_date=date, values=values)
