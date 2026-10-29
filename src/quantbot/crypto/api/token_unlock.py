"""Token Unlock API — crypto token vesting schedule and sell pressure."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.crypto.token_unlock.engine import (
    TokenUnlockCalendar,
    TokenUnlockEvent,
    fetch_upcoming_unlocks,
)

router = APIRouter(prefix="/token-unlocks", tags=["token-unlocks"])

_executor = ThreadPoolExecutor(max_workers=2)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class TokenUnlockEventResponse(BaseModel):
    protocol: str
    symbol: str
    unlock_date: date
    days_until_unlock: int
    unlock_tokens: float
    unlock_usd: float | None
    unlock_pct_circulating: float
    category: str
    sell_pressure_score: float
    signal: str


class TokenUnlockCalendarResponse(BaseModel):
    as_of_date: date
    events: list[TokenUnlockEventResponse]
    total_events: int
    high_risk_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event_to_resp(e: TokenUnlockEvent) -> TokenUnlockEventResponse:
    return TokenUnlockEventResponse(
        protocol=e.protocol,
        symbol=e.symbol,
        unlock_date=e.unlock_date,
        days_until_unlock=e.days_until_unlock,
        unlock_tokens=e.unlock_tokens,
        unlock_usd=e.unlock_usd,
        unlock_pct_circulating=e.unlock_pct_circulating,
        category=e.category,
        sell_pressure_score=e.sell_pressure_score,
        signal=e.signal,
    )


def _cal_to_resp(cal: TokenUnlockCalendar) -> TokenUnlockCalendarResponse:
    return TokenUnlockCalendarResponse(
        as_of_date=cal.as_of_date,
        events=[_event_to_resp(e) for e in cal.events],
        total_events=cal.total_events,
        high_risk_count=cal.high_risk_count,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/upcoming", response_model=TokenUnlockCalendarResponse)
async def get_upcoming_unlocks(
    days: int = Query(default=30, ge=1, le=90, description="Look-ahead window in days"),
) -> Any:
    """Return upcoming crypto token unlock events with sell pressure scores.

    Always returns 200 (even if DefiLlama is unreachable — empty list).
    """
    loop = asyncio.get_event_loop()
    cal = await loop.run_in_executor(_executor, fetch_upcoming_unlocks, days)
    return _cal_to_resp(cal)


@router.get("/by-symbol", response_model=list[TokenUnlockEventResponse])
async def get_unlocks_by_symbol(
    symbol: str = Query(..., description="Token symbol, e.g. ARB"),
    days: int = Query(default=90, ge=1, le=365),
) -> Any:
    """Filter upcoming unlock events for a specific token symbol."""
    symbol = symbol.strip().upper()
    loop = asyncio.get_event_loop()
    cal = await loop.run_in_executor(_executor, fetch_upcoming_unlocks, days)
    filtered = [e for e in cal.events if e.symbol == symbol]
    return [_event_to_resp(e) for e in filtered]


@router.get("/high-risk", response_model=list[TokenUnlockEventResponse])
async def get_high_risk_unlocks(
    days: int = Query(default=30, ge=1, le=90),
    min_score: float = Query(default=0.6, ge=0.0, le=1.0),
) -> Any:
    """Return only high-risk unlock events (sell_pressure_score >= min_score)."""
    loop = asyncio.get_event_loop()
    cal = await loop.run_in_executor(_executor, fetch_upcoming_unlocks, days)
    high_risk = [e for e in cal.events if e.sell_pressure_score >= min_score]
    return [_event_to_resp(e) for e in high_risk]
