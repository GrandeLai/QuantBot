"""SQLite 复盘证据账本：事务、幂等、只追加与完整事件链。"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from quantbot.price_action.review import (
    SCHEMA_VERSION,
    IdempotencyConflictError,
    ReviewJournal,
    stable_client_order_id,
    stable_record_id,
)


def _run_args(**overrides):
    values = {
        "idempotency_key": "paper:2026-08-07:close:completed",
        "trading_date": "2026-08-07",
        "mode": "paper",
        "status": "completed",
        "signal_date": "2026-08-06",
        "data_as_of": "2026-08-06",
        "data_status": "fresh",
        "code_version": "abc123",
        "config_hash": "cfg-sha256",
        "data_hash": "data-sha256",
        "metadata": {"market": "US", "level": 1},
        "occurred_at": "2026-08-07T20:05:00Z",
    }
    values.update(overrides)
    return values


def test_initializes_versioned_wal_schema_and_stable_ids(tmp_path):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path) as journal:
        assert journal.latest_trading_date() is None
        assert journal.schema_version == SCHEMA_VERSION
        assert journal.journal_mode.lower() == "wal"
        assert stable_record_id("runs", "same-key") == stable_record_id(
            "runs", "same-key"
        )
        assert stable_record_id("runs", "same-key") != stable_record_id(
            "orders", "same-key"
        )
        uuid.UUID(stable_client_order_id("decision-id", 2))

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert version == SCHEMA_VERSION
    assert {
        "runs",
        "decisions",
        "orders",
        "fills",
        "position_events",
        "equity_snapshots",
        "reconciliations",
        "state_checkpoints",
    } <= names


def test_readonly_journal_reads_existing_facts_and_rejects_writes(tmp_path):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path) as journal:
        journal.append_run(**_run_args())

    with ReviewJournal(path, readonly=True) as journal:
        assert journal.latest_trading_date() == "2026-08-07"
        with pytest.raises(RuntimeError, match="只读账本禁止写入"):
            journal.append_run(
                **_run_args(idempotency_key="readonly-write-attempt")
            )


def test_readonly_journal_does_not_create_missing_database(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError, match="只读账本不存在"):
        ReviewJournal(path, readonly=True)
    assert not path.exists()


def test_writable_open_migrates_v1_schema_before_new_checkpoints(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY, "
            "applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_version VALUES (1, '2026-08-01T00:00:00Z')"
        )
        connection.execute("PRAGMA user_version = 1")

    with ReviewJournal(path) as journal:
        assert journal.schema_version == SCHEMA_VERSION

    with sqlite3.connect(path) as connection:
        versions = connection.execute(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall()
        checkpoint_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='state_checkpoints'"
        ).fetchone()
    assert versions == [(1,), (2,)]
    assert checkpoint_table == (1,)


def test_transaction_records_complete_price_action_evidence_chain(tmp_path):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path) as journal:
        with journal.transaction() as tx:
            run_id = tx.append_run(**_run_args())
            decision_id = tx.append_decision(
                idempotency_key="decision:2026-08-07:AAPL:enter",
                run_id=run_id,
                symbol="aapl",
                action="enter",
                outcome="accepted",
                reason_code="BREAKOUT_55D",
                intended_quantity=20,
                reference_price=100.0,
                evidence={
                    "source": "price_action",
                    "trigger": "breakout",
                    "atr": 2.5,
                    "signal_bar_date": "2026-08-06",
                },
                occurred_at="2026-08-07T20:06:00+00:00",
            )
            client_order_id = stable_client_order_id(decision_id)
            order_id = tx.append_order(
                idempotency_key="order:AAPL:submitted:1",
                run_id=run_id,
                decision_id=decision_id,
                client_order_id=client_order_id,
                broker_order_id="broker-1",
                symbol="AAPL",
                side="buy",
                quantity=20,
                status="filled",
                reference_price=100.0,
            )
            tx.append_fill(
                idempotency_key="fill:broker-1:1",
                run_id=run_id,
                order_id=order_id,
                symbol="AAPL",
                side="buy",
                quantity=20,
                price=100.05,
                reference_price=100.0,
                commission=0.2,
            )
            tx.append_position_event(
                idempotency_key="position:AAPL:opened:1",
                run_id=run_id,
                decision_id=decision_id,
                order_id=order_id,
                symbol="AAPL",
                event_type="opened",
                quantity_delta=20,
                quantity_after=20,
                price=100.05,
                metadata={"stop": 94.0},
            )
            tx.append_equity_snapshot(
                idempotency_key="equity:2026-08-07:close",
                run_id=run_id,
                equity=100_100,
                cash=98_099,
                buying_power=98_099,
                position_count=1,
                market_level=1,
            )
            tx.append_reconciliation(
                idempotency_key="reconcile:2026-08-07:AAPL",
                run_id=run_id,
                category="position",
                status="matched",
                symbol="AAPL",
                expected={"quantity": 20},
                actual={"quantity": 20},
            )

        records = journal.records_for_date("2026-08-07")
        assert {name: len(rows) for name, rows in records.items()} == {
            "runs": 1,
            "decisions": 1,
            "orders": 1,
            "fills": 1,
            "position_events": 1,
            "equity_snapshots": 1,
            "reconciliations": 1,
        }
        assert records["decisions"][0]["symbol"] == "AAPL"
        evidence = json.loads(records["decisions"][0]["evidence_json"])
        assert evidence["source"] == "price_action"
        assert records["fills"][0]["slippage_bps"] == pytest.approx(5.0)


def test_idempotent_retry_returns_same_uuid_and_conflict_fails(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        first = journal.append_run(**_run_args())
        assert journal.latest_trading_date() == "2026-08-07"
        retried = journal.append_run(**_run_args())
        assert retried == first == stable_record_id(
            "runs", "paper:2026-08-07:close:completed"
        )
        assert journal.count_records("runs") == 1

        with pytest.raises(IdempotencyConflictError, match="已对应不同内容"):
            journal.append_run(**_run_args(status="failed"))
        assert journal.count_records("runs") == 1


def test_transaction_rolls_back_and_database_rejects_rewrite(tmp_path):
    path = tmp_path / "journal.sqlite"
    with ReviewJournal(path) as journal:
        with pytest.raises(ValueError, match="evidence 必须是非空对象"):
            with journal.transaction() as tx:
                run_id = tx.append_run(**_run_args())
                tx.append_decision(
                    idempotency_key="decision:no-evidence",
                    run_id=run_id,
                    symbol="AAPL",
                    action="enter",
                    outcome="accepted",
                    reason_code="BREAKOUT_55D",
                    evidence={},
                )
        assert journal.count_records("runs") == 0
        run_id = journal.append_run(**_run_args())

        with sqlite3.connect(path, timeout=1.0) as connection:
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                connection.execute(
                    "UPDATE runs SET status = 'rewritten' WHERE id = ?", (run_id,)
                )
            with pytest.raises(sqlite3.DatabaseError, match="append-only"):
                connection.execute("DELETE FROM runs WHERE id = ?", (run_id,))


def test_child_record_requires_existing_run_and_times_are_unambiguous(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        with pytest.raises(sqlite3.IntegrityError):
            journal.append_equity_snapshot(
                idempotency_key="orphan-equity",
                run_id="missing-run",
                equity=100,
                cash=100,
                buying_power=100,
                position_count=0,
            )
        with pytest.raises(ValueError, match="必须包含时区"):
            journal.append_run(
                **_run_args(occurred_at="2026-08-07T20:05:00")
            )


def test_order_cannot_link_decision_from_another_run(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        first_run = journal.append_run(**_run_args())
        second_run = journal.append_run(
            **_run_args(
                idempotency_key="paper:2026-08-08:close:completed",
                trading_date="2026-08-08",
            )
        )
        decision_id = journal.append_decision(
            idempotency_key="decision:first-run:AAPL",
            run_id=first_run,
            symbol="AAPL",
            action="enter",
            outcome="accepted",
            reason_code="BREAKOUT_55D",
            evidence={"source": "price_action", "trigger": "breakout"},
        )

        with pytest.raises(sqlite3.IntegrityError):
            journal.append_order(
                idempotency_key="order:wrong-run",
                run_id=second_run,
                decision_id=decision_id,
                client_order_id=stable_client_order_id(decision_id),
                symbol="AAPL",
                side="buy",
                quantity=10,
                status="submitted",
            )
