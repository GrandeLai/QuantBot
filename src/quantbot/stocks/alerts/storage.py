"""告警规则持久化 — SQLite."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from quantbot.stocks.alerts.models import AlertEvent, AlertRule


class AlertStorage:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS alert_rules (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS alert_events (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id TEXT NOT NULL, data TEXT NOT NULL, fired_at TEXT NOT NULL)")
            conn.commit()

    def save_rule(self, rule: AlertRule) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("INSERT OR REPLACE INTO alert_rules (id, data) VALUES (?, ?)", (rule.id, rule.model_dump_json()))
                conn.commit()

    def load_rule(self, rule_id: str) -> AlertRule | None:
        with self._connect() as conn:
            row = conn.execute("SELECT data FROM alert_rules WHERE id = ?", (rule_id,)).fetchone()
        return AlertRule.model_validate_json(row["data"]) if row else None

    def list_rules(self) -> list[AlertRule]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM alert_rules").fetchall()
        return [AlertRule.model_validate_json(r["data"]) for r in rows]

    def delete_rule(self, rule_id: str) -> bool:
        with self._lock:
            with self._connect() as conn:
                cur = conn.execute("DELETE FROM alert_rules WHERE id = ?", (rule_id,))
                conn.commit()
                return cur.rowcount > 0

    def save_event(self, event: AlertEvent) -> None:
        with self._lock:
            with self._connect() as conn:
                conn.execute("INSERT INTO alert_events (rule_id, data, fired_at) VALUES (?, ?, ?)", (event.rule_id, event.model_dump_json(), event.fired_at.isoformat()))
                conn.commit()

    def list_events(self, limit: int = 100) -> list[AlertEvent]:
        with self._connect() as conn:
            rows = conn.execute("SELECT data FROM alert_events ORDER BY fired_at DESC LIMIT ?", (limit,)).fetchall()
        return [AlertEvent.model_validate_json(r["data"]) for r in rows]
