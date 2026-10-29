"""基于 SQLite 的只追加交易复盘证据账本。

设计约束：

* 每条事实由显式业务幂等键生成稳定 UUID；
* 同键同内容可安全重试，同键不同内容会失败；
* 一组事实可在一个事务中原子写入；
* 数据库触发器禁止更新或删除事实记录；
* JSON 使用排序后的紧凑编码，便于哈希、审计和稳定测试；
* WAL 允许日报读取与交易进程写入并行进行。

订单生命周期中的状态变化应写成新的订单事件，不应修改旧记录。
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
_ID_NAMESPACE = uuid.UUID("755cafbe-ebfe-5d40-93bd-a68ca5eaa1f2")
_FACT_TABLES = (
    "runs",
    "decisions",
    "orders",
    "fills",
    "position_events",
    "equity_snapshots",
    "reconciliations",
)
_APPEND_ONLY_TABLES = _FACT_TABLES + ("state_checkpoints",)


class IdempotencyConflictError(ValueError):
    """同一业务幂等键被用于不同内容。"""


class JournalClosedError(RuntimeError):
    """在账本关闭后继续使用。"""


class UnsupportedSchemaError(RuntimeError):
    """数据库 schema 版本高于当前代码可识别版本。"""


class InvalidStateCheckpointError(RuntimeError):
    """最近一次必须交接状态的事实缺少有效状态指纹。"""


# 这些是旧执行器可能写入、但不足以证明券商副作用已经收敛的状态。
# ``partial`` 和 ``dead`` 特别不能视为安全：旧实现只确认撤单请求已提交，
# 没有确认撤单已经成为券商终态。
UNRESOLVED_ORDER_STATUSES = frozenset(
    {"planned", "submitted", "uncertain", "submit_error", "partial", "dead"}
)
TRUSTED_NO_FILL_TERMINAL_STATUSES = frozenset(
    {"canceled", "expired", "rejected"}
)


@dataclass(frozen=True)
class PendingOrderRecovery:
    """一笔必须在新交易副作用前人工收敛的自有订单。"""

    client_order_id: str
    run_id: str
    decision_id: str | None
    broker_order_id: str | None
    symbol: str
    side: str
    quantity: int
    latest_status: str
    latest_event_id: str
    reason: str


@dataclass(frozen=True)
class StateCheckpoint:
    """最近一次已持久化策略状态的账本指纹。"""

    state_sha256: str
    recorded_at: str
    source: str


def stable_record_id(record_type: str, idempotency_key: str) -> str:
    """由记录类型和业务幂等键生成跨进程、跨数据库稳定的 UUID。"""
    record_type = _nonempty(record_type, "record_type")
    idempotency_key = _nonempty(idempotency_key, "idempotency_key")
    return str(uuid.uuid5(_ID_NAMESPACE, f"record:{record_type}:{idempotency_key}"))


def stable_client_order_id(decision_id: str, sequence: int = 0) -> str:
    """为一次决策的第 ``sequence`` 笔订单生成稳定客户端订单 ID。"""
    decision_id = _nonempty(decision_id, "decision_id")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValueError("sequence 必须是非负整数")
    return str(uuid.uuid5(_ID_NAMESPACE, f"client-order:{decision_id}:{sequence}"))


def _nonempty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


def _optional_text(value: str | None, name: str) -> str | None:
    return None if value is None else _nonempty(value, name)


def _symbol(value: str) -> str:
    return _nonempty(value, "symbol").upper()


def _iso_date(value: date | datetime | str | None, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()).isoformat()
        except ValueError as exc:
            raise ValueError(f"{name} 必须是 ISO 日期 YYYY-MM-DD") from exc
    raise TypeError(f"{name} 必须是 date、datetime 或 ISO 日期字符串")


def _iso_timestamp(value: datetime | str | None, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} 必须是带时区的 ISO 时间") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{name} 必须是 datetime 或 ISO 时间字符串")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} 必须包含时区，避免复盘时间歧义")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _finite_number(
    value: int | float | None,
    name: str,
    *,
    minimum: float | None = None,
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数字")
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return result


def _integer(value: int, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} 必须是整数")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


def _json(value: Any, name: str) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是可序列化且不含 NaN/Infinity 的 JSON") from exc


def _json_object(
    value: Mapping[str, Any] | None,
    name: str,
    *,
    required: bool = False,
) -> str:
    if value is None:
        if required:
            raise ValueError(f"{name} 必须是非空对象")
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} 必须是 JSON 对象")
    if required and not value:
        raise ValueError(f"{name} 必须是非空对象")
    return _json(dict(value), name)


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = _json(dict(payload), "payload").encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_SCHEMA_SQL = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    trading_date TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    signal_date TEXT,
    data_as_of TEXT,
    data_status TEXT NOT NULL,
    code_version TEXT,
    config_hash TEXT,
    data_hash TEXT,
    metadata_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    intended_quantity INTEGER,
    reference_price REAL,
    evidence_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (id, run_id)
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    decision_id TEXT,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    reference_price REAL,
    metadata_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    UNIQUE (id, run_id),
    FOREIGN KEY (decision_id, run_id) REFERENCES decisions(id, run_id)
);

CREATE TABLE IF NOT EXISTS fills (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    order_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price >= 0),
    reference_price REAL,
    slippage_bps REAL,
    commission REAL NOT NULL CHECK (commission >= 0),
    metadata_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (order_id, run_id) REFERENCES orders(id, run_id)
);

CREATE TABLE IF NOT EXISTS position_events (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    decision_id TEXT,
    order_id TEXT,
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    quantity_delta INTEGER NOT NULL,
    quantity_after INTEGER NOT NULL,
    price REAL,
    metadata_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (decision_id, run_id) REFERENCES decisions(id, run_id),
    FOREIGN KEY (order_id, run_id) REFERENCES orders(id, run_id)
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    equity REAL NOT NULL CHECK (equity >= 0),
    cash REAL NOT NULL,
    buying_power REAL NOT NULL,
    position_count INTEGER NOT NULL CHECK (position_count >= 0),
    market_level INTEGER,
    metadata_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    category TEXT NOT NULL,
    status TEXT NOT NULL,
    symbol TEXT,
    expected_json TEXT NOT NULL,
    actual_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    occurred_at TEXT,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_checkpoints (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL REFERENCES runs(id),
    state_sha256 TEXT NOT NULL,
    source TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_trading_date ON runs(trading_date);
CREATE INDEX IF NOT EXISTS idx_decisions_run ON decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_run ON orders(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_client_id ON orders(client_order_id);
CREATE INDEX IF NOT EXISTS idx_fills_run ON fills(run_id);
CREATE INDEX IF NOT EXISTS idx_position_events_run ON position_events(run_id);
CREATE INDEX IF NOT EXISTS idx_equity_snapshots_run ON equity_snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_reconciliations_run ON reconciliations(run_id);
CREATE INDEX IF NOT EXISTS idx_state_checkpoints_run ON state_checkpoints(run_id);

INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
INSERT OR IGNORE INTO schema_version(version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));
PRAGMA user_version = 2;

CREATE TRIGGER IF NOT EXISTS runs_no_update
BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT, 'append-only table: runs'); END;
CREATE TRIGGER IF NOT EXISTS runs_no_delete
BEFORE DELETE ON runs BEGIN SELECT RAISE(ABORT, 'append-only table: runs'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_update
BEFORE UPDATE ON decisions BEGIN SELECT RAISE(ABORT, 'append-only table: decisions'); END;
CREATE TRIGGER IF NOT EXISTS decisions_no_delete
BEFORE DELETE ON decisions BEGIN SELECT RAISE(ABORT, 'append-only table: decisions'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_update
BEFORE UPDATE ON orders BEGIN SELECT RAISE(ABORT, 'append-only table: orders'); END;
CREATE TRIGGER IF NOT EXISTS orders_no_delete
BEFORE DELETE ON orders BEGIN SELECT RAISE(ABORT, 'append-only table: orders'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_update
BEFORE UPDATE ON fills BEGIN SELECT RAISE(ABORT, 'append-only table: fills'); END;
CREATE TRIGGER IF NOT EXISTS fills_no_delete
BEFORE DELETE ON fills BEGIN SELECT RAISE(ABORT, 'append-only table: fills'); END;
CREATE TRIGGER IF NOT EXISTS position_events_no_update
BEFORE UPDATE ON position_events BEGIN SELECT RAISE(ABORT, 'append-only table: position_events'); END;
CREATE TRIGGER IF NOT EXISTS position_events_no_delete
BEFORE DELETE ON position_events BEGIN SELECT RAISE(ABORT, 'append-only table: position_events'); END;
CREATE TRIGGER IF NOT EXISTS equity_snapshots_no_update
BEFORE UPDATE ON equity_snapshots BEGIN SELECT RAISE(ABORT, 'append-only table: equity_snapshots'); END;
CREATE TRIGGER IF NOT EXISTS equity_snapshots_no_delete
BEFORE DELETE ON equity_snapshots BEGIN SELECT RAISE(ABORT, 'append-only table: equity_snapshots'); END;
CREATE TRIGGER IF NOT EXISTS reconciliations_no_update
BEFORE UPDATE ON reconciliations BEGIN SELECT RAISE(ABORT, 'append-only table: reconciliations'); END;
CREATE TRIGGER IF NOT EXISTS reconciliations_no_delete
BEFORE DELETE ON reconciliations BEGIN SELECT RAISE(ABORT, 'append-only table: reconciliations'); END;
CREATE TRIGGER IF NOT EXISTS state_checkpoints_no_update
BEFORE UPDATE ON state_checkpoints BEGIN SELECT RAISE(ABORT, 'append-only table: state_checkpoints'); END;
CREATE TRIGGER IF NOT EXISTS state_checkpoints_no_delete
BEFORE DELETE ON state_checkpoints BEGIN SELECT RAISE(ABORT, 'append-only table: state_checkpoints'); END;

COMMIT;
"""


class ReviewJournal:
    """一个进程内的 SQLite 复盘账本连接。

    单条写入可直接调用 ``append_*``。需要把运行、决策、订单等作为
    一个原子批次写入时，使用 ``transaction()`` 返回的 writer。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        timeout_seconds: float = 5.0,
        readonly: bool = False,
    ):
        self.path = Path(path)
        self._readonly = bool(readonly)
        if self._readonly and str(path) == ":memory:":
            raise ValueError("只读账本必须指向已有 SQLite 文件")
        if self._readonly:
            if self.path.is_symlink() or not self.path.is_file():
                raise FileNotFoundError(f"只读账本不存在或不是普通文件：{self.path}")
        elif str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._closed = False
        self._transaction_open = False
        target = str(path)
        connect_options: dict[str, Any] = {}
        if self._readonly:
            target = f"{self.path.resolve().as_uri()}?mode=ro"
            connect_options["uri"] = True
        self._connection = sqlite3.connect(
            target,
            timeout=timeout_seconds,
            isolation_level=None,
            **connect_options,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {int(timeout_seconds * 1000)}")
        if self._readonly:
            self._connection.execute("PRAGMA query_only = ON")
            self._validate_existing_schema()
        else:
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_schema()

    def _validate_existing_schema(self) -> None:
        """只读打开只接受已经完成迁移的完整 schema。"""
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current != SCHEMA_VERSION:
            self._connection.close()
            self._closed = True
            raise UnsupportedSchemaError(
                f"只读账本 schema={current}，要求 {SCHEMA_VERSION}"
            )
        try:
            versions = self._connection.execute(
                "SELECT version FROM schema_version ORDER BY version"
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            self._connection.close()
            self._closed = True
            raise RuntimeError("只读账本缺少 schema_version") from exc
        if [row[0] for row in versions] != list(range(1, SCHEMA_VERSION + 1)):
            self._connection.close()
            self._closed = True
            raise RuntimeError("schema_version 记录不连续，账本可能损坏")

    def _initialize_schema(self) -> None:
        current = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            self._connection.close()
            self._closed = True
            raise UnsupportedSchemaError(
                f"账本 schema={current}，当前代码仅支持到 {SCHEMA_VERSION}"
            )
        try:
            self._connection.executescript(_SCHEMA_SQL)
        except Exception:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        versions = self._connection.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        if [row[0] for row in versions] != list(range(1, SCHEMA_VERSION + 1)):
            raise RuntimeError("schema_version 记录不连续，账本可能损坏")

    @property
    def schema_version(self) -> int:
        self._ensure_open()
        return int(self._connection.execute("PRAGMA user_version").fetchone()[0])

    @property
    def journal_mode(self) -> str:
        self._ensure_open()
        return str(self._connection.execute("PRAGMA journal_mode").fetchone()[0])

    def _ensure_open(self) -> None:
        if self._closed:
            raise JournalClosedError("复盘账本已经关闭")

    @contextmanager
    def transaction(self) -> Iterator[JournalWriter]:
        """开启一个原子写入事务；异常时整个批次回滚。"""
        self._ensure_open()
        if self._readonly:
            raise RuntimeError("只读账本禁止写入事务")
        if self._transaction_open:
            raise RuntimeError("不支持嵌套账本事务")
        self._transaction_open = True
        self._connection.execute("BEGIN IMMEDIATE")
        writer = JournalWriter(self)
        try:
            yield writer
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()
        finally:
            writer._active = False
            self._transaction_open = False

    def _single(self, method_name: str, kwargs: dict[str, Any]) -> str:
        with self.transaction() as writer:
            method = getattr(writer, method_name)
            return str(method(**kwargs))

    def append_run(self, **kwargs: Any) -> str:
        """原子追加一条运行记录；参数同 ``JournalWriter.append_run``。"""
        return self._single("append_run", kwargs)

    def append_decision(self, **kwargs: Any) -> str:
        """原子追加一条决策记录；参数同 ``JournalWriter.append_decision``。"""
        return self._single("append_decision", kwargs)

    def append_order(self, **kwargs: Any) -> str:
        """原子追加一条订单记录；参数同 ``JournalWriter.append_order``。"""
        return self._single("append_order", kwargs)

    def append_fill(self, **kwargs: Any) -> str:
        """原子追加一条成交记录；参数同 ``JournalWriter.append_fill``。"""
        return self._single("append_fill", kwargs)

    def append_position_event(self, **kwargs: Any) -> str:
        """原子追加一条持仓事件；参数同 writer 对应方法。"""
        return self._single("append_position_event", kwargs)

    def append_equity_snapshot(self, **kwargs: Any) -> str:
        """原子追加一条权益快照；参数同 writer 对应方法。"""
        return self._single("append_equity_snapshot", kwargs)

    def append_reconciliation(self, **kwargs: Any) -> str:
        """原子追加一条对账结果；参数同 writer 对应方法。"""
        return self._single("append_reconciliation", kwargs)

    def append_state_checkpoint(self, **kwargs: Any) -> str:
        """原子追加状态指纹；顺序由 SQLite 单调 sequence 决定。"""
        return self._single("append_state_checkpoint", kwargs)

    def records_for_date(
        self, trading_date: date | datetime | str
    ) -> dict[str, list[dict[str, Any]]]:
        """读取某交易日的全部事实，供报告或机器处理使用。"""
        self._ensure_open()
        day = _iso_date(trading_date, "trading_date")
        assert day is not None
        result: dict[str, list[dict[str, Any]]] = {}
        result["runs"] = [
            dict(row)
            for row in self._connection.execute(
                "SELECT * FROM runs WHERE trading_date = ? "
                "ORDER BY COALESCE(occurred_at, recorded_at), id",
                (day,),
            ).fetchall()
        ]
        for table in _FACT_TABLES[1:]:
            rows = self._connection.execute(
                f"SELECT fact.* FROM {table} AS fact "  # noqa: S608 - 白名单表名
                "JOIN runs AS run ON run.id = fact.run_id "
                "WHERE run.trading_date = ? "
                "ORDER BY COALESCE(fact.occurred_at, fact.recorded_at), fact.id",
                (day,),
            ).fetchall()
            result[table] = [dict(row) for row in rows]
        return result

    def count_records(self, record_type: str) -> int:
        """返回事实表行数，主要用于健康检查和聚焦测试。"""
        self._ensure_open()
        if record_type not in _FACT_TABLES:
            raise ValueError(f"未知事实表: {record_type}")
        row = self._connection.execute(
            f"SELECT COUNT(*) FROM {record_type}"  # noqa: S608 - 白名单表名
        ).fetchone()
        return int(row[0])

    def latest_trading_date(self) -> str | None:
        """返回账本内最近交易日；空账本返回 ``None``。"""
        self._ensure_open()
        row = self._connection.execute(
            "SELECT MAX(trading_date) FROM runs"
        ).fetchone()
        return str(row[0]) if row and row[0] is not None else None

    def known_client_order_ids(self) -> set[str]:
        """返回账本中出现过的全部自有 client order ID。"""
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT DISTINCT client_order_id FROM orders"
        ).fetchall()
        return {str(row[0]) for row in rows}

    def list_pending_order_recoveries(self) -> list[PendingOrderRecovery]:
        """跨交易日识别尚未完成券商/本地状态交接的订单。

        订单表是只追加事件流，因此不能只看某一天或简单判断是否出现过
        ``filled``。对每个 client ID 取最后一条非 ``state_committed`` 生命周期
        事件：旧的不确定状态始终未决；``filled`` 必须有其后的、且明确引用
        该终态事件的 ``state_committed`` 才算完成本地交接。未知状态保守地
        视为未决。
        """
        self._ensure_open()
        rows = self._connection.execute(
            "SELECT rowid AS event_sequence, * FROM orders "
            "ORDER BY event_sequence"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["client_order_id"]), []).append(row)

        pending: list[PendingOrderRecovery] = []
        for client_order_id, events in grouped.items():
            lifecycle = [
                row for row in events if str(row["status"]).lower() != "state_committed"
            ]
            if not lifecycle:
                # 损坏或未来 schema 写入的孤立交接事件，不能默认为安全。
                latest = events[-1]
                pending.append(
                    self._pending_from_row(
                        latest,
                        reason="state_committed_without_lifecycle",
                    )
                )
                continue

            latest = lifecycle[-1]
            status = str(latest["status"]).lower()
            reason: str | None = None
            if status in UNRESOLVED_ORDER_STATUSES:
                reason = f"unresolved_{status}"
            elif status == "filled":
                committed = False
                latest_sequence = int(latest["event_sequence"])
                for event in events:
                    if int(event["event_sequence"]) <= latest_sequence:
                        continue
                    if str(event["status"]).lower() != "state_committed":
                        continue
                    try:
                        metadata = json.loads(str(event["metadata_json"]))
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if metadata.get("terminal_order_id") == latest["id"]:
                        committed = True
                        break
                if not committed:
                    reason = "filled_without_state_committed"
            elif status not in TRUSTED_NO_FILL_TERMINAL_STATUSES:
                reason = f"unknown_order_status:{status}"

            if reason is not None:
                pending.append(self._pending_from_row(latest, reason=reason))

        return pending

    def latest_state_checkpoint(self) -> StateCheckpoint | None:
        """读取最近的状态哈希；最新必填事件损坏时绝不回退旧指纹。

        ``completed`` 与 ``state_committed`` 是明确宣称本地状态已交接的事件，
        因此必须携带合法哈希。其他运行终态只有显式携带 ``state_sha256`` 时
        才代表状态已经落盘，例如状态保存后通知失败产生的 ``failed``。
        """
        self._ensure_open()
        checkpoint = self._connection.execute(
            "SELECT source, state_sha256, recorded_at FROM state_checkpoints "
            "ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if checkpoint is not None:
            return StateCheckpoint(
                state_sha256=str(checkpoint["state_sha256"]).lower(),
                recorded_at=str(checkpoint["recorded_at"]),
                source=str(checkpoint["source"]),
            )

        # v1 账本没有单调 checkpoint 表，只用于首次迁移时的保守识别。
        # 一旦写入首条 v2 checkpoint，后续顺序完全由 sequence 决定。
        rows = self._connection.execute(
            "SELECT source, status, metadata_json, recorded_at FROM ("
            "SELECT 'run' AS source, lower(status) AS status, "
            "metadata_json, recorded_at FROM runs "
            "WHERE lower(status) != 'started' "
            "UNION ALL "
            "SELECT 'order' AS source, lower(status) AS status, "
            "metadata_json, recorded_at FROM orders "
            "WHERE lower(status) = 'state_committed'"
            ") ORDER BY recorded_at DESC"
        ).fetchall()
        for row in rows:
            required = (
                str(row["source"]) == "order"
                or str(row["status"]) == "completed"
            )
            try:
                metadata = json.loads(str(row["metadata_json"]))
            except (TypeError, json.JSONDecodeError):
                if required:
                    raise InvalidStateCheckpointError(
                        "最近状态交接事件的 metadata_json 无法解析"
                    )
                continue
            digest = metadata.get("state_sha256") if isinstance(metadata, dict) else None
            if digest is None and not required:
                continue
            if not (
                isinstance(digest, str)
                and len(digest) == 64
                and all(char in "0123456789abcdef" for char in digest.lower())
            ):
                raise InvalidStateCheckpointError(
                    "最近状态交接事件缺少完整 state_sha256"
                )
            return StateCheckpoint(
                state_sha256=digest.lower(),
                recorded_at=str(row["recorded_at"]),
                source=str(row["source"]),
            )
        return None

    @staticmethod
    def _pending_from_row(
        row: sqlite3.Row,
        *,
        reason: str,
    ) -> PendingOrderRecovery:
        return PendingOrderRecovery(
            client_order_id=str(row["client_order_id"]),
            run_id=str(row["run_id"]),
            decision_id=(
                str(row["decision_id"]) if row["decision_id"] is not None else None
            ),
            broker_order_id=(
                str(row["broker_order_id"])
                if row["broker_order_id"] is not None
                else None
            ),
            symbol=str(row["symbol"]),
            side=str(row["side"]),
            quantity=int(row["quantity"]),
            latest_status=str(row["status"]).lower(),
            latest_event_id=str(row["id"]),
            reason=reason,
        )

    def close(self) -> None:
        if not self._closed:
            if self._transaction_open:
                raise RuntimeError("不能在写入事务中关闭账本")
            self._connection.close()
            self._closed = True

    def __enter__(self) -> ReviewJournal:
        self._ensure_open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class JournalWriter:
    """仅在 ``ReviewJournal.transaction`` 上下文中有效的追加器。"""

    def __init__(self, journal: ReviewJournal):
        self._journal = journal
        self._active = True

    @property
    def _connection(self) -> sqlite3.Connection:
        if not self._active:
            raise RuntimeError("事务已经结束，writer 不再可用")
        return self._journal._connection

    def _insert(
        self,
        table: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> str:
        if table not in _APPEND_ONLY_TABLES:
            raise ValueError(f"未知事实表: {table}")
        key = _nonempty(idempotency_key, "idempotency_key")
        record_id = stable_record_id(table, key)
        digest = _payload_hash(payload)
        values = {
            "id": record_id,
            "idempotency_key": key,
            **payload,
            "payload_hash": digest,
            "recorded_at": _utc_now(),
        }
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        sql = (
            f"INSERT INTO {table} ({', '.join(columns)}) "  # noqa: S608 - 白名单表名
            f"VALUES ({placeholders}) "
            "ON CONFLICT(idempotency_key) DO NOTHING"
        )
        cursor = self._connection.execute(sql, tuple(values[column] for column in columns))
        if cursor.rowcount == 1:
            return record_id
        existing = self._connection.execute(
            f"SELECT id, payload_hash FROM {table} WHERE idempotency_key = ?",  # noqa: S608
            (key,),
        ).fetchone()
        if existing is None or existing["payload_hash"] != digest:
            raise IdempotencyConflictError(
                f"{table} 的幂等键 {key!r} 已对应不同内容"
            )
        return str(existing["id"])

    def append_state_checkpoint(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        state_sha256: str,
        source: str = "live_state",
    ) -> str:
        """追加状态文件指纹；AUTOINCREMENT sequence 是唯一的新旧顺序。"""
        digest = _nonempty(state_sha256, "state_sha256").lower()
        if len(digest) != 64 or any(
            char not in "0123456789abcdef" for char in digest
        ):
            raise ValueError("state_sha256 必须是完整 SHA-256")
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "state_sha256": digest,
            "source": _nonempty(source, "source"),
        }
        return self._insert("state_checkpoints", idempotency_key, payload)

    def append_run(
        self,
        *,
        idempotency_key: str,
        trading_date: date | datetime | str,
        mode: str,
        status: str,
        signal_date: date | datetime | str | None = None,
        data_as_of: date | datetime | str | None = None,
        data_status: str = "unknown",
        code_version: str | None = None,
        config_hash: str | None = None,
        data_hash: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """记录一次运行；状态变化应使用新的幂等键追加新运行事件。"""
        payload = {
            "trading_date": _iso_date(trading_date, "trading_date"),
            "mode": _nonempty(mode, "mode"),
            "status": _nonempty(status, "status"),
            "signal_date": _iso_date(signal_date, "signal_date"),
            "data_as_of": _iso_date(data_as_of, "data_as_of"),
            "data_status": _nonempty(data_status, "data_status"),
            "code_version": _optional_text(code_version, "code_version"),
            "config_hash": _optional_text(config_hash, "config_hash"),
            "data_hash": _optional_text(data_hash, "data_hash"),
            "metadata_json": _json_object(metadata, "metadata"),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("runs", idempotency_key, payload)

    def append_decision(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        symbol: str,
        action: str,
        outcome: str,
        reason_code: str,
        evidence: Mapping[str, Any],
        intended_quantity: int | None = None,
        reference_price: int | float | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """记录决策及当时的非空价格行为证据快照。"""
        if intended_quantity is not None:
            intended_quantity = _integer(
                intended_quantity, "intended_quantity", minimum=0
            )
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "symbol": _symbol(symbol),
            "action": _nonempty(action, "action"),
            "outcome": _nonempty(outcome, "outcome"),
            "reason_code": _nonempty(reason_code, "reason_code"),
            "intended_quantity": intended_quantity,
            "reference_price": _finite_number(
                reference_price, "reference_price", minimum=0
            ),
            "evidence_json": _json_object(evidence, "evidence", required=True),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("decisions", idempotency_key, payload)

    def append_order(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        client_order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        status: str,
        decision_id: str | None = None,
        broker_order_id: str | None = None,
        order_type: str = "market",
        reference_price: int | float | None = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """追加订单事件；``client_order_id`` 应在券商请求中原样使用。"""
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "decision_id": _optional_text(decision_id, "decision_id"),
            "client_order_id": _nonempty(client_order_id, "client_order_id"),
            "broker_order_id": _optional_text(broker_order_id, "broker_order_id"),
            "symbol": _symbol(symbol),
            "side": _nonempty(side, "side").lower(),
            "quantity": _integer(quantity, "quantity", minimum=1),
            "order_type": _nonempty(order_type, "order_type"),
            "status": _nonempty(status, "status"),
            "reference_price": _finite_number(
                reference_price, "reference_price", minimum=0
            ),
            "metadata_json": _json_object(metadata, "metadata"),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("orders", idempotency_key, payload)

    def append_fill(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        order_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: int | float,
        reference_price: int | float | None = None,
        slippage_bps: int | float | None = None,
        commission: int | float = 0.0,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """追加真实成交；未给滑点时按参考价和买卖方向自动计算。"""
        normalized_side = _nonempty(side, "side").lower()
        normalized_price = _finite_number(price, "price", minimum=0)
        normalized_reference = _finite_number(
            reference_price, "reference_price", minimum=0
        )
        if slippage_bps is None and normalized_reference not in (None, 0):
            direction = 1.0 if normalized_side == "buy" else -1.0
            slippage_bps = (
                direction
                * (float(normalized_price) - float(normalized_reference))
                / float(normalized_reference)
                * 10_000
            )
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "order_id": _nonempty(order_id, "order_id"),
            "symbol": _symbol(symbol),
            "side": normalized_side,
            "quantity": _integer(quantity, "quantity", minimum=1),
            "price": normalized_price,
            "reference_price": normalized_reference,
            "slippage_bps": _finite_number(slippage_bps, "slippage_bps"),
            "commission": _finite_number(commission, "commission", minimum=0),
            "metadata_json": _json_object(metadata, "metadata"),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("fills", idempotency_key, payload)

    def append_position_event(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        symbol: str,
        event_type: str,
        quantity_delta: int,
        quantity_after: int,
        price: int | float | None = None,
        decision_id: str | None = None,
        order_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """追加建仓、加仓、止损移动、拆股或平仓等持仓事件。"""
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "decision_id": _optional_text(decision_id, "decision_id"),
            "order_id": _optional_text(order_id, "order_id"),
            "symbol": _symbol(symbol),
            "event_type": _nonempty(event_type, "event_type"),
            "quantity_delta": _integer(quantity_delta, "quantity_delta"),
            "quantity_after": _integer(quantity_after, "quantity_after"),
            "price": _finite_number(price, "price", minimum=0),
            "metadata_json": _json_object(metadata, "metadata"),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("position_events", idempotency_key, payload)

    def append_equity_snapshot(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        equity: int | float,
        cash: int | float,
        buying_power: int | float,
        position_count: int,
        market_level: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """追加账户权益快照。"""
        if market_level is not None:
            market_level = _integer(market_level, "market_level")
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "equity": _finite_number(equity, "equity", minimum=0),
            "cash": _finite_number(cash, "cash"),
            "buying_power": _finite_number(buying_power, "buying_power"),
            "position_count": _integer(position_count, "position_count", minimum=0),
            "market_level": market_level,
            "metadata_json": _json_object(metadata, "metadata"),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("equity_snapshots", idempotency_key, payload)

    def append_reconciliation(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        category: str,
        status: str,
        expected: Any,
        actual: Any,
        symbol: str | None = None,
        details: Mapping[str, Any] | None = None,
        occurred_at: datetime | str | None = None,
    ) -> str:
        """追加持仓、订单、现金或止损的严格对账结果。"""
        payload = {
            "run_id": _nonempty(run_id, "run_id"),
            "category": _nonempty(category, "category"),
            "status": _nonempty(status, "status"),
            "symbol": None if symbol is None else _symbol(symbol),
            "expected_json": _json(expected, "expected"),
            "actual_json": _json(actual, "actual"),
            "details_json": _json_object(details, "details"),
            "occurred_at": _iso_timestamp(occurred_at, "occurred_at"),
        }
        return self._insert("reconciliations", idempotency_key, payload)
