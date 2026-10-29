"""账户组合 API.

端点:
  POST /portfolio/strategies           已移除：模拟策略槽位不再提供
  GET  /portfolio/strategies           已移除：返回空列表
  DELETE /portfolio/strategies/{name}  已移除：模拟策略槽位不再提供
  GET  /portfolio/summary              broker 账户组合概览（同时记录历史快照）
  GET  /portfolio/equity               权益曲线（历史净值序列）
  GET  /portfolio/available-strategies 可运行策略目录（模板 + 用户策略）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from quantbot.common.config import get_settings
from quantbot.common.strategy_persistence import StrategyStorage
from quantbot.stocks.broker.provider import get_trading_provider
from quantbot.stocks.broker.types import TradingProviderError
from quantbot.stocks.portfolio.snapshots import PortfolioSnapshot, SnapshotStorage

router = APIRouter(prefix="/portfolio", tags=["账户组合"])

_storage = SnapshotStorage()


class AddStrategyRequest(BaseModel):
    name: str
    strategy_class: str
    symbol: str
    timeframe: str
    allocation: float
    params: dict[str, Any] = {}


@router.post("/strategies")
def add_strategy(req: AddStrategyRequest) -> dict[str, str]:
    """拒绝旧模拟策略槽位入口."""
    raise HTTPException(
        status_code=410,
        detail=(
            "模拟策略槽位已移除；策略上线前请先使用 /api/backtest/run 做实盘前验证，"
            "通过后再进入 /api/trading 执行。"
        ),
    )


@router.get("/strategies")
def list_strategies() -> dict[str, Any]:
    """列出策略槽位.

    本地模拟盘策略槽位已移除，保留空响应用于前端兼容。
    """
    return {"count": 0, "strategies": []}


@router.delete("/strategies/{name}")
def remove_strategy(name: str) -> dict[str, str]:
    """拒绝旧模拟策略槽位删除入口."""
    raise HTTPException(
        status_code=410,
        detail=f"模拟策略槽位已移除，无需删除策略 '{name}'。",
    )


@router.get("/summary")
def portfolio_summary() -> dict[str, Any]:
    """获取 broker 账户组合总览，同时记录历史快照用于 equity 曲线和 daily_pnl."""
    try:
        account = get_trading_provider().get_account_overview()
    except TradingProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    total_cash = round(account.available_cash, 2)
    total_allocated = round(account.positions_market_value, 2)
    total_portfolio_value = round(account.total_assets, 2)
    summary: dict[str, Any] = {
        "total_cash": total_cash,
        "total_allocated": total_allocated,
        "total_portfolio_value": total_portfolio_value,
        "strategies": [],
    }

    # 记录快照
    now_iso = datetime.now(timezone.utc).isoformat()
    _storage.save_snapshot(
        PortfolioSnapshot(
            timestamp=now_iso,
            total_value=total_portfolio_value,
            total_cash=total_cash,
            total_pnl=round(account.total_pnl, 2),
        )
    )

    # 计算 daily_pnl
    snap_24h = _storage.snapshot_24h_ago()
    if snap_24h is not None:
        daily_pnl = total_portfolio_value - snap_24h.total_value
        daily_pnl_pct = (
            daily_pnl / snap_24h.total_value * 100 if snap_24h.total_value > 0 else 0.0
        )
    else:
        daily_pnl = 0.0
        daily_pnl_pct = 0.0

    return {
        **summary,
        "provider": account.provider.value,
        "mode": account.mode.value,
        "positions_market_value": round(account.positions_market_value, 2),
        "total_pnl": round(account.total_pnl, 2),
        "total_pnl_pct": round(account.total_pnl_pct, 6),
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(daily_pnl_pct, 4),
    }


@router.get("/equity")
def portfolio_equity(
    period: Literal["1D", "1W", "1M", "ALL"] = Query(default="1M"),
) -> dict[str, Any]:
    """返回权益曲线历史数据（降采样至最多 200 点）."""
    now = datetime.now(timezone.utc)
    since_map: dict[str, datetime | None] = {
        "1D": now - timedelta(days=1),
        "1W": now - timedelta(weeks=1),
        "1M": now - timedelta(days=30),
        "ALL": None,
    }
    since = since_map[period]
    snaps = _storage.list_snapshots(since=since, limit=5000)

    # 降采样：目标最多 200 个点
    if len(snaps) > 200:
        step = len(snaps) // 200
        snaps = snaps[::step]

    points = [
        {
            "timestamp": s.timestamp,
            "value": s.total_value,
            "date": s.timestamp[:10],  # YYYY-MM-DD
        }
        for s in snaps
    ]
    return {"period": period, "points": points, "count": len(points)}


@router.get("/available-strategies")
def available_strategies() -> dict[str, Any]:
    """返回可运行策略目录（用户策略）.

    模板策略由 Rust quant-assistant 通过独立 HTTP API 提供。
    Stock-assistant 本地仅返回用户自定义策略（由 StrategyStorage 持久化）。
    """
    strategies: list[dict[str, Any]] = []

    # 用户策略可独立列出（StrategyStorage 在 common 中）
    storage = StrategyStorage(get_settings().strategy_dir)
    for meta in storage.list_all():
        strategies.append(
            {
                "id": meta.id,
                "name": meta.name,
                "description": meta.description,
                "default_params": meta.params,
                "version": meta.version,
                "source": "user",
            }
        )

    return {"strategies": strategies, "count": len(strategies)}
