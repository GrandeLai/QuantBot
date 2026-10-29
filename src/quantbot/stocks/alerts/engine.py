"""告警引擎 — 条件评估 + 冷却期去重."""
from __future__ import annotations

import time

from loguru import logger

from quantbot.stocks.alerts.models import AlertConditionType, AlertEvent, AlertRule


class AlertEngine:
    def __init__(self) -> None:
        self._last_fired: dict[str, float] = {}

    def evaluate_rule(self, rule: AlertRule, current_prices: dict[str, float]) -> bool:
        if not rule.enabled:
            return False
        if self._is_in_cooldown(rule):
            return False
        return all(self._eval_condition(cond, current_prices) for cond in rule.conditions)

    def _is_in_cooldown(self, rule: AlertRule) -> bool:
        if rule.cooldown_seconds <= 0:
            return False
        last = self._last_fired.get(rule.id, 0.0)
        return (time.time() - last) < rule.cooldown_seconds

    def record_fired(self, rule_id: str) -> None:
        self._last_fired[rule_id] = time.time()

    def _eval_condition(self, cond: object, prices: dict[str, float]) -> bool:
        from quantbot.stocks.alerts.models import AlertCondition
        if not isinstance(cond, AlertCondition):
            return False
        price = prices.get(cond.symbol)
        if price is None:
            return False
        match cond.condition_type:
            case AlertConditionType.PRICE_ABOVE:
                return price > cond.threshold
            case AlertConditionType.PRICE_BELOW:
                return price < cond.threshold
            case AlertConditionType.PCT_CHANGE_ABOVE:
                if not cond.reference_price:
                    return False
                return (price - cond.reference_price) / cond.reference_price * 100 > cond.threshold
            case AlertConditionType.PCT_CHANGE_BELOW:
                if not cond.reference_price:
                    return False
                return (price - cond.reference_price) / cond.reference_price * 100 < -cond.threshold
            case _:
                logger.warning(f"未知条件类型: {cond.condition_type}")
                return False

    def build_event(self, rule: AlertRule, prices: dict[str, float]) -> AlertEvent | None:
        for cond in rule.conditions:
            price = prices.get(cond.symbol, 0.0)
            return AlertEvent(
                rule_id=rule.id,
                rule_name=rule.name,
                symbol=cond.symbol,
                condition_type=cond.condition_type,
                current_value=price,
                threshold=cond.threshold,
                message=f"{rule.name}: {cond.symbol} 当前 {price:.4f}，阈值 {cond.threshold}",
            )
        return None
