"""QuantBot 统一入口：AI 决策、回放、模拟盘与原有研究工具。"""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
import json
import math
from pathlib import Path
import sys

import pandas as pd

from .ai.config import load_config
from .ai.domain import AccountState
from .ai.journal import AIJournal
from .ai.market import DailyMarketData, EASTERN, FIELDS
from .ai.models import DemoProvider
from .ai.gateway import decision_provider
from .ai.risk import build_plan
from .ai.simulation import simulate
from .ai.transport import ProviderError
from .trading.domain import BrokerAccountSnapshot, MarginStatus
from . import __version__


def build_parser():
    parser = argparse.ArgumentParser(
        prog="quantbot", description="QuantBot：大模型决策 + 确定性风控 + 历史回测"
    )
    parser.add_argument("--version", action="version", version=f"QuantBot {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    intraday = commands.add_parser("intraday-study", help="双市场分钟数据、部分成交与延迟研究；不连接交易账户")
    intraday.add_argument("args", nargs=argparse.REMAINDER)
    factors = commands.add_parser("factor-study", help="加密因子登记、时间序列验证和筛选；不调用模型或交易")
    factors.add_argument("factor_args", nargs=argparse.REMAINDER)
    model_check = commands.add_parser("model-check", help="检查模型配置及本机 Codex 登录，不调用模型")
    model_check.add_argument("--config")
    backtest = commands.add_parser("backtest", help="股票或加密现货：历史回测、规则对照与复盘报告")
    backtest.add_argument("--config")
    backtest.add_argument("--market", choices=["stocks", "crypto"], default="stocks")
    backtest.add_argument("--provider", choices=["ai", "price_action", "demo", "crypto_rules"], default="ai")
    history = backtest.add_mutually_exclusive_group()
    history.add_argument("--data", help="历史 CSV；仅 demo 可省略行情并使用合成数据")
    history.add_argument("--vintage-data", help="含发布时间、原始行情与公司行动的历史版本 JSON")
    backtest.add_argument("--sec-context", help="可选的 SEC 公告输入 JSON，仅支持美股回测")
    backtest.add_argument("--symbols", nargs="+", help="默认取导入行情的标的或演示股票池")
    backtest.add_argument("--initial-cash", type=float)
    backtest.add_argument("--last-bars", type=int, default=30)
    backtest.add_argument("--allow-model-calls", action="store_true")
    backtest.add_argument("--output", help="新的报告目录；默认 runs/backtest-时间")
    options = commands.add_parser("options-backtest", help="导入已有持股，对比期权增强/对冲及指派情景")
    options.add_argument("--data", required=True, help="含历史股价、期权报价和公司行动声明的 JSON")
    options.add_argument("--portfolio", required=True, help="初始股票持仓与现金 JSON")
    options.add_argument("--policy", help="可选的预设研究参数 JSON")
    options.add_argument("--output", help="新的完整证据目录")
    archive = commands.add_parser("sec-archive", help="按季度索引归档指定公司历史 8-K，保留原始文件和修订")
    archive.add_argument("--start", required=True)
    archive.add_argument("--end", required=True)
    archive.add_argument("--cik", nargs="+", required=True)
    archive.add_argument("--user-agent", required=True, help="SEC 要求的名称与联系邮箱")
    archive.add_argument("--directory", default="runs/sec-archive")
    events = commands.add_parser("sec-study", help="回放 SEC 事件可得时点后的固定窗口与基准收益")
    events.add_argument("--events", required=True, help="归档 bundle.json")
    events.add_argument("--prices", required=True, help="含基准的原始历史行情 JSON")
    events.add_argument("--universe", required=True, help="带有效日期的 CIK 与历史标的映射 JSON")
    events.add_argument("--policy", help="窗口、成本及可得时间假设 JSON")
    events.add_argument("--output", help="新的完整事件证据目录")
    for name, help_text in (
        ("decide", "读取行情并生成经过风控的交易计划"),
        ("simulate", "用相同决策链路进行历史回放"),
        ("paper", "执行一次 Alpaca 模拟盘决策"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--config", help="QuantBot TOML 配置；默认 config/quantbot.toml")
        command.add_argument("--data", required=True, help="日线 CSV：date,symbol,open,high,low,close,volume")
        command.add_argument("--strategy", choices=["ai", "price_action"])
        if name == "decide":
            command.add_argument("--as-of", required=True, type=date.fromisoformat)
            command.add_argument("--account", help="可选的离线账户 JSON：cash,equity,positions")
            command.add_argument("--output", default="runs/decision.json")
        elif name == "simulate":
            command.add_argument("--start", type=date.fromisoformat)
            command.add_argument("--end", type=date.fromisoformat)
            command.add_argument("--output", default=None, help="空目录，用于报告和 SQLite 账本")
        else:
            command.add_argument(
                "--confirm-paper", action="store_true", help="明确允许本次向模拟账户提交订单"
            )
            command.add_argument("--admission-root", help="冻结任务与准出证据目录")
            command.add_argument("--admission-job", help="必须与本次配置一致的冻结任务 ID")
            command.add_argument("--admission-evidence", help="已认证的美股正式准出证据 JSONL")
            command.add_argument("--admission-family", choices=["normal", "daytrade"], default="daytrade")
    demo = commands.add_parser("demo", help="离线演示，不调用模型、不连接券商")
    demo.add_argument("--config")
    demo.add_argument("--output", default=None)
    download = commands.add_parser("download", help="下载研究用日线并转换为统一 CSV（需要 data 扩展）")
    download.add_argument("--config")
    download.add_argument("--start", type=date.fromisoformat, required=True)
    download.add_argument("--end", type=date.fromisoformat, required=True, help="截止日期，不含当天")
    download.add_argument("--output", default="data/daily.csv")
    research = commands.add_parser("research", help="StockBot 原有研究、验证及券商诊断工具")
    research.add_argument("args", nargs=argparse.REMAINDER)
    pa = commands.add_parser("price-action", help="PABot 原有回测、复盘和实验工具")
    pa.add_argument("args", nargs=argparse.REMAINDER)
    serve = commands.add_parser("serve", help="启动统一网页工作台")
    serve.add_argument("--config")
    serve.add_argument("--port", type=int, default=8001)
    serve.add_argument("--frontend", help="可选的已构建网页目录")
    serve.add_argument("--worker", action="store_true", help="同时运行已启用的本地模拟任务")
    runtime = commands.add_parser("runtime", help="候选回测、滚动评估与可选持续模拟")
    runtime.add_argument("args", nargs=argparse.REMAINDER)
    commands.add_parser("core", help="启动唯一的 Rust 研究内核")
    crypto = commands.add_parser("crypto", help="加密 AI 现货和 CoinBot 原有策略、回测、验证")
    crypto.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def _output_directory(kind):
    return Path("runs") / (kind + "-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f"))


def _dump(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
        handle.write("\n")


def _provider(config, *, environ=None):
    return decision_provider(config, environ=environ)


def _demo_rows(config):
    count = max(160, config["strategy"]["momentum_window"] + 35)
    days = pd.bdate_range(end="2025-07-25", periods=count)
    records = []
    for number, symbol in enumerate(config["market"]["symbols"]):
        last = 80 + number * 20
        for index, day in enumerate(days):
            close = last + (0.6 + number * 0.1 if index % 10 < 7 else -0.3)
            records.append(
                {
                    "date": day.date().isoformat(),
                    "symbol": symbol,
                    "open": round(last, 4),
                    "high": round(max(last, close) + 0.5, 4),
                    "low": round(min(last, close) - 0.5, 4),
                    "close": round(close, 4),
                    "volume": 1000000,
                }
            )
            last = close
    return records


def _download(args, config):
    if args.start >= args.end or args.end > datetime.now(EASTERN).date():
        raise ValueError("下载日期必须递增，且不得包含尚未完成的日线")
    output = Path(args.output)
    if output.exists():
        raise ValueError("行情输出已存在，使用新路径避免覆盖研究输入")
    try:
        import yfinance as yf
    except ImportError:
        raise ValueError("缺少行情依赖：安装 QuantBot 的 data 扩展") from None
    output.parent.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(output.parent / ".yfinance-cache"))
    rows = []
    for symbol in config["market"]["symbols"]:
        frame = yf.Ticker(symbol).history(
            start=str(args.start), end=str(args.end), auto_adjust=True, actions=False
        )
        if frame.empty:
            raise ValueError(f"未获取到 {symbol} 的行情")
        for timestamp, bar in frame.iterrows():
            values = {key: float(bar[key.capitalize()]) for key in FIELDS[2:]}
            if any(not math.isfinite(value) for value in values.values()):
                raise ValueError(f"{symbol} 的行情包含无效值")
            rows.append({"date": timestamp.date().isoformat(), "symbol": symbol, **values})
    DailyMarketData(rows, config)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["date"], r["symbol"])))
    return {
        "status": "downloaded",
        "rows": len(rows),
        "path": str(output.resolve()),
        "price_basis": "yfinance_auto_adjusted",
        "end_exclusive": str(args.end),
    }


def main(argv=None, *, stdout=None, environ=None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    output_stream = stdout or sys.stdout
    if args_list and args_list[0] == "intraday-study":
        from .intraday.research import main as intraday_main
        return intraday_main(args_list[1:], stdout=output_stream)
    if args_list and args_list[0] == "runtime":
        from .ai.runtime_cli import main as runtime_main
        return runtime_main(args_list[1:], stdout=output_stream, environ=environ)
    if args_list and args_list[0] == "crypto":
        if len(args_list)>1 and args_list[1] == "ai":
            from .crypto.ai_cli import main as crypto_main
            return crypto_main(args_list[2:],stdout=output_stream,environ=environ)
        from .crypto.runtime.cli.main import main as crypto_main
        return crypto_main(args_list[1:]) or 0
    # Delegate before parsing so legacy subcommands retain their own flags/help contracts.
    if args_list and args_list[0] == "factor-study":
        from .factors.cli import main as factor_main

        return factor_main(args_list[1:], stdout=output_stream)
    if args_list and args_list[0] == "research":
        from .research_cli import main as research_main

        return research_main(args_list[1:], stdout=output_stream, environ=environ)
    if args_list and args_list[0] == "price-action":
        allowed = {
            "download",
            "snapshot",
            "backtest",
            "walkforward",
            "review",
            "policy-check",
            "experiment",
            "candidate-bundle",
            "candidate-verify",
            "hypothesis-init",
        }
        if len(args_list) < 2 or args_list[1] not in allowed:
            output_stream.write("价格行为兼容入口用于研究；交易请使用 QuantBot decide/simulate/paper。\n")
            return 2
        from .price_action.cli import main as price_main

        original = sys.argv
        try:
            sys.argv = ["quantbot price-action", *args_list[1:]]
            return price_main() or 0
        finally:
            sys.argv = original
    args = build_parser().parse_args(args_list)
    if args.command == "core":
        import subprocess
        root = Path(__file__).resolve().parents[2]
        binary = root/"target"/"release"/"quantbot-quant-server"
        if not binary.is_file():
            output_stream.write("请先在项目目录构建 Rust 研究内核：cargo build --release\n")
            return 2
        return subprocess.call([str(binary)], cwd=root)
    try:
        if args.command == "sec-archive":
            from .stocks.edgar.archive import archive_edgar

            result = archive_edgar(directory=args.directory, start=args.start, end=args.end, ciks=args.cik, user_agent=args.user_agent)
            output_stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            return 0 if result["status"] == "completed" else 2
        if args.command == "sec-study":
            from .ai.domain import strict_json
            from .stocks.edgar.event_study import event_study

            result = event_study(bundle=strict_json(Path(args.events).read_text()), prices=strict_json(Path(args.prices).read_text()),
                universe=strict_json(Path(args.universe).read_text()), policy=strict_json(Path(args.policy).read_text()) if args.policy else None,
                output=args.output or _output_directory("sec-events"))
            output_stream.write(json.dumps({k: v for k, v in result.items() if k not in {"event_input", "price_input", "events"}}, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            return 0 if result["execution_completed"] else 2
        if args.command == "options-backtest":
            from .ai.domain import strict_json
            from .stocks.options.overlay_report import options_backtest

            result = options_backtest(payload=strict_json(Path(args.data).read_text()),
                portfolio=strict_json(Path(args.portfolio).read_text()),
                policy=strict_json(Path(args.policy).read_text()) if args.policy else None,
                output=args.output or _output_directory("options"))
            output_stream.write(json.dumps({k: v for k, v in result.items() if k not in {"input_data", "results"}}, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            return 0 if result["execution_completed"] else 2
        config_path = args.config or (
            "config/quantbot.toml" if Path("config/quantbot.toml").is_file() else None
        )
        config = load_config(config_path, environ=environ)
        if args.command == "model-check":
            if config["llm"].get("backend") == "codex_cli":
                from .ai.codex import CodexClient

                result = CodexClient(config, environ).check()
            else:
                from .ai.models import OpenAICompatibleProvider

                OpenAICompatibleProvider(config, environ)
                result = {
                    "backend": "openai_compatible", "model": config["llm"]["model"],
                    "ready": True, "model_called": False, "model_access_verified": False,
                }
            output_stream.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            return 0 if result["ready"] else 2
        if args.command == "serve":
            import uvicorn
            from .web.app import create_app
            uvicorn.run(create_app(config=config,frontend_dir=args.frontend,worker=args.worker),host="127.0.0.1",port=args.port)
            return 0
        if getattr(args, "strategy", None):
            config["strategy"]["mode"] = args.strategy
        if args.command == "backtest":
            from .ai.backtest import backtest
            from .ai.domain import strict_json

            sec_context = None
            vintage_data = None
            if args.vintage_data:
                path = Path(args.vintage_data)
                if path.stat().st_size > 32_000_000:
                    raise ValueError("历史版本输入超过 32 MB")
                vintage_data = strict_json(path.read_text(encoding="utf-8-sig"))
            if args.sec_context:
                path = Path(args.sec_context)
                if path.stat().st_size > 64_000_000:
                    raise ValueError("AI 公告输入超过 64 MB")
                sec_context = strict_json(path.read_text())
                if not isinstance(sec_context, dict):
                    raise ValueError("AI 公告输入必须为 JSON 对象")
            if args.initial_cash is not None:
                config["execution"]["initial_cash"] = args.initial_cash
            result = backtest(
                config, output=args.output or _output_directory("backtest"),
                market=args.market, provider=args.provider,
                csv_data=Path(args.data).read_text(encoding="utf-8-sig") if args.data else "",
                symbols=args.symbols, last_bars=args.last_bars, allow_model_calls=args.allow_model_calls,
                sec_context=sec_context, vintage_data=vintage_data,
            )
        elif args.command == "download":
            result = _download(args, config)
        elif args.command in {"demo", "simulate"}:
            directory = Path(args.output) if args.output else _output_directory(args.command)
            market = (
                DailyMarketData(_demo_rows(config), config)
                if args.command == "demo"
                else DailyMarketData.from_csv(args.data, config)
            )
            provider = DemoProvider() if args.command == "demo" else _provider(config, environ=environ)
            result = simulate(
                market, config, provider, directory, getattr(args, "start", None), getattr(args, "end", None)
            )
            _dump(directory / "report.json", result)
            result = {k: v for k, v in result.items() if k not in {"equity_curve", "decisions"}}
            result["report"] = str((directory / "report.json").resolve())
        else:
            market = DailyMarketData.from_csv(args.data, config)
            if args.command == "paper":
                if not args.confirm_paper:
                    raise ValueError("模拟盘提交需要 --confirm-paper；使用 decide 可以先查看计划")
                from .ai.paper import run_paper
                from .ai.admission import AdmissionReference

                admission = AdmissionReference(
                    root=args.admission_root, job_id=args.admission_job,
                    evidence_path=args.admission_evidence, strategy_family=args.admission_family,
                ) if args.admission_root and args.admission_job else None
                result = run_paper(market, config, _provider(config, environ=environ), confirm=True,
                                   admission=admission)
            else:
                target = Path(args.output)
                if target.exists():
                    raise ValueError("决策输出已存在，使用新路径保存本次证据")
                context = market.context(args.as_of)
                if context.as_of > datetime.now(EASTERN):
                    raise ValueError("不能对尚未完成的信号日生成决策")
                account_values = {
                    "cash": config["execution"]["initial_cash"],
                    "equity": config["execution"]["initial_cash"],
                    "positions": {},
                }
                if args.account:
                    from .ai.domain import strict_json

                    account_values = strict_json(Path(args.account).read_text())
                    if set(account_values) != {"cash", "equity", "positions"}:
                        raise ValueError("离线账户 JSON 必须只包含 cash、equity、positions")
                broker = BrokerAccountSnapshot(
                    "offline_preview",
                    context.as_of,
                    account_values["equity"],
                    account_values["cash"],
                    False,
                    False,
                    MarginStatus.SIMULATED,
                )
                account = AccountState(**account_values, broker=broker)
                decision = _provider(config, environ=environ).decide(context, account, config)
                plan = build_plan(context, account, decision, config)
                result = {
                    "status": "planned",
                    "snapshot_id": context.snapshot_id,
                    "as_of": context.as_of.isoformat(),
                    "account_source": "offline_input" if args.account else "configured_initial_cash",
                    **plan.payload(),
                }
                with AIJournal(
                    target.with_suffix(".sqlite"), config, "decision_preview", "offline_preview"
                ) as journal:
                    journal.plan(context, plan)
                _dump(target, result)
        output_stream.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        if result.get("status") == "recovery_hold" or result.get("errors") or result.get("passed") is False:
            return 2
        return 0
    except (ValueError, OSError, ProviderError, RuntimeError) as error:
        output_stream.write(
            json.dumps({"status": "hold", "error": str(error), "submitted_orders": 0}, ensure_ascii=False)
            + "\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
