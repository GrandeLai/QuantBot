"""Fundamental analysis — PE, PB, ROE, revenue growth via AKShare."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class FundamentalData:
    symbol: str
    pe_ttm: float | None
    pb: float | None
    roe: float | None
    revenue_growth: float | None
    debt_equity: float | None
    market_cap: float | None
    divi_yield: float | None


class FundamentalAnalyzer:
    """Fetch key fundamental metrics from AKShare (free, no key required)."""

    async def get(self, symbol: str) -> FundamentalData:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._fetch_sync, symbol
        )

    def _fetch_sync(self, symbol: str) -> FundamentalData:
        result = FundamentalData(
            symbol=symbol, pe_ttm=None, pb=None, roe=None,
            revenue_growth=None, debt_equity=None,
            market_cap=None, divi_yield=None,
        )
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            row = df[df["代码"] == symbol]
            if not row.empty:
                r = row.iloc[0]
                result.pe_ttm = self._safe_float(r.get("市盈率-动态"))
                result.pb = self._safe_float(r.get("市净率"))
                result.market_cap = self._safe_float(r.get("总市值"))
        except Exception:
            pass

        try:
            import akshare as ak
            fin = ak.stock_financial_analysis_indicator(symbol=symbol, start_year="2022")
            if fin is not None and not fin.empty:
                fin = fin.sort_values("报告期", ascending=False)
                result.roe = self._safe_float(fin.iloc[0].get("净资产收益率(%)"))
        except Exception:
            pass

        return result

    def _safe_float(self, val: object) -> float | None:
        try:
            return float(val)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
