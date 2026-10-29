"""LiveRunner 与只追加复盘账本的集成测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import quantbot.price_action.live.runner as runner_mod
from quantbot.price_action.config import config_hash
from quantbot.price_action.governance import source_fingerprint
from quantbot.price_action.live.closing import collect_paper_close
from quantbot.price_action.live.recovery import RecoveryHoldError
from quantbot.price_action.live.runner import LiveRunner
from quantbot.price_action.review import PaperCampaignSpec, ReviewJournal, audit_paper_campaign
from quantbot.price_action.strategy.trend_breakout import EntrySignal

from .conftest import TEST_NOW, MockBroker, make_cfg, mock_runtime_paths


def _enable_review(cfg: dict, tmp_path, mode: str = "sim") -> str:
    path = str(tmp_path / "trading_journal.sqlite")
    cfg["review"] = {
        "enabled": True,
        "journal_path": path,
        "candidate_log_limit": 10,
        "paper_min_trading_days": 60,
    }
    return str(mock_runtime_paths(cfg, mode).journal_file)


def _force_entry(monkeypatch) -> None:
    monkeypatch.setattr(runner_mod, "UNIVERSE", ["TST"])
    monkeypatch.setattr(
        runner_mod,
        "entry_signal",
        lambda row, level, cfg, bench_mom=None: EntrySignal(
            rank=1.0,
            stop=50.0,
            trigger="breakout",
        ),
    )
    monkeypatch.setattr(runner_mod, "notify", lambda _: True)
    monkeypatch.setattr(runner_mod, "heartbeat", lambda: True)


def test_entry_writes_complete_price_action_evidence_chain(
    tmp_path, market_data, monkeypatch
):
    cfg = make_cfg(tmp_path)
    journal_path = _enable_review(cfg, tmp_path)
    _force_entry(monkeypatch)
    broker = MockBroker()
    broker.fill_results["order-1"] = (12, 123.45)

    LiveRunner(broker, cfg, mode="sim").run_once(force=True, data=market_data)

    day = TEST_NOW.date().isoformat()
    signal_day = str(market_data["SPY"].index[-1].date())
    with ReviewJournal(journal_path) as journal:
        records = journal.records_for_date(day)

    assert {row["status"] for row in records["runs"]} == {"started", "completed"}
    assert any(row["reason_code"] == "price_action_breakout"
               for row in records["decisions"])
    assert {row["status"] for row in records["orders"]} == {
        "planned", "submitted", "filled", "state_committed",
    }
    assert records["fills"][0]["quantity"] == 12
    assert records["fills"][0]["price"] == 123.45
    assert records["position_events"][-1]["event_type"] == "opened"
    assert records["equity_snapshots"]
    assert broker.client_order_ids[0]
    evidence = [
        json.loads(row["evidence_json"])
        for row in records["decisions"]
        if row["reason_code"] == "price_action_breakout"
    ][0]
    assert evidence["signal_bar_date"] == signal_day
    assert evidence["values"]["trigger"] == "breakout"


def test_restart_after_submit_enters_hold_without_duplicate_order(
    tmp_path, market_data, monkeypatch
):
    cfg = make_cfg(tmp_path)
    journal_path = _enable_review(cfg, tmp_path)
    _force_entry(monkeypatch)

    class CrashOnceBroker(MockBroker):
        crashed = False

        def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("模拟提交后进程崩溃")
            return True

    broker = CrashOnceBroker()
    try:
        LiveRunner(broker, cfg, mode="sim").run_once(force=True, data=market_data)
    except RuntimeError as exc:
        assert "模拟提交后进程崩溃" in str(exc)
    else:  # pragma: no cover - 明确保护测试前提
        raise AssertionError("第一次运行应模拟崩溃")

    with pytest.raises(RecoveryHoldError, match="启动恢复门禁 HOLD"):
        LiveRunner(broker, cfg, mode="sim").run_once(
            force=True,
            data=market_data,
        )

    assert broker.orders == [("buy", "TST", broker.orders[0][2])]
    assert len(broker.client_order_ids) == 1
    day = TEST_NOW.date().isoformat()
    with ReviewJournal(journal_path) as journal:
        records = journal.records_for_date(day)
    assert {row["status"] for row in records["runs"]} == {
        "started", "failed",
    }
    assert len([row for row in records["orders"] if row["status"] == "submitted"]) == 1


def test_filled_before_state_save_stays_pending_and_blocks_restart(
    tmp_path, market_data, monkeypatch
):
    cfg = make_cfg(tmp_path)
    journal_path = _enable_review(cfg, tmp_path)
    _force_entry(monkeypatch)
    broker = MockBroker()
    broker.fill_results["order-1"] = (12, 123.45)

    def crash_before_atomic_state_commit(self, meta, positions):
        raise RuntimeError("模拟成交后、状态落盘前崩溃")

    monkeypatch.setattr(LiveRunner, "_save_state", crash_before_atomic_state_commit)
    with pytest.raises(RuntimeError, match="状态落盘前崩溃"):
        LiveRunner(broker, cfg, mode="sim").run_once(
            force=True,
            data=market_data,
        )

    with ReviewJournal(journal_path) as journal:
        pending = journal.list_pending_order_recoveries()
    assert len(pending) == 1
    assert pending[0].latest_status == "filled"
    assert pending[0].reason == "filled_without_state_committed"

    with pytest.raises(RecoveryHoldError, match="启动恢复门禁 HOLD"):
        LiveRunner(broker, cfg, mode="sim").run_once(
            force=True,
            data=market_data,
        )
    assert len(broker.orders) == 1


def test_registered_paper_run_emits_auditable_campaign_identity(
    tmp_path, market_data, monkeypatch, market_clock
):
    cfg = make_cfg(tmp_path)
    journal_path = _enable_review(cfg, tmp_path, mode="paper")
    cfg["broker"] = {"provider": "alpaca", "paper": True}
    _force_entry(monkeypatch)

    class SettlingBroker(MockBroker):
        def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
            settled = super().wait_order(order_id, timeout)
            if settled and order_id not in getattr(self, "_applied", set()):
                self._applied = getattr(self, "_applied", set())
                self._applied.add(order_id)
                _, side, symbol, quantity = self._order_intents[order_id]
                fill_quantity, fill_price = self.fill_results[order_id]
                assert fill_quantity == quantity and side == "buy"
                self.positions[symbol] = quantity
                self.avg_prices[symbol] = fill_price
                self.cash -= quantity * fill_price
            return settled

    broker = SettlingBroker()
    broker.fill_results["order-1"] = (8, 123.45)
    execution_day = TEST_NOW.date().isoformat()
    source_hash = source_fingerprint(
        Path(runner_mod.__file__).resolve().parents[4]
    ).source_hash
    campaign = PaperCampaignSpec(
        campaign_id="paper-integration",
        candidate_config_hash=config_hash(cfg),
        source_hash=source_hash,
        account_fingerprint=broker.account_fingerprint(),
        expected_trading_dates=(execution_day,),
        expected_signal_dates=(str(market_data["SPY"].index[-1].date()),),
        minimum_trading_days=1,
    )

    LiveRunner(
        broker,
        cfg,
        mode="paper",
        campaign=campaign,
    ).run_once(force=False, data=market_data)

    with ReviewJournal(journal_path) as journal:
        records = journal.records_for_date(execution_day)
        started = next(row for row in records["runs"] if row["status"] == "started")
        metadata = json.loads(started["metadata_json"])

    assert metadata["campaign_id"] == campaign.campaign_id
    assert metadata["campaign_sha256"] == campaign.sha256
    assert metadata["execution_date"] == execution_day
    assert metadata["source_hash"] == campaign.source_hash
    assert metadata["account_fingerprint"] == campaign.account_fingerprint
    assert not records["reconciliations"]
    assert not audit_paper_campaign(campaign, {execution_day: records}).passed

    market_clock.now = market_clock.now.replace(hour=16, minute=5)
    assert collect_paper_close(broker, cfg, campaign).eligible
    with ReviewJournal(journal_path) as journal:
        records = journal.records_for_date(execution_day)
    assert {row["category"] for row in records["reconciliations"]} == {
        "positions", "working_orders", "account",
    }
    assert {row["status"] for row in records["reconciliations"]} == {"MATCH"}
    summary = audit_paper_campaign(campaign, {execution_day: records})
    assert summary.passed
    assert summary.consecutive_eligible_trading_days == 1
