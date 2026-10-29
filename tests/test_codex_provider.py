"""本地进程协议验收；假 CLI 不访问网络、不调用真实模型。"""

import asyncio
import csv
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.backtest import demo_rows
from quantbot.ai.config import load_config, validate_config
from quantbot.ai.gateway import decision_provider
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.transport import ProviderError
from quantbot.cli import main
from quantbot.crypto.ai import CryptoAccount, market_context
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.models.agent import configure_default_agent, get_default_agent
from quantbot.models._types import ChatMessage
from quantbot.web.app import create_app


@pytest.fixture
def codex(tmp_path, monkeypatch):
    executable = tmp_path / "fake-codex"
    log = tmp_path / "invocations.jsonl"
    executable.write_text(f"#!{sys.executable}\n" + r'''
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
mode = os.environ.get("FAKE_CODEX_MODE", "valid")
if args == ["--version"]:
    print("codex-cli fixture")
    sys.exit(0)
if args == ["--help"]:
    print("--no-daemon")
    sys.exit(0)
if args == ["exec", "--help"]:
    print("--ignore-user-config --ephemeral --output-schema --output-last-message")
    sys.exit(0)
if args == ["login", "status"]:
    sys.exit(1 if mode == "logged_out" else 0)
payload = json.load(sys.stdin)
with open(os.environ["FAKE_CODEX_LOG"], "a") as handle:
    handle.write(json.dumps({"args": args, "cwd": os.getcwd(), "payload": payload}) + "\n")
if mode == "timeout":
    time.sleep(60)
if mode == "exit":
    print("WARNING: PATH aliases: Operation not permitted (os error 1)", file=sys.stderr)
    print("private diagnostic credential must not reach reports", file=sys.stderr)
    sys.exit(9)
if mode == "permission":
    print("private diagnostic credential must not reach reports", file=sys.stderr)
    print("Error: Operation not permitted (os error 1)", file=sys.stderr)
    sys.exit(1)
answer = Path(args[args.index("--output-last-message") + 1])
if "market" in payload:
    market = payload["market"]
    result = {
        "snapshot_id": market["snapshot_id"], "as_of": market["as_of"],
        "decisions": [{"symbol": symbol, "target_weight": 0.1, "confidence": 0.95,
                       "rationale": "受控 CLI 协议验证，非真实模型"}
                      for symbol in market["features"]],
    }
    if mode == "wrong_snapshot":
        result["snapshot_id"] = "not-the-supplied-snapshot"
else:
    result = {"message": "仅依据所提供数据完成研究。"}
if mode != "missing":
    answer.write_text("{" if mode == "invalid_json" else json.dumps(result))
print(json.dumps({"type": "thread.started", "thread_id": "fixture-session"}))
print(json.dumps({"type": "item.completed", "item": {"type": "error", "message":
    "Code Mode is unavailable because code-mode host is disabled. "
    "Code mode will fail closed; enable `features.code_mode_host` "
    "and install `codex-code-mode-host`."}}))
if mode in {"warning", "rerouted"}:
    print(json.dumps({"type": "item.completed", "item": {"type": "error", "message":
        "model rerouted: fixture-model -> different-model" if mode == "rerouted" else
        "private diagnostic credential must not reach reports"}}))
print(json.dumps({"type": "turn.started"}))
print(json.dumps({"type": "item.completed", "item": {
    "type": "command_execution" if mode == "tool" else
    "private diagnostic credential must not reach reports" if mode == "unknown" else
    [] if mode == "invalid_type" else "agent_message"}}))
if mode != "unfinished":
    print(json.dumps({"type": "turn.completed",
                      "usage": {"input_tokens": 5, "cached_input_tokens": 2, "output_tokens": 3}}))
''')
    executable.chmod(0o700)
    monkeypatch.setenv("FAKE_CODEX_LOG", str(log))
    monkeypatch.delenv("QUANTBOT_LLM_BACKEND", raising=False)
    monkeypatch.delenv("QUANTBOT_LLM_MODEL", raising=False)
    config = load_config(environ={})
    config["llm"].update(backend="codex_cli", model="fixture-model", timeout_seconds=5)
    config["codex"].update(executable=str(executable), reasoning_effort="high")
    config["execution"]["initial_cash"] = 1000
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[llm]\nbackend="codex_cli"\nmodel="fixture-model"\ntimeout_seconds=5\n'
        f'[codex]\nexecutable={json.dumps(str(executable))}\nreasoning_effort="high"\n'
        '[execution]\ninitial_cash=1000\n'
    )
    yield config, config_path, log
    configure_default_agent(load_config(environ={}))


def crypto_input(config):
    stream = io.StringIO()
    rows = demo_rows("crypto", config, ["BTC/USDT", "ETH/USDT"])
    writer = csv.DictWriter(stream, rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


@pytest.mark.parametrize("entry", ["cli", "web"])
def test_codex_crypto_backtest_uses_risk_execution_journal_and_reconciliation(codex, tmp_path, entry):
    config, config_path, log = codex
    csv_data = crypto_input(config)
    if entry == "cli":
        data = tmp_path / "crypto.csv"
        data.write_text(csv_data)
        stdout = io.StringIO()
        code = main([
            "backtest", "--config", str(config_path), "--market", "crypto", "--provider", "ai",
            "--data", str(data), "--last-bars", "3", "--allow-model-calls",
            "--output", str(tmp_path / "result"),
        ], stdout=stdout, environ=os.environ)
        assert code == 0, stdout.getvalue()
        report = json.loads(stdout.getvalue())
    else:
        client = TestClient(create_app(config=config, runtime_dir=tmp_path / "web"))
        assert client.get("/api/quantbot/status").json()["model_backend"] == "codex_cli"
        response = client.post("/api/quantbot/allocate", json={
            "market": "crypto", "provider": "ai", "mode": "simulate",
            "csv_data": csv_data, "last_bars": 3, "allow_model_calls": True, "initial_cash": 1000,
        })
        assert response.status_code == 200, response.text
        report = response.json()
    assert report["execution_completed"] and report["candidate"]["fills"] > 0
    assert report["candidate"]["provider_calls"] == 2
    assert report["candidate"]["reported_tokens"] == 16
    for decision in report["candidate"]["decisions"]:
        raw = decision["raw_decision"]
        assert raw["provider"] == "codex_cli" and raw["model"] == "fixture-model"
        assert raw["response_id"] == "fixture-session"
    for label in ("candidate", "baseline"):
        replay = inspect_replay(report[label]["journal_path"])
        assert reconcile_replay(replay["records"])["status"] == "reconciled"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2 and calls[0]["cwd"] != calls[1]["cwd"]
    assert calls[0]["payload"]["market"]["as_of"] < calls[1]["payload"]["market"]["as_of"]
    for call in calls:
        assert set(call["payload"]) == {"market", "account", "risk_limits"}
        assert "--ignore-user-config" in call["args"] and "--ephemeral" in call["args"]
        assert "--no-daemon" in call["args"]
        assert call["args"][call["args"].index("--sandbox") + 1] == "read-only"
        assert 'web_search="disabled"' in call["args"]
        assert "features.shell_tool=false" in call["args"]
        assert "features.memories=false" in call["args"]
        assert not Path(call["cwd"]).exists()


@pytest.mark.parametrize("mode,expected", [
    ("exit", "退出码 9"), ("permission", "本机权限限制"), ("timeout", "超时"), ("tool", "工具"),
    ("missing", "结果文件"), ("unfinished", "完成记录"), ("invalid_json", None),
    ("wrong_snapshot", None), ("warning", "CLI 警告"), ("rerouted", "CLI 警告"),
    ("unknown", "unknown"), ("invalid_type", "条目类型无效"),
])
def test_codex_failure_does_not_accept_output_or_retry(codex, monkeypatch, mode, expected):
    config, _, log = codex
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    if mode == "timeout":
        config["llm"]["timeout_seconds"] = 0.5
    context, _ = market_context(MockExchange().get_candles("BTC/USDT", limit=100), ["BTC/USDT"])
    with pytest.raises((ProviderError, ValueError), match=expected) as error:
        decision_provider(config).decide(context, CryptoAccount(Decimal(1000), {}, {}), config)
    assert "private diagnostic credential" not in str(error.value)
    assert len(log.read_text().splitlines()) == 1
    call = json.loads(log.read_text())
    assert not Path(call["cwd"]).exists()


@pytest.mark.parametrize("mode,ready", [("valid", True), ("logged_out", False)])
def test_model_check_only_checks_local_installation_and_authentication(codex, monkeypatch, mode, ready):
    _, config_path, log = codex
    monkeypatch.setenv("FAKE_CODEX_MODE", mode)
    output = io.StringIO()
    assert main(["model-check", "--config", str(config_path)], stdout=output, environ=os.environ) == (
        0 if ready else 2
    )
    result = json.loads(output.getvalue())
    assert result["ready"] is ready
    assert result["model_called"] is False and result["model_access_verified"] is False
    assert not log.exists()


def test_ai_authorization_checked_before_codex_spawn(codex, tmp_path):
    config, config_path, log = codex
    data = tmp_path / "data.csv"
    data.write_text(crypto_input(config))
    output = io.StringIO()
    assert main([
        "backtest", "--config", str(config_path), "--market", "crypto", "--data", str(data),
        "--output", str(tmp_path / "denied"),
    ], stdout=output, environ=os.environ) == 2
    assert "允许模型调用" in output.getvalue()
    assert not log.exists() and not (tmp_path / "denied").exists()


def test_research_chat_and_stream_share_codex_configuration(codex, monkeypatch):
    config, _, log = codex
    configure_default_agent(config)
    monkeypatch.setenv("QUANTBOT_LLM_BACKEND", "invalid-backend")
    with pytest.raises(ValueError, match="llm.backend"):
        get_default_agent()
    assert not log.exists()
    monkeypatch.delenv("QUANTBOT_LLM_BACKEND")
    agent = get_default_agent()
    history = [ChatMessage(role="user", content="分析给定的历史行情")]
    assert asyncio.run(agent.chat(history)) == "仅依据所提供数据完成研究。"

    async def consume():
        return [part async for part in agent.stream(history)]

    assert asyncio.run(consume()) == ["仅依据所提供数据完成研究。"]
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0]["payload"]["messages"][-1]["content"] == history[0].content


def test_codex_config_is_explicit_and_rejects_automatic_fallback(codex):
    config, _, _ = codex
    assert load_config(environ={"QUANTBOT_LLM_BACKEND": "codex_cli"})["llm"]["backend"] == "codex_cli"
    config["llm"]["fallback_models"] = ["another-model"]
    with pytest.raises(ValueError, match="不自动重试"):
        validate_config(config)
    config["llm"]["fallback_models"] = []
    config["llm"]["model"] = ""
    with pytest.raises(ValueError, match="明确填写"):
        decision_provider(config)
