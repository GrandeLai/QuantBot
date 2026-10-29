"""税务亏损收割（Tax-Loss Harvesting）模块（Phase F.3）."""

from quantbot.stocks.tlh.engine import (
    TaxLot,
    TLHCandidate,
    WashSaleWarning,
    estimate_tax_saving,
    get_replacement_tickers,
    scan_tlh_candidates,
)

__all__ = [
    "TaxLot",
    "TLHCandidate",
    "WashSaleWarning",
    "scan_tlh_candidates",
    "estimate_tax_saving",
    "get_replacement_tickers",
]
