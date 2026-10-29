"""TWAP/VWAP 分单引擎 + TCA 单元测试（Phase F.3.4）."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from quantbot.stocks.execution.engine import (
    adv_check,
    compute_tca,
    create_twap_slices,
    create_vwap_slices,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 1, 15, 9, 30, 0, tzinfo=timezone.utc)
_END = datetime(2025, 1, 15, 11, 0, 0, tzinfo=timezone.utc)  # 90 分钟后


# ---------------------------------------------------------------------------
# 1. create_twap_slices
# ---------------------------------------------------------------------------


class TestTWAP:
    def test_basic_twap_6_slices(self) -> None:
        """90 分钟 / 15 分钟 = 6 切片."""
        report = create_twap_slices("AAPL", 600.0, _NOW, _END)
        assert len(report.child_orders) == 6
        assert all(o.algo == "TWAP" for o in report.child_orders)

    def test_twap_equal_quantity_per_slice(self) -> None:
        """每片数量相等."""
        report = create_twap_slices("AAPL", 600.0, _NOW, _END)
        quantities = [o.quantity for o in report.child_orders]
        assert all(q == pytest.approx(100.0, abs=1e-4) for q in quantities)

    def test_twap_total_matches(self) -> None:
        """子单量之和等于总量（浮点精度内）."""
        report = create_twap_slices("AAPL", 1000.0, _NOW, _END, num_slices=7)
        assert sum(o.quantity for o in report.child_orders) == pytest.approx(1000.0, abs=1e-3)

    def test_twap_num_slices_override(self) -> None:
        """num_slices 优先于 time_interval_minutes."""
        report = create_twap_slices("AAPL", 100.0, _NOW, _END, num_slices=5)
        assert len(report.child_orders) == 5

    def test_twap_scheduled_times_ascending(self) -> None:
        """子单时间递增."""
        report = create_twap_slices("AAPL", 100.0, _NOW, _END)
        times = [o.scheduled_time for o in report.child_orders]
        assert times == sorted(times)

    def test_twap_first_slice_at_start(self) -> None:
        """第一片子单时间 = start_time."""
        report = create_twap_slices("AAPL", 100.0, _NOW, _END)
        assert report.child_orders[0].scheduled_time == _NOW

    def test_twap_invalid_end_before_start(self) -> None:
        with pytest.raises(ValueError, match="end_time"):
            create_twap_slices("AAPL", 100.0, _END, _NOW)

    def test_twap_invalid_zero_quantity(self) -> None:
        with pytest.raises(ValueError, match="total_quantity"):
            create_twap_slices("AAPL", 0.0, _NOW, _END)

    def test_twap_ticker_uppercased(self) -> None:
        report = create_twap_slices("aapl", 100.0, _NOW, _END)
        assert report.ticker == "AAPL"
        assert all(o.ticker == "AAPL" for o in report.child_orders)


# ---------------------------------------------------------------------------
# 2. create_vwap_slices
# ---------------------------------------------------------------------------


class TestVWAP:
    def test_vwap_uniform_fallback_equals_twap(self) -> None:
        """无 volume_profile → 等量切分（与 TWAP 等价）."""
        report = create_vwap_slices("AAPL", 300.0, _NOW, _END, num_slices=3)
        qtys = [o.quantity for o in report.child_orders]
        assert all(q == pytest.approx(100.0, abs=1e-4) for q in qtys)

    def test_vwap_weighted_distribution(self) -> None:
        """volume_profile 权重 [1, 2, 1] → 数量比 1:2:1."""
        profile = [1.0, 2.0, 1.0]
        report = create_vwap_slices("AAPL", 400.0, _NOW, _END, volume_profile=profile, num_slices=3)
        qtys = [o.quantity for o in report.child_orders]
        assert qtys[0] == pytest.approx(100.0, abs=1e-4)
        assert qtys[1] == pytest.approx(200.0, abs=1e-4)
        assert qtys[2] == pytest.approx(100.0, abs=1e-4)

    def test_vwap_total_quantity_preserved(self) -> None:
        """子单量之和 ≈ total_quantity."""
        profile = [2.0, 3.0, 1.5, 2.5]
        report = create_vwap_slices("AAPL", 900.0, _NOW, _END, volume_profile=profile, num_slices=4)
        assert sum(o.quantity for o in report.child_orders) == pytest.approx(900.0, abs=1e-3)

    def test_vwap_profile_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="volume_profile"):
            create_vwap_slices("AAPL", 100.0, _NOW, _END, volume_profile=[1.0, 2.0], num_slices=3)

    def test_vwap_negative_weight_raises(self) -> None:
        with pytest.raises(ValueError, match="负值"):
            create_vwap_slices("AAPL", 100.0, _NOW, _END, volume_profile=[-1.0, 2.0], num_slices=2)

    def test_vwap_algo_label(self) -> None:
        report = create_vwap_slices("AAPL", 100.0, _NOW, _END, num_slices=2)
        assert all(o.algo == "VWAP" for o in report.child_orders)

    def test_vwap_slice_index_monotonic(self) -> None:
        report = create_vwap_slices("AAPL", 100.0, _NOW, _END, num_slices=4)
        indices = [o.slice_index for o in report.child_orders]
        assert indices == list(range(4))


# ---------------------------------------------------------------------------
# 3. compute_tca
# ---------------------------------------------------------------------------


class TestComputeTCA:
    def test_positive_slippage(self) -> None:
        """买单成交价高于到达价 → 正滑点（不利）."""
        rec = compute_tca(arrival_price=100.0, executed_avg_price=100.1)
        # (100.1 - 100) / 100 * 10000 = 10 bps
        assert rec.slippage_bps == pytest.approx(10.0, abs=0.01)

    def test_zero_slippage(self) -> None:
        """完美成交 → 0 bps 滑点."""
        rec = compute_tca(arrival_price=50.0, executed_avg_price=50.0)
        assert rec.slippage_bps == pytest.approx(0.0, abs=1e-6)

    def test_negative_slippage(self) -> None:
        """成交价低于到达价 → 负滑点（有利）."""
        rec = compute_tca(arrival_price=200.0, executed_avg_price=199.8)
        # (-0.2 / 200) * 10000 = -10 bps
        assert rec.slippage_bps == pytest.approx(-10.0, abs=0.01)

    def test_optional_fields_stored(self) -> None:
        rec = compute_tca(
            arrival_price=100.0,
            executed_avg_price=100.5,
            vwap_price=100.2,
            close_price=101.0,
            ticker="AAPL",
            algo="TWAP",
        )
        assert rec.vwap_price == 100.2
        assert rec.close_price == 101.0
        assert rec.ticker == "AAPL"
        assert rec.algo == "TWAP"

    def test_invalid_arrival_price_raises(self) -> None:
        with pytest.raises(ValueError, match="arrival_price"):
            compute_tca(arrival_price=0.0, executed_avg_price=100.0)


# ---------------------------------------------------------------------------
# 4. adv_check
# ---------------------------------------------------------------------------


class TestADVCheck:
    def test_over_threshold_needs_slicing(self) -> None:
        """10000 股 / 1_000_000 ADV = 1% > 0.5% → 需要分单."""
        assert adv_check(10_000.0, 1_000_000.0) is True

    def test_under_threshold_no_slicing(self) -> None:
        """100 股 / 1_000_000 ADV = 0.01% < 0.5% → 不需要分单."""
        assert adv_check(100.0, 1_000_000.0) is False

    def test_custom_threshold(self) -> None:
        """自定义阈值 1%."""
        assert adv_check(5_000.0, 1_000_000.0, threshold_pct=0.01) is False
        assert adv_check(15_000.0, 1_000_000.0, threshold_pct=0.01) is True

    def test_invalid_adv_raises(self) -> None:
        with pytest.raises(ValueError, match="avg_daily_volume"):
            adv_check(100.0, 0.0)
