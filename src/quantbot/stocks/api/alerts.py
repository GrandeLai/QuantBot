"""告警系统 API.

端点:
  GET  /alerts/rules          列出规则
  POST /alerts/rules          创建规则
  DELETE /alerts/rules/{id}   删除规则
  GET  /alerts/events         最近告警事件
  POST /alerts/check          手动传入行情检查规则
"""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from quantbot.stocks.alerts.engine import AlertEngine
from quantbot.stocks.alerts.models import AlertCondition, AlertRule, ChannelConfig
from quantbot.stocks.alerts.storage import AlertStorage

router = APIRouter(prefix="/alerts", tags=["告警系统"])
_engine = AlertEngine()


def _get_storage() -> AlertStorage:
    from quantbot.common.config import get_settings
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return AlertStorage(settings.data_dir / "alerts.sqlite")


class CreateRuleRequest(BaseModel):
    name: str
    conditions: list[AlertCondition]
    channels: list[ChannelConfig] = []
    cooldown_seconds: int = 3600


@router.get("/rules")
def list_rules() -> dict[str, Any]:
    return {"count": len(rules := _get_storage().list_rules()), "rules": [r.model_dump() for r in rules]}


@router.post("/rules")
def create_rule(req: CreateRuleRequest) -> dict[str, Any]:
    rule = AlertRule(id=str(uuid.uuid4())[:8], name=req.name, conditions=req.conditions, channels=req.channels, cooldown_seconds=req.cooldown_seconds)
    _get_storage().save_rule(rule)
    return {"id": rule.id, "message": "规则创建成功"}


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: str) -> dict[str, str]:
    if not _get_storage().delete_rule(rule_id):
        raise HTTPException(status_code=404, detail=f"规则不存在: {rule_id}")
    return {"message": f"规则 {rule_id} 已删除"}


@router.get("/events")
def list_events(limit: int = 50) -> dict[str, Any]:
    events = _get_storage().list_events(limit)
    return {"count": len(events), "events": [e.model_dump() for e in events]}


@router.post("/check")
def check_prices(prices: dict[str, float]) -> dict[str, Any]:
    storage = _get_storage()
    fired = []
    for rule in storage.list_rules():
        if _engine.evaluate_rule(rule, prices):
            event = _engine.build_event(rule, prices)
            if event:
                storage.save_event(event)
                _engine.record_fired(rule.id)
                fired.append(event.model_dump())
    return {"fired": len(fired), "events": fired}
