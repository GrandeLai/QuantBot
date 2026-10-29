"""Market breadth, sector rankings, and overall market stance."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date


@dataclass
class IndexSnapshot:
    name: str
    change_pct: float
    close: float


@dataclass
class SectorPerformance:
    name: str
    change_pct: float


@dataclass
class MarketReview:
    date: str
    advances: int
    declines: int
    flat: int
    advance_ratio: float
    volume_ratio: float
    stance: str       # "A" (强势) | "B" (中性) | "C" (弱势)
    indices: list[IndexSnapshot]
    top_sectors: list[SectorPerformance]
    bottom_sectors: list[SectorPerformance]


class MarketReviewer:
    """Fetch market breadth and sector data from AKShare."""

    async def get_review(self) -> MarketReview:
        return await asyncio.get_event_loop().run_in_executor(None, self._fetch_sync)

    def _fetch_sync(self) -> MarketReview:
        try:
            import akshare as ak
            df = ak.stock_zh_a_spot_em()
            advances = int((df["涨跌幅"] > 0).sum())
            declines = int((df["涨跌幅"] < 0).sum())
            flat = len(df) - advances - declines
            adv_ratio = advances / max(advances + declines, 1)

            indices: list[IndexSnapshot] = []
            for code, name in [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]:
                try:
                    idx = ak.stock_zh_index_daily(symbol=code)
                    if not idx.empty:
                        last = idx.iloc[-1]
                        prev = idx.iloc[-2]["close"] if len(idx) >= 2 else last["close"]
                        chg = (last["close"] - prev) / (prev + 1e-9) * 100
                        indices.append(IndexSnapshot(name=name, change_pct=round(chg, 2), close=float(last["close"])))
                except Exception:
                    pass

            top: list[SectorPerformance] = []
            bottom: list[SectorPerformance] = []
            try:
                sector_df = ak.stock_board_industry_name_em()
                sector_df = sector_df.sort_values("涨跌幅", ascending=False)
                top = [SectorPerformance(row["板块名称"], round(float(row["涨跌幅"]), 2))
                       for _, row in sector_df.head(3).iterrows()]
                bottom = [SectorPerformance(row["板块名称"], round(float(row["涨跌幅"]), 2))
                          for _, row in sector_df.tail(3).iterrows()]
            except Exception:
                pass

            stance = "A" if adv_ratio > 0.6 else ("C" if adv_ratio < 0.4 else "B")
            return MarketReview(
                date=str(date.today()),
                advances=advances, declines=declines, flat=flat,
                advance_ratio=round(adv_ratio, 3),
                volume_ratio=1.0,
                stance=stance,
                indices=indices,
                top_sectors=top,
                bottom_sectors=bottom,
            )
        except Exception as exc:
            from loguru import logger
            logger.warning(f"[MarketReview] AKShare error: {exc}")
            return MarketReview(
                date=str(date.today()), advances=0, declines=0, flat=0,
                advance_ratio=0.5, volume_ratio=1.0, stance="B",
                indices=[], top_sectors=[], bottom_sectors=[],
            )
