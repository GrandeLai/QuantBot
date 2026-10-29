"""Exercise shared model routing, Decimal paper execution, and HTTP write boundaries."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.config import load_config
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.gateway import RoutedDecisionProvider
from quantbot.ai.models import DemoProvider
from quantbot.ai.transport import ProviderError
from quantbot.crypto.ai import CryptoAccount, market_context, run_spot, spot_plan
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from quantbot.web.app import create_app


def candles():
    return MockExchange().get_candles("BTC/USDT", limit=100)


@pytest.fixture
def config():
    return load_config(environ={})


def test_crypto_paper_decimal_cash_and_repeated_snapshot(config, tmp_path):
    ledger = tmp_path / "paper.sqlite"
    first = run_spot(config, symbols=["BTC/USDT"], demo=True, mode="paper", ledger_path=ledger)
    assert first["fills"] and Decimal(first["fills"][0]["quantity"]) < 1
    assert first["account"]["cash"] >= 100
    assert first["model_called"] is False
    again = run_spot(config, symbols=["BTC/USDT"], demo=True, mode="paper", ledger_path=ledger)
    assert again["idempotent_replay"] is True
    assert again["account"] == first["account"]


def test_hold_does_not_round_off_one_lot(config):
    context, _ = market_context(candles(), ["BTC/USDT"])
    q = Decimal("0.0039")
    account = CryptoAccount(Decimal("800"), {"BTC/USDT": q}, {"BTC/USDT": Decimal("49000")})
    weight = account.payload(context)["current_weights"]["BTC/USDT"]
    decision = DecisionBatch((Allocation("BTC/USDT", weight, 1, "hold"),), "fixture", "fixture")
    meta = {i.symbol: i for i in MockExchange().list_instruments("spot")}
    assert spot_plan(context, account, decision, config, meta)["orders"] == []


@pytest.mark.parametrize("mutation", ["future", "incomplete", "duplicate", "ohlc", "gap"])
def test_crypto_rejects_unusable_candles(mutation):
    bars = candles()
    if mutation == "future":
        bars[-1] = replace(bars[-1], timestamp=bars[-1].timestamp + timedelta(days=10000))
    if mutation == "incomplete":
        bars[-1] = replace(bars[-1], complete=False)
    if mutation == "duplicate":
        bars.append(bars[-1])
    if mutation == "ohlc":
        bars[-1] = replace(bars[-1], low=bars[-1].high + 1)
    if mutation == "gap":
        bars.pop(70)
    with pytest.raises(ValueError):
        market_context(bars, ["BTC/USDT"])


def test_model_outage_preserves_existing_protective_stop(config, tmp_path):
    ledger = tmp_path / "paper.sqlite"
    bars = candles()
    first = run_spot(
        config, symbols=["BTC/USDT"], candles=bars, mode="paper", ledger_path=ledger, provider=DemoProvider()
    )
    held = first["account"]["positions"]["BTC/USDT"]
    last = bars[-1]
    next_bar = replace(
        last,
        timestamp=last.timestamp + timedelta(minutes=15),
        open=Decimal("48000"),
        high=Decimal("48500"),
        low=Decimal("47000"),
        close=Decimal("48000"),
    )

    class Broken:
        def decide(self, *args):
            raise ProviderError("offline fixture outage")

    result = run_spot(
        config,
        symbols=["BTC/USDT"],
        candles=[*bars, next_bar],
        mode="paper",
        ledger_path=ledger,
        provider=Broken(),
    )
    assert result["status"] == "hold"
    assert result["plan"]["orders"] == []
    assert result["account"]["positions"] == {}
    assert result["fills"][0]["reason"] == "protective_stop"
    assert result["fills"][0]["quantity"] == held


def test_new_buy_cannot_trigger_on_prior_candle_low(config, tmp_path):
    bars = candles()
    bars[-1] = replace(bars[-1], low=Decimal("45000"))
    result = run_spot(
        config,
        symbols=["BTC/USDT"],
        candles=bars,
        demo=True,
        mode="paper",
        ledger_path=tmp_path / "paper.sqlite",
    )
    assert all(f["reason"] == "allocation" for f in result["fills"])


def test_paper_rejects_changed_configuration(config, tmp_path):
    ledger = tmp_path / "paper.sqlite"
    run_spot(config, demo=True, mode="paper", ledger_path=ledger)
    config["risk"]["max_weight"] = 0.1
    with pytest.raises(ValueError, match="配置"):
        run_spot(config, demo=True, mode="paper", ledger_path=ledger)


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/crypto/orders", "post"),
        ("/crypto/futures/orders", "post"),
        ("/api/crypto/orders/old-id", "delete"),
        ("/api/trading/orders", "post"),
        ("/api/trading/orders/old-id", "delete"),
        ("/api/security/keys/openai", "post"),
    ],
)
def test_legacy_writes_rejected_before_broker_creation(config, tmp_path, monkeypatch, path, method):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not construct a broker")

    monkeypatch.setattr("quantbot.stocks.api.trading.get_trading_provider", forbidden)
    monkeypatch.setattr("quantbot.crypto.api.crypto.get_okx_provider", forbidden)
    c = TestClient(create_app(config=config, runtime_dir=tmp_path))
    assert c.request(method, path, json={}).status_code == 403


def test_http_demo_and_crypto_paper_confirmation(config, tmp_path):
    c = TestClient(create_app(config=config, runtime_dir=tmp_path))
    assert c.get("/api/quantbot/status").json()["legacy_writes_enabled"] is False
    assert c.post("/api/quantbot/allocate", json={"provider": "ai"}).status_code == 400
    stock = c.post("/api/quantbot/allocate", json={"provider": "demo"}).json()
    assert stock["plan"]["model"] == "offline_fixture" and stock["model_called"] is False
    args = {"market": "crypto", "provider": "demo", "mode": "paper", "initial_cash": 1000}
    assert c.post("/api/quantbot/allocate", json=args).status_code == 400
    args["confirm_paper"] = True
    assert c.post("/api/quantbot/allocate", json=args).json()["fills"]
    assert c.post("/api/quantbot/allocate", json=args).json()["idempotent_replay"] is True
    catalog = c.get("/api/quantbot/crypto/strategies").json()["strategy_catalog"]["strategies"]
    assert len(catalog) == 13 and all(not s["live_supported"] for s in catalog)


def test_http_broker_paper_shares_cli_account_directory(config, tmp_path, monkeypatch):
    import csv
    import io
    from quantbot.cli import _demo_rows

    config["execution"]["runtime_dir"] = str(tmp_path / "shared-account")
    received = {}

    def inspect(market, selected, provider, *, confirm, admission):
        received.update(selected["execution"])
        assert admission is None
        return {"status": "inspected"}

    monkeypatch.setattr("quantbot.ai.paper.run_paper", inspect)
    data = io.StringIO()
    writer = csv.DictWriter(data, ["date", "symbol", "open", "high", "low", "close", "volume"])
    writer.writeheader()
    writer.writerows(_demo_rows(config))
    response = TestClient(create_app(config=config)).post(
        "/api/quantbot/allocate",
        json={
            "provider": "price_action",
            "mode": "paper",
            "confirm_paper": True,
            "csv_data": data.getvalue(),
            "initial_cash": 123,
        },
    )
    assert response.status_code == 200
    assert received["runtime_dir"] == config["execution"]["runtime_dir"]
    assert received["initial_cash"] == config["execution"]["initial_cash"]


def test_fallback_uses_actual_selected_model_through_local_http(config):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body["model"])
            self.send_response(503 if body["model"] == "primary" else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if body["model"] == "primary":
                self.wfile.write(b"{}")
                return
            market = json.loads(body["messages"][1]["content"])["market"]
            answer = {
                "snapshot_id": market["snapshot_id"],
                "as_of": market["as_of"],
                "decisions": [
                    {"symbol": s, "target_weight": 0.1, "confidence": 1, "rationale": "fixture"}
                    for s in market["features"]
                ],
            }
            self.wfile.write(
                json.dumps(
                    {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}]}
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config["llm"].update(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            model="primary",
            fallback_models=["fallback"],
        )
        provider = RoutedDecisionProvider(config, environ={})
        context, _ = market_context(candles(), ["BTC/USDT"])
        decision = provider.decide(context, CryptoAccount(Decimal(1000), {}, {}), config)
        assert calls == ["primary", "fallback"] and decision.model == "fallback"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
