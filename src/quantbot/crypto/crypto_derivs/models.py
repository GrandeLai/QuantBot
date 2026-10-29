"""Pydantic models for crypto derivatives data (funding rate / OI / ETF flow)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Exchange = Literal["binance", "okx"]


class FundingRate(BaseModel):
    """永续合约 funding rate 单点观测."""

    exchange: Exchange
    symbol: str = Field(..., description="标准化 base asset，如 BTC / ETH")
    raw_symbol: str = Field(..., description="交易所原始符号，如 BTCUSDT 或 BTC-USDT-SWAP")
    funding_rate: float = Field(..., description="8h funding 比率，0.0001 表示 0.01%")
    next_funding_time: datetime | None = None
    timestamp: datetime


class OpenInterest(BaseModel):
    """永续合约持仓量 (open interest) 单点观测."""

    exchange: Exchange
    symbol: str
    raw_symbol: str
    open_interest: float = Field(..., description="合约张数（交易所原生单位）")
    open_interest_value: float | None = Field(
        default=None, description="美元计价（OKX 直接给，Binance 需另算）"
    )
    timestamp: datetime


class ETFFlowSnapshot(BaseModel):
    """加密现货 ETF 单日净流入快照."""

    date: date
    ticker: str = Field(..., description="ETF 代码，如 IBIT / FBTC / ETHA（外部源喂入，区分大小写无关）")
    net_flow_usd: float = Field(..., description=">0 净申购，<0 净赎回")
    aum_usd: float | None = Field(default=None, description="当日 AUM（美元），可选")
