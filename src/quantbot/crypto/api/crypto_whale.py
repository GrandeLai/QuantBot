"""Crypto whale & CEX inflow API endpoints."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from quantbot.crypto.crypto_whale.engine import (
    CEXInflowData,
    WhaleTransfer,
    compute_cex_inflow,
    fetch_recent_whale_transfers,
)

router = APIRouter(prefix="/crypto-whale", tags=["crypto-whale"])
_executor = ThreadPoolExecutor(max_workers=4)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class WhaleTransferResponse(BaseModel):
    tx_hash: str
    from_address: str
    to_address: str
    value_eth: float
    value_usd: float | None
    timestamp: datetime
    exchange_name: str
    direction: str


class CEXInflowResponse(BaseModel):
    symbol: str
    hours: int
    min_eth: float
    inflow_eth: float
    outflow_eth: float
    net_flow_eth: float
    inflow_usd: float | None
    pressure_score: float
    signal: str
    transfer_count: int
    recent_transfers: list[WhaleTransferResponse]
    as_of_date: str
    api_key_missing: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _transfer_to_resp(t: WhaleTransfer) -> WhaleTransferResponse:
    return WhaleTransferResponse(
        tx_hash=t.tx_hash,
        from_address=t.from_address,
        to_address=t.to_address,
        value_eth=t.value_eth,
        value_usd=t.value_usd,
        timestamp=t.timestamp,
        exchange_name=t.exchange_name,
        direction=t.direction,
    )


def _inflow_to_resp(d: CEXInflowData) -> CEXInflowResponse:
    return CEXInflowResponse(
        symbol=d.symbol,
        hours=d.hours,
        min_eth=d.min_eth,
        inflow_eth=d.inflow_eth,
        outflow_eth=d.outflow_eth,
        net_flow_eth=d.net_flow_eth,
        inflow_usd=d.inflow_usd,
        pressure_score=d.pressure_score,
        signal=d.signal,
        transfer_count=d.transfer_count,
        recent_transfers=[_transfer_to_resp(t) for t in d.recent_transfers],
        as_of_date=d.as_of_date.isoformat(),
        api_key_missing=d.api_key_missing,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/eth-inflow", response_model=CEXInflowResponse)
async def get_eth_cex_inflow(
    hours: int = Query(default=24, ge=1, le=168, description="Lookback window in hours"),
    min_eth: float = Query(default=100.0, ge=1.0, le=10000.0, description="Minimum transfer size in ETH"),
) -> Any:
    """Compute ETH CEX inflow/outflow metrics and pressure score.

    Returns 200 always (with api_key_missing=true when ETHERSCAN_API_KEY is absent).
    """
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        lambda: compute_cex_inflow(symbol="ETH", hours=hours, min_eth=min_eth),
    )
    return _inflow_to_resp(result)


@router.get("/recent-transfers", response_model=list[WhaleTransferResponse])
async def get_recent_whale_transfers(
    hours: int = Query(default=24, ge=1, le=168),
    min_eth: float = Query(default=100.0, ge=1.0, le=10000.0),
    limit: int = Query(default=20, ge=1, le=100),
) -> Any:
    """Return recent large ETH transfers to/from known CEX wallets.

    Returns empty list when ETHERSCAN_API_KEY is absent or all API calls fail.
    """
    loop = asyncio.get_event_loop()
    transfers = await loop.run_in_executor(
        _executor,
        lambda: fetch_recent_whale_transfers(hours=hours, min_eth=min_eth, limit=limit),
    )
    return [_transfer_to_resp(t) for t in transfers]
