"""Tests for insider_trading engine and API endpoint.

Covers:
- _compute_signal boundary logic
- _build_interpretation for each signal type
- compute_insider_trading happy path (mocked EDGAR)
- graceful degradation (CIK not found / exception)
- GET /api/insider-trading 200 + 422
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from quantbot.stocks.insider_trading.engine import (
    _build_interpretation,
    _compute_signal,
    compute_insider_trading,
)


# ---------------------------------------------------------------------------
# IT-1: _compute_signal
# ---------------------------------------------------------------------------


class TestComputeSignal:
    def test_two_buyers_no_sellers_is_cluster_buy(self) -> None:
        buys = {"Alice": 1000.0, "Bob": 500.0}
        sells: dict = {}
        assert _compute_signal(buys, sells) == "cluster_buy"

    def test_one_buyer_is_neutral(self) -> None:
        buys = {"Alice": 1000.0}
        sells: dict = {}
        assert _compute_signal(buys, sells) == "neutral"

    def test_two_sellers_no_buyers_is_cluster_sell(self) -> None:
        buys: dict = {}
        sells = {"Alice": 2000.0, "Bob": 1000.0}
        assert _compute_signal(buys, sells) == "cluster_sell"

    def test_two_buyers_two_sellers_is_mixed(self) -> None:
        buys = {"Alice": 1000.0, "Bob": 500.0}
        sells = {"Carol": 2000.0, "Dave": 1000.0}
        assert _compute_signal(buys, sells) == "mixed"

    def test_empty_both_is_no_data(self) -> None:
        assert _compute_signal({}, {}) == "no_data"

    def test_zero_shares_entry_is_not_counted(self) -> None:
        # Insider with 0 shares should not count toward threshold
        buys = {"Alice": 0.0, "Bob": 500.0}
        sells: dict = {}
        assert _compute_signal(buys, sells) == "neutral"

    def test_exactly_two_buyers_triggers_cluster(self) -> None:
        buys = {"A": 100.0, "B": 200.0}
        assert _compute_signal(buys, {}) == "cluster_buy"

    def test_three_sellers_is_cluster_sell(self) -> None:
        sells = {"A": 100.0, "B": 200.0, "C": 300.0}
        assert _compute_signal({}, sells) == "cluster_sell"


# ---------------------------------------------------------------------------
# IT-2: _build_interpretation
# ---------------------------------------------------------------------------


class TestBuildInterpretation:
    def test_cluster_buy_mentions_buy_count(self) -> None:
        interp = _build_interpretation("cluster_buy", 3, 0, 5000.0)
        assert "3" in interp
        assert "买入" in interp

    def test_cluster_sell_mentions_sell_count(self) -> None:
        interp = _build_interpretation("cluster_sell", 0, 2, -3000.0)
        assert "2" in interp
        assert "卖出" in interp

    def test_mixed_mentions_both_counts(self) -> None:
        interp = _build_interpretation("mixed", 2, 2, 0.0)
        assert "2" in interp
        assert "分歧" in interp

    def test_no_data_interpretation(self) -> None:
        interp = _build_interpretation("no_data", 0, 0, 0.0)
        assert "无" in interp or "10b5" in interp

    def test_neutral_interpretation_non_empty(self) -> None:
        interp = _build_interpretation("neutral", 1, 0, 500.0)
        assert len(interp) > 0


# ---------------------------------------------------------------------------
# Helpers for EDGAR mocking
# ---------------------------------------------------------------------------

def _make_mock_txn(
    name: str = "John CEO",
    title: str = "CEO",
    tx_date: date | None = None,
    shares: float = 1000.0,
    tx_type: str = "P",
    is_10b5: bool = False,
) -> dict:
    if tx_date is None:
        tx_date = date.today() - timedelta(days=10)
    return {
        "insider_name": name,
        "title": title,
        "transaction_date": tx_date,
        "shares": shares,
        "price_per_share": 150.0,
        "transaction_type": tx_type,
        "is_10b5_plan": is_10b5,
        "accession": "0001234567-24-000001",
    }


def _make_filings(n: int = 3) -> list[dict]:
    base = date.today() - timedelta(days=5)
    return [
        {
            "form": "4",
            "date": str(base - timedelta(days=i * 7)),
            "accession": f"0001234567-24-00000{i + 1}",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# IT-3: compute_insider_trading — happy path
# ---------------------------------------------------------------------------


class TestComputeInsiderTrading:
    def test_cluster_buy_signal_with_two_buyers(self) -> None:
        filings = _make_filings(2)
        txns_batch1 = [_make_mock_txn("Alice CEO", "CEO", shares=500.0)]
        txns_batch2 = [_make_mock_txn("Bob CFO", "CFO", shares=300.0)]

        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=filings),
            patch(
                "quantbot.stocks.insider_trading.engine._parse_form4_xml",
                side_effect=[txns_batch1, txns_batch2],
            ),
        ):
            result = compute_insider_trading("AAPL")

        assert result.signal == "cluster_buy"
        assert result.cluster_buy_count == 2
        assert result.net_shares_90d > 0
        assert result.data_available is True

    def test_ticker_uppercased(self) -> None:
        with patch("quantbot.stocks.insider_trading.engine._load_cik", return_value=None):
            result = compute_insider_trading("aapl")
        assert result.ticker == "AAPL"

    def test_as_of_date_is_today(self) -> None:
        filings = _make_filings(1)
        txns = [_make_mock_txn()]
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=filings),
            patch("quantbot.stocks.insider_trading.engine._parse_form4_xml", return_value=txns),
        ):
            result = compute_insider_trading("MSFT")
        assert result.as_of_date == date.today()

    def test_10b5_plan_txn_excluded(self) -> None:
        """10b5-1 plan transactions should not count toward cluster signal."""
        filings = _make_filings(2)
        # Two insiders buy, but both are 10b5-1 plan → no cluster
        txns_a = [_make_mock_txn("Alice", tx_type="P", is_10b5=True)]
        txns_b = [_make_mock_txn("Bob", tx_type="P", is_10b5=True)]
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=filings),
            patch(
                "quantbot.stocks.insider_trading.engine._parse_form4_xml",
                side_effect=[txns_a, txns_b],
            ),
        ):
            result = compute_insider_trading("TSLA")
        assert result.signal in ("neutral", "no_data")

    def test_old_transactions_excluded(self) -> None:
        """Transactions older than 90 days should be excluded from signal."""
        filings = _make_filings(1)
        old_txn = _make_mock_txn(
            tx_date=date.today() - timedelta(days=100),
            tx_type="P",
        )
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=filings),
            patch("quantbot.stocks.insider_trading.engine._parse_form4_xml", return_value=[old_txn]),
        ):
            result = compute_insider_trading("NVDA")
        assert result.signal == "no_data"
        assert result.cluster_buy_count == 0

    def test_cluster_sell_signal(self) -> None:
        filings = _make_filings(2)
        txns_a = [_make_mock_txn("Alice", tx_type="S", shares=2000.0)]
        txns_b = [_make_mock_txn("Bob", tx_type="S", shares=1500.0)]
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=filings),
            patch(
                "quantbot.stocks.insider_trading.engine._parse_form4_xml",
                side_effect=[txns_a, txns_b],
            ),
        ):
            result = compute_insider_trading("META")
        assert result.signal == "cluster_sell"
        assert result.net_shares_90d < 0

    def test_no_filings_gives_no_data(self) -> None:
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=[]),
        ):
            result = compute_insider_trading("EMPTY")
        assert result.signal == "no_data"
        assert result.data_available is True

    def test_cik_not_found_gives_degraded(self) -> None:
        with patch("quantbot.stocks.insider_trading.engine._load_cik", return_value=None):
            result = compute_insider_trading("FAKE")
        assert result.data_available is False
        assert result.cik is None

    def test_transactions_limited_to_20(self) -> None:
        """Response should cap transactions at 20."""
        filings = _make_filings(1)
        many_txns = [_make_mock_txn(f"Person{i}", shares=100.0) for i in range(30)]
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch("quantbot.stocks.insider_trading.engine._get_recent_form4_filings", return_value=filings),
            patch("quantbot.stocks.insider_trading.engine._parse_form4_xml", return_value=many_txns),
        ):
            result = compute_insider_trading("BIG")
        assert len(result.transactions) <= 20


# ---------------------------------------------------------------------------
# IT-4: graceful degradation
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_exception_gives_data_available_false(self) -> None:
        with patch(
            "quantbot.stocks.insider_trading.engine._load_cik",
            side_effect=Exception("network error"),
        ):
            result = compute_insider_trading("CRASH")
        assert result.data_available is False

    def test_never_raises(self) -> None:
        with patch(
            "quantbot.stocks.insider_trading.engine._load_cik",
            side_effect=RuntimeError("boom"),
        ):
            result = compute_insider_trading("ERROR")
        assert result is not None

    def test_degraded_has_today_date(self) -> None:
        with patch(
            "quantbot.stocks.insider_trading.engine._load_cik",
            side_effect=Exception("err"),
        ):
            result = compute_insider_trading("AMZN")
        assert result.as_of_date == date.today()

    def test_edgar_fetch_failure_gives_degraded(self) -> None:
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch(
                "quantbot.stocks.insider_trading.engine._get_recent_form4_filings",
                side_effect=Exception("timeout"),
            ),
        ):
            result = compute_insider_trading("GOOG")
        assert result.data_available is False


# ---------------------------------------------------------------------------
# IT-5: API endpoint
# ---------------------------------------------------------------------------


class TestInsiderTradingAPIEndpoint:
    @pytest.fixture()
    def client(self) -> TestClient:
        from quantbot.web.app import app
        return TestClient(app)

    def test_missing_ticker_returns_422(self, client: TestClient) -> None:
        resp = client.get("/api/insider-trading/")
        assert resp.status_code == 422

    def test_always_200_even_when_degraded(self, client: TestClient) -> None:
        with patch(
            "quantbot.stocks.insider_trading.engine._load_cik",
            side_effect=Exception("fail"),
        ):
            resp = client.get("/api/insider-trading/?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data_available"] is False
        assert body["ticker"] == "AAPL"

    def test_happy_path_returns_all_fields(self, client: TestClient) -> None:
        filings = _make_filings(2)
        txns_a = [_make_mock_txn("Alice CEO")]
        txns_b = [_make_mock_txn("Bob CFO")]
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch(
                "quantbot.stocks.insider_trading.engine._get_recent_form4_filings",
                return_value=filings,
            ),
            patch(
                "quantbot.stocks.insider_trading.engine._parse_form4_xml",
                side_effect=[txns_a, txns_b],
            ),
        ):
            resp = client.get("/api/insider-trading/?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        for field in [
            "ticker", "cik", "signal", "cluster_buy_count", "cluster_sell_count",
            "net_shares_90d", "transactions", "interpretation", "as_of_date", "data_available",
        ]:
            assert field in body, f"Missing field: {field}"

    def test_transactions_list_serialized(self, client: TestClient) -> None:
        filings = _make_filings(1)
        txns = [_make_mock_txn("Alice CEO")]
        with (
            patch("quantbot.stocks.insider_trading.engine._load_cik", return_value="0000789019"),
            patch(
                "quantbot.stocks.insider_trading.engine._get_recent_form4_filings",
                return_value=filings,
            ),
            patch(
                "quantbot.stocks.insider_trading.engine._parse_form4_xml",
                return_value=txns,
            ),
        ):
            resp = client.get("/api/insider-trading/?ticker=MSFT")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body["transactions"], list)
