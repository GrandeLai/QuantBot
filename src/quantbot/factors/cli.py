"""Local, deterministic factor research commands. No account or LLM configuration."""

import argparse
import json
from pathlib import Path
import sys

from .study import register_study, run_study


def main(argv=None, *, stdout=None):
    output = stdout if stdout is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="quantbot factor-study")
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="冻结 12 因子、数据、成本与 72 项诊断，不计算收益")
    register.add_argument("--data", type=Path, required=True)
    register.add_argument("--root", type=Path, required=True, help="同一研究家族共用目录与试验账本")
    register.add_argument("--development-end", required=True, help="最后允许读取的日线收盘，UTC 午夜")
    register.add_argument("--protected-holdout-start", required=True, help="封存留出起点，UTC 午夜")
    register.add_argument("--provenance", type=Path)
    register.add_argument("--validation-days", type=int, default=180, help="每个开发验证窗口的天数，至少 180；其余门槛不变")
    register.add_argument("--prior-study", type=Path, action="append", default=[],
                         help="导入其他目录的既有因子研究；保留失败并冻结其检验结果，可重复指定")
    run = commands.add_parser("run", help="执行已冻结的因子开发验证；每个登记只允许运行一次")
    run.add_argument("--study", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            directory = register_study(args.root, args.data, development_end=args.development_end,
                                       protected_holdout_start=args.protected_holdout_start,
                                       provenance=args.provenance, validation_days=args.validation_days,
                                       prior_studies=tuple(args.prior_study))
            payload = {"status": "registered", "study": str(directory.resolve()),
                       "declared_trials": 72, "model_calls": 0, "trading_admission": False}
        else:
            result = run_study(args.study)
            payload = {key: result[key] for key in ("status", "pool", "declared_trials", "inferential_hypotheses",
                                                   "model_calls", "trading_admission")}
            payload["report"] = str((args.study / "report.md").resolve())
        output.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        output.write(json.dumps({"status": "failed", "error": str(error), "trading_admission": False},
                                 ensure_ascii=False) + "\n")
        return 2
