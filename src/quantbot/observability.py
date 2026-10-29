"""Structured logging and pipeline run reporting."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List


class EventLogger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def info(self, event: str, **fields) -> None:
        self._write("INFO", event, fields)

    def error(self, event: str, **fields) -> None:
        self._write("ERROR", event, fields)

    def _write(self, level: str, event: str, fields) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"level": level, "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True) + "\n")


class AlertDispatcher:
    def __init__(self, sms_sender, phone_number: str):
        self.sms_sender = sms_sender
        self.phone_number = phone_number

    def critical(self, event: str, **fields) -> None:
        parts = [event] + ["%s=%s" % (key, value) for key, value in sorted(fields.items())]
        self.sms_sender(self.phone_number, " | ".join(parts))


class RiskAlertRouter:
    def __init__(self, dispatcher: AlertDispatcher):
        self.dispatcher = dispatcher

    def notify_drawdown(self, drawdown: float, action) -> bool:
        if not (getattr(action, "manual_review", False) or getattr(action, "halt_trading", False)):
            return False
        self.dispatcher.critical(
            "risk_drawdown",
            drawdown="%.6f" % float(drawdown),
            halt_trading=bool(getattr(action, "halt_trading", False)),
            position_scale="%.6f" % float(getattr(action, "position_scale", 1.0)),
            reason=getattr(action, "reason", "drawdown"),
        )
        return True

    def notify_regime_switch(self, previous_regime, current_regime) -> bool:
        previous = _event_value(previous_regime)
        current = _event_value(current_regime)
        if previous == current:
            return False
        self.dispatcher.critical("regime_switch", previous=previous, current=current)
        return True


@dataclass(frozen=True)
class StageRecord:
    name: str
    started_at: float
    finished_at: float
    ok: bool
    error: str = ""


class PipelineRunReport:
    def __init__(self, run_id: str):
        self.run_id = run_id
        self.stages: List[StageRecord] = []

    def record_stage(self, name: str, started_at: float, finished_at: float, ok: bool, error: str = "") -> None:
        self.stages.append(
            StageRecord(
                name=name,
                started_at=started_at,
                finished_at=finished_at,
                ok=ok,
                error=error,
            )
        )

    def summary(self):
        duration = sum(stage.finished_at - stage.started_at for stage in self.stages)
        failed = [stage.name for stage in self.stages if not stage.ok]
        return {
            "run_id": self.run_id,
            "ok": not failed,
            "duration_seconds": duration,
            "failed_stages": failed,
        }


def _event_value(value) -> str:
    return str(getattr(value, "value", value))
