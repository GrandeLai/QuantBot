"""生成显式合成样例并完成六组持仓期权回放；输出目录必须全新。"""

import argparse
import json
from pathlib import Path
import sys

from quantbot.evidence import hash_payload
from quantbot.stocks.options.overlay_example import example_input
from quantbot.stocks.options.overlay_report import options_backtest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sample = example_input()
    attempts = []

    def reject_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            attempts.append(event)
            raise RuntimeError("离线验收禁止网络请求")

    sys.addaudithook(reject_network)
    report = options_backtest(payload=sample["data"], portfolio=sample["portfolio"], policy=sample["policy"], output=args.output)
    for key in ("data", "portfolio", "policy"):
        (args.output / f"{key}.json").write_text(json.dumps(sample[key], ensure_ascii=False, indent=2) + "\n")
    manifest = {"execution_completed": report["execution_completed"], "research_conclusion": report["research_conclusion"],
                "report_path": report["report_path"], "report_hash": hash_payload(report), "code_hash": report["plan"]["code_hash"],
                "data_hash": report["plan"]["data_hash"], "network_attempts": attempts,
                "model_calls": report["model_calls"], "broker_calls": report["broker_calls"],
                "results": [{key: result[key] for key in ("strategy", "assignment_scenario", "execution_completed",
                    "final_equity", "return_fraction", "option_trades", "assignments", "fees", "errors")} for result in report["results"]]}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if report["execution_completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
