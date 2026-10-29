"""Unit tests for risk, dry-run execution, backtesting, and reporting."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.crypto.runtime.arbitrage.scanner import ArbitrageScanner
from quantbot.crypto.runtime.backtesting.engine import BacktestEngine
from quantbot.crypto.runtime.backtesting.metrics import calculate_backtest_metrics
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exceptions import SafetyError
from quantbot.crypto.runtime.execution.engine import ExecutionEngine
from quantbot.crypto.runtime.execution.paper import PaperTradingLedger
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.reporting.report import ReportGenerator
from quantbot.crypto.runtime.risk.manager import RiskManager


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def _first_opportunity():
    settings = load_settings(EXAMPLE_CONFIG)
    scanner = ArbitrageScanner(settings, ExchangeFactory(settings))
    return settings, scanner.scan("cross-exchange", symbol="BTC/USDT")[0]


def test_risk_manager_approves_default_mock_opportunity() -> None:
    settings, opportunity = _first_opportunity()
    decision = RiskManager(settings.risk).evaluate(opportunity)

    assert decision.approved is True
    assert decision.violations == []
    assert decision.risk_score <= Decimal("1")


def test_risk_manager_blocks_oversized_order() -> None:
    settings, opportunity = _first_opportunity()
    opportunity.required_capital = settings.risk.max_order_value_usdt + Decimal("1")

    decision = RiskManager(settings.risk).evaluate(opportunity)

    assert decision.approved is False
    assert "max_order_value_usdt" in decision.violations


def test_risk_manager_blocks_low_net_profit_pct() -> None:
    settings, opportunity = _first_opportunity()
    opportunity.net_profit = Decimal("0.01")

    decision = RiskManager(settings.risk).evaluate(opportunity)

    assert decision.approved is False
    assert "min_net_profit_pct" in decision.violations


def test_dry_run_execution_simulates_without_live_trading() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    engine = ExecutionEngine(settings, ExchangeFactory(settings))

    result = engine.execute("test-opportunity", dry_run=True)

    assert result.status == "simulated"
    assert result.dry_run is True
    assert result.risk_decision.approved is True
    assert result.orders[0].exchange == "mock"


def test_live_execution_is_blocked_without_full_safety_gate(tmp_path: Path) -> None:
    config_path = tmp_path / "live.yaml"
    config_path.write_text(
        """
trading:
  live_trading: true
  dry_run: false
  require_confirm_before_order: true
exchanges:
  mock:
    enabled: true
    sandbox: true
""".strip(),
        encoding="utf-8",
    )
    settings = load_settings(config_path)
    engine = ExecutionEngine(settings, ExchangeFactory(settings))

    with pytest.raises(SafetyError, match="real trading requires a non-mock exchange"):
        engine.execute("test-opportunity", dry_run=False)


def test_paper_trading_ledger_records_simulated_execution() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    result = ExecutionEngine(settings, ExchangeFactory(settings)).execute("test-opportunity", dry_run=True)
    ledger = PaperTradingLedger(starting_cash=Decimal("10000"))

    ledger.record(result)

    assert ledger.cash_usdt > Decimal("10000")
    assert ledger.realized_pnl_usdt == result.net_profit
    assert ledger.trade_count == 1


def test_backtest_metrics_and_engine_return_deterministic_results() -> None:
    metrics = calculate_backtest_metrics([Decimal("10000"), Decimal("10050"), Decimal("10025")], trades=2, wins=1)
    assert metrics.total_return_pct == Decimal("0.25")
    assert metrics.win_rate_pct == Decimal("50.00")

    settings = load_settings(EXAMPLE_CONFIG)
    result = BacktestEngine(settings, ExchangeFactory(settings)).run()
    assert result.metrics.trades >= 1
    assert result.metrics.total_return_pct >= Decimal("0")


def test_daily_report_redacts_sensitive_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QUANTBOT_CRYPTO_MOCK_API_KEY", "report-secret")
    settings = load_settings(EXAMPLE_CONFIG)
    report = ReportGenerator(settings, ExchangeFactory(settings)).generate("daily")

    rendered = str(report.to_dict())
    assert report.report_type == "daily"
    assert "report-secret" not in rendered
    assert report.sections["safety"]["dry_run"] is True
