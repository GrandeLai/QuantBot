"""12 predefined stock screening strategies."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import polars as pl

from quantbot.stocks.screener.scoring import ScoringEngine


@dataclass(frozen=True)
class Strategy:
    """A named screening strategy with scoring and condition thresholds."""

    id: str
    name_zh: str
    description: str
    min_score: int = 60
    conditions: dict[str, Any] = field(default_factory=dict)


class StrategyRegistry:
    """Registry of all 12 predefined screening strategies."""

    def __init__(self) -> None:
        self._strategies: dict[str, Strategy] = {s.id: s for s in self._build()}

    def all(self) -> list[Strategy]:
        """Return all registered strategies."""
        return list(self._strategies.values())

    def get(self, strategy_id: str) -> Strategy:
        """Return strategy by id; raise KeyError if not found."""
        if strategy_id not in self._strategies:
            raise KeyError(f"Strategy {strategy_id!r} not found")
        return self._strategies[strategy_id]

    def _build(self) -> list[Strategy]:
        return [
            Strategy(
                "bull_trend",
                "强势上涨",
                "EMA多头排列+MACD金叉+量价配合",
                65,
                {"min_trend": 60, "min_volume": 50, "min_macd": 55},
            ),
            Strategy(
                "ma_golden_cross",
                "均线金叉",
                "短期均线上穿长期均线+量能确认",
                55,
                {"min_trend": 55, "require_ema_cross": True},
            ),
            Strategy(
                "volume_breakout",
                "放量突破",
                "成交量放大2倍+价格创20日新高",
                50,
                {"min_volume": 70, "min_trend": 45},
            ),
            Strategy(
                "shrink_pullback",
                "缩量回调",
                "下跌期间量能萎缩+MACD底背离",
                45,
                {"max_volume": 60, "min_macd": 50},
            ),
            Strategy(
                "dragon_head",
                "龙头股",
                "板块内动量最强+高分全面超标",
                75,
                {"min_total": 75},  # TODO: wire sector momentum when sector API available
            ),
            Strategy(
                "bottom_reversal",
                "底部反转",
                "RSI超卖+MACD向上+支撑位附近",
                45,
                {"max_rsi_raw": 35, "min_macd": 55, "max_support_pos": 0.3},
            ),
            Strategy(
                "consolidation_breakout",
                "震荡突破",
                "窄幅整理后放量突破压力位",
                55,
                {"min_volume": 60, "min_support": 55},
            ),
            Strategy(
                "high_turnover",
                "高换手率",
                "换手率显著高于历史均值",
                50,
                {"min_volume": 75},
            ),
            Strategy(
                "chip_concentration",
                "筹码集中",
                "筹码高度集中+成本区支撑",
                55,
                {"min_support": 60, "min_total": 55},
            ),
            Strategy(
                "undervalued_growth",
                "低估成长",
                "低PE/PB+高成长性指标",
                50,
                {"min_total": 50},  # TODO: wire PE/PB when fundamental module available
            ),
            Strategy(
                "trend_pullback",
                "趋势回调",
                "上升趋势中的健康回调买入点",
                55,
                {"min_trend": 60, "bias_range": (-0.08, -0.02)},
            ),
            Strategy(
                "momentum_continuation",
                "动量延续",
                "强动量股票延续上涨",
                65,
                {"min_trend": 65, "min_macd": 55, "min_rsi": 55},
            ),
        ]


class StrategyEvaluator:
    """Evaluate whether a stock matches a screening strategy."""

    def __init__(self) -> None:
        self._engine = ScoringEngine()

    def evaluate(self, df: pl.DataFrame, strategy: Strategy) -> bool:
        """Return True if df passes the strategy's conditions."""
        if len(df) < 20:
            return False
        breakdown = self._engine.score(df)
        cond = strategy.conditions

        if breakdown.total < strategy.min_score:
            return False
        if "min_total" in cond and breakdown.total < cond["min_total"]:
            return False
        if "min_trend" in cond and breakdown.trend < cond["min_trend"]:
            return False
        if cond.get("require_ema_cross"):
            if not breakdown.details.get("macd_cross"):
                return False
        if "min_volume" in cond and breakdown.volume < cond["min_volume"]:
            return False
        if "max_volume" in cond and breakdown.volume > cond["max_volume"]:
            return False
        if "min_macd" in cond and breakdown.macd < cond["min_macd"]:
            return False
        if "min_rsi" in cond and breakdown.rsi < cond["min_rsi"]:
            return False
        if "min_support" in cond and breakdown.support < cond["min_support"]:
            return False
        if "max_rsi_raw" in cond:
            rsi_raw = breakdown.details.get("rsi", 50)
            if rsi_raw > cond["max_rsi_raw"]:
                return False
        if "max_support_pos" in cond:
            pos = breakdown.details.get("support_position", 1.0)
            if pos > cond["max_support_pos"]:
                return False
        if "bias_range" in cond:
            lo, hi = cond["bias_range"]
            bias = breakdown.details.get("bias", 0)
            if not (lo <= bias <= hi):
                return False
        return True
