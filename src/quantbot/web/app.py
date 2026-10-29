"""Unified allocation and research HTTP service, bound to loopback by the CLI."""

from __future__ import annotations

from copy import deepcopy
from contextlib import asynccontextmanager
import csv
from datetime import datetime
import importlib
import io
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from quantbot import __version__
from quantbot.ai.config import load_config, validate_config
from quantbot.ai.backtest import BacktestFailure, BacktestValidationFailure
from quantbot.ai.gateway import decision_provider
from quantbot.ai.market import DailyMarketData, EASTERN
from quantbot.ai.models import DemoProvider
from quantbot.ai.risk import build_plan
from quantbot.ai.simulation import simulated_account
from quantbot.ai.transport import ProviderError
from quantbot.crypto.ai import candles_from_rows, run_spot
from quantbot.crypto.runtime.application import TradingAssistantApp
from quantbot.crypto.runtime.config.schema import Settings
from quantbot.evidence import hash_payload
from quantbot.trading.ledger import PortfolioLedger

STOCK_ROUTERS = (
    "advisor",
    "alerts",
    "analyst_consensus",
    "beta_correlation",
    "data",
    "dcf",
    "dividend_analysis",
    "earnings_calendar",
    "earnings_move",
    "earnings_quality",
    "eps_revision",
    "execution",
    "fundamental",
    "gex",
    "index_rebalance",
    "insider_trading",
    "insights",
    "iv_rank",
    "macro_dashboard",
    "max_pain",
    "momentum",
    "options",
    "pead",
    "platform",
    "portfolio",
    "put_call_ratio",
    "quant_signals",
    "relative_strength",
    "risk",
    "screener",
    "sec",
    "seasonality",
    "sector_momentum",
    "security",
    "sentiment",
    "short_interest",
    "smart_money",
    "social_sentiment",
    "technical_score",
    "tlh",
    "trading",
    "unusual_options",
    "ws",
)
CRYPTO_ROUTERS = ("advisor", "crypto", "crypto_derivs", "crypto_research", "crypto_whale", "token_unlock")


class AdmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str
    job_id: str
    evidence_path: str | None = None
    strategy_family: Literal["normal", "daytrade"] = "daytrade"


class AllocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: Literal["stocks", "crypto"] = "stocks"
    mode: Literal["decide", "simulate", "paper"] = "decide"
    provider: Literal["ai", "price_action", "demo"] = "ai"
    csv_data: str = Field(default="", max_length=4_000_000)
    vintage_data: dict | None = None
    sec_context: dict | None = None
    symbols: list[str] | None = Field(default=None, min_length=1, max_length=20)
    initial_cash: float = Field(default=10000, gt=0, le=1e9)
    last_bars: int = Field(default=30, ge=3, le=500, strict=True)
    allow_model_calls: bool = False
    confirm_paper: bool = False
    admission: AdmissionRequest | None = None


class StrategyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str
    symbol: str = "BTC/USDT"
    cycles: int = Field(default=1, ge=1, le=3)


def safe_crypto_runtime(directory):
    """HTTP strategy runs always use injected mock settings, ignoring env overrides."""
    settings = Settings()
    settings.exchanges = {k: v for k, v in settings.exchanges.items() if v.adapter == "mock"}
    for model in (
        settings.strategy_runtime,
        settings.hedged_maker,
        settings.directional,
        settings.agent_trading,
        settings.reporting,
        settings.dex_lp,
    ):
        for name in type(model).model_fields:
            value = getattr(model, name)
            if name.endswith("_path") and isinstance(value, str):
                setattr(model, name, str(Path(directory) / Path(value).name))
    return TradingAssistantApp(settings=settings)


def create_app(*, config=None, runtime_dir=None, frontend_dir=None, worker=False):
    config = (
        deepcopy(config)
        if config is not None
        else load_config("config/quantbot.toml" if Path("config/quantbot.toml").is_file() else None)
    )
    directory = Path(runtime_dir or config["execution"]["runtime_dir"]) / "workbench"
    automation_root = Path(runtime_dir or config["execution"]["runtime_dir"]) / "automation"

    @asynccontextmanager
    async def lifespan(app):
        import asyncio
        import threading
        from quantbot.ai.runtime import RuntimeStore, run_worker

        stop = threading.Event()
        thread = None
        if worker:
            thread = threading.Thread(target=run_worker, args=(RuntimeStore(automation_root), stop), daemon=True)
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                await asyncio.to_thread(thread.join, 30)

    app = FastAPI(title="QuantBot", version=__version__, description="AI 决策、历史回测与研究工作台", lifespan=lifespan)
    from .automation import router as automation_router
    app.include_router(automation_router(config, automation_root))
    from .options_overlay import router as options_overlay_router
    app.include_router(options_overlay_router(directory / "options"))
    from .sec_study import router as sec_study_router
    app.include_router(sec_study_router(directory / "sec-events"))
    app.state.quantbot_config = config
    from quantbot.models.agent import configure_default_agent

    configure_default_agent(config)
    app.state.ai_provider = None

    def shared_provider():
        if app.state.ai_provider is None:
            chosen = deepcopy(config)
            chosen["strategy"]["mode"] = "ai"
            app.state.ai_provider = decision_provider(chosen)
        return app.state.ai_provider

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def legacy_write_guard(request: Request, call_next):
        path = request.url.path.removeprefix("/api")
        blocked = (
            (path.startswith("/trading/orders") and path != "/trading/orders/estimate")
            or path in {"/crypto/orders", "/crypto/futures/orders"}
            or (path.startswith("/crypto/") and "/orders/" in path)
            or path.startswith("/security/keys/")
        )
        if blocked and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            return JSONResponse(
                status_code=403,
                content={"detail": "旧直接写入入口已停用，请使用 QuantBot 统一决策和模拟交易入口"},
            )
        return await call_next(request)

    for namespace, names in (("stocks", STOCK_ROUTERS), ("crypto", CRYPTO_ROUTERS)):
        for name in names:
            module = importlib.import_module(f"quantbot.{namespace}.api.{name}")
            router = module.router
            # Crypto advisory is owned by crypto. Stock overview remains stock-owned.
            if name == "advisor":
                from fastapi import APIRouter

                selected = APIRouter()
                selected.routes = [
                    r
                    for r in router.routes
                    if (r.path.startswith("/advisor/crypto/") == (namespace == "crypto"))
                ]
                router = selected
            app.include_router(router)
            app.include_router(router, prefix="/api", include_in_schema=False)

    @app.get("/healthz")
    def health():
        return {"status": "ok", "app": "QuantBot", "version": __version__}

    @app.get("/api/quantbot/status")
    @app.get("/quantbot/status", include_in_schema=False)
    def status():
        return {
            "app": "QuantBot",
            "version": __version__,
            "model_backend": config["llm"].get("backend", "openai_compatible"),
            "model": config["llm"]["model"],
            "fallback_models": config["llm"].get("fallback_models", []),
            "model_configured": bool(config["llm"]["model"].strip()),
            "markets": ["stocks", "crypto_spot"],
            "live_enabled": False,
            "risk": config["risk"],
            "legacy_writes_enabled": False,
        }

    @app.post("/api/quantbot/allocate")
    @app.post("/quantbot/allocate", include_in_schema=False)
    def allocate(req: AllocationRequest):
        try:
            if req.sec_context is not None and (req.market != "stocks" or req.mode != "simulate"):
                raise ValueError("SEC 公告输入当前仅支持美股历史回测")
            if req.vintage_data is not None and req.mode != "simulate":
                raise ValueError("历史版本输入当前仅支持历史回测")
            selected = deepcopy(config)
            # Broker Paper shares the CLI account journal and configured settings.
            # Research/demo capital is separate from broker-reported account facts.
            if req.market == "crypto" or req.mode != "paper":
                selected["execution"]["initial_cash"] = req.initial_cash
            if runtime_dir is not None:
                selected["execution"]["runtime_dir"] = str(runtime_dir)
            if req.mode == "simulate":
                from quantbot.ai.backtest import backtest

                return backtest(
                    selected, output=directory / "backtests" / uuid4().hex,
                    market=req.market, provider=req.provider, csv_data=req.csv_data,
                    symbols=req.symbols, last_bars=req.last_bars,
                    allow_model_calls=req.allow_model_calls,
                    sec_context=req.sec_context, vintage_data=req.vintage_data,
                )
            if req.market == "crypto":
                if req.provider == "price_action":
                    raise ValueError("加密现货支持 AI 或离线演示规则")
                if req.mode == "paper" and not req.confirm_paper:
                    raise ValueError("必须明确确认记录模拟成交")
                rows = (
                    list(csv.DictReader(io.StringIO(req.csv_data.removeprefix("\ufeff"))))
                    if req.csv_data
                    else None
                )
                if rows is None and req.provider != "demo":
                    raise ValueError("AI 决策需要上传真实离线 CSV；合成行情仅用于离线演示")
                symbols = req.symbols or (sorted({r["symbol"] for r in rows}) if rows else None)
                ledger = (
                    directory
                    / "crypto-paper"
                    / (
                        hash_payload(
                            {"symbols": symbols, "cash": req.initial_cash, "demo": req.provider == "demo"}
                        )[:24]
                        + ".sqlite"
                    )
                )
                return run_spot(
                    selected,
                    symbols=symbols,
                    candles=candles_from_rows(rows) if rows else None,
                    demo=req.provider == "demo",
                    mode=req.mode,
                    initial_cash=req.initial_cash,
                    ledger_path=ledger,
                    provider=shared_provider() if req.provider == "ai" else DemoProvider(),
                )
            if req.symbols:
                selected["market"]["symbols"] = req.symbols
                selected["market"]["benchmark"] = req.symbols[0]
            selected["strategy"]["mode"] = "price_action" if req.provider == "price_action" else "ai"
            if not req.csv_data and req.provider != "demo":
                raise ValueError("请上传完整日线 CSV；或明确选择离线演示")
            if req.csv_data:
                rows = list(csv.DictReader(io.StringIO(req.csv_data.removeprefix("\ufeff"))))
                if req.symbols is None:
                    selected["market"]["symbols"] = sorted({row["symbol"] for row in rows})
                    if selected["market"]["symbols"]:
                        selected["market"]["benchmark"] = selected["market"]["symbols"][0]
            else:
                from quantbot.cli import _demo_rows

                rows = _demo_rows(selected)
            validate_config(selected)
            market = DailyMarketData(rows, selected)
            provider = (
                DemoProvider()
                if req.provider == "demo"
                else shared_provider()
                if req.provider == "ai"
                else decision_provider(selected)
            )
            if req.mode == "paper":
                if not req.confirm_paper or req.provider == "demo":
                    raise ValueError("Alpaca 模拟盘需要明确确认并使用 AI 或价格行为策略")
                from quantbot.ai.paper import run_paper
                from quantbot.ai.admission import AdmissionReference

                admission = AdmissionReference(**req.admission.model_dump()) if req.admission else None
                return run_paper(market, selected, provider, confirm=True, admission=admission)
            context = market.context(market.common_dates[-1])
            if context.as_of > datetime.now(EASTERN):
                raise ValueError("不能对未收盘日线生成决策")
            account = simulated_account(PortfolioLedger(req.initial_cash, context.as_of, allow_short=False))
            decision = provider.decide(context, account, selected)
            return {
                "market": "stocks",
                "mode": "decide",
                "status": "ok",
                "as_of": context.as_of.isoformat(),
                "data_source": "offline_csv" if req.csv_data else "mock_fixture",
                "plan": build_plan(context, account, decision, selected).payload(),
                "account": account.payload(context),
                "model_called": req.provider == "ai",
            }
        except (BacktestFailure, BacktestValidationFailure) as exc:
            raise HTTPException(400 if isinstance(exc, BacktestValidationFailure) else 500, {"message": "回测中断：" + exc.reason,
                "download_url": f"/api/quantbot/backtests/{exc.report_path.parent.name}/failure"}) from None
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        except ProviderError:
            raise HTTPException(503, "模型接口不可用；本次没有新订单") from None
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from None

    @app.get("/api/quantbot/backtests/{identifier}/failure")
    def failed_backtest(identifier: str):
        path = directory / "backtests" / identifier / "failure.json"
        if re.fullmatch(r"[0-9a-f]{32}", identifier) is None or not path.is_file():
            raise HTTPException(404, "失败报告不存在")
        return FileResponse(path, media_type="application/json", filename=f"quantbot-failed-{identifier}.json")

    @app.get("/api/quantbot/crypto/strategies")
    @app.get("/quantbot/crypto/strategies", include_in_schema=False)
    def strategies():
        return safe_crypto_runtime(directory / "strategies").strategy_catalog()

    @app.post("/api/quantbot/crypto/strategy-run")
    @app.post("/quantbot/crypto/strategy-run", include_in_schema=False)
    def strategy_run(req: StrategyRequest):
        try:
            result = safe_crypto_runtime(directory / "strategies").strategy_run(
                req.strategy, req.cycles, 0, "paper", req.symbol
            )
            return {
                "execution_mode": "paper",
                "data_source": "mock_fixture",
                "pnl_basis": "estimated_opportunity_or_strategy_specific_paper",
                **result,
            }
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/backtest/run", include_in_schema=False)
    @app.post("/api/walk-forward", include_in_schema=False)
    @app.post("/api/optimize", include_in_schema=False)
    @app.post("/api/indicators", include_in_schema=False)
    @app.post("/api/ml/predict", include_in_schema=False)
    async def core_proxy(request: Request):
        operation = request.url.path.removeprefix("/api/")
        import httpx

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "http://127.0.0.1:8002/api/" + operation,
                    content=await request.body(),
                    headers={"Content-Type": "application/json"},
                )
            return Response(response.content, status_code=response.status_code, media_type="application/json")
        except httpx.HTTPError:
            raise HTTPException(503, "Rust 研究内核未启动；先运行 quantbot core") from None

    dist = Path(frontend_dir) if frontend_dir else Path(__file__).resolve().parents[3] / "frontend" / "dist"
    if not dist.is_dir():
        dist = Path(__file__).parent / "static"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="workbench")
    return app


app = create_app()
