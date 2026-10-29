"""Bounded crypto AI CLI; shares model and risk settings with the main CLI."""

import argparse
import csv
import json
from pathlib import Path

from quantbot.ai.config import load_config
from quantbot.ai.transport import ProviderError
from .ai import candles_from_rows, run_spot


def main(argv=None, *, stdout=None, environ=None):
    parser = argparse.ArgumentParser(prog="quantbot crypto ai")
    parser.add_argument("--config")
    parser.add_argument("--data", help="带时区的加密现货 OHLCV CSV")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"])
    parser.add_argument("--mode", choices=["decide", "paper"], default="decide")
    parser.add_argument("--initial-cash", type=float, default=1000)
    parser.add_argument("--ledger", default="runtime/crypto-ai-paper.sqlite")
    parser.add_argument("--demo", action="store_true", help="明确使用离线规则演示，不调用模型")
    args = parser.parse_args(argv)
    try:
        config = load_config(
            args.config or ("config/quantbot.toml" if Path("config/quantbot.toml").is_file() else None),
            environ,
        )
        if not args.demo and not args.data:
            raise ValueError("AI 需要 --data 提供离线真实行情；合成行情只用于 --demo")
        candles = None
        if args.data:
            with Path(args.data).open(encoding="utf-8-sig", newline="") as f:
                candles = candles_from_rows(list(csv.DictReader(f)))
        from quantbot.ai.gateway import decision_provider
        from quantbot.ai.models import DemoProvider

        provider = DemoProvider() if args.demo else decision_provider(config, environ=environ)
        result = run_spot(
            config,
            symbols=args.symbols,
            candles=candles,
            demo=args.demo,
            mode=args.mode,
            initial_cash=args.initial_cash,
            ledger_path=args.ledger,
            provider=provider,
        )
        text = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        (stdout.write(text + "\n") if stdout else print(text))
        return 0
    except (ValueError, OSError, ProviderError) as exc:
        text = json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
        (stdout.write(text + "\n") if stdout else print(text))
        return 2
