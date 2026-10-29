"""Sector Momentum Heatmap engine.

Downloads 1M/3M/6M price returns for the 11 SPDR sector ETFs + SPY benchmark
via a single yfinance batch download.  Computes relative performance vs SPY
and grades each sector as leading/in_line/lagging.

Graceful degradation: always returns SectorMomentumData (never None, never
raises). data_available=False when yfinance cannot fetch prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import yfinance as yf
from loguru import logger

# ---------------------------------------------------------------------------
# Sector ETF universe
# ---------------------------------------------------------------------------

SECTOR_ETFS: dict[str, str] = {
    "XLK": "科技",
    "XLV": "医疗健康",
    "XLF": "金融",
    "XLY": "非必需消费",
    "XLP": "必需消费",
    "XLE": "能源",
    "XLI": "工业",
    "XLB": "材料",
    "XLRE": "房地产",
    "XLU": "公用事业",
    "XLC": "通信",
}

_BENCHMARK = "SPY"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class SectorReturn:
    """Single sector ETF performance snapshot."""

    ticker: str
    sector_name: str
    return_1m: float        # %
    return_3m: float        # %
    return_6m: float        # %
    vs_spy_1m: float        # return_1m - spy_return_1m
    grade: str              # "leading" / "in_line" / "lagging"


@dataclass
class SectorMomentumData:
    """Aggregated sector momentum snapshot."""

    sectors: list[SectorReturn] = field(default_factory=list)
    top3: list[SectorReturn] = field(default_factory=list)      # 1M leaders
    bottom3: list[SectorReturn] = field(default_factory=list)    # 1M laggards
    spy_return_1m: float = 0.0
    as_of_date: date = field(default_factory=date.today)
    data_available: bool = True


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _sector_grade(return_1m: float, vs_spy_1m: float) -> str:
    """Grade a sector relative to the SPY benchmark."""
    if vs_spy_1m > 2.0:
        return "leading"
    if vs_spy_1m < -2.0:
        return "lagging"
    return "in_line"


def _pct_return(prices: Any, bars_back: int) -> float:
    """Compute % return over last `bars_back` trading days."""
    try:
        import pandas as pd

        s = prices.dropna()
        if not isinstance(s, pd.Series):
            s = pd.Series(s)
        if len(s) <= bars_back:
            return 0.0
        end_price = float(s.iloc[-1])
        start_price = float(s.iloc[-bars_back])
        if start_price == 0:
            return 0.0
        return round((end_price / start_price - 1) * 100, 2)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Main computation
# ---------------------------------------------------------------------------


def compute_sector_momentum() -> SectorMomentumData:
    """Download 6 months of price data for all 11 sector ETFs + SPY.

    Always returns SectorMomentumData (never None, never raises).
    data_available=False when yfinance is unavailable.
    """
    def _degraded(reason: str) -> SectorMomentumData:
        logger.warning(f"[SectorMomentum] {reason}")
        return SectorMomentumData(data_available=False)

    try:
        all_tickers = [_BENCHMARK] + list(SECTOR_ETFS.keys())

        # Single batch download for efficiency
        raw = yf.download(
            tickers=all_tickers,
            period="6mo",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )

        if raw is None or raw.empty:
            return _degraded("yfinance download returned empty data")

        # Handle multi-level column index produced by batch download
        import pandas as pd

        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.xs("Close", level=0, axis=1)
        else:
            close = raw["Close"] if "Close" in raw.columns else raw

        if isinstance(close, pd.Series):
            close = close.to_frame()

        if close.empty:
            return _degraded("no Close prices in downloaded data")

        # SPY returns
        spy_col = _BENCHMARK if _BENCHMARK in close.columns else None
        spy_1m = _pct_return(close[spy_col], 21) if spy_col else 0.0

        # Build sector returns
        sectors: list[SectorReturn] = []
        for ticker, name in SECTOR_ETFS.items():
            if ticker not in close.columns:
                continue
            r1m = _pct_return(close[ticker], 21)
            r3m = _pct_return(close[ticker], 63)
            r6m = _pct_return(close[ticker], 126)
            vs_spy = round(r1m - spy_1m, 2)
            grade = _sector_grade(r1m, vs_spy)
            sectors.append(
                SectorReturn(
                    ticker=ticker,
                    sector_name=name,
                    return_1m=r1m,
                    return_3m=r3m,
                    return_6m=r6m,
                    vs_spy_1m=vs_spy,
                    grade=grade,
                )
            )

        if not sectors:
            return _degraded("no sector data in downloaded prices")

        # Sort by 1M return for top3/bottom3
        sorted_sectors = sorted(sectors, key=lambda s: s.return_1m, reverse=True)

        return SectorMomentumData(
            sectors=sectors,
            top3=sorted_sectors[:3],
            bottom3=sorted_sectors[-3:],
            spy_return_1m=spy_1m,
            as_of_date=date.today(),
            data_available=True,
        )

    except Exception as exc:
        logger.error(f"[SectorMomentum] unexpected error: {exc}")
        return _degraded(f"unexpected: {exc}")
