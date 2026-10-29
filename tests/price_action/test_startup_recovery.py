"""启动恢复门禁：未知或活跃副作用必须在策略前冻结。"""

from __future__ import annotations

import hashlib

import pytest

import quantbot.price_action.live.runner as runner_mod
from quantbot.price_action.broker.base import BrokerOrderSnapshot
from quantbot.price_action.live.recovery import HOLD, SAFE, RecoveryHoldError, inspect_startup_recovery
from quantbot.price_action.live.runner import LiveRunner
from quantbot.price_action.review import ReviewJournal

from .conftest import MockBroker, make_cfg, mock_runtime_paths


def _enable_review(cfg: dict, tmp_path) -> str:
    path = str(tmp_path / "journal.sqlite")
    cfg["review"] = {
        "enabled": True,
        "journal_path": path,
        "candidate_log_limit": 10,
        "paper_min_trading_days": 60,
    }
    return str(mock_runtime_paths(cfg, "sim").journal_file)


def _append_order(
    journal: ReviewJournal,
    *,
    client_order_id: str,
    status: str,
) -> str:
    run_id = journal.append_run(
        idempotency_key=f"sim:2026-08-07:{client_order_id}:started",
        trading_date="2026-08-07",
        mode="sim",
        status="started",
        data_status="fresh",
    )
    return journal.append_order(
        idempotency_key=f"{client_order_id}:{status}",
        run_id=run_id,
        client_order_id=client_order_id,
        broker_order_id=f"broker-{client_order_id}",
        symbol="TST",
        side="buy",
        quantity=10,
        status=status,
    )


def test_empty_journal_and_empty_broker_are_safe(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        report = inspect_startup_recovery(journal, MockBroker())

    assert report.status == SAFE
    assert report.issues == ()


def test_owned_working_order_and_external_working_order_hold(tmp_path):
    broker = MockBroker()
    owned_id = "owned-client"
    broker.market_buy("TST", 10, owned_id)
    broker.external_open_orders.append(
        BrokerOrderSnapshot(
            order_id="manual-1",
            client_order_id="manual-client",
            symbol="MANUAL",
            side="sell",
            quantity=2,
            filled_quantity=0,
            filled_avg_price=None,
            status="new",
        )
    )
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        _append_order(journal, client_order_id=owned_id, status="submitted")
        report = inspect_startup_recovery(journal, broker)

    assert report.status == HOLD
    assert {issue.code for issue in report.issues} >= {
        "owned_order_open",
        "owned_open_order",
        "external_open_order",
    }


def test_filled_without_state_commit_holds_even_when_broker_is_terminal(tmp_path):
    broker = MockBroker()
    client_id = "filled-client"
    broker_order_id = broker.market_buy("TST", 10, client_id)
    broker.fill_results[broker_order_id] = (10, 101.0)
    assert broker.wait_order(broker_order_id)
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        _append_order(journal, client_order_id=client_id, status="filled")
        report = inspect_startup_recovery(journal, broker)

    assert report.status == HOLD
    assert report.pending_orders[0].reason == "filled_without_state_committed"
    assert {issue.code for issue in report.issues} == {
        "owned_order_local_handoff_pending"
    }


def test_broker_read_failure_is_hold_not_empty_account(tmp_path):
    class UnavailableBroker(MockBroker):
        def list_open_orders(self):
            raise ConnectionError("network down")

    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        report = inspect_startup_recovery(journal, UnavailableBroker())

    assert report.status == HOLD
    assert report.issues[0].code == "open_orders_read_unknown"


def test_state_file_without_checkpoint_holds(tmp_path):
    state_path = tmp_path / "live_state.json"
    state_path.write_text('{"meta": {}, "positions": {}}', encoding="utf-8")
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        report = inspect_startup_recovery(
            journal,
            MockBroker(),
            state_path=state_path,
        )

    assert report.status == HOLD
    assert {issue.code for issue in report.issues} == {"state_checkpoint_missing"}


def test_state_checkpoint_detects_offline_rollback(tmp_path):
    state_path = tmp_path / "live_state.json"
    payload = b'{"meta": {"last_run_date": "2026-08-07"}, "positions": {}}'
    state_path.write_bytes(payload)
    expected_hash = hashlib.sha256(payload).hexdigest()
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        journal.append_run(
            idempotency_key="sim:2026-08-07:completed",
            trading_date="2026-08-07",
            mode="sim",
            status="completed",
            data_status="fresh",
            metadata={"state_sha256": expected_hash},
        )
        safe = inspect_startup_recovery(
            journal,
            MockBroker(),
            state_path=state_path,
        )
        state_path.write_text('{"meta": {}, "positions": {}}', encoding="utf-8")
        rolled_back = inspect_startup_recovery(
            journal,
            MockBroker(),
            state_path=state_path,
        )

    assert safe.status == SAFE
    assert rolled_back.status == HOLD
    assert {issue.code for issue in rolled_back.issues} == {
        "state_checkpoint_mismatch"
    }


def test_legacy_state_commit_without_hash_holds_even_when_state_is_missing(tmp_path):
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        run_id = journal.append_run(
            idempotency_key="legacy:started",
            trading_date="2026-08-07",
            mode="sim",
            status="started",
            data_status="fresh",
        )
        terminal_id = journal.append_order(
            idempotency_key="legacy:filled",
            run_id=run_id,
            client_order_id="legacy-client",
            broker_order_id="legacy-order",
            symbol="TST",
            side="buy",
            quantity=10,
            status="filled",
        )
        journal.append_order(
            idempotency_key="legacy:state-committed",
            run_id=run_id,
            client_order_id="legacy-client",
            broker_order_id="legacy-order",
            symbol="TST",
            side="buy",
            quantity=10,
            status="state_committed",
            metadata={"terminal_order_id": terminal_id},
        )
        report = inspect_startup_recovery(
            journal,
            MockBroker(),
            state_path=tmp_path / "missing-state.json",
        )

    assert report.status == HOLD
    assert {issue.code for issue in report.issues} == {"state_checkpoint_invalid"}


def test_invalid_new_checkpoint_never_falls_back_to_older_valid_hash(tmp_path):
    state_path = tmp_path / "live_state.json"
    old_payload = b'{"meta": {}, "positions": {}}'
    state_path.write_bytes(old_payload)
    with ReviewJournal(tmp_path / "journal.sqlite") as journal:
        journal.append_run(
            idempotency_key="old:completed",
            trading_date="2026-08-06",
            mode="sim",
            status="completed",
            data_status="fresh",
            metadata={"state_sha256": hashlib.sha256(old_payload).hexdigest()},
            occurred_at="2026-08-06T21:00:00Z",
        )
        journal.append_run(
            idempotency_key="new:completed",
            trading_date="2026-08-07",
            mode="sim",
            status="completed",
            data_status="fresh",
            metadata={},
            occurred_at="2026-08-07T21:00:00Z",
        )
        report = inspect_startup_recovery(
            journal,
            MockBroker(),
            state_path=state_path,
        )

    assert report.status == HOLD
    assert {issue.code for issue in report.issues} == {"state_checkpoint_invalid"}


@pytest.mark.parametrize("universe_mode", ["static", "sp500_pit"])
def test_runner_hold_precedes_data_refresh_and_force_cannot_bypass(
    tmp_path, monkeypatch, universe_mode
):
    cfg = make_cfg(tmp_path)
    cfg["universe"]["mode"] = universe_mode
    journal_path = _enable_review(cfg, tmp_path)
    with ReviewJournal(journal_path) as journal:
        _append_order(journal, client_order_id="orphan-planned", status="planned")

    refreshed = False

    def forbidden_refresh(*args, **kwargs):
        nonlocal refreshed
        refreshed = True
        raise AssertionError("恢复 HOLD 后不应刷新行情")

    monkeypatch.setattr(runner_mod, "load_market_data", forbidden_refresh)
    monkeypatch.setattr(runner_mod, "load_all", forbidden_refresh)
    monkeypatch.setattr(runner_mod, "load_all_batch", forbidden_refresh)
    monkeypatch.setattr(runner_mod, "notify", lambda text: True)
    broker = MockBroker()

    with pytest.raises(RecoveryHoldError, match="启动恢复门禁 HOLD"):
        LiveRunner(broker, cfg, mode="sim").run_once(force=True)

    assert not refreshed
    assert broker.orders == []


def test_runner_recovery_precedes_closed_market_skip(tmp_path, monkeypatch):
    """休市不能把历史未知订单隐藏成一次看似正常的跳过。"""
    cfg = make_cfg(tmp_path)
    journal_path = _enable_review(cfg, tmp_path)
    with ReviewJournal(journal_path) as journal:
        _append_order(journal, client_order_id="weekend-orphan", status="planned")

    class ClosedMarketBroker(MockBroker):
        def is_market_open(self) -> bool:
            raise AssertionError("恢复 HOLD 应在交易日检查之前发生")

    monkeypatch.setattr(runner_mod, "notify", lambda text: True)
    with pytest.raises(RecoveryHoldError, match="启动恢复门禁 HOLD"):
        LiveRunner(ClosedMarketBroker(), cfg, mode="sim").run_once(force=False)
