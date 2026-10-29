"""SEC 事件流 API（Phase F.2）.

端点：
  GET /api/sec/8k/recent?ticker=AAPL&max_count=3
  GET /api/sec/8k/diff?ticker=AAPL
  GET /api/sec/form4/signals?ticker=AAPL&days=90
  GET /api/sec/summary?ticker=AAPL

数据来源：EDGAR REST API（免费，官方）
限速：≤ 10 req/s，User-Agent 已在 edgar/client.py 中设置
"""
from __future__ import annotations

from datetime import date, timedelta, timezone
from datetime import datetime as dt
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from loguru import logger

from quantbot.stocks.edgar.client import (
    get_form4_transactions,
    get_recent_8k_filings,
)
from quantbot.stocks.edgar.diff_engine import EightKDiff, diff_8k_filings
from quantbot.stocks.edgar.form4_engine import InsiderCluster, detect_clusters

router = APIRouter(prefix="/sec", tags=["sec"])


# ── 序列化工具 ────────────────────────────────────────────────────────────────


def _serialise_diff(d: EightKDiff) -> dict[str, Any]:
    item_diffs = []
    for item in d.item_diffs:
        paras = []
        for p in item.paragraphs:
            paras.append({
                "diff_type": p.diff_type,
                "old_text": (p.old_text or "")[:500] if p.old_text else None,
                "new_text": (p.new_text or "")[:500] if p.new_text else None,
                "similarity": round(p.similarity, 4),
            })
        item_diffs.append({
            "item_number": item.item_number,
            "item_title": item.item_title,
            "has_material_change": item.has_material_change,
            "change_score": round(item.change_score, 4),
            "paragraphs": paras,
        })
    return {
        "ticker": d.ticker,
        "old_accession": d.old_accession,
        "new_accession": d.new_accession,
        "old_filed_date": d.old_filed_date.isoformat(),
        "new_filed_date": d.new_filed_date.isoformat(),
        "has_material_change": d.has_material_change,
        "overall_change_score": round(d.overall_change_score, 4),
        "changed_items": d.changed_item_numbers,
        "item_diffs": item_diffs,
    }


def _serialise_cluster(c: InsiderCluster) -> dict[str, Any]:
    txns = []
    for t in c.transactions:
        txns.append({
            "insider_name": t.insider_name,
            "insider_title": t.insider_title,
            "transaction_date": t.transaction_date.isoformat(),
            "transaction_type": t.transaction_type,
            "shares": t.shares,
            "price_per_share": t.price_per_share,
            "total_value": t.total_value,
            "is_10b5_1_plan": t.is_10b5_1_plan,
        })
    return {
        "window_start": c.window_start.isoformat(),
        "window_end": c.window_end.isoformat(),
        "insider_count": c.insider_count,
        "total_value": c.total_value,
        "avg_price": round(c.avg_price, 4),
        "signal_strength": c.signal_strength,
        "key_roles": c.key_roles,
        "transactions": txns,
    }


# ── 端点 ──────────────────────────────────────────────────────────────────────


@router.get("/8k/recent")
async def get_8k_recent(
    ticker: str = Query(..., description="股票代码，如 AAPL"),
    max_count: int = Query(default=3, ge=1, le=10),
) -> dict[str, Any]:
    """获取最近 N 份 8-K 及其 item 解析结果."""
    try:
        filings = await get_recent_8k_filings(ticker, max_count=max_count)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"EDGAR 8K fetch failed for {ticker}: {exc}")
        raise HTTPException(status_code=503, detail="EDGAR request failed") from exc

    results = []
    for f in filings:
        results.append({
            "ticker": f.ticker,
            "accession_number": f.accession_number,
            "filed_date": f.filed_date.isoformat(),
            "period_of_report": f.period_of_report.isoformat() if f.period_of_report else None,
            "raw_html_url": f.raw_html_url,
            "items": [
                {
                    "item_number": item.item_number,
                    "item_title": item.item_title,
                    "text_snippet": item.text[:300],
                }
                for item in f.items
            ],
        })

    return {
        "ticker": ticker.upper(),
        "count": len(results),
        "filings": results,
        "generated_at": dt.now(tz=timezone.utc).isoformat(),
    }


@router.get("/8k/diff")
async def get_8k_diff(
    ticker: str = Query(..., description="股票代码，如 AAPL"),
) -> dict[str, Any]:
    """获取最新两份 8-K 的段落级差分结果."""
    try:
        filings = await get_recent_8k_filings(ticker, max_count=2)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"EDGAR 8K diff fetch failed for {ticker}: {exc}")
        raise HTTPException(status_code=503, detail="EDGAR request failed") from exc

    if len(filings) < 2:
        return {
            "ticker": ticker.upper(),
            "has_diff": False,
            "message": "Fewer than 2 8-K filings found",
            "generated_at": dt.now(tz=timezone.utc).isoformat(),
        }

    # filings 按 filed_date 倒序，[0] 最新，[1] 次新
    new_filing, old_filing = filings[0], filings[1]
    diff = diff_8k_filings(old_filing, new_filing)

    return {
        "has_diff": True,
        "generated_at": dt.now(tz=timezone.utc).isoformat(),
        **_serialise_diff(diff),
    }


@router.get("/form4/signals")
async def get_form4_signals(
    ticker: str = Query(..., description="股票代码，如 AAPL"),
    days: int = Query(default=90, ge=7, le=365),
) -> dict[str, Any]:
    """获取过去 N 天的 Form 4 集群买入信号."""
    since = date.today() - timedelta(days=days)
    try:
        txns = await get_form4_transactions(ticker, since_date=since)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(f"EDGAR Form4 fetch failed for {ticker}: {exc}")
        raise HTTPException(status_code=503, detail="EDGAR request failed") from exc

    clusters = detect_clusters(txns, window_days=days)

    return {
        "ticker": ticker.upper(),
        "since_date": since.isoformat(),
        "transaction_count": len(txns),
        "cluster_count": len(clusters),
        "clusters": [_serialise_cluster(c) for c in clusters],
        "generated_at": dt.now(tz=timezone.utc).isoformat(),
    }


@router.get("/summary")
async def get_sec_summary(
    ticker: str = Query(..., description="股票代码，如 AAPL"),
) -> dict[str, Any]:
    """三合一摘要：最新 8-K + diff + Form 4 集群信号（供前端一次请求拿全）."""
    import asyncio

    ticker_up = ticker.upper()

    # 并行拉取三类数据
    async def safe_8k() -> list[Any]:
        try:
            return await get_recent_8k_filings(ticker_up, max_count=2)
        except Exception as exc:
            logger.warning(f"8K fetch for {ticker_up}: {exc}")
            return []

    async def safe_form4() -> list[Any]:
        since = date.today() - timedelta(days=90)
        try:
            return await get_form4_transactions(ticker_up, since_date=since)
        except Exception as exc:
            logger.warning(f"Form4 fetch for {ticker_up}: {exc}")
            return []

    filings, txns = await asyncio.gather(safe_8k(), safe_form4())

    # 8-K 最新快照
    latest_8k: dict[str, Any] = {}
    if filings:
        f = filings[0]
        latest_8k = {
            "filed_date": f.filed_date.isoformat(),
            "accession_number": f.accession_number,
            "items": [
                {
                    "item_number": item.item_number,
                    "item_title": item.item_title,
                    "text_snippet": item.text[:300],
                }
                for item in f.items
            ],
        }

    # 8-K diff（需要 ≥2 份）
    diff_summary: dict[str, Any] = {"has_diff": False}
    if len(filings) >= 2:
        try:
            diff = diff_8k_filings(filings[1], filings[0])
            diff_summary = {
                "has_diff": True,
                "has_material_change": diff.has_material_change,
                "overall_change_score": round(diff.overall_change_score, 4),
                "changed_items": diff.changed_item_numbers,
            }
        except Exception as exc:
            logger.debug(f"diff failed for {ticker_up}: {exc}")

    # Form 4 集群
    clusters = detect_clusters(txns)

    return {
        "ticker": ticker_up,
        "generated_at": dt.now(tz=timezone.utc).isoformat(),
        "latest_8k": latest_8k,
        "8k_diff": diff_summary,
        "insider_clusters": [_serialise_cluster(c) for c in clusters],
    }
