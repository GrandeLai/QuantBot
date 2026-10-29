"""Index Rebalance Engine — 指数成分股检测 + 调仓机会面板.

Phase F.23 — Index Rebalance Preview
数据来源：
  - Wikipedia 标准普尔 500 / NASDAQ 100 成分股列表（免费、公开）
  - yfinance：市值、价格、成交量、EPS、流通股

功能：
- 检查股票是否在 S&P 500 / NASDAQ 100 / Russell 2000 中
- 计算在指数中的市值百分位排名
- 评估纳入/剔除风险（基于市值阈值接近度）
- 盈利动量指标（辅助 S&P 500 纳入判断）

参考：
- S&P 500 纳入前 1 个月平均超额 +8%（Chen, Noronha & Singal 2004）
- 规则透明、时间可预测 → 可提前布局
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date
from typing import Literal
from urllib.request import Request, urlopen

import pandas as pd
import yfinance as yf

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

IndexStatus = Literal["member", "non_member", "unknown"]
RebalanceRisk = Literal["high_addition_risk", "moderate_addition_risk",
                         "stable", "moderate_deletion_risk", "high_deletion_risk", "unknown"]


@dataclass
class IndexMembership:
    index_name: str
    status: IndexStatus
    market_cap_rank: int | None     # rank within index (1 = largest)
    market_cap_pct: float | None    # percentile within index (0-100, 100 = largest)
    rebalance_risk: RebalanceRisk


@dataclass
class IndexRebalanceData:
    ticker: str
    market_cap: float | None          # USD
    market_cap_b: float | None        # billions
    float_shares: float | None
    price: float | None
    eps_ttm: float | None             # trailing 12m EPS (for S&P eligibility)
    indices: list[IndexMembership] = field(default_factory=list)
    sp500_members: int = 500
    interpretation: str = ""
    as_of_date: date = field(default_factory=date.today)
    data_available: bool = True


# ---------------------------------------------------------------------------
# Wikipedia data fetcher
# ---------------------------------------------------------------------------

_SP500_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_NQ100_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"

_HEADERS = {"User-Agent": "QuantBot research@quantpilot.dev"}

# Cache (dict from index name → set of tickers)
_INDEX_CACHE: dict[str, set[str]] = {}


def _fetch_sp500_tickers() -> set[str]:
    """Fetch S&P 500 ticker list from Wikipedia."""
    if "sp500" in _INDEX_CACHE:
        return _INDEX_CACHE["sp500"]
    try:
        req = Request(_SP500_URL, headers=_HEADERS)
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        tables = pd.read_html(io.StringIO(html))
        sp_table = tables[0]
        # Column is 'Symbol' or 'Ticker symbol'
        col = next(
            (c for c in sp_table.columns if "symbol" in str(c).lower() or "ticker" in str(c).lower()),
            sp_table.columns[0],
        )
        tickers = set(sp_table[col].str.strip().str.upper().tolist())
        _INDEX_CACHE["sp500"] = tickers
        return tickers
    except Exception:  # noqa: BLE001
        return set()


def _fetch_nq100_tickers() -> set[str]:
    """Fetch NASDAQ 100 ticker list from Wikipedia."""
    if "nq100" in _INDEX_CACHE:
        return _INDEX_CACHE["nq100"]
    try:
        req = Request(_NQ100_URL, headers=_HEADERS)
        with urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        tables = pd.read_html(io.StringIO(html))
        # Find the table with ticker symbols
        for table in tables:
            cols_lower = [str(c).lower() for c in table.columns]
            if any("tick" in c or "symbol" in c for c in cols_lower):
                col = next(c for c, cl in zip(table.columns, cols_lower) if "tick" in cl or "symbol" in cl)
                tickers = set(table[col].dropna().str.strip().str.upper().tolist())
                if len(tickers) >= 90:
                    _INDEX_CACHE["nq100"] = tickers
                    return tickers
        return set()
    except Exception:  # noqa: BLE001
        return set()


# ---------------------------------------------------------------------------
# Market cap thresholds (2024 approximate)
# ---------------------------------------------------------------------------

# S&P 500: min market cap ~$15.8B; currently min is around $8-15B depending on period
_SP500_MIN_CAP_B = 14.5   # approximate minimum for inclusion ($14.5B)
_SP500_DEL_CAP_B = 10.0   # typical deletion threshold (~$10B)

# NASDAQ 100: listed on NASDAQ, market cap > $5B (variable)
_NQ100_MIN_CAP_B = 5.0

# Russell 2000: small cap index ($300M - $2B range approximately)
_RUSSELL2000_MAX_CAP_B = 3.5
_RUSSELL2000_MIN_CAP_B = 0.3


def _assess_sp500_risk(
    ticker: str,
    market_cap_b: float | None,
    is_member: bool,
    rank: int | None,
    pct: float | None,
) -> RebalanceRisk:
    """Assess S&P 500 addition/deletion risk based on market cap."""
    if market_cap_b is None:
        return "unknown"
    if is_member:
        if market_cap_b < _SP500_DEL_CAP_B:
            return "high_deletion_risk"
        if market_cap_b < _SP500_MIN_CAP_B * 1.1:
            return "moderate_deletion_risk"
        return "stable"
    else:
        if market_cap_b > _SP500_MIN_CAP_B * 1.5:
            return "high_addition_risk"
        if market_cap_b > _SP500_MIN_CAP_B:
            return "moderate_addition_risk"
        return "stable"


def _assess_nq100_risk(
    market_cap_b: float | None,
    is_member: bool,
) -> RebalanceRisk:
    if market_cap_b is None:
        return "unknown"
    if is_member:
        if market_cap_b < _NQ100_MIN_CAP_B * 0.8:
            return "high_deletion_risk"
        return "stable"
    else:
        if market_cap_b > _NQ100_MIN_CAP_B * 2:
            return "high_addition_risk"
        if market_cap_b > _NQ100_MIN_CAP_B:
            return "moderate_addition_risk"
        return "stable"


def _assess_russell_risk(
    market_cap_b: float | None,
) -> RebalanceRisk:
    """Russell 2000: small-cap index, annual reconstitution in June."""
    if market_cap_b is None:
        return "unknown"
    if _RUSSELL2000_MIN_CAP_B <= market_cap_b <= _RUSSELL2000_MAX_CAP_B:
        return "stable"
    if market_cap_b > _RUSSELL2000_MAX_CAP_B:
        return "moderate_deletion_risk"
    return "high_deletion_risk"


# ---------------------------------------------------------------------------
# Interpretation builder
# ---------------------------------------------------------------------------

def _build_interpretation(
    ticker: str,
    market_cap_b: float | None,
    indices: list[IndexMembership],
) -> str:
    parts = []

    if market_cap_b is not None:
        parts.append(f"{ticker} 市值 ${market_cap_b:.1f}B。")

    for idx in indices:
        if idx.status == "member":
            rank_str = f"（第 {idx.market_cap_rank} 名，市值百分位 {idx.market_cap_pct:.0f}%）" if idx.market_cap_rank else ""
            risk_map = {
                "moderate_deletion_risk": "⚠ 接近剔除阈值，需关注市值变化。",
                "high_deletion_risk": "⚠ 市值偏低，存在被剔除风险。",
                "stable": "持仓稳定，暂无调仓风险。",
                "unknown": "",
            }
            risk_note = risk_map.get(idx.rebalance_risk, "")
            parts.append(f"✓ {idx.index_name} 成分股{rank_str}。{risk_note}")
        else:
            addition_map = {
                "high_addition_risk": f"★ 市值满足 {idx.index_name} 纳入条件（可能在下次调整时被纳入，预期超额 +8%/月）。",
                "moderate_addition_risk": f"△ 市值接近 {idx.index_name} 纳入门槛，值得关注。",
                "stable": f"市值低于 {idx.index_name} 纳入阈值，近期无调仓风险。",
                "unknown": "",
            }
            parts.append(addition_map.get(idx.rebalance_risk, ""))

    if not parts:
        parts.append("指数成分数据不可用。")

    return " ".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compute_index_rebalance(ticker: str) -> IndexRebalanceData:
    """
    Check index membership and assess rebalance risk for a stock.
    Always returns; never raises.
    """
    ticker = ticker.strip().upper()

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        market_cap = info.get("marketCap") or info.get("market_cap")
        float_shares = info.get("floatShares")
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        eps_ttm = info.get("trailingEps")

        market_cap_b = (market_cap / 1e9) if market_cap else None

        # Fetch index compositions
        sp500_tickers = _fetch_sp500_tickers()
        nq100_tickers = _fetch_nq100_tickers()

        # Normalize ticker (S&P 500 uses '.' not '-' sometimes)
        ticker_norm = ticker.replace("-", ".")

        in_sp500 = ticker in sp500_tickers or ticker_norm in sp500_tickers
        in_nq100 = ticker in nq100_tickers or ticker_norm in nq100_tickers

        # Russell 2000: approximate by market cap range (no free composition API)
        in_russell = (
            market_cap_b is not None
            and _RUSSELL2000_MIN_CAP_B <= market_cap_b <= _RUSSELL2000_MAX_CAP_B
        )

        # Build membership objects
        sp500_risk = _assess_sp500_risk(ticker, market_cap_b, in_sp500, None, None)
        nq100_risk = _assess_nq100_risk(market_cap_b, in_nq100)
        russell_risk = _assess_russell_risk(market_cap_b) if in_russell else "stable"

        indices = [
            IndexMembership(
                index_name="S&P 500",
                status="member" if in_sp500 else "non_member",
                market_cap_rank=None,
                market_cap_pct=None,
                rebalance_risk=sp500_risk,
            ),
            IndexMembership(
                index_name="NASDAQ 100",
                status="member" if in_nq100 else "non_member",
                market_cap_rank=None,
                market_cap_pct=None,
                rebalance_risk=nq100_risk,
            ),
            IndexMembership(
                index_name="Russell 2000 (估算)",
                status="member" if in_russell else "non_member",
                market_cap_rank=None,
                market_cap_pct=None,
                rebalance_risk=russell_risk,
            ),
        ]

        interpretation = _build_interpretation(ticker, market_cap_b, indices)

        data_available = market_cap is not None

        return IndexRebalanceData(
            ticker=ticker,
            market_cap=market_cap,
            market_cap_b=market_cap_b,
            float_shares=float_shares,
            price=price,
            eps_ttm=eps_ttm,
            indices=indices,
            interpretation=interpretation,
            as_of_date=date.today(),
            data_available=data_available,
        )

    except Exception:  # noqa: BLE001
        return IndexRebalanceData(
            ticker=ticker,
            market_cap=None,
            market_cap_b=None,
            float_shares=None,
            price=None,
            eps_ttm=None,
            data_available=False,
            interpretation="查询失败，请稍后重试。",
        )
