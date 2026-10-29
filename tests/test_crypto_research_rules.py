"""加密研究候选的时点、持仓语义、风险边界与明确离线入口。"""

from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import io
import json

import pytest

from quantbot.ai.config import load_config
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.feeds import Snapshot
from quantbot.ai.models import CryptoRuleProvider, DemoProvider
from quantbot.ai.runtime import prepare
from quantbot.cli import main
from quantbot.crypto.ai import CryptoAccount, spot_plan
from quantbot.crypto.runtime.exchanges.mock import MockExchange
from tests.test_ai_runtime import rows_for, write_rows


@pytest.fixture
def inputs():
    config = load_config(environ={})
    config["market"]["symbols"] = ["BTC/USDT"]
    config["execution"].update(initial_cash=1000, cost_bps=15)
    rows = rows_for("crypto", 70)
    snapshot = Snapshot("crypto", rows, config, "csv")
    context = snapshot.context(snapshot.times[50])
    instruments = {i.symbol: i for i in MockExchange().list_instruments("spot")}
    return config, rows, snapshot, context, instruments


def test_features_use_closed_history_and_channels_exclude_signal_bar(inputs):
    config, rows, snapshot, context, _ = inputs
    v = context.features["BTC/USDT"]
    assert v["prior_high_20"] == max(r["high"] for r in rows[30:50])
    assert v["prior_low_10"] == min(r["low"] for r in rows[40:50])
    assert v["momentum_5"] == pytest.approx(rows[50]["close"] / rows[45]["close"] - 1)
    changed = deepcopy(rows)
    for row in changed[51:]:
        row.update(open=90000, close=90000, low=80000, high=100000)
    assert Snapshot("crypto", changed, config, "csv").context(snapshot.times[50]).payload() == context.payload()
    # 当前信号 K 线的 high/low 不得改变用于判断突破的先前通道。
    changed[50].update(high=100000, low=100)
    revised = Snapshot("crypto", changed, config, "csv").context(snapshot.times[50])
    assert revised.features["BTC/USDT"]["prior_high_20"] == v["prior_high_20"]
    assert revised.features["BTC/USDT"]["prior_low_10"] == v["prior_low_10"]


@pytest.mark.parametrize("rule,signal,exit_signal", [
    ("momentum_hold", {"momentum": 0.01}, {"momentum": 0}),
    ("trend_fast", {"ema_5": 32000, "ema_20": 30000, "momentum_5": 0.01}, {"ema_20": 32000}),
    ("breakout", {"prior_high_20": 30000, "ema_10": 30000, "ema_30": 29000}, {"ema_10": 32000}),
    ("pullback", {"ema_10": 30000, "ema_30": 29000, "rsi_14": 40}, {"rsi_14": 65}),
])
def test_rules_enter_once_hold_quantity_and_exit_through_shared_risk(inputs, rule, signal, exit_signal):
    config, _, _, context, instruments = inputs
    config["strategy"]["crypto_rule"] = rule
    context = replace(context, features={"BTC/USDT": {**context.features["BTC/USDT"], **signal}})
    provider = CryptoRuleProvider()
    empty = CryptoAccount(D(1000), {}, {})
    decision = provider.decide(context, empty, config)
    entry = spot_plan(context, empty, decision, config, instruments)
    assert len(entry["orders"]) == 1 and entry["orders"][0]["side"] == "buy"
    order = entry["orders"][0]
    qty, price, stop = (D(order[key]) for key in ("quantity", "price", "stop"))
    assert qty * price <= D(1000) * D(str(config["risk"]["max_weight"]))
    assert qty * (price * D("1.0015") - stop) <= D(1000) * D(str(config["risk"]["risk_per_trade"]))
    # 仓位低于目标也只持有现有数量，避免每天补仓；浮点权重回转不应产生碎单。
    held = CryptoAccount(D(950), {"BTC/USDT": D("0.001")}, {"BTC/USDT": D(29000)})
    hold = provider.decide(context, held, config)
    assert hold.allocations[0].target_weight == held.payload(context)["current_weights"]["BTC/USDT"]
    assert spot_plan(context, held, hold, config, instruments)["orders"] == []
    exiting = replace(context, features={"BTC/USDT": {**context.features["BTC/USDT"], **exit_signal}})
    exit_plan = spot_plan(exiting, held, provider.decide(exiting, held, config), config, instruments)
    assert len(exit_plan["orders"]) == 1
    assert exit_plan["orders"][0]["side"] == "sell" and D(exit_plan["orders"][0]["quantity"]) == D("0.001")


def test_hold_still_trims_above_cap_and_rounding_is_not_reported_as_risk_rejection(inputs):
    config, _, _, context, instruments = inputs
    held = CryptoAccount(D(100), {"BTC/USDT": D("0.01")}, {"BTC/USDT": D(29000)})
    decision = CryptoRuleProvider().decide(context, held, config)
    plan = spot_plan(context, held, decision, config, instruments)
    assert plan["orders"][0]["side"] == "sell"
    assert D(plan["orders"][0]["quantity"]) > 0
    held = CryptoAccount(D(950), {"BTC/USDT": D("0.001")}, {"BTC/USDT": D(29000)})
    step = instruments["BTC/USDT"].min_size
    equity = D(str(held.payload(context)["equity"]))
    weight = (D("0.001") + step / 2) * D(str(context.prices["BTC/USDT"])) / equity
    request = DecisionBatch((Allocation("BTC/USDT", float(weight), 1, "rounding check"),), "test", "test")
    plan = spot_plan(context, held, request, config, instruments)
    assert plan["orders"] == [] and plan["adjustments"] == ["BTC/USDT:quantity_step_no_change"]
    # 真正不足一个最小交易单位的风险预算仍明确拒绝。
    config["risk"]["risk_per_trade"] = 0.000000001
    empty = CryptoAccount(D(1000), {}, {})
    plan = spot_plan(context, empty, DemoProvider().decide(context, empty, config), config, instruments)
    assert plan["orders"] == [] and "BTC/USDT:cash_or_risk_budget_rejected" in plan["adjustments"]


def test_cli_crypto_rules_finishes_without_model_and_keeps_demo_baseline(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("deterministic candidate must not construct a model")

    monkeypatch.setattr("quantbot.ai.runtime.decision_provider", forbidden)
    rows = rows_for("crypto", 70)
    path = tmp_path / "crypto.csv"
    write_rows(path, rows)
    config = tmp_path / "rules.toml"
    config.write_text('[strategy]\ncrypto_rule="trend_fast"\n[execution]\ninitial_cash=1000\ncost_bps=15\n')
    output = io.StringIO()
    code = main(["backtest", "--market", "crypto", "--provider", "crypto_rules", "--config", str(config),
                 "--data", str(path), "--last-bars", "10", "--output", str(tmp_path / "report")],
                stdout=output, environ={})
    assert code == 0, output.getvalue()
    report = json.loads(output.getvalue())
    assert report["passed"] and report["model_called"] is False
    assert report["candidate"]["fills"] > 0
    assert {d["provider"] for d in report["candidate"]["decisions"]} == {"crypto_rules"}
    assert {d["provider"] for d in report["baseline"]["decisions"]} == {"demo_rules"}


def test_rules_reject_wrong_market_and_unknown_configuration(tmp_path):
    config = load_config(environ={})
    with pytest.raises(ValueError, match="仅支持加密现货"):
        prepare({"provider": "crypto_rules", "market": "stocks", "data_path": "unused.csv"}, config)
    path = tmp_path / "bad.toml"
    path.write_text('[strategy]\ncrypto_rule="typo"\n')
    with pytest.raises(ValueError, match="crypto_rule"):
        load_config(path, environ={})
