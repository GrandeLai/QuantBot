"""生成跨季度 SEC 合成样例，离线验证事件研究，输出完整输入和报告。"""

import argparse
import json
from pathlib import Path
import sys

from quantbot.evidence import hash_payload
from quantbot.stocks.edgar.event_example import example_input
from quantbot.stocks.edgar.event_study import event_study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sample = example_input()
    attempts = []

    def reject_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            attempts.append(event)
            raise RuntimeError("离线验收禁止网络请求")

    sys.addaudithook(reject_network)
    result = event_study(**sample, output=args.output)
    for name, value in sample.items():
        (args.output / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    manifest = {"execution_completed": result["execution_completed"], "research_conclusion": result["research_conclusion"],
                "report_path": result["report_path"], "report_hash": hash_payload(result), "code_hash": result["plan"]["code_hash"],
                "data_quality": result["data_quality"], "summary": result["summary"], "network_attempts": attempts,
                "model_calls": result["model_calls"], "broker_calls": result["broker_calls"]}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if result["execution_completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
