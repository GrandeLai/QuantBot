"""模式和账户隔离必须覆盖状态、账本、行情、模拟券商与报告读取。"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from quantbot.price_action.broker.base import DryRunBroker
from quantbot.price_action.broker.sim_broker import SimBroker
from quantbot.price_action.live.paths import read_runtime_paths, runtime_paths
from quantbot.price_action.live.runner import LiveRunner
from quantbot.price_action.review import ReviewJournal

from .conftest import TEST_NOW, MockBroker, make_cfg


def test_all_modes_and_accounts_have_distinct_runtime_files(tmp_path):
    cfg = make_cfg(tmp_path)
    before = copy.deepcopy(cfg)
    paths = [runtime_paths(cfg, mode, account * 64)
             for mode in ("dry-run", "sim", "paper", "live") for account in ("a", "b")]

    assert len({p.state_file for p in paths}) == 8
    assert len({p.journal_file for p in paths}) == 8
    assert len({p.cache_dir for p in paths}) == 8
    assert cfg == before


def test_dry_run_preserves_paper_positions_and_checkpoint_bytes(tmp_path, market_data):
    cfg = make_cfg(tmp_path)
    cfg["review"] = {"enabled": True, "journal_path": str(tmp_path / "journal.sqlite")}
    paper_paths = runtime_paths(cfg, "paper", MockBroker().account_fingerprint())
    paper_paths.state_file.parent.mkdir(parents=True)
    payload = json.dumps({"meta": {"last_run_date": "2026-09-09"},
                          "positions": {"TST": {"qty": 13, "direction": 1, "stop": 80}}})
    paper_paths.state_file.write_text(payload, encoding="utf-8")
    with ReviewJournal(paper_paths.journal_file) as journal:
        run_id = journal.append_run(idempotency_key="paper-held", mode="paper",
                                    trading_date="2026-09-10", status="completed")
        journal.append_state_checkpoint(
            idempotency_key="paper-held:state", run_id=run_id,
            state_sha256=hashlib.sha256(payload.encode()).hexdigest(),
        )
    before = {path: path.read_bytes() for path in
              (paper_paths.state_file, paper_paths.journal_file)}

    broker = DryRunBroker()
    runner = LiveRunner(broker, cfg, mode="dry-run")
    runner.run_once(force=True, data=market_data)

    assert all(path.read_bytes() == content for path, content in before.items())
    assert json.loads(runner.state_path.read_text())["positions"] == {}
    with ReviewJournal(runner.paths.journal_file, readonly=True) as journal:
        assert {r["mode"] for r in journal.records_for_date(TEST_NOW.date().isoformat())["runs"]} == {"dry-run"}


@pytest.mark.parametrize("mode", ["sim", "paper", "live"])
def test_unpartitioned_state_blocks_account_start(tmp_path, mode):
    cfg = make_cfg(tmp_path)
    legacy = tmp_path / "live_state.json"
    legacy.write_text('{"positions":{"TST":{"qty":10}}}', encoding="utf-8")
    before = legacy.read_bytes()

    with pytest.raises(RuntimeError, match="未分区旧状态"):
        LiveRunner(MockBroker(), cfg, mode=mode)
    assert legacy.read_bytes() == before


def test_sim_broker_uses_same_partition_for_prices_orders_and_runner(tmp_path, market_data):
    cfg = make_cfg(tmp_path)
    broker = SimBroker(cfg)
    paths = runtime_paths(cfg, "sim", broker.account_fingerprint())
    assert broker.state_path == paths.broker_state_file
    assert broker.cache_dir == paths.cache_dir
    broker.cache_dir.mkdir(parents=True)
    market_data["TST"].to_parquet(broker.cache_dir / "TST.parquet")

    order_id = broker.market_buy("TST", 2, "sim-stable")
    restarted = SimBroker(cfg)
    assert restarted.account_fingerprint() == broker.account_fingerprint()
    assert restarted.market_buy("TST", 2, "sim-stable") == order_id
    assert restarted.get_positions()["TST"].shares == 2
    assert not (tmp_path / "sim_broker_state.json").exists()


def test_offline_reports_require_explicit_selection_when_accounts_differ(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["review"] = {"journal_path": str(tmp_path / "journal.sqlite")}
    first = runtime_paths(cfg, "paper", "a" * 64)
    with ReviewJournal(first.journal_file):
        pass
    assert read_runtime_paths(cfg, "paper") == first
    with ReviewJournal(runtime_paths(cfg, "paper", "b" * 64).journal_file):
        pass

    with pytest.raises(ValueError, match="--account-fingerprint"):
        read_runtime_paths(cfg, "paper")
    assert read_runtime_paths(cfg, "paper", "a" * 64) == first
