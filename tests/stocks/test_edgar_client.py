"""Tests for edgar/client.py (EDGAR REST API 客户端).

所有网络请求都通过 httpx mock 拦截，不发真实请求。
"""
from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from quantbot.stocks.edgar.client import (
    _parse_8k_items,
    _strip_html,
    get_cik,
    get_recent_8k_filings,
    get_form4_transactions,
)


# ── HTML 清洗测试 ──────────────────────────────────────────────────────────────

class TestStripHTML:
    def test_plain_text_unchanged(self):
        assert "Hello World" in _strip_html("<p>Hello World</p>")

    def test_removes_tags(self):
        result = _strip_html("<html><body><p>This is text.</p></body></html>")
        assert "<p>" not in result
        assert "This is text." in result

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_no_html(self):
        result = _strip_html("Plain text no tags")
        assert "Plain text no tags" in result


# ── 8-K item 解析测试 ─────────────────────────────────────────────────────────

class TestParse8KItems:
    def test_parses_single_item(self):
        text = """
Item 5.02. Departure of Directors or Certain Officers.

Effective March 15, 2024, John Smith resigned as Chief Executive Officer.
The Board has initiated a search for a replacement.
"""
        items = _parse_8k_items(text)
        assert len(items) == 1
        assert items[0].item_number == "5.02"
        assert "Departure" in items[0].item_title
        assert "John Smith" in items[0].text

    def test_parses_multiple_items(self):
        text = """
Item 1.01. Entry into a Material Definitive Agreement.

We entered into a merger agreement on January 10, 2024.

Item 5.02. Departure of Certain Officers.

The CEO resigned effective today.
"""
        items = _parse_8k_items(text)
        assert len(items) == 2
        assert items[0].item_number == "1.01"
        assert items[1].item_number == "5.02"

    def test_empty_text_returns_empty_list(self):
        assert _parse_8k_items("") == []

    def test_no_items_returns_empty_list(self):
        assert _parse_8k_items("No items here, just random text.") == []


# ── CIK 查询测试 ──────────────────────────────────────────────────────────────

class TestGetCik:
    @pytest.mark.asyncio
    async def test_known_ticker_returns_cik(self):
        # AAPL は内置映射中
        cik = await get_cik("AAPL")
        assert cik == "0000320193"

    @pytest.mark.asyncio
    async def test_case_insensitive(self):
        cik = await get_cik("aapl")
        assert cik == "0000320193"

    @pytest.mark.asyncio
    async def test_unknown_ticker_raises_on_network_error(self):
        """未知 ticker 且网络失败时应抛出 ValueError 或类似异常."""
        with patch("quantbot.stocks.edgar.client._make_client") as mock_client:
            mock_resp = MagicMock()
            mock_resp.status_code = 404
            mock_resp.raise_for_status.side_effect = Exception("404 Not Found")

            mock_async_ctx = AsyncMock()
            mock_async_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_async_ctx.__aexit__ = AsyncMock(return_value=False)

            mock_httpx = AsyncMock()
            mock_httpx.get = AsyncMock(side_effect=Exception("Network error"))
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_httpx)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(Exception):
                await get_cik("ZZZZUNKNOWN9999")


# ── 8-K 抓取测试（mock httpx）────────────────────────────────────────────────

def _make_submissions_response(ticker: str) -> dict:
    """构造模拟的 EDGAR submissions JSON."""
    return {
        "filings": {
            "recent": {
                "form": ["8-K", "10-K", "8-K"],
                "filingDate": ["2024-03-01", "2024-02-15", "2024-01-15"],
                "accessionNumber": [
                    "0000320193-24-000031",
                    "0000320193-24-000020",
                    "0000320193-24-000010",
                ],
                "reportDate": ["2024-02-28", "2023-12-31", "2024-01-10"],
            }
        }
    }


class TestGetRecent8KFilings:
    @pytest.mark.asyncio
    async def test_returns_list_of_filings(self):
        json.dumps(_make_submissions_response("AAPL"))
        doc_html = """
<html><body>
Item 5.02. Departure of Officers.

The CEO resigned effective today.
</body></html>
"""
        with (
            patch("quantbot.stocks.edgar.client.get_cik", return_value="0000320193"),
            patch("quantbot.stocks.edgar.client._find_main_document_url",
                  return_value="https://www.sec.gov/Archives/edgar/data/320193/test.htm"),
            patch("quantbot.stocks.edgar.client._fetch_filing_document",
                  return_value=doc_html),
        ):
            # 手动 mock httpx client for submissions
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _make_submissions_response("AAPL")

            async def mock_get(url):
                return mock_resp

            mock_httpx = AsyncMock()
            mock_httpx.get = mock_get

            with patch("quantbot.stocks.edgar.client._make_client") as mock_factory:
                ctx = AsyncMock()
                ctx.__aenter__ = AsyncMock(return_value=mock_httpx)
                ctx.__aexit__ = AsyncMock(return_value=False)
                mock_factory.return_value = ctx

                filings = await get_recent_8k_filings("AAPL", max_count=2)

        assert len(filings) == 2
        for f in filings:
            assert f.ticker == "AAPL"
            assert isinstance(f.filed_date, date)

    @pytest.mark.asyncio
    async def test_max_count_respected(self):
        with (
            patch("quantbot.stocks.edgar.client.get_cik", return_value="0000320193"),
            patch("quantbot.stocks.edgar.client._find_main_document_url",
                  return_value="https://www.sec.gov/test.htm"),
            patch("quantbot.stocks.edgar.client._fetch_filing_document",
                  return_value="Item 8.01. Other Events.\n\nsome content here."),
        ):
            mock_resp = MagicMock()
            mock_resp.raise_for_status = MagicMock()
            mock_resp.json.return_value = _make_submissions_response("AAPL")

            mock_httpx = AsyncMock()
            mock_httpx.get = AsyncMock(return_value=mock_resp)

            with patch("quantbot.stocks.edgar.client._make_client") as mock_factory:
                ctx = AsyncMock()
                ctx.__aenter__ = AsyncMock(return_value=mock_httpx)
                ctx.__aexit__ = AsyncMock(return_value=False)
                mock_factory.return_value = ctx

                filings = await get_recent_8k_filings("AAPL", max_count=1)

        assert len(filings) == 1


# ── Form 4 解析测试 ───────────────────────────────────────────────────────────

FORM4_XML_SAMPLE = """<?xml version="1.0" ?>
<ownershipDocument>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>APPLE INC</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>COOK TIM</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTransaction>
    <securityTitle><value>Common Stock</value></securityTitle>
    <transactionDate><value>2024-03-15</value></transactionDate>
    <transactionCoding>
      <transactionCode>P</transactionCode>
    </transactionCoding>
    <transactionAmounts>
      <transactionShares><value>10000</value></transactionShares>
      <transactionPricePerShare><value>175.50</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </transactionAmounts>
  </nonDerivativeTransaction>
</ownershipDocument>
"""


class TestGetForm4Transactions:
    @pytest.mark.asyncio
    async def test_parses_purchase_transaction(self):
        subs_data = {
            "filings": {
                "recent": {
                    "form": ["4"],
                    "filingDate": ["2024-03-16"],
                    "accessionNumber": ["0001234567-24-000001"],
                }
            }
        }

        mock_subs_resp = MagicMock()
        mock_subs_resp.raise_for_status = MagicMock()
        mock_subs_resp.json.return_value = subs_data

        mock_xml_resp = MagicMock()
        mock_xml_resp.raise_for_status = MagicMock()
        mock_xml_resp.status_code = 200
        mock_xml_resp.text = FORM4_XML_SAMPLE

        call_count = [0]

        async def mock_get(url):
            call_count[0] += 1
            if "submissions" in url:
                return mock_subs_resp
            return mock_xml_resp

        mock_httpx = AsyncMock()
        mock_httpx.get = mock_get

        with patch("quantbot.stocks.edgar.client._make_client") as mock_factory:
            ctx = AsyncMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_httpx)
            ctx.__aexit__ = AsyncMock(return_value=False)
            mock_factory.return_value = ctx

            with patch("quantbot.stocks.edgar.client.get_cik", return_value="0000320193"):
                txns = await get_form4_transactions("AAPL")

        assert len(txns) == 1
        t = txns[0]
        assert t.transaction_type == "P"
        assert t.shares == 10000.0
        assert t.price_per_share == 175.50
        assert t.total_value == pytest.approx(10000 * 175.50)
        assert "Cook" in t.insider_name or "COOK" in t.insider_name
