"""EDGAR REST API 异步客户端（Phase F.2）.

遵守 SEC 使用条款：
  - User-Agent 必须标识（公司名 + 邮件）
  - 限速：请求间隔 ≥ 0.11s（不超过 10 req/s）
  - 不重新分发原始 EDGAR 文件

公共接口：
  get_cik(ticker)                       → str
  get_recent_8k_filings(ticker, ...)    → list[EightKFiling]
  get_form4_transactions(ticker, ...)   → list[Form4Transaction]
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import date
from html.parser import HTMLParser
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

from quantbot.stocks.edgar.models import (
    EightKFiling,
    EightKItem,
    Form4Transaction,
)

# ── 常量 ──────────────────────────────────────────────────────────────────────

_USER_AGENT = "QuantBot/1.0 jdawlaia@gmail.com"
_RATE_LIMIT_INTERVAL = 0.11  # s，≥0.1 确保 ≤ 10 req/s
_TIMEOUT = 15.0              # s

_BASE_DATA = "https://data.sec.gov"
_BASE_WWW = "https://www.sec.gov"

# ── 内部速率限制器 ────────────────────────────────────────────────────────────

_last_request_time: float = 0.0


async def _rate_limit() -> None:
    """在每次 HTTP 请求前等待，确保不超过 SEC 限速."""
    global _last_request_time
    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_INTERVAL:
        await asyncio.sleep(_RATE_LIMIT_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": _USER_AGENT},
        timeout=_TIMEOUT,
        follow_redirects=True,
    )


# ── CIK 查询 ──────────────────────────────────────────────────────────────────

# 静态映射（MAG7 + 常用）；可通过 EDGAR company_tickers.json 扩展
_TICKER_CIK_CACHE: dict[str, str] = {}

_KNOWN_TICKERS: dict[str, str] = {
    "AAPL": "0000320193",
    "MSFT": "0000789019",
    "GOOGL": "0001652044",
    "GOOG": "0001652044",
    "AMZN": "0001018724",
    "META": "0001326801",
    "NVDA": "0001045810",
    "TSLA": "0001318605",
    "SPY": "0000884394",
    "QQQ": "0001067839",
    "BRK.B": "0001067983",
    "JPM": "0000019617",
    "V": "0001403161",
    "UNH": "0000731766",
}


async def get_cik(ticker: str) -> str:
    """将股票代码转换为 EDGAR CIK（零填充 10 位）.

    先查静态缓存；未命中则调用 EDGAR company_tickers.json。
    """
    t = ticker.upper()
    if t in _TICKER_CIK_CACHE:
        return _TICKER_CIK_CACHE[t]

    # 优先使用内置映射
    if t in _KNOWN_TICKERS:
        _TICKER_CIK_CACHE[t] = _KNOWN_TICKERS[t]
        return _KNOWN_TICKERS[t]

    # 从 EDGAR 全量 ticker 映射文件获取
    await _rate_limit()
    async with _make_client() as client:
        resp = await client.get(f"{_BASE_WWW}/cgi-bin/browse-edgar?company=&CIK={t}&type=8-K&dateb=&owner=include&count=1&search_text=&action=getcompany&output=atom")
        resp.raise_for_status()
        # 从 Atom XML 解析 CIK
        root = ET.fromstring(resp.text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", ns):
            cik_elem = entry.find("atom:id", ns)
            if cik_elem is not None and cik_elem.text:
                m = re.search(r"CIK=(\d+)", cik_elem.text)
                if m:
                    cik = m.group(1).zfill(10)
                    _TICKER_CIK_CACHE[t] = cik
                    return cik

    raise ValueError(f"Cannot resolve CIK for ticker '{ticker}'")


# ── HTML 清洗 ─────────────────────────────────────────────────────────────────

class _HTMLTextExtractor(HTMLParser):
    """剥离 HTML 标签，保留文本（简单版）."""

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    p = _HTMLTextExtractor()
    p.feed(html)
    return p.get_text()


# ── 8-K 解析 ──────────────────────────────────────────────────────────────────

# 匹配 Item X.XX 标题行
_ITEM_RE = re.compile(
    r"Item\s+(\d+\.\d+)[.\s–\-—:]+([^\n\r]{5,120})",
    re.IGNORECASE,
)


def _parse_8k_items(text: str) -> list[EightKItem]:
    """从 8-K 纯文本中解析 item 块."""
    items: list[EightKItem] = []
    matches = list(_ITEM_RE.finditer(text))
    for i, m in enumerate(matches):
        item_number = m.group(1).strip()
        item_title = m.group(2).strip().rstrip(".")
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        item_text = text[start:end].strip()
        if item_text:
            items.append(EightKItem(
                item_number=item_number,
                item_title=item_title,
                text=item_text,
            ))
    return items


async def _fetch_filing_document(url: str) -> str:
    """下载 EDGAR 文件（HTML/TXT）并返回纯文本."""
    await _rate_limit()
    async with _make_client() as client:
        resp = await client.get(url)
        resp.raise_for_status()
        content = resp.text
    # 去 HTML
    if "<html" in content.lower() or "<body" in content.lower():
        content = _strip_html(content)
    return content


async def _get_filing_index_url(cik: str, accession_number: str) -> str:
    """构造 8-K 文件索引页 URL."""
    acc_no_dash = accession_number.replace("-", "")
    return f"{_BASE_WWW}/Archives/edgar/data/{int(cik)}/{acc_no_dash}/{accession_number}-index.htm"


async def _find_main_document_url(cik: str, accession_number: str) -> str:
    """从 filing index 找到主文档（.htm 或 .txt）URL."""
    acc_no_dash = accession_number.replace("-", "")
    base = f"{_BASE_WWW}/Archives/edgar/data/{int(cik)}/{acc_no_dash}/"

    await _rate_limit()
    async with _make_client() as client:
        resp = await client.get(f"{base}{accession_number}-index.htm")
        if resp.status_code != 200:
            # 回退：直接用 .txt
            return f"{base}{accession_number}.txt"
        html = resp.text

    # 找第一个 type=8-K 对应的文件
    m = re.search(r'href="([^"]+\.(?:htm|html|txt))"[^>]*>[^<]*8-K', html, re.IGNORECASE)
    if m:
        path = m.group(1)
        if path.startswith("http"):
            return path
        return f"{_BASE_WWW}{path}" if path.startswith("/") else base + path

    # 回退：找第一个 .htm 链接
    m2 = re.search(r'href="([^"]+\.(?:htm|html))"', html, re.IGNORECASE)
    if m2:
        path = m2.group(1)
        return f"{_BASE_WWW}{path}" if path.startswith("/") else base + path

    return f"{base}{accession_number}.txt"


# ── 公共 API：8-K ─────────────────────────────────────────────────────────────


async def get_recent_8k_filings(
    ticker: str,
    *,
    max_count: int = 5,
) -> list[EightKFiling]:
    """获取最近 N 份 8-K，含解析好的 item 段落.

    Args:
        ticker:    股票代码（如 "AAPL"）
        max_count: 最多返回几份提交记录

    Returns:
        按提交日期倒序排列的 EightKFiling 列表
    """
    cik = await get_cik(ticker)

    # 从 submissions JSON 获取 8-K 提交历史
    await _rate_limit()
    async with _make_client() as client:
        resp = await client.get(f"{_BASE_DATA}/submissions/CIK{cik}.json")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    forms: list[str] = recent.get("form", [])
    dates: list[str] = recent.get("filingDate", [])
    accessions: list[str] = recent.get("accessionNumber", [])
    report_dates: list[str] = recent.get("reportDate", [])

    filings: list[EightKFiling] = []
    for form, filed_str, acc, report_str in zip(forms, dates, accessions, report_dates):
        if form not in ("8-K", "8-K/A"):
            continue
        if len(filings) >= max_count:
            break

        filed_date = date.fromisoformat(filed_str)
        period = date.fromisoformat(report_str) if report_str else None

        # 尝试下载并解析主文档
        try:
            doc_url = await _find_main_document_url(cik, acc)
            raw_text = await _fetch_filing_document(doc_url)
            items = _parse_8k_items(raw_text)
        except Exception as exc:
            logger.warning(f"Failed to parse 8-K {acc}: {exc}")
            items = []
            doc_url = ""

        filings.append(EightKFiling(
            ticker=ticker.upper(),
            cik=cik,
            accession_number=acc,
            filed_date=filed_date,
            period_of_report=period,
            items=items,
            raw_html_url=doc_url,
        ))

    return filings


# ── Form 4 解析 ───────────────────────────────────────────────────────────────

_KEY_ROLE_RE = re.compile(
    r"(CEO|CFO|President|Chairman|Chief\s+Executive|Chief\s+Financial|"
    r"Chief\s+Operating|Chief\s+Technology|Director|VP|Vice\s+President|"
    r"EVP|SVP|COO|CTO|CLO|General\s+Counsel)",
    re.IGNORECASE,
)


def _is_key_insider(title: str) -> bool:
    return bool(_KEY_ROLE_RE.search(title))


def _parse_form4_xml(xml_text: str, ticker: str, cik: str, accession: str) -> list[Form4Transaction]:
    """从 Form 4 XML 解析所有 non-derivative 和 derivative 交易记录."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    # 获取 insider 信息
    rpt_owner = root.find(".//reportingOwner")
    if rpt_owner is None:
        return []

    name_elem = rpt_owner.find(".//rptOwnerName")
    insider_name = name_elem.text.strip() if name_elem is not None and name_elem.text else "Unknown"

    title_elem = rpt_owner.find(".//officerTitle")
    is_director_elem = rpt_owner.find(".//isDirector")
    insider_title = ""
    if title_elem is not None and title_elem.text:
        insider_title = title_elem.text.strip()
    elif is_director_elem is not None and is_director_elem.text == "1":
        insider_title = "Director"

    if not _is_key_insider(insider_title):
        return []

    transactions: list[Form4Transaction] = []

    for txn in root.findall(".//nonDerivativeTransaction") + root.findall(".//derivativeTransaction"):
        try:
            # Transaction code: P = purchase, S = sale
            code_elem = txn.find(".//transactionCode")
            if code_elem is None or code_elem.text not in ("P", "S"):
                continue
            txn_type = code_elem.text

            # Date
            date_elem = txn.find(".//transactionDate/value")
            if date_elem is None or not date_elem.text:
                continue
            txn_date = date.fromisoformat(date_elem.text.strip())

            # Shares
            shares_elem = txn.find(".//transactionShares/value")
            shares = float(shares_elem.text) if shares_elem is not None and shares_elem.text else 0.0

            # Price
            price_elem = txn.find(".//transactionPricePerShare/value")
            price = float(price_elem.text) if price_elem is not None and price_elem.text else 0.0

            # 10b5-1 plan
            plan_elem = txn.find(".//transactionTimeliness")
            is_plan = plan_elem is not None and plan_elem.text in ("E", "L")

            transactions.append(Form4Transaction(
                ticker=ticker.upper(),
                cik=cik,
                insider_name=insider_name,
                insider_title=insider_title,
                transaction_date=txn_date,
                transaction_type=txn_type,
                shares=abs(shares),
                price_per_share=price,
                total_value=abs(shares) * price,
                is_10b5_1_plan=is_plan,
                accession_number=accession,
            ))
        except (ValueError, AttributeError):
            continue

    return transactions


async def get_form4_transactions(
    ticker: str,
    *,
    since_date: date | None = None,
    max_count: int = 100,
) -> list[Form4Transaction]:
    """获取指定 ticker 的 Form 4 内部人交易记录.

    Args:
        ticker:     股票代码
        since_date: 仅返回该日期之后的记录
        max_count:  最多处理的 Form 4 提交数

    Returns:
        Form4Transaction 列表（按日期倒序）
    """
    cik = await get_cik(ticker)

    await _rate_limit()
    async with _make_client() as client:
        resp = await client.get(f"{_BASE_DATA}/submissions/CIK{cik}.json")
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()

    recent = data.get("filings", {}).get("recent", {})
    forms: list[str] = recent.get("form", [])
    dates: list[str] = recent.get("filingDate", [])
    accessions: list[str] = recent.get("accessionNumber", [])

    all_txns: list[Form4Transaction] = []
    processed = 0
    for form, filed_str, acc in zip(forms, dates, accessions):
        if form not in ("4", "4/A"):
            continue
        if processed >= max_count:
            break

        filed_date = date.fromisoformat(filed_str)
        if since_date and filed_date < since_date:
            break  # records are newest-first

        processed += 1
        acc_no_dash = acc.replace("-", "")
        xml_url = (
            f"{_BASE_WWW}/Archives/edgar/data/{int(cik)}/{acc_no_dash}/{acc}.xml"
        )

        try:
            await _rate_limit()
            async with _make_client() as client:
                resp = await client.get(xml_url)
                if resp.status_code == 404:
                    # Some Form 4s don't have a matching .xml — skip
                    continue
                resp.raise_for_status()
                xml_text = resp.text
            txns = _parse_form4_xml(xml_text, ticker, cik, acc)
            all_txns.extend(txns)
        except Exception as exc:
            logger.debug(f"Skipping Form4 {acc}: {exc}")
            continue

    # Sort newest first
    all_txns.sort(key=lambda t: t.transaction_date, reverse=True)
    return all_txns
