"""自动运行、评估和版本管理命令。默认只创建暂停的候选任务。"""

import argparse
import json
from pathlib import Path
import sys

from .config import load_config
from .evaluation import evaluate, walk_forward
from .journal import runtime_lock
from .runtime import RuntimeStore, run_worker, tick
from .research import get_study, list_studies, register_study, run_study
from .research_ops import cancel_queued_study, enqueue_study, research_operations, run_due_study, run_research_worker
from .replay_evidence import inspect_replay


def main(argv=None, *, stdout=None, environ=None):
    parser = argparse.ArgumentParser(prog="quantbot runtime", description="本地账本观察、历史工程评估和只读研究准出；不授权券商交易")
    parser.add_argument("--root", default="runtime/automation", help="持久化任务目录")
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("create", help="创建暂停的候选任务")
    create.add_argument("--name", required=True)
    create.add_argument("--config")
    create.add_argument("--market", choices=["stocks", "crypto"], default="stocks")
    create.add_argument("--provider", choices=["ai", "price_action", "demo", "crypto_rules"], default="ai")
    create.add_argument("--source", choices=["csv", "yahoo", "okx_public", "vintage"], default="csv")
    create.add_argument("--data", default="")
    create.add_argument("--sec-context", default="", help="美股回测使用的冻结 SEC 公告输入 JSON")
    create.add_argument("--symbols", nargs="+")
    create.add_argument("--bar", choices=["15m", "1h", "1d"], default="1d")
    create.add_argument("--interval-seconds", type=int, default=60)
    create.add_argument("--data-delay-seconds", type=int, default=900)
    create.add_argument("--max-daily-loss", type=float, default=0.03)
    create.add_argument("--max-drawdown", type=float, default=0.10)
    create.add_argument("--initial-cash", type=float)
    commands.add_parser("list")
    commands.add_parser("studies", help="列出全部研究，包括失败和淘汰结果")
    commands.add_parser("operations", help="研究计划、实际运行、数据与模型预算看板")
    admission = commands.add_parser("study-admission", help="只读检查加密研究准出阈值；不连接或启动交易账户")
    admission.add_argument("study_id")
    strategy_admission = commands.add_parser("strategy-admission", help="只读检查冻结任务与市场准出证据；不授权交易")
    strategy_admission.add_argument("job_id")
    strategy_admission.add_argument("--study-id", help="加密预登记研究 ID")
    strategy_admission.add_argument("--evidence", help="美股已认证的正式准出证据 JSONL")
    strategy_admission.add_argument("--family", choices=["normal", "daytrade"], default="daytrade")
    commands.add_parser("crypto-policy", help="查看加密回测、OKX 模拟盘和小额实盘的分级阈值")
    journal = commands.add_parser("replay-show", help="只读检查逐步回放证据；不重试任何调用")
    journal.add_argument("path", help="回放 .jsonl 文件完整路径")
    journal.add_argument("--reconcile", action="store_true", help="从成交独立重算现金、持仓、成本和估值")
    queue = commands.add_parser("study-queue", help="将冻结研究加入持久化计划")
    queue.add_argument("study_id")
    queue.add_argument("--at", required=True, help="带时区的 ISO 计划时间")
    queue.add_argument("--allow-model-calls", action="store_true")
    cancel_queue = commands.add_parser("queue-cancel", help="取消尚未开始的研究计划")
    cancel_queue.add_argument("queue_id")
    researcher = commands.add_parser("research-worker", help="执行到期研究；不启动交易模拟工作进程")
    researcher.add_argument("--once", action="store_true", help="只检查队列一次，最多执行一个研究")
    study_create = commands.add_parser("study-create", help="从 JSON 研究卡片登记并冻结方案，不调用模型")
    study_create.add_argument("--plan", required=True, help="包含 job_ids、card、protocol 的 JSON 文件")
    for action in ("study-show", "study-run"):
        command = commands.add_parser(action, help="查看完整研究证据" if action == "study-show" else "执行一次已冻结研究")
        command.add_argument("study_id")
        if action == "study-run":
            command.add_argument("--allow-model-calls", action="store_true")
    rolling = commands.add_parser("walk-forward", help="同一行情下滚动样本外评估和版本对比")
    rolling.add_argument("job_ids", nargs="+", help="1..6 个冻结候选版本的任务 ID")
    rolling.add_argument("--selection-bars", type=int, default=30, help="每折较早的版本选择区间")
    rolling.add_argument("--test-bars", type=int, default=10, help="每折后续测试区间")
    rolling.add_argument("--folds", type=int, default=3)
    rolling.add_argument("--gap-bars", type=int, default=1, help="选择与测试之间跳过的 K 线数")
    rolling.add_argument("--allow-model-calls", action="store_true")
    for action in ("show", "pause", "resume", "exit", "evaluate", "clone", "cancel"):
        command = commands.add_parser(action)
        command.add_argument("job_id")
        if action == "cancel":
            command.add_argument("client_order_id")
        if action in {"resume", "evaluate"}:
            command.add_argument("--allow-model-calls", action="store_true")
        if action == "resume":
            command.add_argument("--acknowledge-risk", action="store_true")
        if action == "evaluate":
            command.add_argument("--last-bars", type=int, default=30)
        if action == "clone":
            command.add_argument("--config", help="新版本模型/策略/风控 TOML")
            command.add_argument("--name")
    worker = commands.add_parser("worker", help="持续运行；按 Ctrl+C 停止工作进程")
    worker.add_argument("--once", action="store_true", help="只检查并运行一次到期任务")
    args = parser.parse_args(argv)
    output = stdout or sys.stdout
    try:
        if args.action == "crypto-policy":
            from .crypto_admission import crypto_policy

            output.write(json.dumps(crypto_policy(), ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            return 0
        if args.action == "replay-show":
            result = inspect_replay(args.path)
            if args.reconcile:
                from .reconciliation import reconcile_replay

                result["accounting"] = reconcile_replay(result["records"])
            output.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            return 0 if result["execution_completed"] and (not args.reconcile or result["accounting"]["status"] == "reconciled") else 2
        store = RuntimeStore(args.root)
        if args.action == "create":
            config = load_config(
                args.config or ("config/quantbot.toml" if Path("config/quantbot.toml").is_file() else None),
                environ,
            )
            if args.initial_cash is not None:
                config["execution"]["initial_cash"] = args.initial_cash
            spec = {
                key: getattr(args, key)
                for key in (
                    "name",
                    "market",
                    "provider",
                    "source",
                    "symbols",
                    "bar",
                    "interval_seconds",
                    "data_delay_seconds",
                    "max_daily_loss",
                    "max_drawdown",
                )
            }
            spec["data_path"] = args.data
            spec["sec_context_path"] = args.sec_context
            result = store.create(spec, config)
        elif args.action == "list":
            result = store.overview()
        elif args.action == "studies":
            result = list_studies(store)
        elif args.action == "operations":
            result = research_operations(store)
        elif args.action == "study-queue":
            result = enqueue_study(store, args.study_id, planned_at=args.at, allow_model_calls=args.allow_model_calls)
        elif args.action == "queue-cancel":
            result = cancel_queued_study(store, args.queue_id)
        elif args.action == "research-worker":
            if not args.once:
                run_research_worker(store)
                return 0
            with runtime_lock(store.root / "research-worker-lock"):
                result = run_due_study(store)
        elif args.action == "study-create":
            result = register_study(store, **json.loads(Path(args.plan).read_text(encoding="utf-8")))
        elif args.action == "study-show":
            result = get_study(store, args.study_id)
        elif args.action == "study-run":
            result = run_study(store, args.study_id, allow_model_calls=args.allow_model_calls)
        elif args.action == "study-admission":
            from .crypto_admission import assess_crypto_study

            result = assess_crypto_study(store, args.study_id)
        elif args.action == "strategy-admission":
            from .admission import assess_strategy_admission

            result = assess_strategy_admission(
                store, args.job_id, study_id=args.study_id,
                evidence_path=args.evidence, strategy_family=args.family,
            )
        elif args.action == "show":
            result = {"job": store.get(args.job_id), **store.history(args.job_id)}
        elif args.action == "walk-forward":
            result = walk_forward(
                store,
                args.job_ids,
                selection_bars=args.selection_bars,
                test_bars=args.test_bars,
                folds=args.folds,
                gap_bars=args.gap_bars,
                allow_model_calls=args.allow_model_calls,
            )
        elif args.action == "evaluate":
            result = evaluate(
                store, args.job_id, last_bars=args.last_bars, allow_model_calls=args.allow_model_calls
            )
        elif args.action == "cancel":
            result = store.cancel_order(args.job_id, args.client_order_id)
        elif args.action == "clone":
            previous = store.get(args.job_id)
            spec = previous["spec"]
            spec["name"] = args.name or spec["name"] + " 新版本"
            config = load_config(args.config, environ) if args.config else previous["config"]
            result = store.create(spec, config, parent=args.job_id)
        elif args.action == "worker":
            if not args.once:
                run_worker(store)
                return 0
            with runtime_lock(store.root / "process-lock"):
                result = tick(store)
        else:
            result = store.control(
                args.job_id,
                args.action,
                allow_model_calls=getattr(args, "allow_model_calls", False),
                acknowledge_risk=getattr(args, "acknowledge_risk", False),
            )
        output.write(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        if args.action == "study-run":
            return 0 if result["report"]["execution_completed"] else 2
        if args.action == "research-worker":
            entries = result["recovered"] + ([result["executed"]] if result["executed"] else [])
            return 2 if any(r["status"] in {"failed", "interrupted"} for r in entries) else 0
        if args.action == "worker" and any(
            item.get("status") in {"held", "model_error", "interrupted"} for item in result
        ):
            return 2
        return 2 if isinstance(result, dict) and result.get("passed") is False else 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, RuntimeError, TypeError) as exc:
        output.write(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False) + "\n")
        return 2
