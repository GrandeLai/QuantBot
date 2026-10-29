"""Earnings Quality module — Piotroski F-Score + Beneish M-Score + Sloan Accrual."""

from quantbot.stocks.earnings_quality.engine import (
    EarningsQualityData,
    EarningsQualityGrade,
    compute_earnings_quality,
)

__all__ = [
    "EarningsQualityData",
    "EarningsQualityGrade",
    "compute_earnings_quality",
]
