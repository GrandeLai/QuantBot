"""Tests for realistic local backtest validation."""

from __future__ import annotations

from pathlib import Path

from quantbot.crypto.runtime.backtesting.engine import BacktestEngine
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_realistic_backtest_uses_completed_candles_and_no_orders() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    result = BacktestEngine(settings, ExchangeFactory(settings)).run(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        exchange="mock",
    )

    assert result.read_only is True
    assert result.orders_sent is False
    assert result.live_orders_sent is False
    assert result.strategy_name == "trend-breakout"
    assert result.symbol == "BTC/USDT"
    assert result.exchange == "mock"
    assert result.candle_count > 0
    assert result.directional_result is not None
    assert result.fill_model["completed_candles_only"] is True
    assert result.fill_model["fee_pct"] == settings.backtest.fee_pct
    assert result.fill_model["slippage_pct"] == settings.backtest.slippage_pct


def test_walk_forward_returns_forward_windows_without_orders() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    report = BacktestEngine(settings, ExchangeFactory(settings)).walk_forward(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        exchange="mock",
        windows=2,
    )

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.live_orders_sent is False
    assert report.strategy_name == "trend-breakout"
    assert len(report.windows) == 2
    assert all(window.train_candle_count > 0 for window in report.windows)
    assert all(window.validation_candle_count > 0 for window in report.windows)
    assert all(window.validation_result is not None for window in report.windows)


def test_bias_check_reports_completed_candle_prefix_diagnostics() -> None:
    settings = load_settings(EXAMPLE_CONFIG)

    report = BacktestEngine(settings, ExchangeFactory(settings)).bias_check(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        exchange="mock",
    )

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.live_orders_sent is False
    assert report.passed is True
    check_names = {check["name"] for check in report.checks}
    assert {"completed_candles_only", "warmup_window_enforced", "prefix_replay_stable"} <= check_names
