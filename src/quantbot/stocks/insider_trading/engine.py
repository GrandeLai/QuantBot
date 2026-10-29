"""Insider Trading Engine — SEC EDGAR Form 4 cluster signal.

Phase F.21 — Form 4 内部人交易聚类信号
数据来源：SEC EDGAR 免费 API（data.sec.gov）

功能：
- 查询公司 CIK（通过 company_tickers.json）
- 拉取最近 Form 4 申报
- 识别 90 天内多位内部人净买入（剔除 10b5-1 自动计划单）
- 输出聚类信号强度：cluster_buy / cluster_sell / mixed / neutral

参考文献：Cohen et al. 2012（内部人集群买入 → 180日超额 6-10%）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal
from urllib.request import Request, urlopen

import json

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

InsiderSignal = Literal["cluster_buy", "cluster_sell", "mixed", "neutral", "no_data"]


@dataclass
class InsiderTransaction:
    insider_name: str
    title: str
    transaction_date: date
    shares: float
    price_per_share: float | None
    transaction_type: str  # "P" buy, "S" sell, "A" award
    is_10b5_plan: bool
    form_url: str


@dataclass
class InsiderTradingData:
    ticker: str
    cik: str | None
    signal: InsiderSignal
    cluster_buy_count: int   # distinct insiders buying in 90d
    cluster_sell_count: int  # distinct insiders selling in 90d
    net_shares_90d: float    # buy - sell (shares)
    transactions: list[InsiderTransaction] = field(default_factory=list)
    interpretation: str = ""
    as_of_date: date = field(default_factory=date.today)
    data_available: bool = True


# ---------------------------------------------------------------------------
# EDGAR helpers
# ---------------------------------------------------------------------------

_EDGAR_BASE = "https://data.sec.gov"
_HEADERS = {"User-Agent": "QuantBot research@quantpilot.dev"}

# Cache company tickers mapping (loaded once per process)
_TICKER_CIK_CACHE: dict[str, str] = {}


def _edgar_get(url: str) -> Any:
    """Fetch JSON from EDGAR with proper User-Agent header."""
    req = Request(url, headers=_HEADERS)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_cik(ticker: str) -> str | None:
    """Look up CIK for a ticker using EDGAR company_tickers.json."""
    global _TICKER_CIK_CACHE
    if not _TICKER_CIK_CACHE:
        try:
            data = _edgar_get(f"{_EDGAR_BASE}/files/company_tickers.json")
            _TICKER_CIK_CACHE = {
                v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                for v in data.values()
            }
        except Exception:  # noqa: BLE001
            return None
    return _TICKER_CIK_CACHE.get(ticker.upper())


def _get_recent_form4_filings(cik: str, max_filings: int = 40) -> list[dict]:
    """Get recent Form 4 filing metadata from EDGAR submissions API."""
    url = f"{_EDGAR_BASE}/submissions/CIK{cik}.json"
    data = _edgar_get(url)
    recent = data.get("filings", {}).get("recent", {})
    if not recent:
        return []

    form_types = recent.get("form", [])
    dates = recent.get("filingDate", [])
    accession_nums = recent.get("accessionNumber", [])

    filings: list[dict[str, str]] = []
    for ft, dt, acc in zip(form_types, dates, accession_nums):
        if ft in ("4", "4/A") and len(filings) < max_filings:
            filings.append({"form": ft, "date": dt, "accession": acc})
    return filings


def _parse_form4_xml(cik: str, accession: str) -> list[dict]:
    """
    Parse a Form 4 filing's XML to extract transactions.
    Returns list of transaction dicts.
    """
    acc_nodash = accession.replace("-", "")
    base_url = f"{_EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{acc_nodash}"
    index_url = f"{base_url}/{accession}-index.htm"

    # Try to find the XML file
    try:
        index_data = Request(index_url, headers=_HEADERS)
        with urlopen(index_data, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []

    # Extract xml filename from index
    xml_match = re.search(r'href="([^"]*\.xml)"', html, re.IGNORECASE)
    if not xml_match:
        return []

    xml_path = xml_match.group(1)
    if not xml_path.startswith("/"):
        xml_path = f"/Archives/edgar/data/{int(cik)}/{acc_nodash}/{xml_path}"

    try:
        xml_req = Request(f"{_EDGAR_BASE}{xml_path}", headers=_HEADERS)
        with urlopen(xml_req, timeout=10) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return []

    return _extract_transactions_from_xml(xml, accession)


def _extract_transactions_from_xml(xml: str, accession: str) -> list[dict]:
    """Extract transaction data from Form 4 XML using regex (no XML parser dep)."""
    transactions = []

    def _tag(tag: str, text: str) -> str | None:
        m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.DOTALL | re.IGNORECASE)
        return m.group(1).strip() if m else None

    # Insider name and title
    insider_name = _tag("rptOwnerName", xml) or "Unknown"
    title_match = re.search(
        r"<officerTitle>(.*?)</officerTitle>", xml, re.DOTALL | re.IGNORECASE
    )
    title = title_match.group(1).strip() if title_match else ""

    # Is this a 10b5-1 plan? Check for plan flag
    is_10b5 = bool(re.search(r"<rule10b5One.*?>true<", xml, re.IGNORECASE))

    # Extract all nonDerivativeTransaction blocks
    blocks = re.findall(
        r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>",
        xml,
        re.DOTALL | re.IGNORECASE,
    )

    for block in blocks:
        tx_date = _tag("transactionDate", block)
        tx_code = _tag("transactionCode", block)  # P=purchase, S=sale, A=award
        shares_str = _tag("transactionShares", block)
        price_str = _tag("transactionPricePerShare", block)

        if not tx_date or not tx_code or not shares_str:
            continue

        try:
            tx_date_parsed = date.fromisoformat(tx_date[:10])
        except ValueError:
            continue

        try:
            shares = float(shares_str)
        except (ValueError, TypeError):
            continue

        price = None
        if price_str:
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                pass

        transactions.append({
            "insider_name": insider_name,
            "title": title,
            "transaction_date": tx_date_parsed,
            "shares": shares,
            "price_per_share": price,
            "transaction_type": tx_code,
            "is_10b5_plan": is_10b5,
            "accession": accession,
        })

    return transactions


# ---------------------------------------------------------------------------
# Signal computation
# ---------------------------------------------------------------------------

_CLUSTER_THRESHOLD = 2  # min distinct insiders for "cluster" signal
_WINDOW_DAYS = 90


def _compute_signal(
    buys: dict[str, float],  # insider → net shares bought
    sells: dict[str, float],
) -> InsiderSignal:
    """Determine signal from distinct insider buy/sell counts."""
    buy_count = sum(1 for s in buys.values() if s > 0)
    sell_count = sum(1 for s in sells.values() if s > 0)

    if buy_count >= _CLUSTER_THRESHOLD and sell_count == 0:
        return "cluster_buy"
    if sell_count >= _CLUSTER_THRESHOLD and buy_count == 0:
        return "cluster_sell"
    if buy_count >= _CLUSTER_THRESHOLD and sell_count >= _CLUSTER_THRESHOLD:
        return "mixed"
    if buy_count == 0 and sell_count == 0:
        return "no_data"
    return "neutral"


def _build_interpretation(
    signal: InsiderSignal,
    buy_count: int,
    sell_count: int,
    net_shares: float,
) -> str:
    if signal == "cluster_buy":
        return (
            f"✓ 集群买入信号：{buy_count} 位内部人在 90 天内净买入合计 "
            f"{net_shares:,.0f} 股（已剔除 10b5-1 计划单）。"
            "根据 Cohen et al.（2012），此信号对应 180 日超额收益 6-10%。"
        )
    if signal == "cluster_sell":
        return (
            f"⚠ 集群卖出信号：{sell_count} 位内部人在 90 天内净卖出 "
            f"{abs(net_shares):,.0f} 股（已剔除 10b5-1 计划单）。"
            "高管集体减仓可能反映对短期前景的担忧，建议结合基本面确认。"
        )
    if signal == "mixed":
        return (
            f"内部人交易方向分歧：{buy_count} 位买入，{sell_count} 位卖出。"
            "净信号不明确，建议等待方向一致信号。"
        )
    if signal == "no_data":
        return "90 天内无内部人公开市场交易记录（或仅有 10b5-1 计划单）。"
    return (
        f"90 天内有内部人交易，但未达集群阈值（≥{_CLUSTER_THRESHOLD} 位）。"
        "净变动较小，信号不显著。"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_insider_trading(ticker: str) -> InsiderTradingData:
    """
    Fetch Form 4 filings from EDGAR and compute insider cluster signal.
    Always returns; never raises.
    """
    ticker = ticker.strip().upper()
    cutoff = date.today() - timedelta(days=_WINDOW_DAYS)

    try:
        cik = _load_cik(ticker)
        if not cik:
            return InsiderTradingData(
                ticker=ticker,
                cik=None,
                signal="no_data",
                cluster_buy_count=0,
                cluster_sell_count=0,
                net_shares_90d=0.0,
                data_available=False,
                interpretation=f"未找到 {ticker} 的 SEC CIK 编号（可能不是美股或 EDGAR 无记录）。",
            )

        filings = _get_recent_form4_filings(cik)
        if not filings:
            return InsiderTradingData(
                ticker=ticker,
                cik=cik,
                signal="no_data",
                cluster_buy_count=0,
                cluster_sell_count=0,
                net_shares_90d=0.0,
                data_available=True,
                interpretation="EDGAR 未找到最近的 Form 4 申报。",
            )

        all_txns: list[InsiderTransaction] = []
        # Only parse filings within 90+30 day window (buffer for indexing delay)
        lookback_cutoff = date.today() - timedelta(days=_WINDOW_DAYS + 30)

        for filing in filings:
            try:
                filing_date = date.fromisoformat(filing["date"])
            except ValueError:
                continue
            if filing_date < lookback_cutoff:
                break  # filings are chronological, stop early

            raw_txns = _parse_form4_xml(cik, filing["accession"])
            for t in raw_txns:
                if t["is_10b5_plan"]:
                    continue  # exclude 10b5-1 plan trades
                if t["transaction_date"] < cutoff:
                    continue
                if t["transaction_type"] not in ("P", "S"):
                    continue  # only open market buy/sell

                form_url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                    f"{filing['accession'].replace('-', '')}/{filing['accession']}-index.htm"
                )
                all_txns.append(
                    InsiderTransaction(
                        insider_name=t["insider_name"],
                        title=t["title"],
                        transaction_date=t["transaction_date"],
                        shares=t["shares"],
                        price_per_share=t["price_per_share"],
                        transaction_type=t["transaction_type"],
                        is_10b5_plan=False,
                        form_url=form_url,
                    )
                )

        # Aggregate by insider
        buys: dict[str, float] = {}
        sells: dict[str, float] = {}

        for txn in all_txns:
            key = txn.insider_name
            if txn.transaction_type == "P":
                buys[key] = buys.get(key, 0.0) + txn.shares
            elif txn.transaction_type == "S":
                sells[key] = sells.get(key, 0.0) + txn.shares

        buy_count = sum(1 for s in buys.values() if s > 0)
        sell_count = sum(1 for s in sells.values() if s > 0)
        total_bought = sum(buys.values())
        total_sold = sum(sells.values())
        net_shares = total_bought - total_sold

        signal = _compute_signal(buys, sells)
        interpretation = _build_interpretation(signal, buy_count, sell_count, net_shares)

        return InsiderTradingData(
            ticker=ticker,
            cik=cik,
            signal=signal,
            cluster_buy_count=buy_count,
            cluster_sell_count=sell_count,
            net_shares_90d=net_shares,
            transactions=all_txns[:20],  # cap for API response size
            interpretation=interpretation,
            as_of_date=date.today(),
            data_available=True,
        )

    except Exception:  # noqa: BLE001
        return InsiderTradingData(
            ticker=ticker,
            cik=None,
            signal="no_data",
            cluster_buy_count=0,
            cluster_sell_count=0,
            net_shares_90d=0.0,
            data_available=False,
            interpretation="查询失败，请稍后重试（SEC EDGAR 接口可能暂时不可用）。",
        )
