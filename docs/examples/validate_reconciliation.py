"""使用合成股票与加密行情验证逐笔现金流核验，另存损坏样例检验拒绝行为。"""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys

from quantbot.ai.backtest import backtest
from quantbot.ai.config import load_config
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args().output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    attempts = []

    def reject_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            attempts.append(event)
            raise RuntimeError("离线核验禁止网络请求")

    sys.addaudithook(reject_network)
    config = load_config(environ={})
    checks = []
    for market in ("stocks", "crypto"):
        report = backtest(config, output=root / market, market=market, provider="demo", last_bars=30)
        assert report["execution_completed"]
        for section in ("candidate", "baseline"):
            journal = inspect_replay(report[section]["journal_path"])
            accounting = reconcile_replay(journal["records"])
            assert accounting["status"] == "reconciled", accounting
            checks.append({"market": market, "section": section, "journal": journal["path"], "accounting": accounting})
        if market == "stocks":
            original = inspect_replay(report["candidate"]["journal_path"])["records"]
            damaged = deepcopy(original)
            first = next(record["payload"]["account"] for record in damaged if record["kind"] == "account")
            first["positions"] = dict(report["candidate"]["account"]["positions"])
            assert first["positions"]
            path = root / "deliberately-corrupted-fixture.jsonl"
            path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in damaged))
            rejected = reconcile_replay(inspect_replay(path)["records"])
            assert rejected["status"] == "mismatch"
            assert any(row["field"] == "positions" for row in rejected["discrepancies"])
    manifest = {"verified": True, "checks": checks, "corrupted_fixture_result": rejected,
                "model_calls": 0, "broker_calls": 0, "network_attempts": attempts, "code_hash": report["code_hash"],
                "boundary": "合成行情与规则；只验证已保存成交、持仓、现金和估值的内部一致性，不证明投资收益或成交真实性"}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"verified": True, "replays": len(checks), "account_snapshots": sum(row["accounting"]["account_snapshots"] for row in checks),
                      "corrupted_fixture_rejected": True, "manifest": str(root / "manifest.json"), "network_attempts": attempts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
