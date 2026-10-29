"""Unit tests for OKX-first spot directional strategy expansion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantbot.crypto.runtime.config.loader import load_settings
from quantbot.crypto.runtime.directional.backtest import DirectionalBacktestEngine
from quantbot.crypto.runtime.directional.indicators import atr, bollinger_bands, donchian_channel, ema, rsi
from quantbot.crypto.runtime.directional.models import DirectionalPositionLifecycle, DirectionalSignal
from quantbot.crypto.runtime.directional.scanner import DirectionalOpportunityScanner
from quantbot.crypto.runtime.directional.strategies import TrendBreakoutStrategy
from quantbot.crypto.runtime.exchanges.base import Candle, utcnow
from quantbot.crypto.runtime.exchanges.factory import ExchangeFactory
from quantbot.crypto.runtime.strategies.directional_sleeve import DirectionalSleeveStatusService
from quantbot.crypto.runtime.strategies.registry import StrategyRegistry


ROOT = Path(__file__).resolve().parents[3]
EXAMPLE_CONFIG = ROOT / "config" / "crypto" / "config.example.yaml"


def test_directional_registry_adds_group_and_aggregate_alias() -> None:
    registry = StrategyRegistry()

    directional = registry.expand("directional-all")
    names = {definition.name for definition in directional}

    assert names >= {
        "trend-breakout",
        "mean-reversion-spot",
        "volatility-squeeze-breakout",
        "momentum-rotation",
        "orderbook-imbalance-scalp",
    }
    assert all(definition.category == "directional" for definition in directional)
    assert registry.get("directional-all").scanner_type == "directional-all"
    assert "orderbook-imbalance-scalp" not in registry.demo_validation_names()


def test_directional_indicators_use_decimal_outputs() -> None:
    candles = _trend_candles(count=40)
    closes = [candle.close for candle in candles]

    assert ema(closes, period=10)[-1] > ema(closes, period=20)[-1]
    assert Decimal("0") <= rsi(closes, period=14)[-1] <= Decimal("100")
    assert atr(candles, period=14)[-1] > Decimal("0")
    middle, upper, lower, bandwidth = bollinger_bands(closes, period=20)
    assert upper[-1] > middle[-1] > lower[-1]
    assert bandwidth[-1] > Decimal("0")
    high, low = donchian_channel(candles, period=20)
    assert high[-1] >= max(candle.high for candle in candles[-20:])
    assert low[-1] <= min(candle.low for candle in candles[-20:])


def test_trend_breakout_ignores_incomplete_breakout_candle() -> None:
    candles = _trend_candles(count=60)[:-1]
    incomplete_breakout = Candle(
        exchange="mock",
        symbol="BTC/USDT",
        bar="15m",
        open=Decimal("21000"),
        high=Decimal("23500"),
        low=Decimal("20950"),
        close=Decimal("23400"),
        volume=Decimal("5000"),
        timestamp=candles[-1].timestamp + timedelta(minutes=15),
        complete=False,
    )

    signal = TrendBreakoutStrategy().generate_signal([*candles, incomplete_breakout], symbol="BTC/USDT", exchange="mock")

    assert signal.signal == "hold"
    assert "incomplete_candle_ignored" in signal.reason_codes


def test_directional_signal_and_lifecycle_are_json_safe() -> None:
    signal = DirectionalSignal(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        exchange="mock",
        signal="buy",
        confidence=Decimal("0.72"),
        expected_edge_pct=Decimal("1.20"),
        stop_loss_pct=Decimal("0.80"),
        take_profit_pct=Decimal("1.80"),
        time_limit_minutes=90,
        reason_codes=["ema_trend", "donchian_breakout"],
    )
    lifecycle = DirectionalPositionLifecycle(
        strategy_name="trend-breakout",
        symbol="BTC/USDT",
        exchange="mock",
        state="planned",
        entry_signal=signal,
    )

    payload = lifecycle.to_dict()

    assert payload["state"] == "planned"
    assert payload["entry_signal"]["signal"] == "buy"
    assert payload["entry_signal"]["expected_edge_pct"] == "1.20"


def test_directional_scanner_wraps_buy_signal_as_opportunity(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    scanner = DirectionalOpportunityScanner(settings, ExchangeFactory(settings))

    opportunities, diagnostics = scanner.scan("trend-breakout", symbol="BTC/USDT", exchange="mock")

    assert diagnostics["approved"] is True
    assert opportunities
    best = opportunities[0]
    assert best.strategy_type == "trend-breakout"
    assert best.buy_exchange == "mock"
    assert best.sell_exchange is None
    assert best.net_profit > Decimal("0")
    assert best.metadata["directional_signal"]["signal"] == "buy"
    assert Decimal(str(best.metadata["backtest"]["profit_factor"])) >= Decimal("1.10")


def test_directional_sleeve_status_reports_promotion_rules(tmp_path: Path) -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    _use_temp_runtime_paths(settings, tmp_path)
    _write_directional_events(Path(settings.strategy_runtime.journal_path), "trend-breakout", count=2, net_profit=Decimal("0.40"))

    report = DirectionalSleeveStatusService(settings).status(execution_mode="paper", limit=20)
    payload = report.to_dict()

    assert payload["read_only"] is True
    assert payload["orders_sent"] is False
    assert payload["live_orders_sent"] is False
    assert payload["summary"]["directional_live_supported"] is False
    assert payload["summary"]["demo_max_order_value_usdt"] == "20"
    trend = next(row for row in payload["strategies"] if row["strategy_name"] == "trend-breakout")
    assert trend["stage"] == "demo_eligible_after_standard_gates"
    assert trend["validation_executed"] == 2
    scalp = next(row for row in payload["strategies"] if row["strategy_name"] == "orderbook-imbalance-scalp")
    assert scalp["stage"] == "paper_only"
    assert "directional_live_trading_not_supported" in payload["guardrails"]


def test_directional_backtest_reports_fee_adjusted_metrics() -> None:
    candles = _trend_candles(count=120)

    result = DirectionalBacktestEngine().run("trend-breakout", "BTC/USDT", "mock", candles)

    assert result.total_trades >= 1
    assert result.net_pnl_usdt > Decimal("0")
    assert result.profit_factor >= Decimal("1.10")
    assert result.max_drawdown_pct <= Decimal("5")
    assert result.account_equity_attribution["strategy_pnl_usdt"] == result.net_pnl_usdt


def _use_temp_runtime_paths(settings, tmp_path: Path) -> None:  # noqa: ANN001
    settings.strategy_runtime.journal_path = str(tmp_path / "strategy-events.jsonl")
    settings.strategy_runtime.runtime_guard_path = str(tmp_path / "strategy-runtime-guard.json")
    settings.strategy_runtime.retrospective_path = str(tmp_path / "strategy-retrospective.md")
    settings.strategy_runtime.retrospective_state_path = str(tmp_path / "strategy-retrospective.state.json")
    settings.strategy_runtime.evolution_state_path = str(tmp_path / "strategy-evolution.state.json")
    settings.strategy_runtime.evolution_report_path = str(tmp_path / "strategy-evolution.md")


def _write_directional_events(path: Path, strategy_name: str, *, count: int, net_profit: Decimal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(count):
        rows.append(
            json.dumps(
                {
                    "event": "strategy_cycle",
                    "created_at": utcnow().isoformat(),
                    "cycle": index + 1,
                    "strategy_name": strategy_name,
                    "decision": "executed",
                    "execution_mode": "paper",
                    "opportunities_found": 1,
                    "selected_opportunity_id": f"directional-{strategy_name}-{index}",
                    "risk_approved": True,
                    "risk_reasons": [],
                    "net_profit": str(net_profit),
                    "execution": {
                        "status": "simulated",
                        "strategy_family": "directional",
                        "dry_run": True,
                        "orders": [],
                        "net_profit": str(net_profit),
                        "pnl_validation": {
                            "cash_flow_net_pnl_usdt": str(net_profit),
                            "within_tolerance": True,
                            "residual_inventory_within_tolerance": True,
                        },
                    },
                }
            )
        )
    path.write_text("\n".join(rows), encoding="utf-8")


def _trend_candles(count: int) -> list[Candle]:
    start = datetime(2026, 5, 11, tzinfo=UTC)
    candles: list[Candle] = []
    price = Decimal("20000")
    for index in range(count):
        if index < count - 1 and index != count // 2:
            price += Decimal("8")
            close = price
            high = close + Decimal("20")
            volume = Decimal("1000") + Decimal(index)
        else:
            close = price + Decimal("350")
            high = close + Decimal("50")
            volume = Decimal("2500")
        candles.append(
            Candle(
                exchange="mock",
                symbol="BTC/USDT",
                bar="15m",
                open=price - Decimal("10"),
                high=high,
                low=price - Decimal("30"),
                close=close,
                volume=volume,
                timestamp=start + timedelta(minutes=15 * index),
                complete=True,
            )
        )
        price = close
    return candles
