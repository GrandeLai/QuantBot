"""洞察分析器 — P&L 与市场状态的关联分析."""
from __future__ import annotations

from quantbot.stocks.insights.regime import RegimeTagger


class InsightAnalyzer:
    """分析 P&L 序列在不同市场状态下的表现."""

    def __init__(self, fast_window: int = 5, slow_window: int = 20) -> None:
        self._tagger = RegimeTagger(fast_window=fast_window, slow_window=slow_window)

    def correlate(
        self,
        prices: list[float],
        pnl_series: list[float],
    ) -> dict[str, dict[str, float | int]]:
        """统计各市场状态下的平均 P&L."""
        empty_result: dict[str, dict[str, float | int]] = {
            "bull": {"avg_pnl": 0.0, "count": 0},
            "bear": {"avg_pnl": 0.0, "count": 0},
            "sideways": {"avg_pnl": 0.0, "count": 0},
        }
        if not prices or not pnl_series:
            return empty_result

        n = min(len(prices), len(pnl_series))
        prices = prices[:n]
        pnl_series = pnl_series[:n]

        regimes = self._tagger.tag_series(prices)

        buckets: dict[str, list[float]] = {"bull": [], "bear": [], "sideways": []}
        for regime, pnl in zip(regimes, pnl_series, strict=False):
            buckets[regime.value].append(float(pnl))

        result: dict[str, dict[str, float | int]] = {}
        for regime_name, pnl_list in buckets.items():
            if pnl_list:
                result[regime_name] = {
                    "avg_pnl": round(sum(pnl_list) / len(pnl_list), 4),
                    "count": len(pnl_list),
                }
            else:
                result[regime_name] = {"avg_pnl": 0.0, "count": 0}
        return result
