"""TLH 引擎单元测试（Phase F.3.1）."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from quantbot.stocks.tlh.engine import (
    TaxLot,
    _REPLACEMENT_MAP,
    estimate_tax_saving,
    get_replacement_tickers,
    scan_tlh_candidates,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TODAY = date(2025, 1, 15)


def _lot(
    ticker: str = "AAPL",
    quantity: float = 100.0,
    cost_basis: float = 150.0,
    days_held: int = 60,
    lot_id: str = "L001",
) -> TaxLot:
    return TaxLot(
        ticker=ticker,
        quantity=quantity,
        cost_basis=cost_basis,
        acquisition_date=TODAY - timedelta(days=days_held),
        lot_id=lot_id,
    )


# ---------------------------------------------------------------------------
# 1. ETF 替代表
# ---------------------------------------------------------------------------


class TestReplacementMap:
    def test_spy_has_three_replacements(self) -> None:
        r = get_replacement_tickers("SPY")
        assert len(r) == 3
        assert "VOO" in r
        assert "IVV" in r

    def test_lowercase_ticker(self) -> None:
        assert get_replacement_tickers("spy") == get_replacement_tickers("SPY")

    def test_qqq_replacements(self) -> None:
        r = get_replacement_tickers("QQQ")
        assert "QQQM" in r

    def test_individual_stock_has_sector_etf(self) -> None:
        r = get_replacement_tickers("AAPL")
        assert "XLK" in r or "VGT" in r

    def test_nvda_semiconductor_etf(self) -> None:
        r = get_replacement_tickers("NVDA")
        assert "SOXX" in r or "SMH" in r

    def test_unknown_ticker_returns_empty(self) -> None:
        assert get_replacement_tickers("ZZZZZ") == []

    def test_replacement_map_has_at_least_20_entries(self) -> None:
        assert len(_REPLACEMENT_MAP) >= 20


# ---------------------------------------------------------------------------
# 2. scan_tlh_candidates — 候选扫描
# ---------------------------------------------------------------------------


class TestScanTLHCandidates:
    def test_basic_candidate_detected(self) -> None:
        """跌幅 > 5%、亏损 > $500 的 lot 应被识别."""
        lot = _lot(ticker="AAPL", quantity=100, cost_basis=150.0, days_held=60)
        current_prices = {"AAPL": 130.0}  # -13.3%，亏损 $2000
        candidates = scan_tlh_candidates(
            [lot], current_prices, {}, reference_date=TODAY
        )
        assert len(candidates) == 1
        c = candidates[0]
        assert c.lot.ticker == "AAPL"
        assert c.unrealized_pnl == pytest.approx(-2000.0)
        assert c.unrealized_pnl_pct == pytest.approx(-2000.0 / 15000.0)

    def test_small_loss_filtered_out(self) -> None:
        """亏损 < $500 的 lot 不应被识别."""
        lot = _lot(ticker="AAPL", quantity=10, cost_basis=150.0, days_held=60)
        # 当前价 140：亏损 $100，不满足 $500
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 140.0}, {}, min_loss_usd=500.0, reference_date=TODAY
        )
        assert len(candidates) == 0

    def test_small_pct_loss_filtered_out(self) -> None:
        """跌幅 < 5% 的 lot 不应被识别."""
        lot = _lot(ticker="AAPL", quantity=100, cost_basis=150.0, days_held=60)
        # 当前价 148：跌幅 1.3%，不满足 -5%
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 148.0}, {}, min_loss_pct=-0.05, reference_date=TODAY
        )
        assert len(candidates) == 0

    def test_profit_lot_excluded(self) -> None:
        """盈利 lot 不应出现在候选中."""
        lot = _lot(ticker="AAPL", quantity=100, cost_basis=100.0, days_held=60)
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 150.0}, {}, reference_date=TODAY
        )
        assert len(candidates) == 0

    def test_missing_price_skipped(self) -> None:
        """缺少价格数据的 lot 应被跳过（不报错）."""
        lot = _lot(ticker="AAPL")
        candidates = scan_tlh_candidates([lot], {}, {}, reference_date=TODAY)
        assert len(candidates) == 0

    def test_sorted_by_largest_loss_first(self) -> None:
        """候选应按亏损金额从大到小排序."""
        lot1 = _lot(ticker="AAPL", quantity=100, cost_basis=150.0, lot_id="L1")
        lot2 = _lot(ticker="MSFT", quantity=100, cost_basis=200.0, lot_id="L2")
        prices = {
            "AAPL": 120.0,  # 亏损 $3000
            "MSFT": 160.0,  # 亏损 $4000
        }
        candidates = scan_tlh_candidates([lot1, lot2], prices, {}, reference_date=TODAY)
        assert len(candidates) == 2
        assert candidates[0].lot.ticker == "MSFT"  # 更大亏损排前面

    def test_multiple_lots_same_ticker(self) -> None:
        """同一 ticker 的多个 lots 均应独立评估."""
        lot1 = _lot(ticker="AAPL", quantity=50, cost_basis=150.0, lot_id="L1", days_held=60)
        lot2 = _lot(ticker="AAPL", quantity=200, cost_basis=140.0, lot_id="L2", days_held=200)
        prices = {"AAPL": 120.0}
        candidates = scan_tlh_candidates([lot1, lot2], prices, {}, reference_date=TODAY)
        assert len(candidates) == 2


# ---------------------------------------------------------------------------
# 3. 持有期与长短期分类
# ---------------------------------------------------------------------------


class TestHoldingPeriod:
    def test_short_term_lot(self) -> None:
        """持有 < 365 天应被标记为短期."""
        lot = _lot(days_held=200)
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, {}, reference_date=TODAY
        )
        assert candidates[0].is_long_term is False
        assert candidates[0].holding_days == 200

    def test_long_term_lot(self) -> None:
        """持有 >= 365 天应被标记为长期."""
        lot = _lot(days_held=400)
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, {}, reference_date=TODAY
        )
        assert candidates[0].is_long_term is True
        assert candidates[0].holding_days == 400


# ---------------------------------------------------------------------------
# 4. Wash Sale 过滤
# ---------------------------------------------------------------------------


class TestWashSaleDetection:
    def test_wash_sale_risk_detected(self) -> None:
        """近 30 天内有买入记录应标记 wash_sale_risk=True."""
        lot = _lot(ticker="AAPL")
        recent = {"AAPL": TODAY - timedelta(days=15)}  # 15 天前买入
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, recent, reference_date=TODAY
        )
        assert candidates[0].wash_sale_risk is True

    def test_no_wash_sale_beyond_window(self) -> None:
        """30 天前买入不触发 wash sale 风险."""
        lot = _lot(ticker="AAPL")
        recent = {"AAPL": TODAY - timedelta(days=31)}
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, recent, reference_date=TODAY
        )
        assert candidates[0].wash_sale_risk is False

    def test_no_wash_sale_without_recent_purchase(self) -> None:
        """无买入记录不触发 wash sale 风险."""
        lot = _lot(ticker="AAPL")
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, {}, reference_date=TODAY
        )
        assert candidates[0].wash_sale_risk is False

    def test_wash_sale_exactly_at_boundary(self) -> None:
        """恰好 30 天不触发（窗口为 < 30 天）."""
        lot = _lot(ticker="AAPL")
        recent = {"AAPL": TODAY - timedelta(days=30)}
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, recent, reference_date=TODAY
        )
        assert candidates[0].wash_sale_risk is False


# ---------------------------------------------------------------------------
# 5. 税额估算
# ---------------------------------------------------------------------------


class TestEstimateTaxSaving:
    def test_short_term_saving(self) -> None:
        """短期亏损节税 = 亏损金额 × 短期税率."""
        lot = _lot(days_held=100)
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, {}, reference_date=TODAY
        )
        # 亏损 = (120-150)*100 = -$3000
        saving = estimate_tax_saving(candidates, short_term_rate=0.37, long_term_rate=0.20)
        assert saving == pytest.approx(3000 * 0.37, abs=0.01)

    def test_long_term_saving(self) -> None:
        """长期亏损节税 = 亏损金额 × 长期税率."""
        lot = _lot(days_held=400)
        candidates = scan_tlh_candidates(
            [lot], {"AAPL": 120.0}, {}, reference_date=TODAY
        )
        saving = estimate_tax_saving(candidates, short_term_rate=0.37, long_term_rate=0.20)
        assert saving == pytest.approx(3000 * 0.20, abs=0.01)

    def test_empty_candidates_returns_zero(self) -> None:
        """空候选列表应返回 0.0."""
        assert estimate_tax_saving([]) == 0.0

    def test_mixed_short_and_long_term(self) -> None:
        """混合短期 + 长期亏损，分别按对应税率计算总和."""
        lot_st = _lot(ticker="AAPL", days_held=100, lot_id="L1")
        lot_lt = _lot(ticker="MSFT", cost_basis=200.0, days_held=400, lot_id="L2")
        prices = {"AAPL": 120.0, "MSFT": 160.0}  # 亏损 $3000 + $4000
        candidates = scan_tlh_candidates([lot_st, lot_lt], prices, {}, reference_date=TODAY)
        saving = estimate_tax_saving(candidates, short_term_rate=0.37, long_term_rate=0.20)
        expected = 3000 * 0.37 + 4000 * 0.20
        assert saving == pytest.approx(expected, abs=0.01)
