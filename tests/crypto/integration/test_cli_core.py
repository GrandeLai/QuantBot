"""Integration tests for the trading assistant CLI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.crypto.runtime.arbitrage.opportunity import ArbitrageOpportunity
from quantbot.crypto.runtime.cli.main import main


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def _invoke(args: Sequence[str], capsys) -> tuple[int, dict]:
    code = main([*args, "--json"])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def test_cli_config_exchange_market_account_arbitrage_execution_and_report(capsys) -> None:
    code, payload = _invoke(["config", "validate", "--config", str(EXAMPLE_CONFIG)], capsys)
    assert code == 0
    assert payload["valid"] is True

    code, payload = _invoke(["exchange", "ping", "--exchange", "mock"], capsys)
    assert code == 0
    assert payload["ok"] is True

    code, payload = _invoke(["exchange", "ping", "--config", str(EXAMPLE_CONFIG), "--exchange", "mock"], capsys)
    assert code == 0
    assert payload["ok"] is True

    code, payload = _invoke(["market", "ticker", "--exchange", "mock", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert payload["ticker"]["symbol"] == "BTC/USDT"

    code, payload = _invoke(["market", "ticker", "--config", str(EXAMPLE_CONFIG), "--exchange", "mock", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert payload["ticker"]["symbol"] == "BTC/USDT"

    code, payload = _invoke(["market", "orderbook", "--exchange", "mock", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert payload["orderbook"]["bids"]

    code, payload = _invoke(
        ["market", "candles", "--config", str(EXAMPLE_CONFIG), "--exchange", "mock", "--symbol", "BTC/USDT", "--bar", "15m", "--limit", "80"],
        capsys,
    )
    assert code == 0
    assert payload["candles"]
    assert payload["candles"][0]["complete"] is True

    code, payload = _invoke(["account", "balance", "--config", str(EXAMPLE_CONFIG), "--exchange", "mock"], capsys)
    assert code == 0
    assert payload["account"]["balances"]["USDT"]["free"] == "10000"

    code, payload = _invoke(["arbitrage", "scan", "--type", "cross-exchange", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert payload["opportunities"][0]["opportunity_id"] == "test-opportunity"

    code, payload = _invoke(["arbitrage", "execute", "--opportunity-id", "test-opportunity", "--dry-run"], capsys)
    assert code == 0
    assert payload["execution"]["status"] == "simulated"

    code, payload = _invoke(["report", "generate", "--type", "daily"], capsys)
    assert code == 0
    assert payload["report"]["type"] == "daily"


def test_cli_invalid_exchange_returns_nonzero_and_json_error(capsys) -> None:
    code = main(["exchange", "ping", "--exchange", "missing", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "ExchangeError"
    assert "Unknown exchange" in payload["error"]["message"]


def test_cli_okx_sandbox_check_requires_enabled_exchange(capsys) -> None:
    code = main(["exchange", "sandbox-check", "--exchange", "okx", "--symbol", "BTC/USDT", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "ExchangeError"
    assert "Exchange is disabled: okx" in payload["error"]["message"]


def test_cli_workflow_run_returns_full_route(capsys) -> None:
    code = main(["workflow", "run", "--config", str(EXAMPLE_CONFIG), "--symbol", "BTC/USDT", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["workflow"]["completed"] is True
    assert payload["workflow"]["paper_trading"]["execution"]["dry_run"] is True
    assert payload["workflow"]["sandbox_readiness"]["ready"] is True
    assert payload["workflow"]["live_readiness"]["ready"] is False
    assert payload["workflow"]["agent_live_readiness"]["ready"] is False


def test_cli_backtest_realistic_walk_forward_and_bias_check(capsys) -> None:
    code, payload = _invoke(
        [
            "backtest",
            "run",
            "--config",
            str(EXAMPLE_CONFIG),
            "--strategy",
            "trend-breakout",
            "--symbol",
            "BTC/USDT",
            "--exchange",
            "mock",
        ],
        capsys,
    )
    assert code == 0
    assert payload["backtest"]["read_only"] is True
    assert payload["backtest"]["orders_sent"] is False
    assert payload["backtest"]["live_orders_sent"] is False
    assert payload["backtest"]["fill_model"]["completed_candles_only"] is True

    code, payload = _invoke(
        [
            "backtest",
            "walk-forward",
            "--config",
            str(EXAMPLE_CONFIG),
            "--strategy",
            "trend-breakout",
            "--symbol",
            "BTC/USDT",
            "--exchange",
            "mock",
            "--windows",
            "2",
        ],
        capsys,
    )
    assert code == 0
    assert payload["backtest_walk_forward"]["read_only"] is True
    assert payload["backtest_walk_forward"]["orders_sent"] is False
    assert len(payload["backtest_walk_forward"]["windows"]) == 2

    code, payload = _invoke(
        [
            "backtest",
            "bias-check",
            "--config",
            str(EXAMPLE_CONFIG),
            "--strategy",
            "trend-breakout",
            "--symbol",
            "BTC/USDT",
            "--exchange",
            "mock",
        ],
        capsys,
    )
    assert code == 0
    assert payload["backtest_bias_check"]["orders_sent"] is False
    assert payload["backtest_bias_check"]["passed"] is True


def test_cli_autopilot_run_status_and_report(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "autopilot.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "strategy-events.jsonl"}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  retrospective_path: {tmp_path / "strategy-retrospective.md"}
  retrospective_state_path: {tmp_path / "strategy-retrospective.state.json"}
  evolution_state_path: {tmp_path / "strategy-evolution.state.json"}
  evolution_report_path: {tmp_path / "strategy-evolution.md"}
  autopilot_state_path: {tmp_path / "autopilot-state.json"}
  review_min_samples: 1
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(
        [
            "autopilot",
            "run",
            "--config",
            str(config_path),
            "--mode",
            "paper",
            "--strategy",
            "cross-exchange",
            "--cycles",
            "1",
            "--interval-seconds",
            "0",
        ],
        capsys,
    )
    assert code == 0
    assert payload["autopilot_run"]["status"] == "Completed"
    assert payload["autopilot_run"]["live_orders_sent"] is False

    code, payload = _invoke(["autopilot", "status", "--config", str(config_path)], capsys)
    assert code == 0
    assert payload["autopilot_status"]["status"] == "Completed"

    code, payload = _invoke(
        ["autopilot", "report", "--config", str(config_path), "--mode", "paper", "--strategy", "cross-exchange"],
        capsys,
    )
    assert code == 0
    assert payload["autopilot_report"]["state"]["status"] == "Completed"
    assert payload["autopilot_report"]["live_orders_sent"] is False


def test_cli_agent_live_readiness_blocks_by_default(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_TRADING_ENABLED", "false")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_ALLOW_LIVE_ORDERS", "false")

    code = main(["agent", "live-readiness", "--config", str(EXAMPLE_CONFIG), "--opportunity-id", "test-opportunity", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["agent_live_readiness"]["ready"] is False
    assert "agent_trading_disabled" in payload["agent_live_readiness"]["reasons"]


def test_cli_agent_execute_live_blocks_by_default(capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_TRADING_ENABLED", "false")
    monkeypatch.setenv("QUANTBOT_CRYPTO_AGENT_ALLOW_LIVE_ORDERS", "false")

    code = main(["agent", "execute-live", "--config", str(EXAMPLE_CONFIG), "--opportunity-id", "test-opportunity", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "尚未准入" in payload["error"]["message"]


def test_cli_agent_live_readiness_accepts_opportunity_file(tmp_path: Path, capsys) -> None:
    opportunity_path = tmp_path / "okx-opportunity.json"
    opportunity_path.write_text(json.dumps(_okx_opportunity_payload()), encoding="utf-8")

    code = main(["agent", "live-readiness", "--config", str(EXAMPLE_CONFIG), "--opportunity-file", str(opportunity_path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    assert payload["agent_live_readiness"]["ready"] is False
    assert "mock_exchange_involved" not in payload["agent_live_readiness"]["reasons"]
    assert "exchange_disabled" in payload["agent_live_readiness"]["reasons"]


def test_cli_agent_execute_live_accepts_opportunity_file_and_blocks_before_provider(tmp_path: Path, capsys) -> None:
    opportunity_path = tmp_path / "okx-opportunity.json"
    opportunity_path.write_text(json.dumps(_okx_opportunity_payload()), encoding="utf-8")

    code = main(["agent", "execute-live", "--config", str(EXAMPLE_CONFIG), "--opportunity-file", str(opportunity_path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "尚未准入" in payload["error"]["message"]


def test_cli_agent_operation_catalog_reports_demo_validation(capsys) -> None:
    code = main(["agent", "operation-catalog", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code == 0
    catalog = payload["operation_catalog"]
    assert catalog["missing_demo_validation"] == []
    operation_ids = {operation["operation_id"] for operation in catalog["operations"]}
    assert "okx.sandbox_check" in operation_ids
    assert "okx.agent_spot_limit_order" in operation_ids
    assert "okx.hedged_maker_demo_order_manager" in operation_ids
    for operation in catalog["operations"]:
        if operation["live_supported"]:
            assert operation["demo_supported"] is True
            assert operation["demo_config"] == "config/crypto/okx.demo.example.yaml"
            assert "--config config/crypto/okx.demo.example.yaml" in operation["demo_command"]


def test_cli_strategy_hedged_maker_demo_blocks_default_config(tmp_path: Path, capsys) -> None:
    opportunity_path = tmp_path / "hedged-maker-okx-opportunity.json"
    opportunity_path.write_text(json.dumps(_hedged_maker_okx_opportunity_payload()), encoding="utf-8")

    code = main(
        [
            "strategy",
            "hedged-maker-demo",
            "--config",
            str(EXAMPLE_CONFIG),
            "--opportunity-file",
            str(opportunity_path),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "dry_run_enabled" in payload["error"]["message"]


def test_cli_strategy_list_run_and_review(tmp_path: Path, capsys) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    retrospective_path = tmp_path / "strategy-retrospective.md"
    retrospective_state_path = tmp_path / "strategy-retrospective.state.json"
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {journal_path}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  retrospective_path: {retrospective_path}
  retrospective_state_path: {retrospective_state_path}
  evolution_state_path: {tmp_path / "strategy-evolution.state.json"}
  evolution_report_path: {tmp_path / "strategy-evolution.md"}
  review_min_samples: 1
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(["strategy", "list"], capsys)
    assert code == 0
    assert {strategy["name"] for strategy in payload["strategies"]} >= {
        "cross-exchange",
        "triangular-multi-route",
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
    }

    code, payload = _invoke(["strategy", "catalog", "--config", str(config_path)], capsys)
    assert code == 0
    assert payload["strategy_catalog"]["aliases"]["triangular"] == "triangular-multi-route"
    directional_names = {
        strategy["name"]
        for strategy in payload["strategy_catalog"]["strategies"]
        if strategy["category"] == "directional"
    }
    assert directional_names >= {"trend-breakout", "mean-reversion-spot", "volatility-squeeze-breakout", "momentum-rotation"}

    code, payload = _invoke(["strategy", "scan", "--config", str(config_path), "--strategy", "all", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert {report["strategy_name"] for report in payload["strategy_scan"]} >= {"triangular-multi-route", "spot-perp-carry"}
    assert sum(len(report["opportunities"]) for report in payload["strategy_scan"]) >= 5
    spot_perp_scan = next(report for report in payload["strategy_scan"] if report["strategy_name"] == "spot-perp-carry")
    assert "diagnostics" in spot_perp_scan
    assert "net_profit_usdt" in spot_perp_scan["diagnostics"]

    code, payload = _invoke(["strategy", "score", "--config", str(config_path), "--strategy", "all"], capsys)
    assert code == 0
    assert len(payload["strategy_score"]) >= 5

    code, payload = _invoke(["strategy", "scan", "--config", str(config_path), "--strategy", "directional-all", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert {report["strategy_name"] for report in payload["strategy_scan"]} >= {
        "trend-breakout",
        "mean-reversion-spot",
        "volatility-squeeze-breakout",
        "momentum-rotation",
        "orderbook-imbalance-scalp",
    }
    assert sum(len(report["opportunities"]) for report in payload["strategy_scan"]) >= 1

    code, payload = _invoke(["strategy", "score", "--config", str(config_path), "--strategy", "directional-all", "--symbol", "BTC/USDT"], capsys)
    assert code == 0
    assert len(payload["strategy_score"]) == 5
    assert any(card["best_opportunity_id"] for card in payload["strategy_score"])

    code, payload = _invoke(
        [
            "strategy",
            "run",
            "--config",
            str(config_path),
            "--strategy",
            "directional-all",
            "--max-cycles",
            "1",
            "--interval-seconds",
            "0",
            "--execution-mode",
            "paper",
        ],
        capsys,
    )
    assert code == 0
    assert payload["strategy_run"]["completed"] is True
    assert any(result["decision"] == "executed" for result in payload["strategy_run"]["results"])

    code, payload = _invoke(["strategy", "market-compare", "--config", str(config_path), "--strategy", "all", "--target-exchange", "mock"], capsys)
    assert code == 0
    comparison = payload["strategy_market_compare"]
    assert comparison["read_only"] is True
    assert comparison["orders_sent"] is False
    assert comparison["target_exchange"] == "mock"
    assert len(comparison["comparisons"]) >= 5

    code, payload = _invoke(["strategy", "discover-routes", "--config", str(config_path), "--exchange", "mock", "--quote", "USDT"], capsys)
    assert code == 0
    route_report = payload["strategy_discover_routes"]
    assert route_report["read_only"] is True
    assert route_report["orders_sent"] is False
    assert route_report["accepted_routes"]

    code, payload = _invoke(
        [
            "strategy",
            "scan",
            "--config",
            str(config_path),
            "--strategy",
            "triangular-multi-route",
            "--exchange",
            "mock",
            "--route-mode",
            "discovered",
        ],
        capsys,
    )
    assert code == 0
    assert payload["strategy_scan"][0]["opportunities"]

    code, payload = _invoke(["strategy", "portfolio-status", "--config", str(config_path)], capsys)
    assert code == 0
    assert payload["strategy_portfolio_status"]["selected_strategies"]

    code, payload = _invoke(
        [
            "strategy",
            "run",
            "--config",
            str(config_path),
            "--strategy",
            "all",
            "--max-cycles",
            "1",
            "--interval-seconds",
            "0",
            "--execution-mode",
            "paper",
        ],
        capsys,
    )
    assert code == 0
    assert payload["strategy_run"]["completed"] is True
    assert payload["strategy_run"]["cycles_completed"] == 1
    assert payload["retrospective_before"]["enabled"] is True
    assert payload["retrospective_after"]["retrospective_path"] == str(retrospective_path)
    assert payload["retrospective_path"] == str(retrospective_path)
    assert journal_path.exists()
    assert retrospective_path.exists()
    assert retrospective_state_path.exists()

    code, payload = _invoke(["strategy", "review", "--config", str(config_path)], capsys)
    assert code == 0
    assert payload["strategy_review"]["total_events"] >= 1
    assert "cross-exchange" in payload["strategy_review"]["strategy_stats"]
    assert "retrospective_summary" in payload

    code, payload = _invoke(["strategy", "review", "--config", str(config_path), "--execution-mode", "paper"], capsys)
    assert code == 0
    assert payload["strategy_review"]["total_events"] >= 1

    code, payload = _invoke(["strategy", "retrospective", "--config", str(config_path)], capsys)
    assert code == 0
    assert payload["strategy_retrospective"]["retrospective_path"] == str(retrospective_path)
    assert "optimization_pressure" in payload["strategy_retrospective"]

    code, payload = _invoke(["strategy", "validation-report", "--config", str(config_path), "--execution-mode", "paper"], capsys)
    assert code == 0
    assert payload["strategy_validation_report"]["scanned_events"] >= 1
    assert payload["strategy_validation_report"]["total"]["executed"] >= 1
    assert "recommendations" in payload["strategy_validation_report"]

    code, payload = _invoke(["strategy", "opportunity-report", "--config", str(config_path), "--window", "24h"], capsys)
    assert code == 0
    opportunity_report = payload["strategy_opportunity_report"]
    assert opportunity_report["read_only"] is True
    assert opportunity_report["orders_sent"] is False
    assert opportunity_report["scan_count"] >= 1
    assert "by_strategy" in opportunity_report

    code, payload = _invoke(["strategy", "guard-status", "--config", str(config_path), "--execution-mode", "paper"], capsys)
    assert code == 0
    assert payload["strategy_guard_status"]["enabled"] is True
    assert {entry["strategy_name"] for entry in payload["strategy_guard_status"]["entries"]} >= {
        "cross-exchange",
        "triangular-multi-route",
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
    }

    code, payload = _invoke(
        [
            "strategy",
            "validate-local",
            "--config",
            str(config_path),
            "--strategy",
            "all",
            "--cycles",
            "1",
        ],
        capsys,
    )
    assert code == 0
    assert payload["local_validation"]["status"] in {"Pass", "Needs More Samples"}
    assert payload["strategy_run"]["execution_mode"] == "paper"
    assert payload["retrospective_after"]["retrospective_path"] == str(retrospective_path)

    code, payload = _invoke(["strategy", "promotion-status", "--config", str(config_path), "--strategy", "all"], capsys)
    assert code == 0
    assert payload["strategy_promotion_status"]
    assert all(item["live"]["status"] == "Fail" for item in payload["strategy_promotion_status"])


def test_cli_portfolio_status_applies_retrospective_pressure(tmp_path: Path, capsys) -> None:
    state_path = tmp_path / "retrospective.state.json"
    config_path = tmp_path / "strategy.yaml"
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "updated_at": "2026-05-10T00:00:00+00:00",
                "recent_runs": [],
                "issues": {
                    "spot-perp-carry|demo|preflight_not_profitable|demo_preflight_not_profitable": {
                        "issue_id": "ri-test",
                        "dedupe_key": "spot-perp-carry|demo|preflight_not_profitable|demo_preflight_not_profitable",
                        "strategy_name": "spot-perp-carry",
                        "execution_mode": "demo",
                        "category": "preflight_not_profitable",
                        "reason": "demo_preflight_not_profitable",
                        "severity": "warning",
                        "status": "open",
                        "occurrences": 10,
                        "first_seen_at": "2026-05-10T00:00:00+00:00",
                        "last_seen_at": "2026-05-10T00:00:00+00:00",
                        "recommendation": "Review spread and cost thresholds before another demo attempt.",
                        "last_context": {"net_profit": "-0.258630", "decision": "skipped"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "strategy-events.jsonl"}
  retrospective_path: {tmp_path / "retrospective.md"}
  retrospective_state_path: {state_path}
  evolution_state_path: {tmp_path / "evolution.state.json"}
  evolution_report_path: {tmp_path / "evolution.md"}
  retrospective_demo_preflight_score_penalty: "30"
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(["strategy", "portfolio-status", "--config", str(config_path), "--symbol", "BTC/USDT"], capsys)

    assert code == 0
    score_cards = payload["strategy_portfolio_status"]["score_cards"]
    spot_perp = next(card for card in score_cards if card["strategy_name"] == "spot-perp-carry")
    assert Decimal(spot_perp["score"]) < Decimal("50")
    assert "repeated_demo_preflight_not_profitable" in spot_perp["reasons"]
    assert "demo_break_even_gap_usdt:0.258630" in spot_perp["reasons"]


def test_cli_strategy_universe_regime_and_exit_commands(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(
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

    with pytest.raises(SystemExit) as universe_help_exit:
        main(["strategy", "universe", "--help"])
    universe_help = capsys.readouterr()
    assert universe_help_exit.value.code == 0
    assert "--exchange" in universe_help.out

    with pytest.raises(SystemExit) as exit_help_exit:
        main(["strategy", "exit-optimize", "--help"])
    exit_help = capsys.readouterr()
    assert exit_help_exit.value.code == 0
    assert "--strategy" in exit_help.out

    code, payload = _invoke(["strategy", "universe", "--config", str(config_path), "--exchange", "mock"], capsys)
    assert code == 0
    assert payload["strategy_universe"]["read_only"] is True
    assert payload["strategy_universe"]["orders_sent"] is False
    assert payload["strategy_universe"]["accepted_symbols"]

    code, payload = _invoke(
        ["strategy", "regime-report", "--config", str(config_path), "--exchange", "mock", "--symbol", "BTC/USDT"],
        capsys,
    )
    assert code == 0
    assert payload["strategy_regime_report"]["symbol"] == "BTC/USDT"
    assert payload["strategy_regime_report"]["accepted"] is True

    code, payload = _invoke(
        ["strategy", "exit-optimize", "--config", str(config_path), "--strategy", "trend-breakout", "--symbol", "BTC/USDT", "--exchange", "mock"],
        capsys,
    )
    assert code == 0
    assert payload["strategy_exit_optimization"]["read_only"] is True
    assert payload["strategy_exit_optimization"]["orders_sent"] is False
    assert payload["strategy_exit_optimization"]["candidates"]

    code, payload = _invoke(
        ["strategy", "position-report", "--config", str(config_path), "--strategy", "trend-breakout", "--symbol", "BTC/USDT", "--exchange", "mock"],
        capsys,
    )
    assert code == 0
    assert payload["strategy_position_report"]["read_only"] is True
    assert payload["strategy_position_report"]["live_orders_sent"] is False


def test_cli_strategy_range_grid_scans_and_runs_in_paper(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "range-grid.yaml"
    config_path.write_text(
        f"""
universe:
  trend_return_threshold_pct: "100"
  range_volatility_max_pct: "10"
range_grid:
  max_range_width_pct: "20"
  min_grid_spacing_pct: "0.50"
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

    code, payload = _invoke(
        ["strategy", "scan", "--config", str(config_path), "--strategy", "range-grid", "--symbol", "BTC/USDT", "--exchange", "mock"],
        capsys,
    )
    assert code == 0
    report = payload["strategy_scan"][0]
    assert report["strategy_name"] == "range-grid"
    assert report["opportunities"]
    assert report["opportunities"][0]["metadata"]["paper_only"] is True

    code, payload = _invoke(
        [
            "strategy",
            "run",
            "--config",
            str(config_path),
            "--strategy",
            "range-grid",
            "--symbol",
            "BTC/USDT",
            "--max-cycles",
            "1",
            "--interval-seconds",
            "0",
            "--execution-mode",
            "paper",
        ],
        capsys,
    )
    assert code == 0
    result = payload["strategy_run"]["results"][0]
    assert result["strategy_name"] == "range-grid"
    assert result["decision"] == "executed"
    assert result["execution"]["dry_run"] is True


def test_cli_strategy_smart_dca_scans_and_runs_in_paper(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "smart-dca.yaml"
    config_path.write_text(
        f"""
smart_dca:
  min_drawdown_pct: "1"
  target_weights_pct:
    BTC/USDT: "60"
    ETH/USDT: "25"
    SOL/USDT: "15"
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

    code, payload = _invoke(
        ["strategy", "scan", "--config", str(config_path), "--strategy", "smart-dca-basket", "--symbol", "BTC/USDT", "--exchange", "mock"],
        capsys,
    )
    assert code == 0
    report = payload["strategy_scan"][0]
    assert report["strategy_name"] == "smart-dca-basket"
    assert report["opportunities"]
    assert report["opportunities"][0]["metadata"]["paper_only"] is True
    assert Decimal(report["opportunities"][0]["metadata"]["dca_order"]["drawdown_pct"]) > Decimal("1")

    code, payload = _invoke(
        [
            "strategy",
            "run",
            "--config",
            str(config_path),
            "--strategy",
            "smart-dca-basket",
            "--symbol",
            "BTC/USDT",
            "--max-cycles",
            "1",
            "--interval-seconds",
            "0",
            "--execution-mode",
            "paper",
        ],
        capsys,
    )
    assert code == 0
    result = payload["strategy_run"]["results"][0]
    assert result["strategy_name"] == "smart-dca-basket"
    assert result["decision"] == "executed"
    assert result["execution"]["dry_run"] is True

    code, payload = _invoke(
        ["arbitrage", "scan", "--type", "smart-dca-basket", "--symbol", "SOL/USDT", "--exchange", "mock"],
        capsys,
    )
    assert code == 0
    assert payload["opportunities"][0]["strategy_type"] == "smart-dca-basket"


def test_cli_strategy_advisory_rank_is_read_only(tmp_path: Path, capsys) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        json.dumps(
            {
                "event": "strategy_cycle",
                "created_at": "2026-05-16T00:00:00+00:00",
                "strategy_name": "smart-dca-basket",
                "decision": "executed",
                "execution_mode": "paper",
                "opportunities_found": 1,
                "selected_opportunity_id": "smart-dca-basket-mock-sol-usdt",
                "risk_approved": True,
                "risk_reasons": [],
                "net_profit": "1.25",
                "execution": {"status": "simulated", "dry_run": True, "orders": [], "net_profit": "1.25"},
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "advisory-rank.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {journal_path}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  retrospective_path: {tmp_path / "strategy-retrospective.md"}
  retrospective_state_path: {tmp_path / "strategy-retrospective.state.json"}
  evolution_state_path: {tmp_path / "strategy-evolution.state.json"}
  evolution_report_path: {tmp_path / "strategy-evolution.md"}
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(
        [
            "strategy",
            "advisory-rank",
            "--config",
            str(config_path),
            "--strategy",
            "smart-dca-basket",
            "--symbol",
            "SOL/USDT",
            "--execution-mode",
            "paper",
            "--limit",
            "10",
            "--window",
            "24h",
        ],
        capsys,
    )

    assert code == 0
    report = payload["strategy_advisory_rank"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["model_policy"]["external_model_called"] is False
    assert report["rankings"][0]["strategy_name"] == "smart-dca-basket"
    assert report["rankings"][0]["recommendation"] in {"prioritize_paper_validation", "collect_more_samples"}


def test_cli_strategy_diversification_report_is_read_only(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "diversification.yaml"
    config_path.write_text(
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

    code, payload = _invoke(
        [
            "strategy",
            "diversification-report",
            "--config",
            str(config_path),
            "--strategy",
            "all",
            "--symbol",
            "BTC/USDT",
            "--execution-mode",
            "paper",
            "--limit",
            "20",
            "--window",
            "24h",
            "--max-family-share-pct",
            "60",
            "--min-queue-quality-score",
            "80",
        ],
        capsys,
    )

    assert code == 0
    report = payload["strategy_diversification_report"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["summary"]["max_family_share_pct"] == "60"
    assert "triangular" in {row["family"] for row in report["families"]}
    assert report["summary"]["validation_queue_count"] == len(report["validation_queue"])
    assert report["summary"]["min_queue_quality_score"] == "80"
    assert report["summary"]["filtered_validation_queue_count"] >= 1
    assert report["summary"]["non_triangular_queue_count"] >= 1
    assert all(Decimal(row["quality_score"]) >= Decimal("80") for row in report["validation_queue"])
    assert report["validation_queue"][0]["family"] != "triangular"
    assert "family_concentration_guard" in report["guardrails"]


def test_cli_strategy_directional_sleeve_status_is_read_only(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "directional-sleeve.yaml"
    config_path.write_text(
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

    code, payload = _invoke(
        [
            "strategy",
            "directional-sleeve-status",
            "--config",
            str(config_path),
            "--execution-mode",
            "paper",
            "--limit",
            "20",
        ],
        capsys,
    )

    assert code == 0
    report = payload["strategy_directional_sleeve_status"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["summary"]["directional_live_supported"] is False
    assert "directional_live_trading_not_supported" in report["guardrails"]
    assert {row["strategy_name"] for row in report["strategies"]} >= {"trend-breakout", "orderbook-imbalance-scalp"}


def test_cli_strategy_dex_lp_readiness_is_read_only(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "dex-readiness.yaml"
    config_path.write_text(
        """
dex_lp:
  enabled: false
  gateway_enabled: false
  network: none
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(["strategy", "dex-lp-readiness", "--config", str(config_path)], capsys)

    assert code == 0
    report = payload["strategy_dex_lp_readiness"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["status"] == "Deferred"
    assert report["testnet_ready"] is False
    assert report["execution_supported"] is False
    assert "dex_lp_disabled" in report["reasons"]


def test_cli_strategy_hedged_maker_scans_and_runs_in_paper(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "hedged-maker.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "strategy-events.jsonl"}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  retrospective_path: {tmp_path / "strategy-retrospective.md"}
  retrospective_state_path: {tmp_path / "strategy-retrospective.state.json"}
  evolution_state_path: {tmp_path / "strategy-evolution.state.json"}
  evolution_report_path: {tmp_path / "strategy-evolution.md"}
hedged_maker:
  paper_state_path: {tmp_path / "hedged-maker-paper-state.json"}
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(
        ["strategy", "scan", "--config", str(config_path), "--strategy", "hedged-maker", "--symbol", "BTC/USDT", "--exchange", "mock"],
        capsys,
    )
    assert code == 0
    report = payload["strategy_scan"][0]
    assert report["strategy_name"] == "hedged-maker"
    assert report["opportunities"]
    assert report["opportunities"][0]["metadata"]["paper_only"] is True
    assert report["opportunities"][0]["metadata"]["maker_order_type"] == "limit_post_only"

    code, payload = _invoke(
        [
            "strategy",
            "run",
            "--config",
            str(config_path),
            "--strategy",
            "hedged-maker",
            "--symbol",
            "BTC/USDT",
            "--max-cycles",
            "1",
            "--interval-seconds",
            "0",
            "--execution-mode",
            "paper",
        ],
        capsys,
    )
    assert code == 0
    result = payload["strategy_run"]["results"][0]
    assert result["strategy_name"] == "hedged-maker"
    assert result["decision"] == "executed"
    assert result["execution"]["dry_run"] is True
    lifecycle = result["execution"]["hedged_maker_lifecycle"]
    assert lifecycle["status"] == "quoted"
    assert lifecycle["orders_sent"] is False
    assert lifecycle["live_orders_sent"] is False
    assert lifecycle["realized_net_profit_usdt"] == "0"
    assert (tmp_path / "hedged-maker-paper-state.json").exists()
    assert payload["retrospective_after"]["open_issue_count"] == 0

    code, payload = _invoke(
        [
            "strategy",
            "run",
            "--config",
            str(config_path),
            "--strategy",
            "hedged-maker",
            "--symbol",
            "BTC/USDT",
            "--max-cycles",
            "1",
            "--interval-seconds",
            "0",
            "--execution-mode",
            "paper",
        ],
        capsys,
    )
    assert code == 0
    result = payload["strategy_run"]["results"][0]
    assert result["execution"]["hedged_maker_lifecycle"]["status"] == "active_quote_unchanged"
    assert result["budget"]["active_orders"] == 1
    assert result["budget"]["projected_open_orders"] == 1
    assert Decimal(result["budget"]["active_capital_usdt"]) > Decimal("0")
    assert result["budget"]["projected_strategy_capital_usdt"] == result["budget"]["active_capital_usdt"]

    code, payload = _invoke(["strategy", "hedged-maker-report", "--config", str(config_path), "--limit", "10"], capsys)
    assert code == 0
    report = payload["strategy_hedged_maker_report"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["scanned_events"] == 2
    assert report["summary"]["lifecycle_counts"]["quoted"] == 1
    assert report["summary"]["active_state_orders"] == 1


def test_cli_strategy_hedged_maker_demo_candidate_is_read_only(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "hedged-maker-candidate.yaml"
    config_path.write_text(
        f"""
trading:
  live_trading: false
  dry_run: false
  require_confirm_before_order: false
exchanges:
  okx:
    enabled: true
    sandbox: true
    adapter: mock
    okx_demo: true
hedged_maker:
  quote_spread_pct: 0.50
  min_edge_pct: 0.01
  quote_notional_usdt: 80
  demo_state_path: {tmp_path / "hedged-maker-demo-state.json"}
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

    code, payload = _invoke(
        [
            "strategy",
            "hedged-maker-demo-candidate",
            "--config",
            str(config_path),
            "--symbol",
            "BTC/USDT",
            "--target-exchange",
            "okx",
        ],
        capsys,
    )

    assert code == 0
    report = payload["strategy_hedged_maker_demo_candidate"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["target_exchange"] == "okx"
    assert report["opportunity_file_payload"]["strategy_type"] == "hedged-maker"
    assert report["opportunity_file_payload"]["metadata"]["maker_quote"]["exchange"] == "okx"
    assert report["demo_manager_compatible"] is False
    assert "target_exchange_adapter_is_mock" in report["reasons"]


def test_cli_strategy_retrospective_empty_history(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "empty-strategy-events.jsonl"}
  runtime_guard_path: {tmp_path / "empty-runtime-guard.json"}
  retrospective_path: {tmp_path / "empty-retrospective.md"}
  retrospective_state_path: {tmp_path / "empty-retrospective.state.json"}
  evolution_state_path: {tmp_path / "empty-evolution.state.json"}
  evolution_report_path: {tmp_path / "empty-evolution.md"}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as help_exit:
        main(["strategy", "retrospective", "--help"])
    help_output = capsys.readouterr()
    assert help_exit.value.code == 0
    assert "--json" in help_output.out

    with pytest.raises(SystemExit) as report_help_exit:
        main(["strategy", "validation-report", "--help"])
    report_help_output = capsys.readouterr()
    assert report_help_exit.value.code == 0
    assert "--limit" in report_help_output.out

    with pytest.raises(SystemExit) as pnl_help_exit:
        main(["strategy", "pnl-attribution", "--help"])
    pnl_help_output = capsys.readouterr()
    assert pnl_help_exit.value.code == 0
    assert "--execution-mode" in pnl_help_output.out

    with pytest.raises(SystemExit) as hedged_maker_report_help_exit:
        main(["strategy", "hedged-maker-report", "--help"])
    hedged_maker_report_help_output = capsys.readouterr()
    assert hedged_maker_report_help_exit.value.code == 0
    assert "--limit" in hedged_maker_report_help_output.out

    with pytest.raises(SystemExit) as hedged_maker_demo_help_exit:
        main(["strategy", "hedged-maker-demo", "--help"])
    hedged_maker_demo_help_output = capsys.readouterr()
    assert hedged_maker_demo_help_exit.value.code == 0
    assert "--opportunity-file" in hedged_maker_demo_help_output.out

    with pytest.raises(SystemExit) as hedged_maker_candidate_help_exit:
        main(["strategy", "hedged-maker-demo-candidate", "--help"])
    hedged_maker_candidate_help_output = capsys.readouterr()
    assert hedged_maker_candidate_help_exit.value.code == 0
    assert "--target-exchange" in hedged_maker_candidate_help_output.out

    with pytest.raises(SystemExit) as operator_brief_help_exit:
        main(["strategy", "operator-brief", "--help"])
    operator_brief_help_output = capsys.readouterr()
    assert operator_brief_help_exit.value.code == 0
    assert "--limit" in operator_brief_help_output.out

    with pytest.raises(SystemExit) as carry_basis_help_exit:
        main(["strategy", "carry-basis-optimize", "--help"])
    carry_basis_help_output = capsys.readouterr()
    assert carry_basis_help_exit.value.code == 0
    assert "--symbol" in carry_basis_help_output.out
    assert "--symbols" in carry_basis_help_output.out
    assert "--target-exchange" in carry_basis_help_output.out
    assert "--min-quality-score" in carry_basis_help_output.out
    assert "--max-symbols" in carry_basis_help_output.out
    assert "--request-budget-seconds" in carry_basis_help_output.out
    assert "--per-symbol-timeout-seconds" in carry_basis_help_output.out

    with pytest.raises(SystemExit) as opportunity_help_exit:
        main(["strategy", "opportunity-report", "--help"])
    opportunity_help_output = capsys.readouterr()
    assert opportunity_help_exit.value.code == 0
    assert "--window" in opportunity_help_output.out

    with pytest.raises(SystemExit) as advisory_rank_help_exit:
        main(["strategy", "advisory-rank", "--help"])
    advisory_rank_help_output = capsys.readouterr()
    assert advisory_rank_help_exit.value.code == 0
    assert "--window" in advisory_rank_help_output.out

    with pytest.raises(SystemExit) as diversification_help_exit:
        main(["strategy", "diversification-report", "--help"])
    diversification_help_output = capsys.readouterr()
    assert diversification_help_exit.value.code == 0
    assert "--max-family-share-pct" in diversification_help_output.out
    assert "--min-queue-quality-score" in diversification_help_output.out

    with pytest.raises(SystemExit) as directional_sleeve_help_exit:
        main(["strategy", "directional-sleeve-status", "--help"])
    directional_sleeve_help_output = capsys.readouterr()
    assert directional_sleeve_help_exit.value.code == 0
    assert "--execution-mode" in directional_sleeve_help_output.out

    with pytest.raises(SystemExit) as dex_lp_help_exit:
        main(["strategy", "dex-lp-readiness", "--help"])
    dex_lp_help_output = capsys.readouterr()
    assert dex_lp_help_exit.value.code == 0
    assert "--config" in dex_lp_help_output.out

    with pytest.raises(SystemExit) as discover_routes_help_exit:
        main(["strategy", "discover-routes", "--help"])
    discover_routes_help_output = capsys.readouterr()
    assert discover_routes_help_exit.value.code == 0
    assert "--quote" in discover_routes_help_output.out

    with pytest.raises(SystemExit) as compare_help_exit:
        main(["strategy", "market-compare", "--help"])
    compare_help_output = capsys.readouterr()
    assert compare_help_exit.value.code == 0
    assert "--target-exchange" in compare_help_output.out

    with pytest.raises(SystemExit) as evolve_help_exit:
        main(["strategy", "evolve", "--help"])
    evolve_help_output = capsys.readouterr()
    assert evolve_help_exit.value.code == 0
    assert "--execution-mode" in evolve_help_output.out

    with pytest.raises(SystemExit) as revival_help_exit:
        main(["strategy", "revival-window", "--help"])
    revival_help_output = capsys.readouterr()
    assert revival_help_exit.value.code == 0
    assert "--cycles" in revival_help_output.out

    with pytest.raises(SystemExit) as demo_window_help_exit:
        main(["strategy", "demo-window", "--help"])
    demo_window_help_output = capsys.readouterr()
    assert demo_window_help_exit.value.code == 0
    assert "--public-health-only" in demo_window_help_output.out

    with pytest.raises(SystemExit) as demo_sampling_help_exit:
        main(["strategy", "demo-sampling", "--help"])
    demo_sampling_help_output = capsys.readouterr()
    assert demo_sampling_help_exit.value.code == 0
    assert "--cycles-per-window" in demo_sampling_help_output.out

    with pytest.raises(SystemExit) as candidate_help_exit:
        main(["strategy", "candidate-backtest", "--help"])
    candidate_help_output = capsys.readouterr()
    assert candidate_help_exit.value.code == 0
    assert "--limit" in candidate_help_output.out

    code, payload = _invoke(["strategy", "retrospective", "--config", str(config_path)], capsys)

    assert code == 0
    assert payload["strategy_retrospective"]["open_issue_count"] == 0
    assert payload["strategy_retrospective"]["top_open_issues"] == []
    assert payload["strategy_retrospective"]["optimization_pressure"] == []

    code, payload = _invoke(["strategy", "validation-report", "--config", str(config_path), "--limit", "10"], capsys)
    assert code == 0
    assert payload["strategy_validation_report"]["scanned_events"] == 0
    assert payload["strategy_validation_report"]["total"]["executed"] == 0

    code, payload = _invoke(["strategy", "opportunity-report", "--config", str(config_path), "--window", "24h"], capsys)
    assert code == 0
    assert payload["strategy_opportunity_report"]["scan_count"] == 0
    assert payload["strategy_opportunity_report"]["orders_sent"] is False

    code, payload = _invoke(["strategy", "evolve", "--config", str(config_path), "--limit", "10"], capsys)
    assert code == 0
    assert payload["strategy_evolution"]["orders_sent"] is False
    assert payload["strategy_evolution"]["live_orders_sent"] is False
    assert payload["strategy_evolution"]["decisions"]

    code, payload = _invoke(["strategy", "revival-window", "--config", str(config_path), "--cycles", "1"], capsys)
    assert code == 0
    assert payload["strategy_revival_window"]["candidates"] == []
    assert payload["strategy_revival_window"]["orders_sent"] is False
    assert payload["strategy_revival_window"]["live_orders_sent"] is False

    code, payload = _invoke(["strategy", "candidate-backtest", "--config", str(config_path), "--strategy", "trend-breakout", "--limit", "10"], capsys)
    assert code == 0
    assert payload["strategy_candidate_backtest"]["orders_sent"] is False
    assert payload["strategy_candidate_backtest"]["live_orders_sent"] is False
    assert payload["strategy_candidate_backtest"]["checked_in_config_modified"] is False
    assert payload["strategy_candidate_backtest"]["candidates"]


def test_cli_strategy_pnl_attribution_reads_journal_without_trading(tmp_path: Path, capsys) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "strategy_cycle",
                        "execution_mode": "demo",
                        "strategy_name": "triangular-multi-route",
                        "decision": "executed",
                        "net_profit": "0.20",
                        "execution": {
                            "preflight": {"net_pnl_usdt": "0.18"},
                            "pnl_validation": {
                                "cash_flow_net_pnl_usdt": "0.20",
                                "equity_delta_usdt": "0.23",
                                "within_tolerance": True,
                                "residual_inventory_usdt": "0.01",
                                "residual_inventory_within_tolerance": True,
                            },
                        },
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(f"strategy_runtime:\n  journal_path: {journal_path}\n", encoding="utf-8")

    code, payload = _invoke(
        [
            "strategy",
            "pnl-attribution",
            "--config",
            str(config_path),
            "--execution-mode",
            "demo",
            "--strategy",
            "triangular-multi-route",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    report = payload["strategy_pnl_attribution"]
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert report["summary"]["strategy_cash_flow_net_pnl_usdt"] == "0.200000"


def test_cli_strategy_operator_brief_is_read_only(tmp_path: Path, capsys) -> None:
    journal_path = tmp_path / "strategy-events.jsonl"
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {journal_path}
  runtime_guard_path: {tmp_path / "strategy-runtime-guard.json"}
  demo_strategy_size_overrides:
    cross-exchange:
      order_size_multiplier: "1"
      max_order_value_usdt: "10"
""".strip(),
        encoding="utf-8",
    )
    journal_path.write_text(
        json.dumps(
            {
                "event": "strategy_cycle",
                "execution_mode": "demo",
                "strategy_name": "cross-exchange",
                "decision": "executed",
                "net_profit": "0.12",
                "risk_reasons": [],
                "execution": {
                    "demo_orders_sent": True,
                    "live_orders_sent": False,
                    "pnl_validation": {"within_tolerance": True, "residual_inventory_within_tolerance": True},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    code, payload = _invoke(
        ["strategy", "operator-brief", "--config", str(config_path), "--execution-mode", "demo", "--strategy", "all"],
        capsys,
    )

    assert code == 0
    brief = payload["strategy_operator_brief"]
    assert brief["orders_sent"] is False
    assert brief["live_orders_sent"] is False
    assert brief["safety"]["live_trading"] is False
    assert brief["size_stage"]["demo_order_size_multiplier"] == "1"
    assert brief["size_stage"]["demo_strategy_size_overrides"]["cross-exchange"]["max_order_value_usdt"] == "10"
    assert brief["validation"]["total"]["executed"] == 1
    assert "live_canary_disabled" in brief["recommendations"]


def test_cli_strategy_carry_basis_optimize_is_read_only(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "strategy.yaml"
    config_path.write_text(
        f"""
strategy_runtime:
  journal_path: {tmp_path / "strategy-events.jsonl"}
  runtime_guard_path: {tmp_path / "runtime-guard.json"}
  retrospective_path: {tmp_path / "retrospective.md"}
  retrospective_state_path: {tmp_path / "retrospective.state.json"}
""".strip(),
        encoding="utf-8",
    )

    code, payload = _invoke(
        ["strategy", "carry-basis-optimize", "--config", str(config_path), "--symbol", "BTC/USDT", "--json"],
        capsys,
    )

    assert code == 0
    report = payload["strategy_carry_basis_optimization"]
    assert report["read_only"] is True
    assert report["orders_sent"] is False
    assert report["live_orders_sent"] is False
    assert {card["strategy_name"] for card in report["cards"]} == {
        "funding-carry-hedged",
        "spot-perp-carry",
        "futures-perp-basis",
    }

    code, payload = _invoke(
        [
            "strategy",
            "carry-basis-optimize",
            "--config",
            str(config_path),
            "--symbol",
            "BTC/USDT",
            "--target-exchange",
            "mock",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    report = payload["strategy_carry_basis_optimization"]
    assert report["target_exchange"] == "mock"
    assert all("unlock_priority" in card for card in report["cards"])
    assert "target_demo_preflight_candidate_count" in report["summary"]

    code, payload = _invoke(
        [
            "strategy",
            "carry-basis-optimize",
            "--config",
            str(config_path),
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--target-exchange",
            "mock",
            "--min-quality-score",
            "80",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    sweep = payload["strategy_carry_basis_optimization"]
    assert sweep["mode"] == "sweep"
    assert sweep["read_only"] is True
    assert sweep["orders_sent"] is False
    assert sweep["live_orders_sent"] is False
    assert sweep["symbols"] == ["BTC/USDT", "ETH/USDT"]
    assert sweep["target_exchange"] == "mock"
    assert len(sweep["reports"]) == 2
    assert sweep["ranked_cards"]
    assert all(Decimal(card["quality_score"]) >= Decimal("80") for card in sweep["ranked_cards"])
    assert sweep["summary"]["symbol_count"] == 2
    assert sweep["summary"]["total_card_count"] == 6
    assert sweep["summary"]["min_quality_score"] == "80"
    assert "target_demo_preflight_candidate_count" in sweep["summary"]

    code, payload = _invoke(
        [
            "strategy",
            "carry-basis-optimize",
            "--config",
            str(config_path),
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--request-budget-seconds",
            "0",
            "--per-symbol-timeout-seconds",
            "0.01",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    sweep = payload["strategy_carry_basis_optimization"]
    assert sweep["reports"] == []
    assert sweep["ranked_cards"] == []
    assert sweep["summary"]["requested_symbol_count"] == 2
    assert sweep["summary"]["skipped_symbol_count"] == 2
    assert sweep["summary"]["request_budget_seconds"] == "0"
    assert [observation["status"] for observation in sweep["observations"]] == [
        "skipped_request_budget",
        "skipped_request_budget",
    ]


def test_cli_strategy_validate_demo_blocks_with_safe_default_config(capsys) -> None:
    code = main(["strategy", "validate-demo", "--config", str(EXAMPLE_CONFIG), "--strategy", "all", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "enabled OKX sandbox" in payload["error"]["message"]


def test_cli_strategy_validate_demo_window_blocks_with_safe_default_config(capsys) -> None:
    code = main(["strategy", "validate-demo-window", "--config", str(EXAMPLE_CONFIG), "--strategy", "all", "--cycles", "1", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "enabled OKX sandbox" in payload["error"]["message"]


def test_cli_strategy_demo_window_blocks_with_safe_default_config(capsys) -> None:
    code = main(["strategy", "demo-window", "--config", str(EXAMPLE_CONFIG), "--strategy", "triangular-multi-route", "--cycles", "1", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "enabled OKX sandbox" in payload["error"]["message"]


def test_cli_strategy_demo_sampling_blocks_with_safe_default_config(capsys) -> None:
    code = main(["strategy", "demo-sampling", "--config", str(EXAMPLE_CONFIG), "--strategy", "triangular-multi-route", "--windows", "1", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert code != 0
    assert payload["error"]["type"] == "SafetyError"
    assert "enabled OKX sandbox" in payload["error"]["message"]


def _okx_opportunity_payload() -> dict:
    opportunity = ArbitrageOpportunity(
        opportunity_id="okx-file-opportunity",
        strategy_type="cross-exchange",
        symbol="BTC/USDT",
        buy_exchange="okx",
        sell_exchange="okx",
        expected_profit=Decimal("2.0"),
        expected_profit_pct=Decimal("2.0"),
        estimated_fee=Decimal("0.2"),
        estimated_slippage=Decimal("0.01"),
        required_capital=Decimal("100"),
        net_profit=Decimal("1.79"),
        risk_score=Decimal("0.2"),
        confidence=Decimal("0.85"),
        metadata={
            "quantity": "0.002",
            "execution_quality": {
                "spread_persistence": {"passed": True},
                "depth_fill": {
                    "buy": {"complete": True},
                    "sell": {"complete": True},
                },
            },
            "legs": [
                {"exchange": "okx", "side": "buy", "market": "spot", "symbol": "BTC/USDT", "price": "50010"},
                {"exchange": "okx", "side": "sell", "market": "spot", "symbol": "BTC/USDT", "price": "50280"},
            ],
        },
    )
    return opportunity.to_dict()


def _hedged_maker_okx_opportunity_payload() -> dict:
    opportunity = ArbitrageOpportunity(
        opportunity_id="hm-okx-demo",
        strategy_type="hedged-maker",
        symbol="BTC/USDT",
        buy_exchange="okx",
        sell_exchange="okx",
        expected_profit=Decimal("1.20"),
        expected_profit_pct=Decimal("1.50"),
        estimated_fee=Decimal("0.10"),
        estimated_slippage=Decimal("0.01"),
        required_capital=Decimal("80"),
        net_profit=Decimal("1.09"),
        risk_score=Decimal("0.20"),
        confidence=Decimal("0.80"),
        metadata={
            "maker_quote": {
                "exchange": "okx",
                "symbol": "BTC/USDT",
                "side": "buy",
                "price": "79990",
                "quantity": "0.001",
                "notional_usdt": "79.99",
            },
            "hedge_preview": {
                "exchange": "okx",
                "symbol": "BTC/USDT",
                "side": "sell",
                "price": "79980",
                "quantity": "0.001",
                "notional_usdt": "79.98",
            },
            "execution_quality": {
                "spread_persistence": {"passed": True},
                "depth_fill": {
                    "buy": {"complete": True},
                    "sell": {"complete": True},
                },
            },
        },
    )
    return opportunity.to_dict()
