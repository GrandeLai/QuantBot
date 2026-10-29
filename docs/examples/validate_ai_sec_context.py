"""离线验证 SEC 公告进入主回测和冻结研究；全部使用显式合成输入与规则决策。"""

import argparse
import csv
import io
import json
from pathlib import Path
import sys

from quantbot.ai.backtest import backtest
from quantbot.ai.config import load_config
from quantbot.ai.feeds import stock_calendar
from quantbot.ai.market import FIELDS
from quantbot.ai.research import register_study, run_study
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.runtime import RuntimeStore
from quantbot.evidence import hash_payload
from quantbot.stocks.edgar.event_example import example_input


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    sample = example_input()
    rows = [{key: row[key] for key in FIELDS} for row in sample["prices"]["bars"]]
    calendar = stock_calendar("2024-09-01", "2025-03-31")
    earlier = [day.date().isoformat() for day in calendar.sessions_in_range("2024-09-01", "2024-11-29")]
    for index, day in enumerate(earlier):
        opening = 100 - (len(earlier) - index) * 0.04
        rows.extend({"date": day, "symbol": symbol, "open": opening, "close": opening + 0.03,
                     "high": opening + 1, "low": opening - 1, "volume": 1000000} for symbol in ("DEMO", "SPY"))
    rows.sort(key=lambda row: (row["date"], row["symbol"]))
    data = io.StringIO()
    writer = csv.DictWriter(data, FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    (root / "prices.csv").write_text(data.getvalue())
    sec_context = {"schema_version": 1, "bundle": sample["bundle"], "universe": sample["universe"],
                   "policy": {"lookback_days": 30}}
    (root / "sec-context.json").write_text(json.dumps(sec_context, ensure_ascii=False, indent=2) + "\n")
    (root / "config.toml").write_text("[strategy]\nmomentum_window=20\n")
    config = load_config(root / "config.toml", environ={})
    attempts = []

    def reject_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            attempts.append(event)
            raise RuntimeError("离线验收禁止网络请求")

    sys.addaudithook(reject_network)
    result = backtest(config, output=root / "backtest", provider="demo", csv_data=data.getvalue(),
                      symbols=["SPY", "DEMO"], last_bars=70, sec_context=sec_context)
    store = RuntimeStore(root / "research")
    job = store.create({"name": "合成公告时点研究", "market": "stocks", "provider": "demo", "symbols": ["SPY", "DEMO"],
                        "source": "csv", "data_path": str(root / "prices.csv"), "sec_context_path": str(root / "sec-context.json")}, config)
    study = register_study(store, job_ids=[job["id"]], data_kind="synthetic",
        card={"title": "公告输入流程验收", "hypothesis": "检查已知公告只在可得时间后进入决策", "rationale": "合成数据验证工程路径，规则不使用公告",
              "failure_condition": "未来正文提前可见、无法复现或导出缺少输入则失败"},
        protocol={"development_bars": 10, "folds": 2, "gap_bars": 2, "holdout_bars": 10, "min_observations": 5, "placebo_seeds": [7, 42]})
    research = run_study(store, study["id"])
    journal = inspect_replay(result["candidate"]["journal_path"])
    recorded_accounts = [record["payload"]["account"] for record in journal["records"] if record["kind"] == "account"]
    curve_accounts = [{key: value for key, value in row.items() if key != "as_of"} for row in result["candidate"]["equity_curve"]]
    assert journal["execution_completed"] and recorded_accounts == curve_accounts
    assert curve_accounts[0]["positions"] == {} and any(row["positions"] for row in curve_accounts[1:])
    decisions = result["candidate"]["decisions"]
    observations = [(row["as_of"], event) for row in decisions for values in row["sec_events"]["symbols"].values() for event in values["events"]]
    assert observations and all(event["available_at"] < at for at, event in observations)
    for row in decisions:
        if row["as_of"] < "2025-02-03T14:01:00+00:00":
            assert "Later corrected synthetic content" not in json.dumps(row["sec_events"])
    manifest = {"execution_completed": result["execution_completed"] and research["report"]["execution_completed"],
                "backtest_report": result["report_path"], "research_report": research["report"]["report_path"],
                "backtest_hash": hash_payload(result), "research_hash": hash_payload(research),
                "code_hash": result["code_hash"], "decision_count": len(decisions), "visible_event_observations": len(observations),
                "research_trials": len(research["trials"]), "research_conclusion": research["report"]["conclusion"],
                "account_snapshots_match_journal": True, "account_snapshot_count": len(recorded_accounts),
                "model_calls": 0, "broker_calls": 0, "network_attempts": attempts,
                "boundary": "合成行情与公告；使用价格规则，只验证时点输入、风控、回测和研究证据，不验证模型推理或投资收益"}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["execution_completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
