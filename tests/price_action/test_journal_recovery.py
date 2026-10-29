"""跨日订单事件流的启动恢复判定。"""

from __future__ import annotations

import pytest

import quantbot.price_action.review.journal as journal_mod
from quantbot.price_action.review import ReviewJournal


def _run(journal: ReviewJournal, day: str, suffix: str) -> str:
    return journal.append_run(
        idempotency_key=f"sim:{day}:{suffix}:started",
        trading_date=day,
        mode="sim",
        status="started",
        data_status="fresh",
    )


def _order(
    journal: ReviewJournal,
    *,
    run_id: str,
    client_order_id: str,
    status: str,
    sequence: int,
    metadata: dict | None = None,
) -> str:
    return journal.append_order(
        idempotency_key=f"{client_order_id}:{sequence}:{status}",
        run_id=run_id,
        client_order_id=client_order_id,
        broker_order_id=f"broker-{client_order_id}",
        symbol="TST",
        side="buy",
        quantity=10,
        status=status,
        reference_price=100.0,
        metadata=metadata,
    )


@pytest.mark.parametrize(
    "status",
    ["planned", "submitted", "uncertain", "submit_error", "partial", "dead"],
)
def test_legacy_unresolved_statuses_remain_pending(tmp_path, status):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        run_id = _run(journal, "2026-08-07", status)
        _order(
            journal,
            run_id=run_id,
            client_order_id=f"client-{status}",
            status=status,
            sequence=1,
        )

        pending = journal.list_pending_order_recoveries()

    assert [(item.client_order_id, item.latest_status) for item in pending] == [
        (f"client-{status}", status)
    ]


def test_filled_requires_later_state_committed_for_same_terminal_event(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        run_id = _run(journal, "2026-08-07", "filled")
        terminal_id = _order(
            journal,
            run_id=run_id,
            client_order_id="client-filled",
            status="filled",
            sequence=1,
        )
        assert journal.list_pending_order_recoveries()[0].reason \
            == "filled_without_state_committed"

        _order(
            journal,
            run_id=run_id,
            client_order_id="client-filled",
            status="state_committed",
            sequence=2,
            metadata={
                "terminal_order_id": terminal_id,
                "terminal_status": "filled",
                "state_sha256": "a" * 64,
            },
        )

        assert journal.list_pending_order_recoveries() == []
        checkpoint = journal.latest_state_checkpoint()
        assert checkpoint is not None
        assert checkpoint.state_sha256 == "a" * 64
        assert checkpoint.source == "order"


def test_pending_query_is_cross_day_and_trusted_cancel_is_resolved(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        old_run = _run(journal, "2026-08-06", "old")
        new_run = _run(journal, "2026-08-07", "new")
        _order(
            journal,
            run_id=old_run,
            client_order_id="old-submitted",
            status="submitted",
            sequence=1,
        )
        _order(
            journal,
            run_id=new_run,
            client_order_id="new-canceled",
            status="canceled",
            sequence=1,
        )

        pending = journal.list_pending_order_recoveries()

    assert [item.client_order_id for item in pending] == ["old-submitted"]


def test_failed_run_with_persisted_state_becomes_latest_checkpoint(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        journal.append_run(
            idempotency_key="sim:2026-08-07:failed",
            trading_date="2026-08-07",
            mode="sim",
            status="failed",
            data_status="unknown",
            metadata={"state_sha256": "b" * 64},
        )

        checkpoint = journal.latest_state_checkpoint()

    assert checkpoint is not None
    assert checkpoint.state_sha256 == "b" * 64
    assert checkpoint.source == "run"


def test_state_checkpoint_uses_logical_sequence_not_wall_clock(tmp_path, monkeypatch):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        run_id = _run(journal, "2026-08-07", "clock-rollback")
        timestamps = iter(
            ["2026-08-07T10:00:02Z", "2026-08-07T10:00:01Z"]
        )
        monkeypatch.setattr(journal_mod, "_utc_now", lambda: next(timestamps))
        journal.append_state_checkpoint(
            idempotency_key="checkpoint-a",
            run_id=run_id,
            state_sha256="a" * 64,
        )
        journal.append_state_checkpoint(
            idempotency_key="checkpoint-b",
            run_id=run_id,
            state_sha256="b" * 64,
        )

        checkpoint = journal.latest_state_checkpoint()

    assert checkpoint is not None
    assert checkpoint.state_sha256 == "b" * 64
    assert checkpoint.recorded_at == "2026-08-07T10:00:01Z"
