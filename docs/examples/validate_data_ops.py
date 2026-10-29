"""一次离线验收：合成版本行情 → 冻结研究 → 持久化队列 → 完整报告。"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path

from quantbot.ai.config import load_config
from quantbot.ai.feeds import stock_calendar
from quantbot.ai.research import get_study, register_study
from quantbot.ai.research_ops import enqueue_study, research_operations, run_due_study
from quantbot.ai.runtime import RuntimeStore
from quantbot.evidence import source_tree_hash


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    store = RuntimeStore(root / "automation")
    manifest = {"data_kind": "synthetic", "code_hash": source_tree_hash(), "studies": []}
    calendar = stock_calendar("2025-01-01", "2025-12-31")
    for market in ("stocks", "crypto"):
        symbol = "SPY" if market == "stocks" else "BTC/USDT"
        rows = []
        for i in range(220):
            price = 100 + i * 0.1 if market == "stocks" else 30000 + i * 10
            width = 1 if market == "stocks" else 50
            row = {"symbol": symbol, "open": price, "high": price + width, "low": price - width,
                   "close": price + width * 0.1, "volume": 1000000 if market == "stocks" else 100}
            if market == "stocks":
                row["date"] = calendar.sessions[i].date().isoformat()
                close = calendar.session_close(row["date"]).to_pydatetime()
            else:
                start = datetime(2025, 1, 2, tzinfo=UTC) + timedelta(minutes=15 * i)
                row.update(timestamp=start.isoformat(), bar="15m")
                close = start + timedelta(minutes=15)
            rows.append({**row, "available_at": close.isoformat()})
        for original, released in ((130, 170), (150, 180)):
            rows.append({**rows[original], "close": rows[original]["close"] + width * 0.2,
                         "available_at": rows[released]["available_at"]})
        payload = {"schema_version": 1, "market": market, "provider": "local-synthetic-acceptance",
                   "captured_at": datetime.now(UTC).isoformat(), "data_kind": "synthetic",
                   "price_basis": "raw", "rows": rows, "actions": []}
        path = root / (market + "-vintages.json")
        path.write_text(json.dumps(payload, indent=2) + "\n")
        config = load_config(environ={})
        config["strategy"]["momentum_window"] = 20
        job = store.create({"name": market + " 合成数据验收", "market": market, "symbols": [symbol],
                            "source": "vintage", "data_path": str(path), "provider": "demo",
                            "bar": "1d" if market == "stocks" else "15m"}, config)
        study = register_study(store, job_ids=[job["id"]], card={
            "title": market + " 数据版本与研究队列验收", "hypothesis": "发布时间之后才允许历史修订进入特征",
            "rationale": "检查冻结数据、一次留出、压力对照与队列实际记录", "failure_condition": "发生前视、重复执行或未归档即失败",
        })
        queue = enqueue_study(store, study["id"], planned_at=datetime.now(UTC).isoformat())
        executed = run_due_study(store)["executed"]
        result = get_study(store, study["id"])
        assert executed["id"] == queue["id"] and executed["status"] == "completed"
        assert result["report"]["execution_completed"] and result["report"]["conclusion"] == "insufficient"
        assert result["plan"]["data_quality"]["data_kind"] == "synthetic"
        assert result["plan"]["data_quality"]["revision_count"] == 2
        assert store.get(job["id"])["account"]["as_of"] is None
        assert result["report"]["model_usage"]["provider_calls"] == 0
        report_path = Path(result["report"]["report_path"])
        manifest["studies"].append({"market": market, "job_id": job["id"], "study_id": study["id"],
            "queue_id": queue["id"], "report": str(report_path), "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "execution_completed": True, "conclusion": result["report"]["conclusion"], "trials": len(result["trials"]),
            "initial_bars": 220, "revisions": 2, "model_calls": 0, "broker_calls": 0})
    assert run_due_study(store)["executed"] is None
    operations = research_operations(store)
    assert not operations["worker_online"] and operations["queue_counts"] == {"completed": 2}
    (root / "operations.json").write_text(json.dumps(operations, ensure_ascii=False, indent=2) + "\n")
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
