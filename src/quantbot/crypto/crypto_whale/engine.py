"""Crypto on-chain whale & CEX inflow monitor.

Background:
    Large ETH transfers to centralized exchange (CEX) hot wallets signal that
    holders are preparing to sell.  Conversely, outflows indicate self-custody
    accumulation.  Net inflow extremes are historically correlated with near-term
    price pressure (Glassnode, 2021; CryptoQuant research).

Data:
    - Etherscan API (free tier with ETHERSCAN_API_KEY env var)
    - ETH spot price from Binance public API (no key required)
    - Returns empty data gracefully when API key is absent or calls fail.

Formula:
    pressure_score = (net_ratio + 1) / 2   ∈ [0, 1]
    where net_ratio = (inflow − outflow) / (inflow + outflow + ε)
    1.0 = maximum inflow (bearish), 0.0 = maximum outflow (bullish)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

import httpx
from loguru import logger


# ---------------------------------------------------------------------------
# Known CEX hot-wallet addresses (lower-cased for comparison)
# ---------------------------------------------------------------------------

CEX_WALLETS: dict[str, list[str]] = {
    "Binance": [
        "0x3f5ce5fbfe3e9af3971dd833d26ba9b5c936f0be",
        "0xd551234ae421e3bcba99a0da6d736074f22192ff",
    ],
    "Coinbase": [
        "0x71660c4005ba85c37ccec55d0c4493e66fe775d3",
        "0x503828976d22510aad0201ac7ec88293211d23da",
    ],
    "Kraken": [
        "0x2910543af39aba0cd09dbb2d50200b3e800a63d2",
    ],
    "OKX": [
        "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b",
    ],
}

# Flat lookup: address → exchange name
_ADDR_TO_EXCHANGE: dict[str, str] = {
    addr: name for name, addrs in CEX_WALLETS.items() for addr in addrs
}

# All CEX addresses as a set for O(1) lookup
_ALL_CEX_ADDRS: frozenset[str] = frozenset(_ADDR_TO_EXCHANGE.keys())

# ── Types ────────────────────────────────────────────────────────────────────

WhaleSignal = Literal[
    "heavy_inflow",
    "elevated_inflow",
    "neutral",
    "accumulation",
    "heavy_accumulation",
]

ETHERSCAN_BASE = "https://api.etherscan.io/api"
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT"

_HTTP_TIMEOUT = 10.0  # seconds


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class WhaleTransfer:
    """Single large on-chain ETH transfer involving a known CEX wallet."""

    tx_hash: str
    from_address: str
    to_address: str
    value_eth: float
    value_usd: float | None
    timestamp: datetime
    exchange_name: str          # which CEX is involved
    direction: Literal["inflow", "outflow"]   # relative to CEX


@dataclass
class CEXInflowData:
    """Aggregated CEX inflow/outflow metrics for ETH over a time window."""

    symbol: str                  # "ETH"
    hours: int                   # lookback window
    min_eth: float               # minimum transfer size
    inflow_eth: float            # total ETH flowing INTO CEX wallets
    outflow_eth: float           # total ETH flowing OUT OF CEX wallets
    net_flow_eth: float          # inflow − outflow (positive = net into CEX)
    inflow_usd: float | None
    pressure_score: float        # 0.0 (max outflow) → 1.0 (max inflow)
    signal: WhaleSignal
    transfer_count: int
    recent_transfers: list[WhaleTransfer] = field(default_factory=list)
    as_of_date: date = field(default_factory=date.today)
    api_key_missing: bool = False  # True when ETHERSCAN_API_KEY not set


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _compute_pressure_score(inflow: float, outflow: float) -> float:
    """Map net-inflow ratio to [0, 1] pressure score.

    Returns 0.5 (neutral) when both flows are zero.
    """
    total = inflow + outflow
    if total <= 0.0:
        return 0.5
    net_ratio = (inflow - outflow) / total   # in [-1, 1]
    return round((net_ratio + 1.0) / 2.0, 4)


def _pressure_signal(score: float) -> WhaleSignal:
    if score > 0.75:
        return "heavy_inflow"
    if score > 0.60:
        return "elevated_inflow"
    if score >= 0.40:
        return "neutral"
    if score >= 0.25:
        return "accumulation"
    return "heavy_accumulation"


def _wei_to_eth(wei_str: str) -> float:
    try:
        return int(wei_str) / 1e18
    except (ValueError, TypeError):
        return 0.0


def _get_eth_price() -> float | None:
    """Fetch current ETH/USDT price from Binance."""
    try:
        resp = httpx.get(BINANCE_PRICE_URL, timeout=_HTTP_TIMEOUT)
        data = resp.json()
        return float(data["price"])
    except Exception as exc:
        logger.debug(f"[WhaleMonitor] ETH price fetch failed: {exc}")
        return None


def _fetch_txlist(address: str, api_key: str, start_ts: int) -> list[dict]:
    """Fetch latest transactions for a CEX wallet address."""
    params = {
        "module": "account",
        "action": "txlist",
        "address": address,
        "sort": "desc",
        "apikey": api_key,
        # Fetch up to 200 most recent; we filter by timestamp in Python
        "offset": "200",
        "page": "1",
    }
    try:
        resp = httpx.get(ETHERSCAN_BASE, params=params, timeout=_HTTP_TIMEOUT)
        data = resp.json()
        if data.get("status") != "1":
            return []
        result = data.get("result", [])
        if not isinstance(result, list):
            return []
        return [
            tx for tx in result
            if int(tx.get("timeStamp", 0)) >= start_ts and tx.get("isError") == "0"
        ]
    except Exception as exc:
        logger.debug(f"[WhaleMonitor] Etherscan txlist failed for {address}: {exc}")
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_cex_inflow(
    symbol: str = "ETH",
    hours: int = 24,
    min_eth: float = 100.0,
) -> CEXInflowData:
    """Compute CEX inflow/outflow metrics and pressure score.

    Always returns a CEXInflowData (never None); uses `api_key_missing=True`
    and zero flows when the API key is absent or all calls fail.

    Args:
        symbol:  Currently only "ETH" is supported.
        hours:   Lookback window in hours.
        min_eth: Minimum transfer size to count (ETH).

    Returns:
        CEXInflowData with aggregated metrics.
    """
    api_key = os.environ.get("ETHERSCAN_API_KEY", "")
    if not api_key:
        logger.warning("[WhaleMonitor] ETHERSCAN_API_KEY not set — returning empty data")
        return CEXInflowData(
            symbol=symbol, hours=hours, min_eth=min_eth,
            inflow_eth=0.0, outflow_eth=0.0, net_flow_eth=0.0,
            inflow_usd=None, pressure_score=0.5, signal="neutral",
            transfer_count=0, api_key_missing=True,
        )

    start_ts = int(time.time()) - hours * 3600
    eth_price = _get_eth_price()

    transfers: list[WhaleTransfer] = []
    all_addrs = list(_ADDR_TO_EXCHANGE.keys())

    for addr in all_addrs:
        txs = _fetch_txlist(addr, api_key, start_ts)
        for tx in txs:
            val_eth = _wei_to_eth(tx.get("value", "0"))
            if val_eth < min_eth:
                continue
            from_addr = tx.get("from", "").lower()
            to_addr = tx.get("to", "").lower()
            ts = datetime.fromtimestamp(int(tx.get("timeStamp", 0)), tz=timezone.utc)
            val_usd = val_eth * eth_price if eth_price else None

            if to_addr == addr:
                direction: Literal["inflow", "outflow"] = "inflow"
                exchange = _ADDR_TO_EXCHANGE[addr]
            elif from_addr == addr:
                direction = "outflow"
                exchange = _ADDR_TO_EXCHANGE[addr]
            else:
                continue

            transfers.append(WhaleTransfer(
                tx_hash=tx.get("hash", ""),
                from_address=from_addr,
                to_address=to_addr,
                value_eth=round(val_eth, 4),
                value_usd=round(val_usd, 0) if val_usd else None,
                timestamp=ts,
                exchange_name=exchange,
                direction=direction,
            ))

    # Deduplicate by tx_hash (a single tx may match both from & to if internal)
    seen: set[str] = set()
    deduped: list[WhaleTransfer] = []
    for t in transfers:
        if t.tx_hash not in seen:
            seen.add(t.tx_hash)
            deduped.append(t)

    inflow_eth = sum(t.value_eth for t in deduped if t.direction == "inflow")
    outflow_eth = sum(t.value_eth for t in deduped if t.direction == "outflow")
    net_flow = round(inflow_eth - outflow_eth, 4)
    score = _compute_pressure_score(inflow_eth, outflow_eth)
    signal = _pressure_signal(score)
    inflow_usd = round(inflow_eth * eth_price, 0) if eth_price else None

    # Sort by value desc for display, keep top 50
    deduped.sort(key=lambda t: t.value_eth, reverse=True)

    return CEXInflowData(
        symbol=symbol, hours=hours, min_eth=min_eth,
        inflow_eth=round(inflow_eth, 4),
        outflow_eth=round(outflow_eth, 4),
        net_flow_eth=net_flow,
        inflow_usd=inflow_usd,
        pressure_score=score,
        signal=signal,
        transfer_count=len(deduped),
        recent_transfers=deduped[:50],
    )


def fetch_recent_whale_transfers(
    hours: int = 24,
    min_eth: float = 100.0,
    limit: int = 20,
) -> list[WhaleTransfer]:
    """Return sorted list of recent large whale transfers.

    Convenience wrapper around compute_cex_inflow that returns just the
    transfer list.
    """
    data = compute_cex_inflow(hours=hours, min_eth=min_eth)
    return data.recent_transfers[:limit]
