"""主入口的生产模式、复盘与对账落账测试。"""

from __future__ import annotations

import json

import pytest
import quantbot.price_action.cli as cli

from quantbot.price_action.cli import (
    cmd_hypothesis_init,
    cmd_paper_account_fingerprint,
    cmd_paper_audit,
    cmd_paper_campaign_init,
    cmd_review,
    cmd_trade,
    record_reconciliation,
)
from quantbot.price_action.config import config_hash
from quantbot.price_action.experiment import build_candidate_config
from quantbot.price_action.governance import HypothesisSpec
from quantbot.price_action.live.reconcile import MATCH, ReconciliationResult
from quantbot.price_action.live.paths import local_account_fingerprint, runtime_paths
from quantbot.price_action.review import (
    PaperCampaignSpec,
    ReviewJournal,
)
from quantbot.price_action.review.paper import LEGACY_EVIDENCE_INCOMPLETE

from .conftest import make_cfg


def test_paper_live_command_must_match_config_mode(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["broker"] = {"provider": "alpaca", "paper": True}

    with pytest.raises(RuntimeError, match="broker.paper 不一致"):
        cmd_trade(cfg, "live", confirm_live=True)


def test_legacy_live_entry_cannot_bypass_quantbot_admission(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg["broker"] = {"provider": "alpaca", "paper": False}
    monkeypatch.setattr(
        "quantbot.price_action.broker.alpaca_broker.AlpacaBroker",
        lambda **kwargs: pytest.fail("must reject before constructing a live broker"),
    )
    with pytest.raises(RuntimeError, match="QuantBot 当前只开放模拟执行"):
        cmd_trade(cfg, "live", confirm_live=True)


def test_review_defaults_to_latest_ledger_day(tmp_path):
    cfg = make_cfg(tmp_path)
    journal_path = tmp_path / "journal.sqlite"
    cfg["review"] = {"enabled": True, "journal_path": str(journal_path)}
    with ReviewJournal(journal_path) as journal:
        journal.append_run(
            idempotency_key="run:2026-08-07",
            trading_date="2026-08-07",
            mode="paper",
            status="completed",
            data_status="fresh",
        )

    output = tmp_path / "review.md"
    cmd_review(cfg, None, str(output))

    assert output.exists()
    assert "2026-08-07" in output.read_text(encoding="utf-8")


def test_structured_reconciliation_is_persisted_even_without_drift(tmp_path):
    cfg = make_cfg(tmp_path)
    journal_path = tmp_path / "journal.sqlite"
    cfg["review"] = {"enabled": True, "journal_path": str(journal_path)}
    result = ReconciliationResult(
        status=MATCH,
        asof="2026-08-07",
        source="test-broker",
        theory_positions={"TST": 10},
        actual_positions={"TST": 10},
        matched_symbols=["TST"],
        generated_at="2026-08-08T01:00:00+00:00",
    )

    record_reconciliation(cfg, result)

    with ReviewJournal(journal_path) as journal:
        records = journal.records_for_date("2026-08-07")
    assert records["runs"][0]["mode"] == "reconcile"
    assert records["reconciliations"][0]["status"] == MATCH


def test_hypothesis_init_freezes_hashes_without_changing_config(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["data"]["snapshot"] = "snapshot-1"
    before_hash = config_hash(cfg)
    output = tmp_path / "hypothesis.json"

    cmd_hypothesis_init(
        cfg,
        change="strategy.breakout_window=60",
        hypothesis_id="h-breakout-60",
        title="延长突破确认",
        rationale="只改变前高突破窗口，检验更严格的价格结构是否改善样本外收益",
        output=str(output),
    )

    spec = HypothesisSpec.from_json(output.read_text(encoding="utf-8"))
    candidate, _ = build_candidate_config(
        cfg, {"strategy.breakout_window": 60}
    )
    assert spec.baseline_config_hash == before_hash == config_hash(cfg)
    assert spec.candidate_config_hash == config_hash(candidate)
    with pytest.raises(FileExistsError):
        cmd_hypothesis_init(
            cfg,
            change="strategy.breakout_window=60",
            hypothesis_id="h-breakout-60",
            title="延长突破确认",
            rationale="同一目标不得被静默覆盖",
            output=str(output),
        )


def test_paper_campaign_init_freezes_config_source_account_and_dates(tmp_path):
    cfg = make_cfg(tmp_path)
    cfg["broker"] = {"provider": "alpaca", "paper": True}
    cfg["review"] = {"paper_min_trading_days": 2}
    trading_sessions = [
        {"execution_date": "2026-08-10", "signal_date": "2026-08-07"},
        {"execution_date": "2026-08-11", "signal_date": "2026-08-10"},
    ]
    dates_path = tmp_path / "trading-dates.json"
    dates_path.write_text(json.dumps(trading_sessions), encoding="utf-8")
    output = tmp_path / "campaign.json"

    campaign = cmd_paper_campaign_init(
        cfg,
        campaign_id="paper-breakout-60",
        account_fingerprint="a" * 64,
        trading_dates_path=str(dates_path),
        output=str(output),
    )

    restored = PaperCampaignSpec.from_json(output.read_text(encoding="utf-8"))
    assert restored == campaign
    assert restored.candidate_config_hash == config_hash(cfg)
    assert restored.expected_trading_dates == ("2026-08-10", "2026-08-11")
    assert restored.expected_signal_dates == ("2026-08-07", "2026-08-10")
    with pytest.raises(FileExistsError):
        cmd_paper_campaign_init(
            cfg,
            campaign_id="paper-breakout-60",
            account_fingerprint="a" * 64,
            trading_dates_path=str(dates_path),
            output=str(output),
        )


def test_paper_account_fingerprint_outputs_only_anonymous_identity(
    tmp_path, monkeypatch, capsys
):
    cfg = make_cfg(tmp_path)
    cfg["broker"] = {"provider": "alpaca", "paper": True}

    class FakePaperBroker:
        def __init__(self, *, paper):
            assert paper is True

        def account_fingerprint(self):
            return "f" * 64

    monkeypatch.setattr(
        "quantbot.price_action.broker.alpaca_broker.AlpacaBroker",
        FakePaperBroker,
    )

    assert cmd_paper_account_fingerprint(cfg) == "f" * 64
    output = capsys.readouterr().out
    assert "f" * 64 in output
    assert "账号原文" in output


def test_paper_audit_writes_fail_closed_summary_for_legacy_ledger(tmp_path):
    cfg = make_cfg(tmp_path)
    journal_path = tmp_path / "journal.sqlite"
    cfg["review"] = {
        "enabled": True,
        "journal_path": str(journal_path),
        "paper_min_trading_days": 1,
    }
    campaign = PaperCampaignSpec(
        campaign_id="paper-legacy",
        candidate_config_hash=config_hash(cfg),
        source_hash="b" * 64,
        account_fingerprint="c" * 64,
        expected_trading_dates=("2026-08-10",),
        expected_signal_dates=("2026-08-07",),
        minimum_trading_days=1,
    )
    campaign_path = tmp_path / "campaign.json"
    campaign_path.write_text(campaign.to_json(), encoding="utf-8")
    journal_path = runtime_paths(cfg, "paper", campaign.account_fingerprint).journal_file
    with ReviewJournal(journal_path) as journal:
        journal.append_run(
            idempotency_key="paper:2026-08-10:completed",
            trading_date="2026-08-10",
            mode="paper",
            status="completed",
            signal_date="2026-08-07",
            data_as_of="2026-08-07",
            data_status="fresh",
            config_hash=config_hash(cfg),
            metadata={"market_level": 2},
        )

    output = tmp_path / "paper-summary.json"
    summary = cmd_paper_audit(cfg, str(campaign_path), str(output))

    assert not summary.passed
    assert summary.day_assessments[0].reason_codes == (
        LEGACY_EVIDENCE_INCOMPLETE,
    )
    assert output.exists()


@pytest.mark.parametrize("flag", ["--force", "--date=2026-09-10"])
def test_paper_close_cli_rejects_override_before_contacting_broker(tmp_path, monkeypatch, flag):
    cfg = make_cfg(tmp_path)
    cfg["broker"] = {"paper": True}
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: cfg)

    def forbidden(**kwargs):
        raise AssertionError("非法覆盖参数不得接触券商")

    monkeypatch.setattr("quantbot.price_action.broker.alpaca_broker.AlpacaBroker", forbidden)
    monkeypatch.setattr("sys.argv", ["main.py", "paper-close", "--campaign", "unused.json", flag])
    with pytest.raises(ValueError, match="禁止|不支持"):
        cli.main()


def test_report_cli_reads_only_selected_sim_account(tmp_path, monkeypatch, capsys):
    cfg = make_cfg(tmp_path)
    cfg["broker"] = {"paper": True}  # --mode sim 必须覆盖默认报告分区。
    paths = runtime_paths(cfg, "sim", local_account_fingerprint(cfg, "sim"))
    paths.logs_dir.mkdir(parents=True)
    (paths.logs_dir / "equity_history.csv").write_text(
        "date,equity,cash,n_positions,level\n2026-09-10,100000,100000,0,2\n", encoding="utf-8"
    )
    legacy = tmp_path / "logs" / "equity_history.csv"
    legacy.write_text("this legacy file must not be read", encoding="utf-8")
    monkeypatch.setattr(cli, "load_config", lambda *args, **kwargs: cfg)
    monkeypatch.setattr("sys.argv", ["main.py", "report", "--mode", "sim"])

    def unavailable(*args, **kwargs):
        assert kwargs["cache_dir_override"] == str(paths.cache_dir)
        assert args[0]["data"]["cache_dir"] == str(paths.cache_dir)
        raise RuntimeError("isolated shadow unavailable")

    monkeypatch.setattr("quantbot.price_action.data.downloader.load_market_data", unavailable)
    cli.main()

    assert "$100,000" in capsys.readouterr().out
    assert (paths.logs_dir / "live_report.txt").exists()
    assert legacy.read_text() == "this legacy file must not be read"
