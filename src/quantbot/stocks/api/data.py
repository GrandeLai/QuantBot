"""数据模块 API 路由.

端点：
  GET  /data/bars              查询 K 线数据
  POST /data/fetch             触发数据拉取
  GET  /data/symbols           查询已有标的列表
  GET  /data/universe          查询双产品支持的市场/标的宇宙
  GET  /data/range             查询数据时间范围
  GET  /data/onchain/btc       获取 BTC 链上指标
  GET  /data/onchain/btc/{metric}  获取单个 BTC 链上指标
"""

import re
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from loguru import logger

from quantbot.common.data.fetchers.akshare_fetcher import AKShareFetcher
from quantbot.common.data.fetchers.okx_fetcher import OKXFetcher, normalize_symbol as normalize_crypto_symbol
from quantbot.common.data.fetchers.yfinance_fetcher import YFinanceFetcher
from quantbot.common.data import get_storage
from quantbot.common.data.models import (
    DataFetchRequest,
    OHLCVBar,
    OHLCVResponse,
)
from quantbot.common.data.storage import MarketDataStorage
from quantbot.common.data.universe import ProductId, list_supported_instruments

_CRYPTO_QUOTES = ("USDT", "BUSD", "USDC", "BTC", "ETH", "OKB")


def _is_crypto_symbol(symbol: str) -> bool:
    """判断是否为加密货币交易对（如 BTCUSDT / BTC-USDT / BTC/USDT）."""
    s = re.sub(r"[^A-Za-z0-9]", "", symbol).upper()
    return any(s.endswith(q) for q in _CRYPTO_QUOTES) and "." not in symbol


router = APIRouter(prefix="/data", tags=["数据"])

StorageDep = Annotated[MarketDataStorage, Depends(get_storage)]


@router.get("/bars", response_model=OHLCVResponse)
def get_bars(
    storage: StorageDep,
    symbol: str = Query(..., description="标的代码"),
    timeframe: str = Query(default="1d", description="K 线周期"),
    limit: int = Query(default=500, ge=1, le=5000, description="返回条数"),
    start: str | None = Query(default=None, description="开始日期 YYYY-MM-DD"),
    end: str | None = Query(default=None, description="结束日期 YYYY-MM-DD"),
) -> OHLCVResponse:
    """查询 K 线数据."""
    start_dt: datetime | None = None
    end_dt: datetime | None = None

    if start:
        try:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"start 日期格式错误: {start}") from e
    if end:
        try:
            end_dt = datetime.strptime(end, "%Y-%m-%d")
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"end 日期格式错误: {end}") from e

    df = storage.query_bars(
        symbol=symbol,
        timeframe=timeframe,
        start=start_dt,
        end=end_dt,
        limit=limit,
    )

    bars: list[OHLCVBar] = []
    for row in df.iter_rows(named=True):
        bars.append(
            OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=row["timestamp"],
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
                turnover=row.get("turnover"),
            )
        )

    return OHLCVResponse(symbol=symbol, timeframe=timeframe, count=len(bars), bars=bars)


@router.post("/fetch")
def fetch_data(
    req: DataFetchRequest,
    background_tasks: BackgroundTasks,
    storage: StorageDep,
) -> dict[str, str]:
    """触发数据拉取（后台执行）."""
    from datetime import date as _date

    def _do_fetch() -> None:
        try:
            start_date = datetime.strptime(req.start, "%Y-%m-%d").date()
            end_date = (
                datetime.strptime(req.end, "%Y-%m-%d").date()
                if req.end
                else _date.today()
            )

            symbol = req.symbol

            if _is_crypto_symbol(req.symbol):
                fetcher = OKXFetcher()
                symbol = normalize_crypto_symbol(req.symbol)  # 规范化为 BTC-USDT
            else:
                source = req.source
                if source == "auto":
                    # 自动选择：A 股用 akshare，其他用 yfinance
                    source = "akshare" if len(req.symbol) == 6 and req.symbol.isdigit() else "yfinance"

                if source == "yfinance":
                    fetcher = YFinanceFetcher()
                else:
                    fetcher = AKShareFetcher()

            bars = fetcher.fetch_ohlcv(symbol, req.timeframe, start_date, end_date)
            if bars:
                written = storage.upsert_bars(bars)
                logger.info(f"[API/fetch] {symbol} 写入 {written} 条")
        except Exception as e:
            logger.error(f"[API/fetch] 后台任务失败: {e}")

    background_tasks.add_task(_do_fetch)
    return {
        "status": "accepted",
        "message": f"数据拉取任务已提交：{req.symbol} {req.timeframe}",
    }


@router.get("/symbols")
def list_symbols(storage: StorageDep) -> dict[str, list[str]]:
    """列出数据库中已有数据的标的."""
    return {"symbols": storage.list_symbols()}


@router.get("/universe")
def get_market_universe(
    product: ProductId | None = Query(
        default=None,
        description="按产品过滤：quant-assistant 或 stock-assistant",
    ),
    market: Literal["stocks", "crypto"] | None = Query(default=None),
) -> dict[str, Any]:
    """返回两个产品共享的可选市场与标的清单."""
    instruments = list_supported_instruments(product)
    if market is not None:
        instruments = [i for i in instruments if (i.asset_type.value == "crypto") == (market == "crypto")]
    return {
        "count": len(instruments),
        "product": product,
        "instruments": [instrument.model_dump(mode="json") for instrument in instruments],
    }


@router.get("/range")
def get_range(
    storage: StorageDep,
    symbol: str = Query(...),
    timeframe: str = Query(default="1d"),
) -> dict[str, str | None]:
    """查询标的数据的时间范围."""
    earliest, latest = storage.get_date_range(symbol, timeframe)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "earliest": earliest.isoformat() if earliest else None,
        "latest": latest.isoformat() if latest else None,
        "count": str(storage.count_bars(symbol, timeframe)),
    }


@router.get("/onchain/btc")
async def get_btc_onchain() -> dict[str, Any]:
    """获取 BTC 链上指标（来自 blockchain.info）."""
    from quantbot.common.data.onchain import OnChainProvider

    provider = OnChainProvider()
    metrics = await provider.fetch_btc_stats()
    return {
        "asset": "BTC",
        "count": len(metrics),
        "metrics": [m.model_dump() for m in metrics],
    }


@router.get("/prices")
async def get_latest_prices() -> dict[str, Any]:
    """从 Redis 缓存读取所有标的最新价格（O(1) 查询）."""
    from quantbot.common.redis.client import RedisClient
    from quantbot.common.redis.price_cache import PriceCache
    if RedisClient._instance is None:
        raise HTTPException(status_code=503, detail="Redis 未连接")
    prices = await PriceCache().get_all()
    return {"prices": prices, "count": len(prices)}


@router.get("/prices/{symbol}")
async def get_symbol_price(symbol: str) -> dict[str, Any]:
    """读取单个标的最新缓存价格."""
    from quantbot.common.redis.client import RedisClient
    from quantbot.common.redis.price_cache import PriceCache
    if RedisClient._instance is None:
        raise HTTPException(status_code=503, detail="Redis 未连接")
    price = await PriceCache().get_price(symbol.upper())
    if price is None:
        raise HTTPException(status_code=404, detail=f"{symbol} 暂无缓存价格")
    return {"symbol": symbol.upper(), "price": price}


@router.get("/onchain/btc/{metric}")
async def get_btc_metric(metric: str) -> dict[str, Any]:
    """获取单个 BTC 链上指标."""
    from fastapi import HTTPException

    from quantbot.common.data.onchain import OnChainProvider

    provider = OnChainProvider()
    result = await provider.fetch_metric(metric)
    if result is None:
        raise HTTPException(status_code=404, detail=f"指标 '{metric}' 未找到或不支持")
    return result.model_dump()


@router.get("/resolve/{symbol}")
def resolve_symbol_alias(symbol: str):
    from quantbot.common.data.universe import find_supported_instrument
    instrument = find_supported_instrument(symbol)
    if instrument is None:
        raise HTTPException(404,"未找到支持的资产")
    return instrument.model_dump(mode="json")
