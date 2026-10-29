"""一次离线验收：主回测公司行动、CLI/API 一致性、冻结重放与独立对账。"""

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import io
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient

from quantbot.ai.backtest import backtest
from quantbot.ai.config import load_config
from quantbot.ai.feeds import stock_calendar
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.cli import main as cli
from quantbot.web.app import create_app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    root = parser.parse_args().output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    attempts = []

    def reject_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            attempts.append(event)
            raise RuntimeError("公司行动验收禁止网络请求")

    sys.addaudithook(reject_network)
    calendar = stock_calendar("2025-01-01", "2025-12-31")
    sessions = calendar.sessions_in_range("2025-01-01", "2025-12-31")[:220]
    rows = []
    for i, session in enumerate(sessions):
        ratio = 2 if i >= 206 else 1
        dividend = (1 if i >= 212 else 0) + (0.5 if i >= 218 else 0)
        price = 100 + i * 0.2
        rows.append({"date": session.date().isoformat(), "symbol": "SPY", "open": price / ratio - dividend,
                     "high": (price + 1) / ratio - dividend, "low": (price - 1) / ratio - dividend,
                     "close": (price + 0.1) / ratio - dividend, "volume": 1_000_000 * ratio,
                     "available_at": calendar.session_close(session).isoformat()})
    actions = [{"id": "synthetic-split", "symbol": "SPY", "type": "split", "date": rows[206]["date"],
                "available_at": rows[205]["available_at"], "ratio": "2"}]
    for identity, index, amount, pay_date in (
        ("synthetic-dividend-paid", 212, "1", rows[216]["date"]),
        ("synthetic-dividend-unpaid", 218, "0.5", (sessions[-1].date() + timedelta(days=10)).isoformat()),
    ):
        actions.append({"id": identity, "symbol": "SPY", "type": "dividend", "date": rows[index]["date"],
                        "available_at": rows[index - 1]["available_at"], "amount_per_share": amount,
                        "pay_date": pay_date, "currency": "USD", "distribution": "ordinary_cash"})
    payload = {"schema_version": 1, "market": "stocks", "provider": "local-synthetic-acceptance",
               "captured_at": datetime.now(UTC).isoformat(), "data_kind": "synthetic", "price_basis": "raw",
               "rows": rows, "actions": actions}
    data = root / "stocks-vintages.json"
    data.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    stdout = io.StringIO()
    code = cli(["backtest", "--provider", "demo", "--vintage-data", str(data),
                "--last-bars", "30", "--output", str(root / "cli")], stdout=stdout, environ={})
    assert code == 0, stdout.getvalue()
    command_report = json.loads(stdout.getvalue())
    config = load_config(environ={})
    client = TestClient(create_app(config=config, runtime_dir=root / "web"))
    response = client.post("/api/quantbot/allocate", json={"market": "stocks", "mode": "simulate",
                           "provider": "demo", "vintage_data": payload, "last_bars": 30,
                           "initial_cash": config["execution"]["initial_cash"]})
    assert response.status_code == 200, response.text
    web_report = response.json()
    frozen = backtest(config, output=root / "frozen", provider="demo", last_bars=30,
                      vintage_data=web_report["input_snapshot"]["vintage_input"])
    reports = [command_report, web_report, frozen]
    checks = []
    for report in reports:
        assert report["execution_completed"] and not report["model_called"]
        assert report["data_quality"]["point_in_time_verified"] is False
        assert report["candidate"]["equity_curve"] == command_report["candidate"]["equity_curve"]
        assert report["candidate"]["corporate_action_events"] == command_report["candidate"]["corporate_action_events"]
        assert report["candidate"]["account"]["dividend_receivable"] > 0
        assert report["candidate"]["account"]["dividend_income"] > report["candidate"]["account"]["dividend_receivable"]
        for section in ("candidate", "baseline"):
            replay = inspect_replay(report[section]["journal_path"])
            accounting = reconcile_replay(replay["records"])
            assert accounting["status"] == "reconciled", accounting
            checks.append({"section": section, "journal": replay["path"], "accounting": accounting})
    assert not client.get("/api/quantbot/runtime").json()["jobs"]
    manifest = {"verified": True, "data_kind": "synthetic", "code_hash": command_report["code_hash"],
                "input": str(data), "input_sha256": hashlib.sha256(data.read_bytes()).hexdigest(),
                "reports": [r["report_path"] for r in reports], "checks": checks,
                "model_calls": 0, "broker_calls": 0, "network_attempts": attempts,
                "boundary": "合成行情与规则：证明回测会计和入口一致性，不证明真实来源、交易所成交或盈利效果"}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"verified": True, "replays": len(checks), "account_snapshots": sum(
        row["accounting"]["account_snapshots"] for row in checks), "manifest": str(root / "manifest.json"),
        "account": command_report["candidate"]["account"], "network_attempts": attempts}, ensure_ascii=False))


if __name__ == "__main__":
    main()
