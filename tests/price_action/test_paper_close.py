"""正式 Paper 从真实加载入口到收盘审计的隔离集成测试。"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import quantbot.price_action.live.runner as runner_mod
from quantbot.price_action.cli import cmd_paper_close
from quantbot.price_action.config import config_hash
from quantbot.price_action.data.session import EASTERN, MarketSession
from quantbot.price_action.governance import source_fingerprint
from quantbot.price_action.live.closing import collect_paper_close
from quantbot.price_action.live.recovery import RecoveryHoldError
from quantbot.price_action.live.runner import LiveRunner
from quantbot.price_action.review import JournalWriter, PaperCampaignSpec, ReviewJournal, audit_paper_campaign
from quantbot.price_action.review.paper import EQUITY_MISSING, RECONCILIATION_MISSING, RECONCILIATION_UNKNOWN

from .conftest import MockBroker, make_cfg, make_trending_ohlcv


@pytest.fixture
def paper_execution(tmp_path, monkeypatch, market_clock):
    def start(*, day="2026-09-11", signal="2026-09-10", close_hour=16):
        market_clock.now = datetime.fromisoformat(day).replace(hour=9, minute=35, tzinfo=EASTERN)
        session = MarketSession(
            day, signal,
            market_clock.now.replace(minute=30).isoformat(),
            market_clock.now.replace(hour=close_hour, minute=0).isoformat(),
            "alpaca:calendar",
        )
        cfg = make_cfg(tmp_path)
        cfg["data"]["snapshot"] = "frozen-research-identity"
        cfg["broker"] = {"paper": True}
        cfg["risk"]["max_positions"] = 0
        cfg["review"] = {"enabled": True, "journal_path": str(tmp_path / "journal.sqlite")}
        broker = MockBroker()
        monkeypatch.setattr(broker, "get_market_session", lambda _: session)
        raw = {symbol: make_trending_ohlcv(end=pd.Timestamp(signal), start_price=price)
               for symbol, price in (("SPY", 300), ("TST", 100))}
        for frame in raw.values():
            frame.loc[pd.Timestamp(day)] = frame.iloc[-1]  # 模拟当日未收盘日线

        def download(symbol, start, end, cache_dir, force=False):
            assert force
            path = Path(cache_dir)
            path.mkdir(parents=True, exist_ok=True)
            raw[symbol].to_parquet(path / f"{symbol}.parquet")
            return raw[symbol].copy()

        # 仅替换外部行情边界，保留 load_market_data、路径解析和半根 K 线处理。
        monkeypatch.setattr("quantbot.price_action.data.downloader.download_symbol", download)
        monkeypatch.setattr("quantbot.price_action.data.universe.all_symbols", lambda: ["SPY", "TST"])
        campaign = PaperCampaignSpec(
            campaign_id="integration-close",
            candidate_config_hash=config_hash(cfg),
            source_hash=source_fingerprint(Path(runner_mod.__file__).resolve().parents[4]).source_hash,
            account_fingerprint=broker.account_fingerprint(),
            expected_trading_dates=(day,), expected_signal_dates=(signal,),
            minimum_trading_days=1,  # 仅验单日逻辑，不替代正式 60 日门禁。
        )
        before = copy.deepcopy(cfg)
        runner = LiveRunner(broker, cfg, mode="paper", campaign=campaign)
        runner.run_once()
        assert cfg == before
        assert runner.cfg == before
        assert broker.orders == []
        return SimpleNamespace(cfg=cfg, broker=broker, campaign=campaign, paths=runner.paths,
                               session=session, day=day)

    return start


def _records(execution):
    with ReviewJournal(execution.paths.journal_file, readonly=True) as journal:
        return journal.records_for_date(execution.day)


def test_open_is_ineligible_until_actual_close_and_close_command_is_idempotent(
    paper_execution, market_clock, monkeypatch, tmp_path
):
    execution = paper_execution()
    records = _records(execution)
    assessment = audit_paper_campaign(execution.campaign, {execution.day: records}).day_assessments[0]
    assert set(assessment.reason_codes) == {EQUITY_MISSING, RECONCILIATION_MISSING}
    assert {r["config_hash"] for r in records["runs"]} == {execution.campaign.candidate_config_hash}
    assert {r["signal_date"] for r in records["runs"]} == {"2026-09-10"}
    assert json.loads(records["equity_snapshots"][0]["metadata_json"])["phase"] == "execution"
    with pytest.raises(ValueError, match="实际收盘后"):
        collect_paper_close(execution.broker, execution.cfg, execution.campaign)
    assert _records(execution) == records

    # 不同时刻的重复开盘调度保留首次时刻和同一 attempt，不制造新的订单。
    market_clock.now = market_clock.now.replace(hour=10)
    LiveRunner(execution.broker, execution.cfg, mode="paper", campaign=execution.campaign).run_once()
    market_clock.now = market_clock.now.replace(hour=16, minute=5)
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(execution.campaign.to_json(), encoding="utf-8")
    monkeypatch.setattr("quantbot.price_action.broker.alpaca_broker.AlpacaBroker", lambda *, paper: execution.broker)
    assert cmd_paper_close(execution.cfg, str(campaign_path)).eligible
    closed = _records(execution)
    assert len(closed["equity_snapshots"]) == 2
    assert len(closed["reconciliations"]) == 3
    market_clock.now = market_clock.now.replace(hour=17)
    assert cmd_paper_close(execution.cfg, str(campaign_path)).eligible
    assert _records(execution) == closed
    assert execution.broker.orders == []


def test_early_close_uses_exchange_session_instead_of_fixed_1600(paper_execution, market_clock):
    execution = paper_execution(day="2026-11-27", signal="2026-11-25", close_hour=13)
    market_clock.now = market_clock.now.replace(hour=12, minute=59)
    with pytest.raises(ValueError, match="实际收盘后"):
        collect_paper_close(execution.broker, execution.cfg, execution.campaign)
    market_clock.now = market_clock.now.replace(hour=13, minute=5)
    assert collect_paper_close(execution.broker, execution.cfg, execution.campaign).eligible


def test_close_rejects_next_day_backfill(paper_execution, market_clock):
    execution = paper_execution()
    before = _records(execution)
    market_clock.now += timedelta(days=1)
    with pytest.raises(ValueError, match="禁止补录历史收盘"):
        collect_paper_close(execution.broker, execution.cfg, execution.campaign)
    assert _records(execution) == before


@pytest.mark.parametrize("changed", ["config", "source", "account", "calendar"])
def test_close_rejects_identity_changes(paper_execution, market_clock, monkeypatch, changed):
    execution = paper_execution()
    market_clock.now = market_clock.now.replace(hour=16, minute=5)
    before = _records(execution)
    campaign = execution.campaign
    if changed == "config":
        execution.cfg["strategy"]["breakout_window"] += 1
    elif changed == "source":
        campaign = replace(campaign, source_hash="a" * 64)
    elif changed == "account":
        monkeypatch.setattr(execution.broker, "account_fingerprint", lambda: "b" * 64)
    else:
        different = replace(execution.session, closes_at=f"{execution.day}T15:00:00-04:00")
        monkeypatch.setattr(execution.broker, "get_market_session", lambda _: different)
    with pytest.raises(ValueError, match="不一致"):
        collect_paper_close(execution.broker, execution.cfg, campaign)
    assert _records(execution) == before


def test_close_keeps_unknown_positions_and_does_not_replace_them_on_retry(
    paper_execution, market_clock, monkeypatch
):
    execution = paper_execution()
    market_clock.now = market_clock.now.replace(hour=16, minute=5)

    def unavailable():
        raise TimeoutError("position read unavailable")

    monkeypatch.setattr(execution.broker, "get_positions", unavailable)
    result = collect_paper_close(execution.broker, execution.cfg, execution.campaign)
    assert not result.eligible
    assert RECONCILIATION_UNKNOWN in result.reason_codes
    before = _records(execution)
    monkeypatch.setattr(execution.broker, "get_positions", lambda: {})
    assert not collect_paper_close(execution.broker, execution.cfg, execution.campaign).eligible
    assert _records(execution) == before


def test_close_transaction_rolls_back_all_four_facts_if_append_fails(
    paper_execution, market_clock, monkeypatch
):
    execution = paper_execution()
    market_clock.now = market_clock.now.replace(hour=16, minute=5)
    before = _records(execution)
    original = JournalWriter.append_reconciliation

    def fail_on_account(self, **kwargs):
        if kwargs["category"] == "account":
            raise OSError("simulated storage failure")
        return original(self, **kwargs)

    monkeypatch.setattr(JournalWriter, "append_reconciliation", fail_on_account)
    with pytest.raises(OSError, match="storage failure"):
        collect_paper_close(execution.broker, execution.cfg, execution.campaign)
    assert _records(execution) == before
    monkeypatch.setattr(JournalWriter, "append_reconciliation", original)
    assert collect_paper_close(execution.broker, execution.cfg, execution.campaign).eligible


def test_close_rechecks_state_checkpoint_before_recording(paper_execution, market_clock):
    execution = paper_execution()
    market_clock.now = market_clock.now.replace(hour=16, minute=5)
    before = _records(execution)
    execution.paths.state_file.write_text('{"positions":{},"meta":{}}', encoding="utf-8")
    with pytest.raises(RecoveryHoldError, match="HOLD"):
        collect_paper_close(execution.broker, execution.cfg, execution.campaign)
    assert _records(execution) == before
