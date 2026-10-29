"""Tests for P0 profit-expansion foundations."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.exchanges.base import Candle
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.position_executor import ExitOptimizerService, TripleBarrierPositionExecutor
from quantbot.crypto.runtime.strategies.universe import StrategyUniverseService


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_strategy_universe_accepts_liquid_mock_symbols(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)

    report = StrategyUniverseService(settings, ExchangeFactory(settings)).report(exchange="mock")

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.live_orders_sent is False
    assert report.exchange == "mock"
    assert report.accepted_symbols
    btc = next(row for row in report.symbols if row.symbol == "BTC/USDT")
    assert btc.accepted is True
    assert btc.regime in {"trend", "range", "carry", "avoid", "illiquid"}
    assert btc.spread_pct >= Decimal("0")


def test_strategy_universe_rejects_low_volume_symbols(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    settings.universe.min_24h_volume_usdt = Decimal("100000000000")

    report = StrategyUniverseService(settings, ExchangeFactory(settings)).report(exchange="mock")

    btc = next(row for row in report.symbols if row.symbol == "BTC/USDT")
    assert btc.accepted is False
    assert btc.regime == "illiquid"
    assert "volume_below_minimum" in btc.reasons


def test_triple_barrier_executor_exits_on_take_profit() -> None:
    candles = _position_candles(["100", "101", "102.5", "102.2"])
    executor = TripleBarrierPositionExecutor()

    result = executor.simulate_long(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        entry_price=Decimal("100"),
        quantity=Decimal("1"),
        candles=candles,
        take_profit_pct=Decimal("2"),
        stop_loss_pct=Decimal("1"),
        trailing_stop_pct=Decimal("0.5"),
        time_limit_bars=10,
        fee_pct=Decimal("0.001"),
    )

    assert result.exit_reason == "take_profit"
    assert result.orders_sent is False
    assert result.live_orders_sent is False
    assert result.net_pnl_usdt > Decimal("0")


def test_exit_optimizer_returns_read_only_ranked_candidates(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)

    report = ExitOptimizerService(settings, ExchangeFactory(settings)).optimize(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        exchange="mock",
    )

    assert report.read_only is True
    assert report.orders_sent is False
    assert report.live_orders_sent is False
    assert report.candidates
    top = report.candidates[0]
    assert top.take_profit_pct > Decimal("0")
    assert top.stop_loss_pct > Decimal("0")
    assert top.trailing_stop_pct >= Decimal("0")
    assert top.time_limit_bars > 0
    assert [candidate.score for candidate in report.candidates] == sorted(
        (candidate.score for candidate in report.candidates),
        reverse=True,
    )


def _position_candles(closes: list[str]) -> list[Candle]:
    from datetime import UTC, datetime, timedelta

    rows: list[Candle] = []
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        rows.append(
            Candle(
                exchange="mock",
                symbol="BTC/USDT",
                bar="15m",
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("100"),
                timestamp=datetime(2026, 5, 16, tzinfo=UTC) + timedelta(minutes=index * 15),
                complete=True,
            )
        )
    return rows


def _use_temp_runtime_paths(settings, tmp_path: Path) -> None:
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.retrospective_path = str(tmp_path / "strategy-retrospective.md")
    settings.strategy_runtime.retrospective_state_path = str(tmp_path / "strategy-retrospective.state.json")
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "strategy-evolution.state.json")
    settings.strategy_runtime.evolution_report_path = str(tmp_path / "strategy-evolution.md")
