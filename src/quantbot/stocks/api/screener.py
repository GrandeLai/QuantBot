"""Screener API router — stock screening, scoring, LLM analysis, market review."""
from __future__ import annotations

import asyncio
import json

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from quantbot.stocks.screener import (
    AnalysisPipeline,
    ChipAnalyzer,
    FundamentalAnalyzer,
    MacroAnalyzer,
    MarketReviewer,
    PeerComparator,
    ScoringEngine,
    StrategyEvaluator,
    StrategyRegistry,
)

router = APIRouter(prefix="/screener", tags=["screener"])

_registry = StrategyRegistry()
_scoring = ScoringEngine()
_chip = ChipAnalyzer()
_evaluator = StrategyEvaluator()
_market = MarketReviewer()
_macro = MacroAnalyzer()


class ScreenRequest(BaseModel):
    strategy_id: str = "bull_trend"
    symbols: list[str] = Field(min_length=1, max_length=50)
    lookback_days: int = Field(default=120, ge=60, le=250)


class ScreenResult(BaseModel):
    symbol: str
    score: float
    matched: bool
    trend: float
    bias: float
    volume: float
    support: float
    macd: float
    rsi: float


@router.get("/strategies")
async def list_strategies() -> dict:
    strategies = [
        {
            "id": s.id,
            "name_zh": s.name_zh,
            "description": s.description,
            "min_score": s.min_score,
        }
        for s in _registry.all()
    ]
    return {"strategies": strategies, "count": len(strategies)}


@router.post("/screen")
async def run_screen(req: ScreenRequest) -> dict:
    try:
        strategy = _registry.get(req.strategy_id)
    except KeyError:
        raise HTTPException(404, f"Strategy {req.strategy_id!r} not found") from None

    async def _score_symbol(symbol: str) -> ScreenResult:
        try:
            import akshare as ak

            df_raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    adjust="qfq",
                    start_date="20220101",
                ),
            )
            df = pl.from_pandas(
                df_raw[["开盘", "收盘", "最高", "最低", "成交量"]].rename(
                    columns={
                        "开盘": "open",
                        "收盘": "close",
                        "最高": "high",
                        "最低": "low",
                        "成交量": "volume",
                    }
                )
            ).tail(req.lookback_days)
            breakdown = _scoring.score(df)
            matched = await asyncio.get_event_loop().run_in_executor(
                None, _evaluator.evaluate, df, strategy
            )
            return ScreenResult(
                symbol=symbol,
                score=breakdown.total,
                matched=matched,
                trend=breakdown.trend,
                bias=breakdown.bias,
                volume=breakdown.volume,
                support=breakdown.support,
                macd=breakdown.macd,
                rsi=breakdown.rsi,
            )
        except Exception:
            return ScreenResult(
                symbol=symbol, score=0.0, matched=False,
                trend=0.0, bias=0.0, volume=0.0,
                support=0.0, macd=0.0, rsi=0.0,
            )

    results = await asyncio.gather(*[_score_symbol(s) for s in req.symbols])
    results_sorted = sorted(results, key=lambda r: r.score, reverse=True)
    matched = [r for r in results_sorted if r.matched]

    return {
        "strategy_id": req.strategy_id,
        "strategy_name": strategy.name_zh,
        "total_screened": len(results),
        "matched_count": len(matched),
        "results": [r.model_dump() for r in results_sorted],
    }


@router.get("/score/{symbol}")
async def get_score(
    symbol: str,
    lookback_days: int = Query(default=120, ge=60, le=250),
) -> dict:
    try:
        import akshare as ak

        df_raw = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: ak.stock_zh_a_hist(
                symbol=symbol,
                period="daily",
                adjust="qfq",
                start_date="20220101",
            ),
        )
        df = pl.from_pandas(
            df_raw[["开盘", "收盘", "最高", "最低", "成交量"]].rename(
                columns={
                    "开盘": "open",
                    "收盘": "close",
                    "最高": "high",
                    "最低": "low",
                    "成交量": "volume",
                }
            )
        ).tail(lookback_days)
        breakdown = _scoring.score(df)
        chip = _chip.analyze(df)
        return {
            "symbol": symbol,
            "score": breakdown.__dict__,
            "chip": chip.__dict__,
        }
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/analyze/{symbol}")
async def analyze_symbol(
    symbol: str,
    lookback_days: int = Query(default=120, ge=60, le=250),
) -> StreamingResponse:
    """Stream 4-phase LLM analysis as Server-Sent Events."""

    async def _event_stream():
        # Build context
        score_summary = "无评分数据"
        try:
            import akshare as ak

            df_raw = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ak.stock_zh_a_hist(
                    symbol=symbol,
                    period="daily",
                    adjust="qfq",
                    start_date="20220101",
                ),
            )
            df = pl.from_pandas(
                df_raw[["开盘", "收盘", "最高", "最低", "成交量"]].rename(
                    columns={
                        "开盘": "open",
                        "收盘": "close",
                        "最高": "high",
                        "最低": "low",
                        "成交量": "volume",
                    }
                )
            ).tail(lookback_days)
            breakdown = _scoring.score(df)
            score_summary = (
                f"综合得分 {breakdown.total}/100 "
                f"(趋势{breakdown.trend:.0f} 量能{breakdown.volume:.0f} MACD{breakdown.macd:.0f})"
            )
        except Exception:
            pass

        sentiment_summary = "情绪中性"
        try:
            from quantbot.stocks.sentiment.provider import NewsSentimentProvider

            sent = await NewsSentimentProvider().get_sentiment(symbol)
            sentiment_summary = f"新闻情绪得分 {sent:.2f}"
        except Exception:
            pass

        context = {
            "score_summary": score_summary,
            "sentiment_summary": sentiment_summary,
            "fundamental_summary": "无基本面数据",
        }

        pipeline = AnalysisPipeline()
        async for update in pipeline.run(symbol, context):
            payload: dict = {
                "phase": update.phase,
                "title": update.title,
                "token": update.token,
                "start": update.start,
                "done": update.done,
            }
            if update.decision:
                d = update.decision
                payload["decision"] = {
                    "recommendation": d.recommendation,
                    "conviction": d.conviction,
                    "buy_price": d.buy_price,
                    "stop_loss": d.stop_loss,
                    "summary": d.summary,
                    "checklist": d.checklist,
                }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_event_stream(), media_type="text/event-stream")


@router.get("/market")
async def get_market() -> dict:
    review = await _market.get_review()
    return {
        "date": review.date,
        "advances": review.advances,
        "declines": review.declines,
        "flat": review.flat,
        "advance_ratio": review.advance_ratio,
        "stance": review.stance,
        "indices": [i.__dict__ for i in review.indices],
        "top_sectors": [s.__dict__ for s in review.top_sectors],
        "bottom_sectors": [s.__dict__ for s in review.bottom_sectors],
    }


@router.get("/fundamentals/{symbol}")
async def get_fundamentals(symbol: str) -> dict:
    data = await FundamentalAnalyzer().get(symbol)
    return data.__dict__


@router.get("/macro")
async def get_macro() -> dict:
    ctx = await _macro.get()
    return ctx.__dict__


@router.get("/peers/{symbol}")
async def get_peers(
    symbol: str,
    period_days: int = Query(default=90, ge=30, le=365),
) -> dict:
    comp = await PeerComparator().compare(symbol, period_days)
    return {
        "symbol": comp.symbol,
        "sector": comp.sector,
        "target": comp.target.__dict__,
        "peers": [p.__dict__ for p in comp.peers],
        "sector_avg_sharpe": comp.sector_avg_sharpe,
        "sector_avg_sortino": comp.sector_avg_sortino,
        "sharpe_percentile": comp.sharpe_percentile,
        "sortino_percentile": comp.sortino_percentile,
    }
