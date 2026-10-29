"""Observe the merged public chain, model failures, risk limits, and restart order identity."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
import json
import sqlite3
from threading import Thread

import pytest

from quantbot.ai.config import load_config
from quantbot.ai.domain import AccountState, DecisionBatch, strict_json, validate_decision
from quantbot.ai.market import DailyMarketData, session_time
from quantbot.ai.models import DemoProvider, OpenAICompatibleProvider, PriceActionProvider
from quantbot.ai.paper import AlpacaPaperClient, PAPER_URL, run_paper
from quantbot.ai.risk import build_plan
from quantbot.ai.simulation import simulate
from quantbot.ai.transport import JsonTransport, ProviderError
from quantbot.cli import _demo_rows, main
from quantbot.trading.domain import BrokerAccountSnapshot, MarginStatus


@pytest.fixture
def config(tmp_path):
    cfg = load_config(environ={})
    cfg["execution"]["runtime_dir"] = str(tmp_path / "runtime")
    return cfg


@pytest.fixture(scope="module")
def market():
    config = load_config(environ={})
    return DailyMarketData(_demo_rows(config), config)


@pytest.fixture(scope="module")
def context(market):
    return market.context(market.common_dates[-2])


def account(context, cash=100000, positions=None, blocked=False):
    positions = positions or {}
    equity = cash + sum(q * context.prices[s] for s, q in positions.items())
    broker = BrokerAccountSnapshot(
        "test_sim", context.as_of, equity, cash, blocked, False, MarginStatus.SIMULATED
    )
    return AccountState(cash, equity, positions, broker)


def response_payload(context, *, weight=0.2, confidence=0.95):
    return {
        "snapshot_id": context.snapshot_id,
        "as_of": context.as_of.isoformat(),
        "decisions": [
            {"symbol": s, "target_weight": weight, "confidence": confidence, "rationale": "test allocation"}
            for s in context.features
        ],
    }


class Provider:
    def __init__(self, weight=0.2):
        self.calls = 0
        self.weight = weight

    def decide(self, context, state, config):
        self.calls += 1
        return DecisionBatch(
            validate_decision(response_payload(context, weight=self.weight), context),
            "test_fixture",
            "not_a_model",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"llm": {"base_url": "http://external.example/v1"}},
        {"llm": {"base_url": "https://key:secret@example.test/v1"}},
        {"risk": {"max_gross_exposure": 1}},
        {"risk": {"min_confidence": True}},
        {"market": {"timeframe": "1m"}},
    ],
)
def test_config_rejects_invalid_contracts(tmp_path, change):
    path = tmp_path / "config.toml"
    path.write_text(
        "\n".join(
            f"[{section}]\n" + "\n".join(f"{k} = {json.dumps(v)}" for k, v in values.items())
            for section, values in change.items()
        )
    )
    with pytest.raises((ValueError, TypeError)):
        load_config(path, environ={})


def test_future_rows_do_not_change_asof_features_or_identity(config, market, context):
    rows = _demo_rows(config)
    for row in rows:
        if row["date"] > str(context.as_of.date()):
            row.update(open=900, high=1000, low=800, close=950)
    revised = DailyMarketData(rows, config).context(context.as_of.date())
    assert revised.snapshot_id == context.snapshot_id
    assert revised.payload() == context.payload()
    assert market.context(market.common_dates[-1]).snapshot_id != context.snapshot_id


def test_missing_signal_day_is_rejected(config, context):
    rows = [
        r
        for r in _demo_rows(config)
        if not (r["symbol"] == "AAPL" and r["date"] == str(context.as_of.date()))
    ]
    with pytest.raises(ValueError, match="缺少完整"):
        DailyMarketData(rows, config).context(context.as_of.date())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "0.2", -0.1, 1.1])
def test_model_weight_validation(context, bad):
    payload = response_payload(context)
    payload["decisions"][0]["target_weight"] = bad
    with pytest.raises(ValueError):
        validate_decision(payload, context)


@pytest.mark.parametrize(
    "mutation",
    ["unknown_symbol", "duplicate_symbol", "stale_snapshot", "future_asof", "extra_tool", "missing_symbol"],
)
def test_model_cannot_expand_decision_authority(context, mutation):
    payload = response_payload(context)
    if mutation == "unknown_symbol":
        payload["decisions"][0]["symbol"] = "UNKNOWN"
    elif mutation == "duplicate_symbol":
        payload["decisions"][0]["symbol"] = payload["decisions"][1]["symbol"]
    elif mutation == "stale_snapshot":
        payload["snapshot_id"] = "old"
    elif mutation == "future_asof":
        payload["as_of"] = (context.as_of + timedelta(days=1)).isoformat()
    elif mutation == "extra_tool":
        payload["decisions"][0]["execute"] = "buy everything"
    else:
        payload["decisions"].pop()
    with pytest.raises(ValueError):
        validate_decision(payload, context)


@pytest.mark.parametrize("encoded", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}'])
def test_strict_json_rejects_ambiguous_payload(encoded):
    with pytest.raises(ValueError):
        strict_json(encoded)


def test_ai_can_buy_without_a_price_action_entry_but_cannot_bypass_limits(config, context):
    features = {
        s: {**v, "pa_accepted": False, "pa_reason": "no_price_action_trigger"}
        for s, v in context.features.items()
    }
    context = replace(context, features=features)
    state = account(context)
    decision = DecisionBatch(
        validate_decision(response_payload(context, weight=1), context), "test_fixture", "fake"
    )
    plan = build_plan(context, state, decision, config)
    assert plan.intents
    assert all(i.side == "buy" for i in plan.intents)
    total = 0
    for intent in plan.intents:
        notional = intent.quantity * plan.limit_prices[intent.symbol]
        assert notional <= state.equity * config["risk"]["max_weight"]
        assert (
            intent.quantity * (plan.limit_prices[intent.symbol] - plan.stops[intent.symbol])
            <= state.equity * config["risk"]["risk_per_trade"]
        )
        total += notional
    assert total <= min(
        state.cash - state.equity * config["risk"]["cash_buffer"],
        state.equity * config["risk"]["max_gross_exposure"],
    )


def test_cash_and_portfolio_heat_reserve_is_enforced(config, context):
    config["risk"]["max_portfolio_heat"] = 0.0001
    state = account(context)
    decision = Provider().decide(context, state, config)
    plan = build_plan(context, state, decision, config)
    assert (
        sum(i.quantity * (plan.limit_prices[i.symbol] - plan.stops[i.symbol]) for i in plan.intents)
        <= state.equity * 0.0001
    )


def test_low_confidence_holds_and_account_blocked_stops_orders(config, context):
    state = account(context, positions={"AAPL": 10})
    low = DecisionBatch(
        validate_decision(response_payload(context, weight=0, confidence=0.1), context), "test", "test"
    )
    assert not build_plan(context, state, low, config).intents
    blocked = account(context, blocked=True)
    assert not build_plan(context, blocked, Provider().decide(context, blocked, config), config).intents


def test_current_weight_holds_for_ai_and_price_action_without_spurious_sell(config, context):
    features = {
        s: {**v, "pa_accepted": False, "channel_exit": False}
        for s, v in context.features.items()
    }
    context = replace(context, features=features)
    state = account(context, positions={"AAPL": 10})
    payload = response_payload(context, weight=0)
    for allocation in payload["decisions"]:
        symbol = allocation["symbol"]
        allocation["target_weight"] = state.positions.get(symbol, 0) * context.prices[symbol] / state.equity
    ai_decision = DecisionBatch(validate_decision(payload, context), "test_fixture", "hold")
    pa_decision = PriceActionProvider().decide(context, state, config)
    for decision in (ai_decision, pa_decision):
        assert not build_plan(context, state, decision, config).intents


def test_rebalance_values_existing_shares_at_market_and_reserves_only_new_buy_limits(config, context):
    features = {s: {**v, "close": 100.0, "suggested_stop": 95.0} for s, v in context.features.items()}
    context = replace(context, features=features)
    state = account(context, positions={"AAPL": 10})
    payload = response_payload(context, weight=0)
    aapl = next(a for a in payload["decisions"] if a["symbol"] == "AAPL")
    aapl["target_weight"] = 500 / state.equity
    reduction = build_plan(
        context, state, DecisionBatch(validate_decision(payload, context), "test_fixture", "reduce"), config
    )
    assert [(i.symbol, i.side, i.quantity) for i in reduction.intents] == [("AAPL", "sell", 5)]
    aapl["target_weight"] = 1200 / state.equity
    increase = build_plan(
        context, state, DecisionBatch(validate_decision(payload, context), "test_fixture", "increase"), config
    )
    assert [(i.symbol, i.side, i.quantity) for i in increase.intents] == [("AAPL", "buy", 1)]
    assert 10 * context.prices["AAPL"] + increase.limit_prices["AAPL"] <= 1200


def test_no_key_has_no_demo_fallback(config):
    config["llm"]["model"] = "test-model"
    with pytest.raises(ValueError, match="缺少模型密钥"):
        OpenAICompatibleProvider(config, environ={})


@pytest.mark.parametrize(
    "finish,extra",
    [("length", {}), ("stop", {"refusal": "cannot"}), ("stop", {"tool_calls": [{"id": "trade"}]})],
)
def test_incomplete_or_tool_model_answers_rejected(config, context, finish, extra):
    class Transport:
        def post_json(self, *args, **kwargs):
            return {
                "choices": [
                    {
                        "finish_reason": finish,
                        "message": {"content": json.dumps(response_payload(context)), **extra},
                    }
                ]
            }

    config["llm"]["model"] = "test-model"
    provider = OpenAICompatibleProvider(
        config, environ={"QUANTBOT_LLM_API_KEY": "test-placeholder"}, transport=Transport()
    )
    with pytest.raises(ValueError):
        provider.decide(context, account(context), config)


def test_real_http_compatibility_transport_and_cli(config, market, context, tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append((self.path, request))
            inputs = json.loads(request["messages"][1]["content"])
            payload = response_payload(context)
            assert inputs["market"]["snapshot_id"] == context.snapshot_id
            encoded = json.dumps(
                {
                    "id": "local-test",
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload)}}],
                    "usage": {"total_tokens": 10},
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    path = tmp_path / "bars.csv"
    config_path = tmp_path / "quantbot.toml"
    config_path.write_text('[llm]\nbackend = "openai_compatible"\n')
    import csv
    from quantbot.ai.market import FIELDS

    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, FIELDS)
        writer.writeheader()
        writer.writerows(_demo_rows(config))
    try:
        env = {
            "QUANTBOT_LLM_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1",
            "QUANTBOT_LLM_MODEL": "local-test-model",
        }
        output = tmp_path / "decision.json"
        stream = StringIO()
        code = main(
            ["decide", "--config", str(config_path), "--data", str(path),
             "--as-of", str(context.as_of.date()), "--output", str(output)],
            stdout=stream,
            environ=env,
        )
        assert code == 0, stream.getvalue()
        result = json.loads(output.read_text())
        assert result["provider"] == "openai_compatible" and result["orders"]
        assert received[0][0] == "/v1/chat/completions"
        assert received[0][1]["response_format"] == {"type": "json_object"}
        assert "tool_calls" not in received[0][1]
        assert output.with_suffix(".sqlite").is_file()
        assert (
            main(
                [
                    "decide",
                    "--config",
                    str(config_path),
                    "--data",
                    str(path),
                    "--as-of",
                    str(context.as_of.date()),
                    "--output",
                    str(output),
                ],
                stdout=StringIO(),
                environ=env,
            )
            == 2
        )
        assert len(received) == 1  # Reject an existing output before consuming another provider call.
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_error_body_never_exposes_credentials():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"secret-placeholder-token")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProviderError) as error:
            JsonTransport().get_json(f"http://127.0.0.1:{server.server_port}/test")
        assert error.value.status == 401
        assert "secret-placeholder-token" not in str(error.value)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_simulation_uses_next_open_and_keeps_evidence(config, market, tmp_path):
    day = market.common_dates[-3]
    output = tmp_path / "replay"
    result = simulate(market, config, DemoProvider(), output, day)
    assert result["fills"] > 0 and result["real_model_calls"] == 0
    with sqlite3.connect(output / "journal.sqlite") as connection:
        fills = connection.execute("SELECT occurred_at FROM fills").fetchall()
        assert all(value > session_time(day).isoformat() for (value,) in fills)
        assert connection.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] > 0
        assert (
            connection.execute("SELECT COUNT(*) FROM orders WHERE status='state_committed'").fetchone()[0] > 0
        )
    assert not result["errors"]


def test_simulation_gap_limit_cancels_without_negative_cash(config, market, tmp_path):
    first = market.common_dates[-2]
    rows = _demo_rows(config)
    for row in rows:
        if row["date"] > str(first):
            row.update(open=500, high=501, low=499, close=500)
    result = simulate(DailyMarketData(rows, config), config, DemoProvider(), tmp_path / "gap", first)
    assert result["fills"] == 0
    assert result["final_equity"] == config["execution"]["initial_cash"]


def test_model_outage_holds_and_emits_no_orders(config, market, tmp_path):
    class Failed:
        def decide(self, *args):
            raise ProviderError("test timeout")

    output = tmp_path / "outage"
    result = simulate(market, config, Failed(), output, market.common_dates[-3])
    assert result["errors"] and result["fills"] == 0
    with sqlite3.connect(output / "journal.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


class PaperTransport:
    """In-memory broker API, no connection to any account."""

    def __init__(self, market, *, timeout_after_accept=False):
        self.cash = 100000.0
        self.market = market
        self.positions = {}
        self.orders = {}
        self.protections = []
        self.posts = []
        self.timeout_after_accept = timeout_after_accept
        self.clock_day = market.common_dates[-1] + timedelta(days=3)
        self.fail_before_accept = False
        self.fail_protection_after_accept = False

    def get_json(self, url, headers=None, params=None):
        assert url.startswith(PAPER_URL)
        if url.endswith("/clock"):
            return {"is_open": True, "timestamp": session_time(self.clock_day, close=False).isoformat()}
        if url.endswith("/calendar"):
            return [{"date": str(day)} for day in self.market.common_dates[-5:]]
        if url.endswith("/account"):
            return {
                "id": "test-account",
                "equity": str(self.cash + sum(q * p for q, p in self.positions.values())),
                "cash": str(self.cash),
                "buying_power": str(self.cash),
                "status": "ACTIVE",
                "multiplier": "1",
                "trading_blocked": False,
                "account_blocked": False,
                "trade_suspended_by_user": False,
                "shorting_enabled": False,
            }
        if url.endswith("/positions"):
            return [{"symbol": s, "qty": str(q)} for s, (q, _) in self.positions.items() if q]
        if url.endswith("orders:by_client_order_id"):
            if params["client_order_id"] not in self.orders:
                raise ProviderError("not found", 404)
            return self.orders[params["client_order_id"]]
        if url.endswith("/orders"):
            return [o for o in self.protections if o["status"] not in {"filled", "canceled"}]
        if "/v2/orders/" in url:
            return next(o for o in self.protections if o["id"] == url.rsplit("/", 1)[1])
        raise AssertionError(url)

    def delete_json(self, url, headers=None):
        order = next(o for o in self.protections if o["id"] == url.rsplit("/", 1)[1])
        order["status"] = "canceled"
        return {}

    def post_json(self, url, headers=None, payload=None):
        assert url == PAPER_URL + "/v2/orders"
        self.posts.append(deepcopy(payload))
        if self.fail_before_accept:
            raise ProviderError("unknown before accept")
        quantity = int(payload["qty"])
        symbol = payload["symbol"]
        if payload["type"] == "stop":
            leg = {"id": "id-" + payload["client_order_id"], **payload, "filled_qty": "0", "status": "new"}
            self.protections.append(leg)
            self.orders[payload["client_order_id"]] = leg
            if self.fail_protection_after_accept:
                self.fail_protection_after_accept = False
                raise ProviderError("unknown after stop accepted")
            return leg
        if payload["side"] == "buy":
            assert payload["order_class"] == "oto" and "stop_loss" in payload
            assert payload["time_in_force"] == "gtc"
            price = float(payload["limit_price"])
            self.cash -= quantity * price
            before = self.positions.get(symbol, (0, price))[0]
            self.positions[symbol] = (before + quantity, price)
            leg = {
                "id": "leg-" + payload["client_order_id"],
                "symbol": symbol,
                "qty": str(quantity),
                "filled_qty": "0",
                "type": "stop",
                "side": "sell",
                "stop_price": payload["stop_loss"]["stop_price"],
                "status": "new",
            }
            self.protections.append(leg)
        else:
            before, price = self.positions[symbol]
            assert quantity <= before
            self.cash += quantity * price
            self.positions[symbol] = (before - quantity, price)
            leg = None
        response = {
            "id": "id-" + payload["client_order_id"],
            "client_order_id": payload["client_order_id"],
            "symbol": symbol,
            "qty": str(quantity),
            "side": payload["side"],
            "type": payload["type"],
            "status": "filled",
            "filled_qty": str(quantity),
            "filled_avg_price": str(price),
            "legs": [leg] if leg else [],
        }
        self.orders[payload["client_order_id"]] = response
        if self.timeout_after_accept:
            self.timeout_after_accept = False
            raise ProviderError("unknown after accept")
        return response


@pytest.fixture
def admitted_paper_cycle(monkeypatch):
    # These tests isolate broker recovery after admission. The public admission
    # boundary is exercised without this patch in test_ai_admission.py.
    monkeypatch.setattr("quantbot.ai.paper.require_broker_paper_admission", lambda *args: {})


def test_paper_oto_and_restart_never_duplicate_orders(config, market, admitted_paper_cycle):
    transport = PaperTransport(market)
    client = AlpacaPaperClient({"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}, transport)
    provider = Provider()
    first = run_paper(market, config, provider, confirm=True, client=client)
    assert first["status"] == "submitted" and first["submitted_orders"] > 0
    before = len(transport.posts)
    second = run_paper(market, config, provider, confirm=True, client=client)
    assert second["status"] == "already_processed"
    assert len(transport.posts) == before and provider.calls == 1
    assert client.base_url == PAPER_URL


def test_paper_uncertain_submit_is_queried_before_any_new_model_call(config, market, admitted_paper_cycle):
    transport = PaperTransport(market, timeout_after_accept=True)
    client = AlpacaPaperClient({"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}, transport)
    provider = Provider()
    first = run_paper(market, config, provider, confirm=True, client=client)
    assert first["status"] == "recovery_hold"
    second = run_paper(market, config, provider, confirm=True, client=client)
    assert second["status"] == "already_processed"
    assert len(transport.posts) == 1 and provider.calls == 1


def test_paper_unmanaged_order_holds_before_model(config, market, admitted_paper_cycle):
    transport = PaperTransport(market)
    transport.protections = [
        {"id": "foreign", "symbol": "AAPL", "type": "limit", "side": "buy", "status": "new"}
    ]
    provider = Provider()
    result = run_paper(
        market,
        config,
        provider,
        confirm=True,
        client=AlpacaPaperClient({"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}, transport),
    )
    assert result["status"] == "recovery_hold" and provider.calls == 0 and not transport.posts


def test_paper_without_confirmation_or_with_demo_cannot_connect(config, market):
    with pytest.raises(ValueError, match="confirm-paper"):
        run_paper(market, config, Provider(), confirm=False)
    with pytest.raises(ValueError, match="演示规则"):
        run_paper(market, config, DemoProvider(), confirm=True)


def _next_session(market, config, transport):
    day = transport.clock_day
    rows = _demo_rows(config)
    for symbol, frame in market.frames.items():
        row = frame.iloc[-1]
        rows.append(
            {
                "date": str(day),
                "symbol": symbol,
                "open": float(row.close),
                "high": float(row.close) + 0.5,
                "low": float(row.close) - 0.5,
                "close": float(row.close),
                "volume": 1000000,
            }
        )
    advanced = DailyMarketData(rows, config)
    transport.market = advanced
    transport.clock_day = day + timedelta(days=1)
    return advanced


def test_partial_paper_exit_restores_stops_then_restart_is_idempotent(config, market, admitted_paper_cycle):
    transport = PaperTransport(market)
    client = AlpacaPaperClient({"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}, transport)
    provider = Provider()
    assert run_paper(market, config, provider, confirm=True, client=client)["status"] == "submitted"
    next_market = _next_session(market, config, transport)
    provider.weight = 0.01
    result = run_paper(next_market, config, provider, confirm=True, client=client)
    assert result["status"] == "submitted" and result["submitted_orders"] == 3
    active = [o for o in transport.protections if o["status"] == "new"]
    assert len(active) == 3 and all(o["client_order_id"].startswith("qp-") for o in active)
    assert all(int(o["qty"]) == transport.positions[o["symbol"]][0] for o in active)
    assert (
        run_paper(next_market, config, provider, confirm=True, client=client)["status"] == "already_processed"
    )
    assert provider.calls == 2


def test_lost_replacement_stop_response_is_queried_without_duplicate_stop(config, market, admitted_paper_cycle):
    transport = PaperTransport(market)
    client = AlpacaPaperClient({"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}, transport)
    provider = Provider()
    run_paper(market, config, provider, confirm=True, client=client)
    next_market = _next_session(market, config, transport)
    provider.weight = 0.01
    transport.fail_protection_after_accept = True
    assert run_paper(next_market, config, provider, confirm=True, client=client)["status"] == "recovery_hold"
    before = len(transport.posts)
    assert (
        run_paper(next_market, config, provider, confirm=True, client=client)["status"] == "already_processed"
    )
    assert len(transport.posts) == before and provider.calls == 2


def test_unknown_404_after_attempt_never_reposts_or_calls_model(config, market, admitted_paper_cycle):
    transport = PaperTransport(market)
    transport.fail_before_accept = True
    client = AlpacaPaperClient({"ALPACA_API_KEY": "test", "ALPACA_SECRET_KEY": "test"}, transport)
    provider = Provider()
    assert run_paper(market, config, provider, confirm=True, client=client)["status"] == "recovery_hold"
    second = run_paper(market, config, provider, confirm=True, client=client)
    assert second["status"] == "recovery_hold" and second["reason"] == "order_status_unknown"
    assert len(transport.posts) == 1 and provider.calls == 1


def test_risk_uses_existing_broker_stop_instead_of_inventing_tighter_one(config, context):
    state = account(context, positions={"AAPL": 20})
    stop = context.prices["AAPL"] / 2
    state = replace(state, stops={"AAPL": stop})
    plan = build_plan(context, state, Provider().decide(context, state, config), config)
    assert plan.stops["AAPL"] == stop


def test_primary_cli_does_not_expose_legacy_live_command():
    stream = StringIO()
    assert main(["price-action", "live"], stdout=stream, environ={}) == 2
    assert "研究" in stream.getvalue()
