"""市场状态识别 — 基于移动平均交叉的牛熊判断."""
from __future__ import annotations

from enum import Enum


class Regime(Enum):
    """市场状态枚举."""

    BULL = "bull"
    BEAR = "bear"
    SIDEWAYS = "sideways"


class RegimeTagger:
    """用快慢均线交叉识别市场状态."""

    def __init__(self, fast_window: int = 50, slow_window: int = 200) -> None:
        self.fast_window = fast_window
        self.slow_window = slow_window

    def _ma(self, prices: list[float], window: int) -> list[float | None]:
        """计算简单移动平均序列."""
        result: list[float | None] = []
        for i in range(len(prices)):
            if i + 1 < window:
                result.append(None)
            else:
                result.append(sum(prices[i + 1 - window: i + 1]) / window)
        return result

    def tag(self, prices: list[float]) -> Regime:
        """对最新价格序列打一个市场状态标签."""
        if len(prices) < self.slow_window:
            return Regime.SIDEWAYS

        fast_ma = self._ma(prices, self.fast_window)
        slow_ma = self._ma(prices, self.slow_window)

        last_fast = fast_ma[-1]
        last_slow = slow_ma[-1]
        if last_fast is None or last_slow is None:
            return Regime.SIDEWAYS

        threshold = last_slow * 0.001
        if last_fast > last_slow + threshold:
            return Regime.BULL
        if last_fast < last_slow - threshold:
            return Regime.BEAR
        return Regime.SIDEWAYS

    def tag_series(self, prices: list[float]) -> list[Regime]:
        """对价格序列的每个时间点打状态标签."""
        result = []
        for i in range(len(prices)):
            result.append(self.tag(prices[: i + 1]))
        return result
