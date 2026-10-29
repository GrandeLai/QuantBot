"""Risk-adjusted return comparison vs sector peers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class PeerMetrics:
    symbol: str
    sharpe: float
    sortino: float
    max_drawdown: float
    total_return: float


@dataclass
class PeerComparison:
    symbol: str
    sector: str
    target: PeerMetrics
    peers: list[PeerMetrics]
    sector_avg_sharpe: float
    sector_avg_sortino: float
    sharpe_percentile: float
    sortino_percentile: float


class PeerComparator:
    """Compare a stock's risk-adjusted returns to its sector peers."""

    async def compare(self, symbol: str, period_days: int = 90) -> PeerComparison:
        return await asyncio.get_event_loop().run_in_executor(
            None, self._compare_sync, symbol, period_days
        )

    def _compare_sync(self, symbol: str, period_days: int) -> PeerComparison:
        import numpy as np

        sector = "未知"
        peer_symbols: list[str] = []
        try:
            import akshare as ak
            stock_info = ak.stock_individual_info_em(symbol=symbol)
            if not stock_info.empty:
                row = stock_info[stock_info["item"] == "行业"]
                if not row.empty:
                    sector = str(row.iloc[0]["value"])
            peers_df = ak.stock_board_industry_cons_em(symbol=sector)
            peer_symbols = peers_df["代码"].tolist()[:10]
        except Exception:
            pass

        def compute_metrics(sym: str) -> PeerMetrics | None:
            try:
                import akshare as ak
                df = ak.stock_zh_a_hist(
                    symbol=sym, period="daily", adjust="qfq", start_date="20230101"
                )
                if len(df) < 20:
                    return None
                closes = df["收盘"].astype(float).to_numpy()
                returns = np.diff(closes) / (closes[:-1] + 1e-9)
                returns = returns[-period_days:]
                mu = returns.mean() * 252
                sigma = returns.std() * (252**0.5) + 1e-9
                neg_sigma = returns[returns < 0].std() * (252**0.5) + 1e-9
                sharpe = float(mu / sigma)
                sortino = float(mu / neg_sigma)
                cum = np.cumprod(1 + returns)
                drawdown = float((cum / np.maximum.accumulate(cum) - 1).min())
                total_ret = float(cum[-1] - 1) if len(cum) else 0.0
                return PeerMetrics(sym, round(sharpe, 3), round(sortino, 3),
                                   round(drawdown, 3), round(total_ret, 3))
            except Exception:
                return None

        target_metrics = compute_metrics(symbol) or PeerMetrics(symbol, 0.0, 0.0, 0.0, 0.0)
        peer_metrics = [
            m for s in peer_symbols
            if s != symbol and (m := compute_metrics(s)) is not None
        ]

        all_sharpes = [p.sharpe for p in peer_metrics] or [0.0]
        all_sortinos = [p.sortino for p in peer_metrics] or [0.0]
        pct_sharpe = float(
            np.mean([1 for s in all_sharpes if s < target_metrics.sharpe]) * 100
        )
        pct_sortino = float(
            np.mean([1 for s in all_sortinos if s < target_metrics.sortino]) * 100
        )

        return PeerComparison(
            symbol=symbol, sector=sector,
            target=target_metrics, peers=peer_metrics,
            sector_avg_sharpe=round(float(np.mean(all_sharpes)), 3),
            sector_avg_sortino=round(float(np.mean(all_sortinos)), 3),
            sharpe_percentile=round(pct_sharpe, 1),
            sortino_percentile=round(pct_sortino, 1),
        )
