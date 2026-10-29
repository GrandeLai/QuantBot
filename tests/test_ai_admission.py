"""Frozen identity and public broker boundaries; entirely offline fixtures."""

from copy import deepcopy
import csv
import io
import json

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.admission import AdmissionReference, assess_strategy_admission, strategy_identity
from quantbot.ai.config import load_config
from quantbot.ai.models import PriceActionProvider
from quantbot.ai.paper import run_paper
from quantbot.ai.runtime_cli import main
from quantbot.cli import _demo_rows, main as quantbot_main
from quantbot.evidence import hash_payload
from quantbot.web.app import create_app
from tests.test_ai_runtime import setup


def test_frozen_identity_changes_with_model_risk_execution_and_code(tmp_path):
    _, job, _, _ = setup(tmp_path)
    original = strategy_identity(job["spec"], job["config"], job["code_hash"])
    for section, key, value in (("llm", "model", "another-model"),
                                ("risk", "max_weight", 0.1),
                                ("execution", "cost_bps", 30)):
        config = deepcopy(job["config"])
        config[section][key] = value
        changed = strategy_identity(job["spec"], config, job["code_hash"])
        assert changed["strategy_version"] != original["strategy_version"]
    spec = {**job["spec"], "max_drawdown": 0.01}
    assert strategy_identity(spec, job["config"], job["code_hash"])["risk_hash"] != original["risk_hash"]
    assert strategy_identity(job["spec"], job["config"], "0" * 64) != original


@pytest.mark.parametrize("reference", [None, {"passed": True, "broker_paper_authorized": True}])
def test_broker_paper_requires_persisted_evidence_before_client_construction(monkeypatch, reference):
    def forbidden(*args, **kwargs):
        raise AssertionError("the admission failure must precede any broker client")

    monkeypatch.setattr("quantbot.ai.paper.AlpacaPaperClient", forbidden)
    with pytest.raises(ValueError, match="冻结策略准出引用"):
        run_paper(None, load_config(environ={}), PriceActionProvider(), confirm=True, admission=reference)


def test_missing_reference_does_not_create_an_empty_runtime(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(ValueError, match="目录不存在"):
        run_paper(None, load_config(environ={}), PriceActionProvider(), confirm=True,
                  admission=AdmissionReference(str(root), "missing"))
    assert not root.exists()


def test_cli_and_http_broker_paper_reject_missing_evidence_before_network(tmp_path, monkeypatch):
    config = load_config(environ={})
    path = tmp_path / "synthetic.csv"
    with path.open("w") as file:
        rows = _demo_rows(config)
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    def forbidden(*args, **kwargs):
        raise AssertionError("a public paper request without admission must not connect")

    monkeypatch.setattr("quantbot.ai.paper.AlpacaPaperClient", forbidden)
    output = io.StringIO()
    assert quantbot_main(["paper", "--data", str(path), "--strategy", "price_action", "--confirm-paper"],
                        stdout=output, environ={}) == 2
    assert "冻结策略准出引用" in json.loads(output.getvalue())["error"]
    response = TestClient(create_app(config=config, runtime_dir=tmp_path / "web")).post("/api/quantbot/allocate", json={
        "market": "stocks", "mode": "paper", "provider": "price_action",
        "confirm_paper": True, "csv_data": path.read_text(),
    })
    assert response.status_code == 400
    assert "冻结策略准出引用" in response.json()["detail"]


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_engineering_job_never_implies_market_admission(tmp_path, market):
    store, job, _, _ = setup(tmp_path, market)
    report = assess_strategy_admission(store, job["id"])
    assert report["identity"] == job["strategy_identity"]
    assert report["backtest_passed"] is False
    assert report["broker_paper_authorized"] is False
    assert report["live_authorized"] is False
    assert job["execution"] == store.overview()["execution"] == "local_observation"
    output = io.StringIO()
    assert main(["--root", str(store.root), "strategy-admission", job["id"]], stdout=output) == 2
    assert json.loads(output.getvalue())["backtest_passed"] is False


def test_unsigned_positive_stock_report_and_matching_hashes_cannot_authorize_ai(tmp_path, monkeypatch):
    store, job, _, _ = setup(tmp_path)
    job = store.create({**job["spec"], "provider": "price_action"}, job["config"])
    evidence_path = tmp_path / "claimed-admission.jsonl"
    evidence_path.write_text(json.dumps({
        "requirement_id": "daytrade_strategy_admission", "passed": True,
        "config_hash": hash_payload(job["config"]), "code_hash": job["code_hash"],
    }) + "\n")
    report = assess_strategy_admission(store, job["id"], evidence_path=evidence_path)
    assert report["checks"]["frozen_config_and_code_match"]
    assert not report["checks"]["authenticated_market_policy_passed"]
    assert not report["checks"]["admitted_strategy_implementation_bound"]

    def forbidden(*args, **kwargs):
        raise AssertionError("unverified evidence must not reach the broker")

    monkeypatch.setattr("quantbot.ai.paper.AlpacaPaperClient", forbidden)
    with pytest.raises(ValueError, match="尚未通过"):
        run_paper(None, job["config"], PriceActionProvider(), confirm=True,
                  admission=AdmissionReference(str(store.root), job["id"], evidence_path=str(evidence_path)))
    changed = deepcopy(job["config"])
    changed["risk"]["max_weight"] /= 2
    with pytest.raises(ValueError, match="完整配置.*不一致"):
        run_paper(None, changed, PriceActionProvider(), confirm=True,
                  admission=AdmissionReference(str(store.root), job["id"], evidence_path=str(evidence_path)))
