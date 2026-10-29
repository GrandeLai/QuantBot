"""Tests for edgar/form4_engine.py."""
from __future__ import annotations

from datetime import date, timedelta


from quantbot.stocks.edgar.form4_engine import (
    _compute_signal_strength,
    _extract_role_label,
    _is_key_insider,
    detect_clusters,
)
from quantbot.stocks.edgar.models import Form4Transaction


# ── 工具函数测试 ──────────────────────────────────────────────────────────────

class TestIsKeyInsider:
    def test_ceo_is_key(self):
        assert _is_key_insider("Chief Executive Officer")

    def test_cfo_is_key(self):
        assert _is_key_insider("Chief Financial Officer")

    def test_director_is_key(self):
        assert _is_key_insider("Director")

    def test_vp_is_key(self):
        assert _is_key_insider("VP Sales")

    def test_random_employee_is_not_key(self):
        assert not _is_key_insider("Software Engineer")

    def test_empty_string_is_not_key(self):
        assert not _is_key_insider("")


class TestExtractRoleLabel:
    def test_ceo(self):
        assert _extract_role_label("Chief Executive Officer") == "CEO"

    def test_cfo(self):
        assert _extract_role_label("Chief Financial Officer") == "CFO"

    def test_director(self):
        # "Independent Director" → only "Director" matches, no CEO/CFO
        result = _extract_role_label("Independent Director")
        assert result == "Director"

    def test_president(self):
        assert _extract_role_label("President and CEO") == "CEO"  # CEO takes precedence


class TestComputeSignalStrength:
    def test_minimum_cluster_low_value(self):
        # 2 insiders, $10k total, no CEO/CFO → weak signal
        strength = _compute_signal_strength(2, 10_000, ["Director"])
        assert 0.0 <= strength <= 0.3

    def test_max_insiders_high_value_ceo(self):
        # 10 insiders, $10M, CEO present → strong signal
        strength = _compute_signal_strength(10, 10_000_000, ["CEO", "CFO"])
        assert strength > 0.7

    def test_value_zero(self):
        strength = _compute_signal_strength(2, 0, ["Director"])
        assert strength >= 0.0

    def test_strength_in_range(self):
        for insiders in [2, 3, 5, 10]:
            for value in [10_000, 100_000, 1_000_000]:
                s = _compute_signal_strength(insiders, value, ["Director"])
                assert 0.0 <= s <= 1.0


# ── detect_clusters 测试 ──────────────────────────────────────────────────────

def _make_txn(
    ticker: str,
    insider_name: str,
    insider_title: str,
    txn_date: date,
    txn_type: str = "P",
    shares: float = 1000.0,
    price: float = 100.0,
    is_plan: bool = False,
) -> Form4Transaction:
    return Form4Transaction(
        ticker=ticker,
        cik="0000320193",
        insider_name=insider_name,
        insider_title=insider_title,
        transaction_date=txn_date,
        transaction_type=txn_type,
        shares=shares,
        price_per_share=price,
        total_value=shares * price,
        is_10b5_1_plan=is_plan,
        accession_number=f"ACC-{insider_name[:3]}-{txn_date}",
    )


BASE_DATE = date(2024, 3, 15)


class TestDetectClusters:
    def test_two_insiders_within_window_detected(self):
        txns = [
            _make_txn("AAPL", "Tim Cook", "Chief Executive Officer", BASE_DATE),
            _make_txn("AAPL", "Luca Maestri", "Chief Financial Officer", BASE_DATE - timedelta(days=5)),
        ]
        clusters = detect_clusters(txns)
        assert len(clusters) >= 1
        assert clusters[0].insider_count >= 2

    def test_single_insider_not_a_cluster(self):
        txns = [
            _make_txn("AAPL", "Tim Cook", "Chief Executive Officer", BASE_DATE),
        ]
        clusters = detect_clusters(txns, min_insiders=2)
        assert len(clusters) == 0

    def test_10b5_1_plan_excluded(self):
        txns = [
            _make_txn("AAPL", "Tim Cook", "CEO", BASE_DATE, is_plan=True),
            _make_txn("AAPL", "Luca Maestri", "CFO", BASE_DATE - timedelta(days=5), is_plan=True),
        ]
        clusters = detect_clusters(txns)
        assert len(clusters) == 0

    def test_sales_excluded(self):
        txns = [
            _make_txn("AAPL", "Tim Cook", "CEO", BASE_DATE, txn_type="S"),
            _make_txn("AAPL", "Luca Maestri", "CFO", BASE_DATE - timedelta(days=5), txn_type="S"),
        ]
        clusters = detect_clusters(txns)
        assert len(clusters) == 0

    def test_non_key_role_excluded(self):
        txns = [
            _make_txn("AAPL", "Alice", "Software Engineer", BASE_DATE),
            _make_txn("AAPL", "Bob", "Product Manager", BASE_DATE - timedelta(days=5)),
        ]
        clusters = detect_clusters(txns)
        assert len(clusters) == 0

    def test_outside_window_not_clustered(self):
        """Two purchases 120 days apart should not form a 90-day cluster."""
        txns = [
            _make_txn("AAPL", "Tim Cook", "CEO", BASE_DATE),
            _make_txn("AAPL", "Luca Maestri", "CFO", BASE_DATE - timedelta(days=120)),
        ]
        clusters = detect_clusters(txns, window_days=90)
        # Each might be its own "window" but as standalone they won't meet min_insiders
        assert all(c.insider_count < 2 for c in clusters)

    def test_signal_strength_in_range(self):
        txns = [
            _make_txn("AAPL", "Tim Cook", "CEO", BASE_DATE),
            _make_txn("AAPL", "Luca Maestri", "CFO", BASE_DATE - timedelta(days=5)),
        ]
        clusters = detect_clusters(txns)
        for c in clusters:
            assert 0.0 <= c.signal_strength <= 1.0

    def test_min_single_value_filter(self):
        """小额交易（< $10k）应被过滤."""
        txns = [
            _make_txn("AAPL", "Tim Cook", "CEO", BASE_DATE, shares=10, price=50),  # $500
            _make_txn("AAPL", "Luca Maestri", "CFO", BASE_DATE, shares=10, price=50),  # $500
        ]
        clusters = detect_clusters(txns, min_single_value=10_000)
        assert len(clusters) == 0

    def test_empty_transactions(self):
        clusters = detect_clusters([])
        assert clusters == []
