"""Momentum module — Jegadeesh-Titman price momentum factor signal."""

from quantbot.stocks.momentum.engine import (
    MomentumGrade,
    MomentumSignal,
    compute_momentum_signal,
)

__all__ = [
    "MomentumGrade",
    "MomentumSignal",
    "compute_momentum_signal",
]
