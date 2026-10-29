"""市场状态洞察测试 — T-4.5 验收."""
from __future__ import annotations

from quantbot.stocks.insights.analyzer import InsightAnalyzer
from quantbot.stocks.insights.regime import Regime, RegimeTagger


def _prices(n: int = 60, trend: str = "up") -> list[float]:
    """生成价格序列."""
    prices = [100.0]
    for _ in range(n - 1):
        if trend == "up":
            prices.append(prices[-1] * 1.005)
        elif trend == "down":
            prices.append(prices[-1] * 0.995)
        else:
            prices.append(prices[-1])
    return prices


class TestRegime:
    def test_enum_values(self) -> None:
        assert Regime.BULL.value == "bull"
        assert Regime.BEAR.value == "bear"
        assert Regime.SIDEWAYS.value == "sideways"


class TestRegimeTagger:
    def test_bull_market(self) -> None:
        prices = _prices(60, trend="up")
        tagger = RegimeTagger(fast_window=5, slow_window=20)
        regime = tagger.tag(prices)
        assert regime == Regime.BULL

    def test_bear_market(self) -> None:
        prices = _prices(60, trend="down")
        tagger = RegimeTagger(fast_window=5, slow_window=20)
        regime = tagger.tag(prices)
        assert regime == Regime.BEAR

    def test_sideways_market(self) -> None:
        prices = [100.0 + (i % 2) * 0.1 for i in range(60)]
        tagger = RegimeTagger(fast_window=5, slow_window=20)
        regime = tagger.tag(prices)
        assert regime == Regime.SIDEWAYS

    def test_tag_series(self) -> None:
        prices = _prices(60, trend="up")
        tagger = RegimeTagger(fast_window=5, slow_window=20)
        series = tagger.tag_series(prices)
        assert len(series) == len(prices)
        assert all(isinstance(r, Regime) for r in series)

    def test_insufficient_data_returns_sideways(self) -> None:
        prices = [100.0, 101.0, 99.0]
        tagger = RegimeTagger(fast_window=5, slow_window=20)
        regime = tagger.tag(prices)
        assert regime == Regime.SIDEWAYS


class TestInsightAnalyzer:
    def test_correlate_pnl_with_regime(self) -> None:
        prices = _prices(60, trend="up")
        pnl_series = [i * 100.0 for i in range(60)]
        analyzer = InsightAnalyzer()
        result = analyzer.correlate(prices, pnl_series)
        assert "bull" in result
        assert "bear" in result
        assert "sideways" in result
        assert isinstance(result["bull"]["avg_pnl"], float)

    def test_correlate_returns_all_regimes(self) -> None:
        prices = _prices(30, "up") + _prices(30, "down")
        pnl = list(range(60))
        analyzer = InsightAnalyzer()
        result = analyzer.correlate(prices, pnl)
        assert set(result.keys()) == {"bull", "bear", "sideways"}

    def test_empty_input(self) -> None:
        analyzer = InsightAnalyzer()
        result = analyzer.correlate([], [])
        assert result == {
            "bull": {"avg_pnl": 0.0, "count": 0},
            "bear": {"avg_pnl": 0.0, "count": 0},
            "sideways": {"avg_pnl": 0.0, "count": 0},
        }
