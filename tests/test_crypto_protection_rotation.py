"""冷却、强弱轮动和独立保护订单的可观察行为；不调用模型或交易账户。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json

import pytest

from quantbot.ai.config import load_config, validate_config
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import replay
from quantbot.ai.feeds import Snapshot
from quantbot.ai.local_execution import account_view, advance, create_plan, new_account, queue_plan
from quantbot.ai.models import CryptoRuleProvider
from quantbot.ai.reconciliation import reconcile_replay
from quantbot.ai.replay_evidence import inspect_replay
from quantbot.ai.runtime import prepare
from quantbot.ai.transport import ProviderError
from quantbot.crypto.ai import CryptoAccount, run_spot


def setup():
    config = load_config(environ={})
    config["execution"].update(initial_cash=1000, cost_bps=15)
    config["risk"].update(crypto_stop_loss_pct=0.03, crypto_take_profit_pct=0.06)
    spec, config = prepare({"market": "crypto", "symbols": ["BTC/USDT"], "provider": "crypto_rules",
                            "bar": "1d", "data_path": "unused.csv", "max_daily_loss": 0.02,
                            "max_drawdown": 0.05}, config)
    rows = [{"timestamp": (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=i)).isoformat(),
             "symbol": "BTC/USDT", "bar": "1d", "open": 100, "close": 100, "low": 99, "high": 101,
             "volume": 1000} for i in range(70)]
    return config, spec, rows


def held_state(snapshot, config, spec):
    context = snapshot.context(snapshot.times[50])
    state = new_account(1000)
    advance(state, snapshot, context, config, spec)
    state.update(cash="900", positions={"BTC/USDT": "1"}, average_cost={"BTC/USDT": "100"},
                 stops={"BTC/USDT": "97"}, take_profits={"BTC/USDT": "106"})
    return state


def request(weight=0.2):
    return DecisionBatch((Allocation("BTC/USDT", weight, 1, "controlled research request"),), "test", "test")


@pytest.mark.parametrize("bar,reason,price,timing", [
    ({"open": 100, "low": 96, "high": 107, "close": 100}, "protective_stop", "97", "intrabar_time_unknown"),
    ({"open": 95, "low": 94, "high": 107, "close": 100}, "protective_stop", "95", "bar_open"),
    ({"open": 108, "low": 96, "high": 109, "close": 100}, "take_profit", "106", "bar_open"),
    ({"open": 100, "low": 99, "high": 107, "close": 106}, "take_profit", "106", "intrabar_time_unknown"),
])
def test_bracket_intrabar_ambiguity_and_opening_gaps(bar, reason, price, timing):
    config, spec, rows = setup()
    rows[51].update(bar)
    snapshot = Snapshot("crypto", rows, config, "csv")
    state = held_state(snapshot, config, spec)
    before = account_view(state)
    events = advance(state, snapshot, snapshot.context(snapshot.times[51]), config, spec)
    assert len(events) == 1 and events[0]["status"] == "filled"
    assert events[0]["reason"] == reason and D(events[0]["reference_price"]) == D(price)
    assert events[0]["execution_time_basis"] == timing
    assert D(state["cash"]) == D(900) + D(price) * D("0.9985")
    assert not state["positions"] and not state["stops"] and not state["take_profits"]
    assert state["last_exit_bar"] == {"BTC/USDT": snapshot.times[51].isoformat()}
    assert before["take_profits"] == {"BTC/USDT": "106"} and before["last_exit_bar"] == {}


def test_opening_fill_anchors_bracket_and_partial_sale_keeps_protection_after_restart():
    config, spec, rows = setup()
    snapshot = Snapshot("crypto", rows, config, "csv")
    context = snapshot.context(snapshot.times[50])
    state = new_account(1000)
    advance(state, snapshot, context, config, spec)
    plan = create_plan(state, context, request(), config, "crypto", "test")
    assert D(plan["orders"][0]["stop"]) == 95  # 数量继续按原 ATR 风险距离保守计算。
    queue_plan(state, plan)
    events = advance(state, snapshot, snapshot.context(snapshot.times[51]), config, spec)
    assert events[0]["status"] == "filled"
    assert D(state["stops"]["BTC/USDT"]) == 97 and D(state["take_profits"]["BTC/USDT"]) == 106
    quantity = D(state["positions"]["BTC/USDT"])
    assert quantity * (D(100) * D("1.0015") - D(95)) <= 5
    state = json.loads(json.dumps(state))
    state["pending"] = [{"symbol": "BTC/USDT", "side": "sell", "quantity": str(quantity / 2),
                         "client_order_id": "partial"}]
    advance(state, snapshot, snapshot.context(snapshot.times[52]), config, spec)
    assert state["last_exit_bar"] == {} and D(state["positions"]["BTC/USDT"]) == quantity / 2
    rows[53].update(high=107, close=106)
    restored = Snapshot("crypto", rows, config, "csv")
    events = advance(state, restored, restored.context(restored.times[53]), config, spec)
    assert len(events) == 1 and events[0]["reason"] == "take_profit"
    assert D(events[0]["quantity"]) == quantity / 2 and not state["positions"]


def test_stop_is_never_loosened_and_zero_volume_preserves_brackets():
    config, spec, rows = setup()
    rows[51].update(low=96, high=107, volume=0)
    snapshot = Snapshot("crypto", rows, config, "csv")
    state = held_state(snapshot, config, spec)
    events = advance(state, snapshot, snapshot.context(snapshot.times[51]), config, spec)
    assert not events  # No invented fill; the protective exit remains queued.
    assert state["pending"][0]["reason"] == "protective_stop"
    assert state["pending"][0]["remaining"] == "1"
    assert state["positions"] == {"BTC/USDT": "1"} and state["take_profits"] == {"BTC/USDT": "106"}
    assert state["last_exit_bar"] == {}
    state["stops"]["BTC/USDT"] = "99.5"
    state["pending"] = [{"symbol": "BTC/USDT", "side": "buy", "quantity": "0.1", "stop": "95",
                         "limit_price": "101", "stop_loss_pct": 0.03, "take_profit_pct": 0.06,
                         "client_order_id": "add"}]
    rows[52].update(low=99.8)
    snapshot = Snapshot("crypto", rows, config, "csv")
    advance(state, snapshot, snapshot.context(snapshot.times[52]), config, spec)
    assert state["stops"]["BTC/USDT"] == "99.5" and D(state["take_profits"]["BTC/USDT"]) == 106


def test_buy_gap_below_planned_stop_is_canceled_before_cash_changes():
    config, spec, rows = setup()
    rows[51].update(open=94, close=94, low=93, high=95)
    snapshot = Snapshot("crypto", rows, config, "csv")
    state = new_account(1000)
    context = snapshot.context(snapshot.times[50])
    advance(state, snapshot, context, config, spec)
    queue_plan(state, create_plan(state, context, request(), config, "crypto", "test"))
    events = advance(state, snapshot, snapshot.context(snapshot.times[51]), config, spec)
    assert events[0]["reason"] == "open_at_or_below_stop" and events[0]["status"] == "canceled"
    assert D(state["cash"]) == 1000 and state["positions"] == {} and state["take_profits"] == {}


def test_risk_enforces_cooldown_even_if_provider_requests_entry_and_state_is_reloaded():
    config, spec, rows = setup()
    config["strategy"]["crypto_cooldown_bars"] = 3
    snapshot = Snapshot("crypto", rows, config, "csv")
    state = new_account(1000)
    state["last_exit_bar"] = {"BTC/USDT": snapshot.times[50].isoformat()}
    state = json.loads(json.dumps(state))
    for index in (50, 52):
        context = snapshot.context(snapshot.times[index])
        plan = create_plan(state, context, request(), config, "crypto", "test")
        assert plan["orders"] == [] and "BTC/USDT:entry_cooldown" in plan["adjustments"]
    context = snapshot.context(snapshot.times[53])
    assert create_plan(state, context, request(), config, "crypto", "test")["orders"]


def test_filter_needs_two_completed_bars_and_rejects_oscillation():
    config, _, rows = setup()
    rows[50].update(open=110, close=110, high=111, low=109)
    rows[51].update(open=111, close=111, high=112, low=110)
    snapshot = Snapshot("crypto", rows, config, "csv")
    assert snapshot.context(snapshot.times[50]).features["BTC/USDT"]["trend_confirmed_2"] is False
    context = snapshot.context(snapshot.times[51])
    assert context.features["BTC/USDT"]["trend_confirmed_2"] is True
    assert context.features["BTC/USDT"]["efficiency_ratio_10"] == 1
    config["strategy"]["crypto_rule"] = "trend_filtered"
    provider = CryptoRuleProvider()
    assert provider.decide(context, CryptoAccount(D(1000), {}, {}), config).allocations[0].target_weight == 0.2
    choppy = replace(context, features={"BTC/USDT": {**context.features["BTC/USDT"], "efficiency_ratio_10": 0.2}})
    assert provider.decide(choppy, CryptoAccount(D(1000), {}, {}), config).allocations[0].target_weight == 0
    revised = deepcopy(rows)
    for row in revised[52:]:
        row.update(open=1000, close=1000, high=1001, low=999)
    assert Snapshot("crypto", revised, config, "csv").context(snapshot.times[51]).payload() == context.payload()


def test_rotation_margin_cooldown_and_all_weak_cash():
    config, _, rows = setup()
    config["strategy"].update(crypto_rule="strength_rotation", crypto_cooldown_bars=3, crypto_rotation_margin=0.01)
    snapshot = Snapshot("crypto", rows, config, "csv")
    original = snapshot.context(snapshot.times[50])
    base = {**original.features["BTC/USDT"], "ema_20": 95, "trend_confirmed_2": True, "efficiency_ratio_10": 0.5}
    context = replace(original, features={"BTC/USDT": {**base, "momentum": 0.1},
                                          "ETH/USDT": {**base, "momentum": 0.108}})
    account = CryptoAccount(D(900), {"BTC/USDT": D(1)}, {"BTC/USDT": D(97)})
    provider = CryptoRuleProvider()
    def weights(ctx, acct):
        return {a.symbol: a.target_weight for a in provider.decide(ctx, acct, config).allocations}
    assert weights(context, account) == {"BTC/USDT": 0.1, "ETH/USDT": 0}
    stronger = replace(context, features={**context.features, "ETH/USDT": {**base, "momentum": 0.12}})
    assert weights(stronger, account) == {"BTC/USDT": 0, "ETH/USDT": 0.2}
    account.last_exit_bar["ETH/USDT"] = context.as_of.isoformat()
    assert weights(stronger, account) == {"BTC/USDT": 0.1, "ETH/USDT": 0}
    weak = replace(context, features={s: {**v, "momentum": -0.01} for s, v in context.features.items()})
    assert weights(weak, account) == {"BTC/USDT": 0, "ETH/USDT": 0}


def test_take_profit_survives_provider_failure_and_independently_reconciles(tmp_path):
    config, spec, rows = setup()
    rows[52].update(high=107, close=106)
    snapshot = Snapshot("crypto", rows, config, "csv")

    class FailsAfterEntry:
        def decide(self, context, account, config):
            if context.as_of == snapshot.times[50]:
                return request()
            raise ProviderError("controlled failure after entry")

    result = replay(snapshot, spec, config, FailsAfterEntry(), times=snapshot.times[50:54],
                    journal_path=tmp_path / "replay.jsonl")
    assert len(result["errors"]) == 2
    fills = [o for o in result["orders"] if o["status"] == "filled"]
    assert len(fills) == 2 and fills[-1]["reason"] == "take_profit"
    assert not result["account"]["positions"]
    assert reconcile_replay(inspect_replay(result["journal_path"])["records"])["status"] == "reconciled"


def test_entrypoints_reject_unsupported_or_incomplete_protection():
    config = load_config(environ={})
    config["strategy"]["crypto_rule"] = "strength_rotation"
    with pytest.raises(ValueError, match="止盈止损"):
        prepare({"market": "crypto", "provider": "crypto_rules", "data_path": "unused.csv"}, config)
    config["risk"].update(crypto_stop_loss_pct=0.03, crypto_take_profit_pct=0.06)
    with pytest.raises(ValueError, match="同时提供"):
        prepare({"market": "crypto", "provider": "crypto_rules", "data_path": "unused.csv"}, config)
    with pytest.raises(ValueError, match="backtest/runtime"):
        run_spot(config)
    config["risk"]["crypto_take_profit_pct"] = 0
    with pytest.raises(ValueError, match="同时启用"):
        validate_config(config)
