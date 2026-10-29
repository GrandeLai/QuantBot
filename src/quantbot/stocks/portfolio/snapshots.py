"""组合历史快照存储（SQLite）.

用途：
  - 记录每次 summary() 调用时的组合总价值、各策略价值
  - 支持 daily_pnl、equity 曲线、max_drawdown、sharpe_ratio 的历史计算
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


_DB_PATH = Path("runtime") / "portfolio" / "portfolio_snapshots.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class PortfolioSnapshot:
    """单次组合快照."""

    timestamp: str  # ISO 8601 UTC
    total_value: float
    total_cash: float
    total_pnl: float


@dataclass
class StrategySnapshot:
    """单个策略单次快照."""

    timestamp: str  # ISO 8601 UTC
    strategy_name: str
    portfolio_value: float
    pnl: float
    allocation: float


class SnapshotStorage:
    """SQLite 组合历史快照存储（线程安全）."""

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        """初始化存储，创建表结构."""
        self._path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp   TEXT    NOT NULL,
                    total_value REAL    NOT NULL,
                    total_cash  REAL    NOT NULL,
                    total_pnl   REAL    NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategy_snapshots (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp       TEXT    NOT NULL,
                    strategy_name   TEXT    NOT NULL,
                    portfolio_value REAL    NOT NULL,
                    pnl             REAL    NOT NULL,
                    allocation      REAL    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_port_ts
                    ON portfolio_snapshots(timestamp);
                CREATE INDEX IF NOT EXISTS idx_strat_name_ts
                    ON strategy_snapshots(strategy_name, timestamp);
                """
            )
            self._conn.commit()

    # ── 写入 ──────────────────────────────────────────────────────────────────

    def save_snapshot(self, snap: PortfolioSnapshot) -> None:
        """保存一次组合总快照."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO portfolio_snapshots "
                "(timestamp, total_value, total_cash, total_pnl) VALUES (?, ?, ?, ?)",
                (snap.timestamp, snap.total_value, snap.total_cash, snap.total_pnl),
            )
            self._conn.commit()

    def save_strategy_snapshot(self, snap: StrategySnapshot) -> None:
        """保存一次策略快照."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO strategy_snapshots "
                "(timestamp, strategy_name, portfolio_value, pnl, allocation) VALUES (?, ?, ?, ?, ?)",
                (snap.timestamp, snap.strategy_name, snap.portfolio_value, snap.pnl, snap.allocation),
            )
            self._conn.commit()

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def list_snapshots(
        self,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[PortfolioSnapshot]:
        """返回 since 之后（含）的组合快照，按时间升序."""
        since_str = since.isoformat() if since else "0000-01-01T00:00:00+00:00"
        with self._lock:
            rows = self._conn.execute(
                "SELECT timestamp, total_value, total_cash, total_pnl "
                "FROM portfolio_snapshots WHERE timestamp >= ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (since_str, limit),
            ).fetchall()
        return [
            PortfolioSnapshot(timestamp=r[0], total_value=r[1], total_cash=r[2], total_pnl=r[3])
            for r in rows
        ]

    def snapshot_24h_ago(self) -> PortfolioSnapshot | None:
        """返回最接近 24 小时前的快照（用于 daily_pnl 计算）."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with self._lock:
            row = self._conn.execute(
                "SELECT timestamp, total_value, total_cash, total_pnl "
                "FROM portfolio_snapshots WHERE timestamp <= ? "
                "ORDER BY timestamp DESC LIMIT 1",
                (cutoff,),
            ).fetchone()
        if row is None:
            return None
        return PortfolioSnapshot(timestamp=row[0], total_value=row[1], total_cash=row[2], total_pnl=row[3])

    def list_strategy_snapshots(
        self,
        strategy_name: str,
        since: datetime | None = None,
        limit: int = 1000,
    ) -> list[StrategySnapshot]:
        """返回指定策略的历史快照，按时间升序."""
        since_str = since.isoformat() if since else "0000-01-01T00:00:00+00:00"
        with self._lock:
            rows = self._conn.execute(
                "SELECT timestamp, strategy_name, portfolio_value, pnl, allocation "
                "FROM strategy_snapshots "
                "WHERE strategy_name = ? AND timestamp >= ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (strategy_name, since_str, limit),
            ).fetchall()
        return [
            StrategySnapshot(
                timestamp=r[0],
                strategy_name=r[1],
                portfolio_value=r[2],
                pnl=r[3],
                allocation=r[4],
            )
            for r in rows
        ]
