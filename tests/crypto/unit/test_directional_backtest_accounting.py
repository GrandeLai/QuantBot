"""Synthetic accounting regressions for the legacy directional backtest."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.crypto.runtime.backtesting.engine import BacktestEngine
from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.directional.backtest import DirectionalBacktestEngine
from quantbot.crypto.runtime.directional.models import DirectionalSignal
from quantbot.crypto.runtime.exchanges.base import Candle
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory


D = Decimal
EXAMPLE_CONFIG = Path(__file__).resolve().parents[3] / "config" / "crypto" / "config.example.yaml"


class _OneEntry:
    def __init__(self, time_limit_minutes: int = 60):
        self.time_limit_minutes = time_limit_minutes

    def generate_signal(self, candles, **kwargs):
        return DirectionalSignal(
            strategy_name="trend-breakout",
            symbol=kwargs["symbol"],
            exchange=kwargs["exchange"],
            signal="buy" if len(candles) == 30 else "hold",
            confidence=D("1"),
            expected_edge_pct=D("0"),
            stop_loss_pct=D("50"),
            take_profit_pct=D("50"),
            time_limit_minutes=self.time_limit_minutes,
        )


def _candles(count=35, minutes=15):
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return [
        Candle(
            exchange="mock", symbol="BTC/USDT", bar=f"{minutes}m",
            open=D("100"), high=D("100"), low=D("100"), close=D("100"), volume=D("200"),
            timestamp=start + timedelta(minutes=minutes * index),
        )
        for index in range(count)
    ]


def _run(monkeypatch, candles, *, fee=D("0"), time_limit=60):
    monkeypatch.setattr(
        "quantbot.crypto.runtime.directional.backtest.strategy_for_name",
        lambda name: _OneEntry(time_limit),
    )
    return DirectionalBacktestEngine(notional_usdt=D("100"), fee_pct=fee, slippage_pct=D("0")).run(
        "trend-breakout", "BTC/USDT", "mock", candles,
    )


@pytest.mark.parametrize("count", [31, 35], ids=["terminal_liquidation", "deadline_exit"])
def test_flat_round_trip_deducts_both_fees_and_reconciles_equity(monkeypatch, count):
    result = _run(monkeypatch, _candles(count), fee=D("0.001"))

    assert result.total_trades == 1
    assert result.net_pnl_usdt == D("-0.2")
    assert result.fees_usdt == D("0.2")
    assert result.gross_pnl_usdt == D("0")
    assert result.losses == 1
    assert result.equity_curve_usdt[-1] == D("99.8")
    assert result.equity_curve_usdt[-1] - result.equity_curve_usdt[0] == result.net_pnl_usdt
    assert result.max_drawdown_pct == D("0.2")


@pytest.mark.parametrize("gap_minutes", [60, 90])
def test_deadline_uses_actual_timestamps_and_first_eligible_open(monkeypatch, gap_minutes):
    candles = _candles(minutes=60)
    # The close is deliberately different from the opening execution price.
    candles[31] = replace(
        candles[31], timestamp=candles[30].timestamp + timedelta(minutes=gap_minutes),
        high=D("120"), close=D("120"),
    )
    for index in range(32, len(candles)):
        candles[index] = replace(candles[index], open=D("120"), high=D("120"), low=D("120"), close=D("120"))
    result = _run(monkeypatch, candles)

    assert result.average_hold_minutes == D(gap_minutes)
    assert result.net_pnl_usdt == D("0")
    assert result.total_trades == 1


@pytest.mark.parametrize("initial, expected_drawdown", [(D("100"), D("20")), (D("1000"), D("2"))])
def test_summary_preserves_open_position_drawdown_and_account_capital(monkeypatch, initial, expected_drawdown):
    candles = _candles(33)
    candles[31] = replace(candles[31], low=D("80"), close=D("80"))
    candles[32] = replace(candles[32], high=D("110"), close=D("110"))
    monkeypatch.setattr(
        "quantbot.crypto.runtime.directional.backtest.strategy_for_name",
        lambda name: _OneEntry(10000),
    )
    settings = load_settings(EXAMPLE_CONFIG)
    settings.backtest.initial_capital_usdt = initial
    settings.backtest.fee_pct = D("0")
    settings.backtest.slippage_pct = D("0")
    settings.directional.max_position_value_usdt = D("100")
    engine = BacktestEngine(settings, ExchangeFactory(settings))
    monkeypatch.setattr(engine, "_completed_candles", lambda *args: candles)

    result = engine.run()

    assert result.directional_result.max_drawdown_pct == D("20")
    assert result.metrics.max_drawdown_pct == expected_drawdown
    assert min(result.equity_curve) == initial - D("20")
    assert result.metrics.final_equity == initial + D("10")
    assert len(result.equity_curve) > 2
    assert result.orders_sent is False


def test_summary_rejects_missing_equity_evidence(monkeypatch):
    directional = _run(monkeypatch, _candles())
    settings = load_settings(EXAMPLE_CONFIG)
    engine = BacktestEngine(settings, ExchangeFactory(settings))
    monkeypatch.setattr(engine, "_completed_candles", lambda *args: [])
    monkeypatch.setattr(engine, "_directional_backtest", lambda *args: replace(directional, equity_curve_usdt=[]))

    with pytest.raises(ValueError, match="missing its marked equity curve"):
        engine.run()
