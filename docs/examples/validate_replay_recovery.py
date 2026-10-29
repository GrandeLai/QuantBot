"""一次性离线验证：正常回放、异常退出、强制退出和独立证据检查。全部为合成规则。"""

import argparse
import csv
import json
from pathlib import Path
import subprocess
import sys

from quantbot.ai import evaluation
from quantbot.ai.backtest import demo_rows
from quantbot.ai.config import load_config
from quantbot.ai.models import DemoProvider
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.runtime import RuntimeStore
from quantbot.evidence import hash_payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--child", choices=["exception", "forced_exit"])
    args = parser.parse_args()
    root = args.output.resolve()
    if not args.child:
        root.mkdir(parents=True, exist_ok=False)

    def reject_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            with (root / "network-attempts.jsonl").open("a") as handle:
                handle.write(json.dumps({"event": event}) + "\n")
            raise RuntimeError("离线可靠性验收禁止网络请求")

    sys.addaudithook(reject_network)
    store = RuntimeStore(root / "runtime")
    if args.child:
        import os

        class ControlledFailure:
            def __init__(self):
                self.calls = 0

            def decide(self, context, account, config):
                self.calls += 1
                if self.calls == 2:
                    if args.child == "forced_exit":
                        os._exit(17)
                    raise RuntimeError("explicit synthetic interruption after one decision")
                return DemoProvider().decide(context, account, config)

        evaluation.provider_for = lambda *unused: ControlledFailure()
        evaluation.evaluate(store, (root / "job-id.txt").read_text().strip(), last_bars=6)
        return 0

    config = load_config(environ={})
    rows = demo_rows("crypto", config, ["BTC/USDT"])
    data = root / "prices.csv"
    with data.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    job = store.create({"name": "显式合成中断验收", "market": "crypto", "provider": "demo", "symbols": ["BTC/USDT"],
                        "source": "csv", "bar": "15m", "data_path": str(data)}, config)
    (root / "job-id.txt").write_text(job["id"] + "\n")
    complete = evaluation.evaluate(store, job["id"], last_bars=6)
    assert complete["passed"] and store.get(job["id"])["stage"] == "evaluated"
    cases = [{"scenario": "completed", "journal": inspect_replay(complete["candidate"]["journal_path"], include_records=False)}]
    for scenario, expected_exit in (("exception", 1), ("forced_exit", 17)):
        child = subprocess.run([sys.executable, __file__, "--output", str(root), "--child", scenario],
                               capture_output=True, text=True, timeout=30)
        (root / (scenario + ".stderr.txt")).write_text(child.stderr)
        assert child.returncode == expected_exit, child.stderr
        attempt = next(e for e in store.history(job["id"])["events"] if e["kind"] == "evaluation_started")
        path = Path(attempt["payload"]["journal_directory"]) / "candidate.jsonl"
        journal = inspect_replay(path, include_records=False)
        assert journal["resolved_decisions"] == 1 and len(journal["unresolved_decision_calls"]) == 1
        assert store.get(job["id"])["stage"] == "candidate"
        cases.append({"scenario": scenario, "child_exit_code": child.returncode, "journal": journal})
    manifest = {"verified": True, "job_id": job["id"], "job_stage": store.get(job["id"])["stage"],
                "cases": cases, "model_calls": 0, "broker_calls": 0,
                "network_attempts": (root / "network-attempts.jsonl").read_text().splitlines() if (root / "network-attempts.jsonl").exists() else [],
                "code_hash": job["code_hash"], "input_hash": hash_payload(rows),
                "boundary": "合成行情与本地规则，验证异常留痕和状态约束，不验证模型服务的收费语义或真实交易"}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
