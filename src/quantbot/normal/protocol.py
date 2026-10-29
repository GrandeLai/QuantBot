"""Frozen validation windows for the point-in-time Normal experiments."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import MappingProxyType

from ..validation_protocol import (
    FORMAL_SEALED_END,
    FORMAL_SEALED_START,
    NORMAL_FORMAL_SEALED_WARMUP_END,
    NORMAL_FORMAL_SEALED_WARMUP_START,
)


@dataclass(frozen=True, slots=True)
class NormalExperimentWindow:
    """One exact request interval and its complete factor warmup interval."""

    request_start: date
    request_end: date
    warmup_start: date
    warmup_end: date


NORMAL_RESEARCH_WINDOW = NormalExperimentWindow(
    request_start=date(2010, 1, 1),
    request_end=date(2020, 12, 31),
    warmup_start=date(2007, 1, 1),
    warmup_end=date(2009, 12, 31),
)
NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW = NormalExperimentWindow(
    request_start=date(2021, 1, 1),
    request_end=date(2024, 12, 31),
    warmup_start=date(2018, 1, 1),
    warmup_end=date(2020, 12, 31),
)
NORMAL_SEALED_WINDOW = NormalExperimentWindow(
    request_start=FORMAL_SEALED_START,
    request_end=FORMAL_SEALED_END,
    warmup_start=NORMAL_FORMAL_SEALED_WARMUP_START,
    warmup_end=NORMAL_FORMAL_SEALED_WARMUP_END,
)

NORMAL_EXPERIMENT_WINDOWS = MappingProxyType(
    {
        "research": NORMAL_RESEARCH_WINDOW,
        "adaptive_development": NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW,
        "sealed": NORMAL_SEALED_WINDOW,
    }
)


def require_exact_normal_experiment_window(
    *,
    mode: str,
    request_start: date,
    request_end: date,
    warmup_start: date,
    warmup_end: date,
) -> NormalExperimentWindow:
    """Reject partial or caller-selected Normal evaluation windows.

    Normal has no remaining fixed holdout.  Research and adaptive development
    may reject a candidate, while the future sealed interval is the sole
    formal admission window.  The warmup is part of the read interval and is
    frozen here so shortening it cannot silently change the strategy.
    """

    expected = NORMAL_EXPERIMENT_WINDOWS.get(mode)
    if expected is None:
        raise PermissionError(
            "normal experiments allow only research, adaptive_development, or sealed access"
        )
    actual = NormalExperimentWindow(
        request_start=request_start,
        request_end=request_end,
        warmup_start=warmup_start,
        warmup_end=warmup_end,
    )
    if actual != expected:
        raise ValueError(
            "normal %s window is frozen at request %s..%s with warmup %s..%s"
            % (
                mode,
                expected.request_start.isoformat(),
                expected.request_end.isoformat(),
                expected.warmup_start.isoformat(),
                expected.warmup_end.isoformat(),
            )
        )
    return expected


__all__ = (
    "NORMAL_ADAPTIVE_DEVELOPMENT_WINDOW",
    "NORMAL_EXPERIMENT_WINDOWS",
    "NORMAL_RESEARCH_WINDOW",
    "NORMAL_SEALED_WINDOW",
    "NormalExperimentWindow",
    "require_exact_normal_experiment_window",
)
