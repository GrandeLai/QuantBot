"""Tests for api/sec.py (SEC API endpoints)."""
from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from quantbot.stocks.edgar.form4_engine import InsiderCluster
from quantbot.stocks.edgar.models import EightKFiling, EightKItem, Form4Transaction


def _get_client() -> TestClient:
    from quantbot.web.app import create_app
    return TestClient(create_app())


def _make_filing(
    ticker: str = "AAPL",
    accession: str = "0000320193-24-000031",
    filed_date: date = date(2024, 3, 1),
    items: list | None = None,
) -> EightKFiling:
    return EightKFiling(
        ticker=ticker,
        cik="0000320193",
        accession_number=accession,
        filed_date=filed_date,
        period_of_report=None,
        items=items or [
            EightKItem("5.02", "Director Changes", "The CEO resigned effective today."),
        ],
    )


def _make_cluster() -> InsiderCluster:
    txn = Form4Transaction(
        ticker="AAPL",
        cik="0000320193",
        insider_name="Tim Cook",
        insider_title="Chief Executive Officer",
        transaction_date=date(2024, 3, 15),
        transaction_type="P",
        shares=10000.0,
        price_per_share=175.0,
        total_value=1_750_000.0,
        is_10b5_1_plan=False,
        accession_number="ACC-001",
    )
    return InsiderCluster(
        ticker="AAPL",
        window_start=date(2024, 1, 1),
        window_end=date(2024, 3, 15),
        insider_count=2,
        total_value=1_750_000.0,
        avg_price=175.0,
        transactions=[txn],
        signal_strength=0.65,
        key_roles=["CEO"],
    )


class TestGet8KRecent:
    def test_happy_path(self):
        mock_filings = [_make_filing()]
        with patch(
            "quantbot.stocks.api.sec.get_recent_8k_filings",
            new_callable=AsyncMock,
            return_value=mock_filings,
        ):
            resp = _get_client().get("/api/sec/8k/recent?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert body["count"] == 1
        assert len(body["filings"]) == 1
        assert body["filings"][0]["accession_number"] == "0000320193-24-000031"

    def test_ticker_not_found_returns_404(self):
        with patch(
            "quantbot.stocks.api.sec.get_recent_8k_filings",
            new_callable=AsyncMock,
            side_effect=ValueError("Cannot resolve CIK for ticker 'ZZZZZ'"),
        ):
            resp = _get_client().get("/api/sec/8k/recent?ticker=ZZZZZ")
        assert resp.status_code == 404

    def test_network_error_returns_503(self):
        with patch(
            "quantbot.stocks.api.sec.get_recent_8k_filings",
            new_callable=AsyncMock,
            side_effect=Exception("Connection timeout"),
        ):
            resp = _get_client().get("/api/sec/8k/recent?ticker=AAPL")
        assert resp.status_code == 503

    def test_api_alias_works(self):
        mock_filings = [_make_filing()]
        with patch(
            "quantbot.stocks.api.sec.get_recent_8k_filings",
            new_callable=AsyncMock,
            return_value=mock_filings,
        ):
            resp = _get_client().get("/sec/8k/recent?ticker=AAPL")
        assert resp.status_code == 200


class TestGet8KDiff:
    def test_happy_path_with_two_filings(self):
        old_filing = _make_filing(accession="ACC-001", filed_date=date(2024, 1, 1))
        new_filing = _make_filing(
            accession="ACC-002",
            filed_date=date(2024, 3, 1),
            items=[EightKItem("5.02", "Director Changes", "The new CEO is Jane Doe.")],
        )
        with patch(
            "quantbot.stocks.api.sec.get_recent_8k_filings",
            new_callable=AsyncMock,
            return_value=[new_filing, old_filing],
        ):
            resp = _get_client().get("/api/sec/8k/diff?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_diff"] is True
        assert "overall_change_score" in body

    def test_fewer_than_two_filings(self):
        with patch(
            "quantbot.stocks.api.sec.get_recent_8k_filings",
            new_callable=AsyncMock,
            return_value=[_make_filing()],
        ):
            resp = _get_client().get("/api/sec/8k/diff?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_diff"] is False


class TestGetForm4Signals:
    def test_happy_path_with_cluster(self):
        cluster = _make_cluster()
        with patch(
            "quantbot.stocks.api.sec.get_form4_transactions",
            new_callable=AsyncMock,
            return_value=[cluster.transactions[0]],
        ):
            with patch(
                "quantbot.stocks.edgar.form4_engine.detect_clusters",
                return_value=[cluster],
            ):
                resp = _get_client().get("/api/sec/form4/signals?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert "clusters" in body

    def test_404_on_unknown_ticker(self):
        with patch(
            "quantbot.stocks.api.sec.get_form4_transactions",
            new_callable=AsyncMock,
            side_effect=ValueError("Cannot resolve CIK"),
        ):
            resp = _get_client().get("/api/sec/form4/signals?ticker=ZZZZZ")
        assert resp.status_code == 404


class TestGetSECSummary:
    def test_happy_path(self):
        mock_filing = _make_filing()
        mock_txn = Form4Transaction(
            ticker="AAPL",
            cik="0000320193",
            insider_name="Tim Cook",
            insider_title="CEO",
            transaction_date=date(2024, 3, 15),
            transaction_type="P",
            shares=1000.0,
            price_per_share=175.0,
            total_value=175_000.0,
            is_10b5_1_plan=False,
            accession_number="ACC-001",
        )
        with (
            patch(
                "quantbot.stocks.api.sec.get_recent_8k_filings",
                new_callable=AsyncMock,
                return_value=[mock_filing, _make_filing(accession="ACC-OLD", filed_date=date(2024, 1, 1))],
            ),
            patch(
                "quantbot.stocks.api.sec.get_form4_transactions",
                new_callable=AsyncMock,
                return_value=[mock_txn],
            ),
        ):
            resp = _get_client().get("/api/sec/summary?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "AAPL"
        assert "latest_8k" in body
        assert "8k_diff" in body
        assert "insider_clusters" in body
        assert "generated_at" in body

    def test_empty_data_graceful(self):
        with (
            patch(
                "quantbot.stocks.api.sec.get_recent_8k_filings",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "quantbot.stocks.api.sec.get_form4_transactions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            resp = _get_client().get("/api/sec/summary?ticker=AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert body["latest_8k"] == {}
        assert body["8k_diff"]["has_diff"] is False
        assert body["insider_clusters"] == []

    def test_api_alias(self):
        with (
            patch(
                "quantbot.stocks.api.sec.get_recent_8k_filings",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "quantbot.stocks.api.sec.get_form4_transactions",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            resp = _get_client().get("/sec/summary?ticker=AAPL")
        assert resp.status_code == 200
