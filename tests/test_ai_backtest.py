"""一次性回测的股票/加密入口、证据、调用授权和账户隔离。"""

from copy import deepcopy
import csv
import io
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.backtest import backtest
from quantbot.ai.config import load_config
from quantbot.ai.runtime import RuntimeStore
from quantbot.ai.transport import ProviderError
from quantbot.cli import main
from quantbot.web.app import create_app

from tests.test_ai_runtime import rows_for


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_cli_demo_finishes_with_report_and_paused_independent_account(tmp_path, market):
    output = tmp_path / market
    stdout = io.StringIO()
    code = main([
        "backtest", "--market", market, "--provider", "demo", "--output", str(output),
    ], stdout=stdout, environ={})
    assert code == 0, stdout.getvalue()
    report = json.loads(stdout.getvalue())
    assert json.loads((output / "report.json").read_text()) == report
    assert report["data_source"] == "mock_fixture" and report["model_called"] is False
    assert report["passed"] and report["status"] == "completed"
    assert report["candidate"]["provider_calls"] == 29
    assert len(report["candidate"]["equity_curve"]) == 30
    assert report["candidate"]["execution_basis"] == "shared_volume_bar_v1"
    assert report["candidate"]["execution_model"]["max_volume_participation"] == 0.01
    assert report["candidate"]["fills"] > 0
    assert report["candidate"]["account"]["estimated_execution_cost"] > 0
    assert Path(report["snapshot"]["path"]).is_file()
    store = RuntimeStore(output / "evaluation")
    job = store.get(report["job_id"])
    assert job["status"] == "paused" and job["stage"] == "evaluated"
    assert job["account"]["as_of"] is None and job["account"]["positions"] == {}
    assert store.history(job["id"])["cycles"] == []


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_web_csv_backtest_uses_same_report_without_broker_or_runtime_changes(tmp_path, monkeypatch, market):
    def forbidden(*args, **kwargs):
        raise AssertionError("backtest must not call a paper/broker execution entry")

    monkeypatch.setattr("quantbot.ai.paper.run_paper", forbidden)
    monkeypatch.setattr("quantbot.web.app.run_spot", forbidden)
    config = load_config(environ={})
    config["strategy"]["momentum_window"] = 20
    original = deepcopy(config)
    rows = rows_for(market, 100)
    second_symbol = "AAPL" if market == "stocks" else "ETH/USDT"
    rows += [{**row, "symbol": second_symbol} for row in rows]
    csv_data = io.StringIO()
    csv_data.write("\ufeff")
    writer = csv.DictWriter(csv_data, rows[0].keys(), quoting=csv.QUOTE_ALL)
    writer.writeheader()
    writer.writerows(rows)
    client = TestClient(create_app(config=config, runtime_dir=tmp_path))
    before = client.get("/api/quantbot/runtime").json()["jobs"]
    response = client.post("/api/quantbot/allocate", json={
        "market": market, "mode": "simulate", "provider": "demo", "csv_data": csv_data.getvalue(),
        "initial_cash": 1234, "last_bars": 10,
    })
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["market"] == market and report["data_source"] == "offline_csv"
    assert report["candidate"]["initial_equity"] == 1234 and report["candidate"]["provider_calls"] == 9
    assert report["spec"]["symbols"] == sorted({row["symbol"] for row in rows})
    assert json.loads(Path(report["report_path"]).read_text()) == report
    assert client.get("/api/quantbot/runtime").json()["jobs"] == before
    assert config == original
    direct_config = deepcopy(config)
    direct_config["execution"]["initial_cash"] = 1234
    direct = backtest(direct_config, output=tmp_path / "direct", market=market, provider="demo",
                      csv_data=csv_data.getvalue(), last_bars=10)
    assert report["candidate"]["equity_curve"] == direct["candidate"]["equity_curve"]
    assert report["candidate"]["orders"] == direct["candidate"]["orders"]
    if market == "stocks":
        decision = client.post("/api/quantbot/allocate", json={"market": "stocks", "mode": "decide", "provider": "demo",
                               "csv_data": csv_data.getvalue(), "initial_cash": 1234})
        assert decision.status_code == 200, decision.text
        assert [row["symbol"] for row in decision.json()["plan"]["allocations"]] == ["AAPL", "SPY"]


def test_ai_backtest_requires_authorization_before_provider_or_output(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized request must not construct a model provider")

    monkeypatch.setattr("quantbot.ai.evaluation.provider_for", forbidden)
    config = load_config(environ={})
    with pytest.raises(ValueError, match="允许模型调用"):
        backtest(config, output=tmp_path / "denied")
    assert not (tmp_path / "denied").exists()
    client = TestClient(create_app(config=config, runtime_dir=tmp_path))
    response = client.post("/api/quantbot/allocate", json={"mode": "simulate", "provider": "ai"})
    assert response.status_code == 400 and "允许模型调用" in response.json()["detail"]


def test_failed_model_backtest_keeps_errors_and_cli_fails_without_enabling(tmp_path, monkeypatch):
    class Broken:
        def decide(self, *args):
            raise ProviderError("controlled outage")

    monkeypatch.setattr("quantbot.ai.evaluation.provider_for", lambda *args: Broken())
    rows = rows_for("crypto", 40)
    data = tmp_path / "candles.csv"
    with data.open("w") as f:
        writer = csv.DictWriter(f, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    stdout = io.StringIO()
    output = tmp_path / "failed"
    code = main([
        "backtest", "--market", "crypto", "--provider", "ai", "--data", str(data),
        "--last-bars", "3", "--allow-model-calls", "--output", str(output),
    ], stdout=stdout, environ={})
    report = json.loads(stdout.getvalue())
    assert code == 2 and report["passed"] is False and report["status"] == "failed"
    assert len(report["candidate"]["errors"]) == 2
    job = RuntimeStore(output / "evaluation").get(report["job_id"])
    assert job["status"] == "paused" and job["stage"] == "candidate"
    assert (output / "report.json").is_file()


def test_backtest_never_shortens_window_or_overwrites_output(tmp_path):
    config = load_config(environ={})
    with pytest.raises(ValueError, match="至少 35 根"):
        backtest(config, output=tmp_path / "insufficient", market="crypto", provider="demo", last_bars=70)
    assert not (tmp_path / "insufficient/report.json").exists()
    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("original report")
    with pytest.raises(ValueError, match="已存在"):
        backtest(config, output=output, market="crypto", provider="demo")
    assert marker.read_text() == "original report"


def test_web_validation_failure_keeps_downloadable_evidence(tmp_path):
    client = TestClient(create_app(config=load_config(environ={}), runtime_dir=tmp_path))
    response = client.post("/api/quantbot/allocate", json={"market": "crypto", "provider": "demo",
                           "mode": "simulate", "last_bars": 70})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "至少 35 根" in detail["message"]
    downloaded = client.get(detail["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.json()["execution_completed"] is False
    assert downloaded.json()["error_type"] == "ValueError"
    assert downloaded.json()["input_data"]["rows"]
