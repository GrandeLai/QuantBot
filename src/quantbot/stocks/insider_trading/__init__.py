"""Insider Trading module — SEC EDGAR Form 4 cluster signal."""

from quantbot.stocks.insider_trading.engine import (
    InsiderSignal,
    InsiderTradingData,
    InsiderTransaction,
    compute_insider_trading,
)

__all__ = [
    "InsiderSignal",
    "InsiderTradingData",
    "InsiderTransaction",
    "compute_insider_trading",
]
