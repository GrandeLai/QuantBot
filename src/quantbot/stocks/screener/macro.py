"""A-share macroeconomic context — margin trading, northbound flows, M2, PMI."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date


@dataclass
class MacroContext:
    date: str
    margin_balance: float | None
    margin_change_pct: float | None
    northbound_net: float | None
    m2_growth: float | None
    pmi: float | None
    macro_score: float


class MacroAnalyzer:
    """Fetch macroeconomic indicators from AKShare."""

    async def get(self) -> MacroContext:
        return await asyncio.get_event_loop().run_in_executor(None, self._fetch_sync)

    def _fetch_sync(self) -> MacroContext:
        ctx = MacroContext(
            date=str(date.today()), margin_balance=None,
            margin_change_pct=None, northbound_net=None,
            m2_growth=None, pmi=None, macro_score=50.0,
        )
        try:
            import akshare as ak
            margin = ak.stock_margin_sse_summary()
            if not margin.empty:
                vals = margin["融资余额"].dropna().astype(float)
                if len(vals) >= 2:
                    ctx.margin_balance = float(vals.iloc[-1]) / 1e8
                    ctx.margin_change_pct = float(
                        (vals.iloc[-1] - vals.iloc[-5]) / (vals.iloc[-5] + 1e-9) * 100
                        if len(vals) >= 5 else 0
                    )
        except Exception:
            pass

        try:
            import akshare as ak
            nb = ak.stock_hsgt_north_net_flow_in_em()
            if not nb.empty:
                ctx.northbound_net = float(nb.iloc[-1]["当日净流入"])
        except Exception:
            pass

        try:
            import akshare as ak
            pmi_df = ak.macro_china_pmi()
            if not pmi_df.empty:
                ctx.pmi = float(pmi_df.iloc[-1]["制造业-指数"])
        except Exception:
            pass

        ctx.macro_score = self._compute_score(ctx)
        return ctx

    def _compute_score(self, ctx: MacroContext) -> float:
        score = 50.0
        if ctx.margin_change_pct is not None:
            score += min(max(ctx.margin_change_pct * 2, -20), 20)
        if ctx.northbound_net is not None:
            score += 10 if ctx.northbound_net > 0 else -10
        if ctx.pmi is not None:
            score += 10 if ctx.pmi > 50 else -10
        return round(min(max(score, 0), 100), 1)
