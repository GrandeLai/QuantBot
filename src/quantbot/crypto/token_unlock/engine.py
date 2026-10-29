"""Crypto token unlock calendar + sell pressure model.

Background:
    Token unlocks release previously locked allocations to team members,
    early investors, or ecosystem funds. Recipients have strong economic
    incentive to sell immediately (profit taking, hedging).

    Historical pattern (based on Nansen / Messari research):
    - Unlocks > 5% of circulating supply → avg -3% to -8% in 7 days prior
    - Post-unlock (14-30 days) → avg +2% to +5% rebound as selling pressure clears

Strategy signals:
    - HIGH_RISK (score ≥ 0.6): pre-unlock short opportunity
    - MODERATE_RISK (0.3-0.6): monitor, reduce long exposure
    - POST_UNLOCK_REBOUND (past unlock, score was high): potential entry
    - LOW_RISK (< 0.3): no action needed

Data source:
    DefiLlama Emissions API (free, no API key required).
    Endpoint: https://defillama-datasets.llama.fi/emissionsProtocolOverview
    Falls back to empty list if API is unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Literal

import httpx
from loguru import logger


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFILLAMA_BASE = "https://defillama-datasets.llama.fi"
_DEFILLAMA_EMISSIONS = f"{_DEFILLAMA_BASE}/emissionsProtocolOverview"
_HTTP_TIMEOUT = 15.0  # seconds

# Category weights for sell pressure (higher = more likely to sell)
_CATEGORY_WEIGHTS: dict[str, float] = {
    "team": 1.0,
    "investors": 0.9,
    "insiders": 0.95,
    "advisors": 0.85,
    "foundation": 0.6,
    "ecosystem": 0.5,
    "community": 0.35,
    "public_sale": 0.3,
    "liquidity": 0.4,
    "other": 0.4,
}

UnlockSignal = Literal["high_risk", "moderate_risk", "low_risk", "post_unlock_rebound"]
UnlockCategory = Literal["team", "investors", "ecosystem", "public_sale", "other"]


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class TokenUnlockEvent:
    """A single scheduled token unlock event."""

    protocol: str
    symbol: str
    unlock_date: date
    days_until_unlock: int        # negative = already unlocked
    unlock_tokens: float
    unlock_usd: float | None      # None if price data unavailable
    unlock_pct_circulating: float # % of circulating supply being unlocked
    category: str                 # team | investors | ecosystem | public_sale | other
    sell_pressure_score: float    # 0.0 – 1.0
    signal: UnlockSignal


@dataclass
class TokenUnlockCalendar:
    """Upcoming token unlock events."""

    as_of_date: date
    events: list[TokenUnlockEvent] = field(default_factory=list)
    total_events: int = 0
    high_risk_count: int = 0      # sell_pressure_score >= 0.6


# ---------------------------------------------------------------------------
# Sell pressure model
# ---------------------------------------------------------------------------

def compute_sell_pressure_score(
    unlock_pct_circulating: float,
    days_until_unlock: int,
    category: str = "other",
) -> float:
    """Compute a 0-1 sell pressure score.

    Args:
        unlock_pct_circulating: % of circulating supply being unlocked (0-100)
        days_until_unlock: days until the unlock (negative = already past)
        category: allocation category (team, investors, ecosystem, etc.)

    Returns:
        Score in [0.0, 1.0]; higher = more sell pressure
    """
    if unlock_pct_circulating <= 0:
        return 0.0

    # Base score: 10%+ unlock → full score
    base_score = min(1.0, unlock_pct_circulating / 10.0)

    # Time multiplier: peaks at 0 days, decays to 0.3 at 60+ days
    if days_until_unlock < 0:
        # Already passed — lingering post-unlock pressure
        time_mult = max(0.0, 0.3 + days_until_unlock * 0.02)  # decays after unlock
    else:
        time_mult = max(0.3, 1.0 - days_until_unlock / 60.0)

    # Category weight
    cat_key = category.lower().replace(" ", "_").replace("-", "_")
    cat_weight = _CATEGORY_WEIGHTS.get(cat_key, 0.4)

    score = base_score * time_mult * cat_weight
    return round(min(1.0, max(0.0, score)), 4)


def _signal_from_score(score: float, days_until: int) -> UnlockSignal:
    """Classify the unlock signal based on sell pressure score and timing."""
    if days_until < -14:
        # More than 2 weeks past — selling pressure likely cleared → rebound
        return "post_unlock_rebound"
    if score >= 0.6:
        return "high_risk"
    if score >= 0.3:
        return "moderate_risk"
    return "low_risk"


# ---------------------------------------------------------------------------
# DefiLlama API client
# ---------------------------------------------------------------------------

def _normalise_category(raw: str) -> str:
    """Map DefiLlama category labels to our canonical set."""
    raw_l = raw.lower()
    for key in _CATEGORY_WEIGHTS:
        if key in raw_l:
            return key
    return "other"


def _parse_event(item: dict[str, Any], today: date) -> TokenUnlockEvent | None:
    """Parse a single event dict from DefiLlama into a TokenUnlockEvent."""
    try:
        protocol = str(item.get("name") or item.get("protocol") or "unknown")
        symbol   = str(item.get("gecko_id") or item.get("symbol") or "?").upper()

        # Unlock timestamp (seconds)
        ts_raw = item.get("timestamp") or item.get("date") or item.get("unlock_date")
        if ts_raw is None:
            return None
        ts = int(float(ts_raw))
        unlock_dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        days_until = (unlock_dt - today).days

        # Skip events more than 90 days past
        if days_until < -90:
            return None

        unlock_tokens = float(item.get("unlockTokens") or item.get("tokens") or 0)
        unlock_usd_raw = item.get("unlockUsd") or item.get("usd")
        unlock_usd: float | None = float(unlock_usd_raw) if unlock_usd_raw else None

        circ_raw = item.get("circSupply") or item.get("circulating_supply")
        if circ_raw and float(circ_raw) > 0 and unlock_tokens > 0:
            pct = unlock_tokens / float(circ_raw) * 100
        elif unlock_usd and item.get("mcap"):
            pct = unlock_usd / float(item["mcap"]) * 100
        else:
            pct = float(item.get("pctCirculating") or item.get("pct") or 0)

        category = _normalise_category(
            str(item.get("category") or item.get("label") or "other")
        )
        score = compute_sell_pressure_score(pct, days_until, category)
        signal = _signal_from_score(score, days_until)

        return TokenUnlockEvent(
            protocol=protocol,
            symbol=symbol,
            unlock_date=unlock_dt,
            days_until_unlock=days_until,
            unlock_tokens=unlock_tokens,
            unlock_usd=unlock_usd,
            unlock_pct_circulating=round(pct, 4),
            category=category,
            sell_pressure_score=score,
            signal=signal,
        )
    except Exception as e:
        logger.debug(f"[TokenUnlock] parse error: {e} | item={item}")
        return None


def _fetch_defillama(days_ahead: int) -> list[dict[str, Any]]:
    """Call DefiLlama emissions overview and return raw event list."""
    try:
        resp = httpx.get(_DEFILLAMA_EMISSIONS, timeout=_HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()

        # DefiLlama may return a list or a dict with "events" key
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("events") or data.get("data") or []
        return []
    except httpx.TimeoutException:
        logger.warning("[TokenUnlock] DefiLlama API timeout")
        return []
    except httpx.HTTPStatusError as e:
        logger.warning(f"[TokenUnlock] DefiLlama HTTP {e.response.status_code}")
        return []
    except Exception as e:
        logger.warning(f"[TokenUnlock] DefiLlama fetch failed: {e}")
        return []


def fetch_upcoming_unlocks(days_ahead: int = 30) -> TokenUnlockCalendar:
    """Fetch and classify upcoming token unlock events.

    Uses DefiLlama Emissions API. Returns an empty calendar (not None)
    if the API is unreachable, so the caller never has to handle None.

    Args:
        days_ahead: include events within this many days in the future (+ past 90 days)

    Returns:
        TokenUnlockCalendar sorted by unlock_date ascending
    """
    today = date.today()
    raw_items = _fetch_defillama(days_ahead)

    events: list[TokenUnlockEvent] = []
    for raw in raw_items:
        evt = _parse_event(raw, today)
        if evt is None:
            continue
        # Filter to window: -90 days ... +days_ahead days
        if -90 <= evt.days_until_unlock <= days_ahead:
            events.append(evt)

    # Sort by days_until_unlock ascending (soonest first)
    events.sort(key=lambda e: e.days_until_unlock)

    high_risk = sum(1 for e in events if e.sell_pressure_score >= 0.6)

    return TokenUnlockCalendar(
        as_of_date=today,
        events=events,
        total_events=len(events),
        high_risk_count=high_risk,
    )
