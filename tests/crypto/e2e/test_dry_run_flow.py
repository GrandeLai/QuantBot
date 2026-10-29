"""End-to-end dry-run CLI flow tests."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
BACKEND = ROOT
CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def _run_cli(*args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "quantbot.crypto.runtime", *args],
        cwd=BACKEND,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def test_complete_mock_dry_run_chain_outputs_json_and_exit_zero() -> None:
    assert _run_cli("config", "validate", "--config", str(CONFIG), "--json")["valid"] is True
    assert _run_cli("exchange", "ping", "--exchange", "mock", "--json")["ok"] is True
    assert _run_cli("market", "ticker", "--exchange", "mock", "--symbol", "BTC/USDT", "--json")["ticker"]["symbol"] == "BTC/USDT"
    opportunities = _run_cli("arbitrage", "scan", "--type", "cross-exchange", "--symbol", "BTC/USDT", "--json")["opportunities"]
    assert opportunities[0]["opportunity_id"] == "test-opportunity"
    execution = _run_cli("arbitrage", "execute", "--opportunity-id", "test-opportunity", "--dry-run", "--json")["execution"]
    assert execution["status"] == "simulated"
    assert _run_cli("report", "generate", "--type", "daily", "--json")["report"]["type"] == "daily"


def test_workflow_run_connects_research_paper_sandbox_and_live_gate() -> None:
    payload = _run_cli("workflow", "run", "--config", str(CONFIG), "--symbol", "BTC/USDT", "--json")

    assert payload["workflow"]["completed"] is True
    assert payload["workflow"]["selected_opportunity_id"] == "test-opportunity"
    assert payload["workflow"]["sandbox_readiness"]["ready"] is True
    assert payload["workflow"]["live_readiness"]["ready"] is False
    assert payload["workflow"]["agent_live_readiness"]["ready"] is False
    assert "real orders were not sent" in payload["workflow"]["summary"]


def test_strategy_platform_mock_scan_score_paper_review_chain(tmp_path: Path) -> None:
    strategy_config = tmp_path / "strategy.yaml"
    strategy_config.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "strategy-events.jsonl"}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  retrospective_path: {tmp_path / "strategy-retrospective.md"}
  retrospective_state_path: {tmp_path / "strategy-retrospective.state.json"}
  evolution_state_path: {tmp_path / "strategy-evolution.state.json"}
  evolution_report_path: {tmp_path / "strategy-evolution.md"}
""".strip(),
        encoding="utf-8",
    )
    catalog = _run_cli("strategy", "catalog", "--config", str(strategy_config), "--json")["strategy_catalog"]
    assert catalog["aliases"]["triangular"] == "triangular-multi-route"

    scan = _run_cli("strategy", "scan", "--config", str(strategy_config), "--strategy", "all", "--symbol", "BTC/USDT", "--json")[
        "strategy_scan"
    ]
    assert sum(len(report["opportunities"]) for report in scan) >= 5

    score = _run_cli("strategy", "score", "--config", str(strategy_config), "--strategy", "all", "--json")["strategy_score"]
    assert len(score) >= 5
    assert max(Decimal(str(item["score"])) for item in score) >= Decimal("50")

    portfolio = _run_cli("strategy", "portfolio-status", "--config", str(strategy_config), "--json")["strategy_portfolio_status"]
    assert portfolio["selected_strategies"]

    run = _run_cli(
        "strategy",
        "run",
        "--config",
        str(strategy_config),
        "--strategy",
        "all",
        "--max-cycles",
        "1",
        "--execution-mode",
        "paper",
        "--json",
    )["strategy_run"]
    assert run["completed"] is True
    assert {item["strategy_name"] for item in run["results"]} >= {"triangular-multi-route", "futures-perp-basis"}

    review = _run_cli("strategy", "review", "--config", str(strategy_config), "--execution-mode", "paper", "--json")["strategy_review"]
    assert review["total_events"] >= 1

    promotion = _run_cli("strategy", "promotion-status", "--config", str(strategy_config), "--strategy", "all", "--json")[
        "strategy_promotion_status"
    ]
    assert all(item["live"]["status"] == "Fail" for item in promotion)
