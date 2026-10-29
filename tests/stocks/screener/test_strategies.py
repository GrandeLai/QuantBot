"""Tests for the 12 predefined screening strategies and evaluator."""
import numpy as np
import polars as pl
import pytest

from quantbot.stocks.screener.strategies import StrategyEvaluator, StrategyRegistry


def _make_trending_df(n: int = 60) -> pl.DataFrame:
    """Strongly trending upward stock with clear MACD/volume signals.

    Uses an upward-drifting sine wave so that EMA12 > EMA26 (positive MACD
    histogram) while price oscillates enough to avoid extreme RSI/bias readings
    that would penalise the scorer.
    """
    rng = np.random.default_rng(99)
    t = np.arange(n)
    closes = 10.0 + 0.03 * t + 1.2 * np.sin(2 * np.pi * t / 30) + rng.normal(0, 0.01, n)
    vols = np.full(n, 1_000_000, dtype=float)
    vols[-3:] = 4_000_000
    return pl.DataFrame({
        "close": closes,
        "open": closes * 0.99,
        "high": closes * 1.015,
        "low": closes * 0.985,
        "volume": vols,
    })


def test_registry_has_12_strategies() -> None:
    reg = StrategyRegistry()
    assert len(reg.all()) == 12


def test_strategy_has_required_fields() -> None:
    reg = StrategyRegistry()
    for s in reg.all():
        assert s.id and s.name_zh and s.description
        assert isinstance(s.min_score, int)


def test_evaluate_returns_bool() -> None:
    evaluator = StrategyEvaluator()
    df = _make_trending_df()
    reg = StrategyRegistry()
    result = evaluator.evaluate(df, reg.get("bull_trend"))
    assert isinstance(result, bool)


def test_bull_trend_matches_trending_stock() -> None:
    evaluator = StrategyEvaluator()
    df = _make_trending_df()
    reg = StrategyRegistry()
    result = evaluator.evaluate(df, reg.get("bull_trend"))
    assert result is True


def test_get_unknown_strategy_raises() -> None:
    reg = StrategyRegistry()
    with pytest.raises(KeyError):
        reg.get("nonexistent_strategy_id")
