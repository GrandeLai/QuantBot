"""PEAD module — Post-Earnings Announcement Drift signal."""

from quantbot.stocks.pead.engine import (
    EarningsEvent,
    EarningsSurpriseGrade,
    PEADSignal,
    compute_pead_signal,
)

__all__ = [
    "EarningsEvent",
    "EarningsSurpriseGrade",
    "PEADSignal",
    "compute_pead_signal",
]
