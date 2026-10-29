"""Numeric-domain validation shared by paper and live acceptance flows."""

from math import isfinite
from typing import Iterable


def is_nonnegative_integer(value) -> bool:
    return type(value) is int and value >= 0


def is_finite_number(value) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def is_valid_drawdown(value) -> bool:
    return is_finite_number(value) and 0.0 <= float(value) <= 1.0


def valid_error_total(values: Iterable[object]) -> int:
    return sum(value for value in values if is_nonnegative_integer(value))


def require_nonnegative_integer(value, field: str) -> None:
    if not is_nonnegative_integer(value):
        raise ValueError("invalid_%s" % field)


def require_finite_number(value, field: str) -> None:
    if not is_finite_number(value):
        raise ValueError("invalid_%s" % field)


def require_drawdown(value) -> None:
    if not is_valid_drawdown(value):
        raise ValueError("invalid_drawdown")
