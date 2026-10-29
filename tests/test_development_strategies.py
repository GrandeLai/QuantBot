"""固定研究候选的标的边界、因果输入和持仓语义，不调用模型。"""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import json
from math import pi, sin

import pytest

from quantbot.ai.config import load_config
from quantbot.ai.development_strategies import (
    development_candidates, development_controls, development_provider,
)
from quantbot.ai.domain import MarketContext
from quantbot.ai.feeds import Snapshot, stock_calendar
from quantbot.ai.local_execution import create_plan, model_account, new_account
from quantbot.ai.market import DailyMarketData
from quantbot.crypto.ai import CryptoAccount


def inputs(market):
    config = load_config(environ={})
    symbols = ["BTC/USDT", "ETH/USDT"] if market == "crypto" else ["SPY", "QQQ"]
    config["market"].update(symbols=symbols, benchmark=symbols[0])
    config["strategy"].update(momentum_window=20, breakout_window=20, exit_window=10)
    values = {"close": 110.0, "momentum": 0.1, "ema_10": 105.0, "ema_20": 103.0,
              "ema_30": 100.0, "prior_high_20": 109.0, "prior_low_10": 95.0,
              "rsi_14": 40.0, "trend_confirmed_2": True, "efficiency_ratio_10": 0.5,
              "bar": "1d", "stop_candidate": 95.0,
              "uptrend": True, "breakout": True, "pullback": True, "bullish_reversal": True,
              "channel_exit": False, "factor_score": 1.0, "suggested_stop": 95.0,
              "previous_rotation_eligible": True}
    context = MarketContext("controlled", datetime(2025, 3, 1, tzinfo=UTC),
                            {symbol: dict(values) for symbol in symbols},
                            "crypto_spot" if market == "crypto" else "R1", symbols[0])
    state = new_account(1000)
    state["prices"] = context.prices
    return config, context, state


def weights(provider, context, account, config):
    decision = provider.decide(context, account, config)
    assert decision.provider == "development_rules"
    return {allocation.symbol: allocation.target_weight for allocation in decision.allocations}


def test_candidate_registry_is_finite_and_separates_controls():
    assert len(development_candidates("crypto")) == 7  # 首批六个，加一次预登记的无信号改进。
    assert len(development_candidates("stocks")) == 4  # 三个原候选，加一次预登记的入场确认改进。
    assert [c.id for c in development_candidates("stocks") if c.confirm_rotation_entry] \
        == ["relative_rotation_confirmed"]
    for market in ("crypto", "stocks"):
        candidates = development_candidates(market)
        assert len({candidate.id for candidate in candidates}) == len(candidates)
        assert all(asdict(candidate)["cooldown_bars"] == 3 and not candidate.control for candidate in candidates)
        assert {candidate.id for candidate in development_controls(market)} == {"cash", "momentum"}
        with pytest.raises(ValueError, match="未登记"):
            development_provider(market, "new_parameter_after_result")


@pytest.mark.parametrize("candidate_id,selected", [
    ("btc_breakout20", "BTC/USDT"), ("btc_breakout20_filtered", "BTC/USDT"),
    ("eth_momentum20", "ETH/USDT"), ("eth_momentum20_filtered", "ETH/USDT"),
])
def test_single_asset_scope_explicitly_flattens_other_asset(candidate_id, selected):
    config, context, _ = inputs("crypto")
    other = next(symbol for symbol in context.features if symbol != selected)
    account = CryptoAccount(D(900), {other: D("0.1")}, {other: D(95)})
    result = weights(development_provider("crypto", candidate_id), context, account, config)
    assert result == {selected: config["risk"]["max_weight"], other: 0}
    incomplete = replace(context, features={other: context.features[other]})
    with pytest.raises(ValueError, match="缺少"):
        development_provider("crypto", candidate_id).decide(incomplete, account, config)


@pytest.mark.parametrize("candidate_id,symbol", [
    ("btc_breakout20_filtered", "BTC/USDT"), ("eth_momentum20_filtered", "ETH/USDT"),
])
@pytest.mark.parametrize("chop", [{"efficiency_ratio_10": 0.29}, {"trend_confirmed_2": False}])
def test_filtered_candidates_reject_chop_without_changing_raw_entry(candidate_id, symbol, chop):
    config, context, _ = inputs("crypto")
    context = replace(context, features={name: {**values, **chop} for name, values in context.features.items()})
    account = CryptoAccount(D(1000), {}, {})
    assert weights(development_provider("crypto", candidate_id), context, account, config)[symbol] == 0
    assert weights(development_provider("crypto", candidate_id.removesuffix("_filtered")),
                   context, account, config)[symbol] == config["risk"]["max_weight"]


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_candidates_hold_existing_quantity_without_daily_top_up(market):
    config, context, state = inputs(market)
    for candidate in development_candidates(market):
        symbol = candidate.symbols[0] if candidate.symbols else "QQQ"
        quantity = "0.1" if market == "crypto" else "1"
        state.update(cash="900", positions={symbol: quantity}, average_cost={symbol: "100"},
                     stops={symbol: "95"})
        account = model_account(state, context, market)
        provider = development_provider(market, candidate.id)
        decision = provider.decide(context, account, config)
        actual = {allocation.symbol: allocation.target_weight for allocation in decision.allocations}
        assert actual[symbol] == account.payload(context)["current_weights"][symbol]
        # 其他标的可能新开仓，但当前持仓不能出现补仓或舍入碎单。
        plan = create_plan(state, context, decision, config, market, "development-test")
        assert not any(order["symbol"] == symbol for order in plan["orders"])


def test_crypto_cooldown_uses_completed_bars_and_survives_provider_recreation():
    config, context, _ = inputs("crypto")
    account = CryptoAccount(D(1000), {}, {}, last_exit_bar={"ETH/USDT": context.as_of.isoformat()})
    for offset, expected in ((2, 0), (3, config["risk"]["max_weight"])):
        now = replace(context, as_of=context.as_of + timedelta(days=offset))
        assert weights(development_provider("crypto", "eth_momentum20"), now, account, config)["ETH/USDT"] \
            == expected


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_rotation_keeps_incumbent_until_margin_and_all_weak_goes_cash(market):
    config, context, state = inputs(market)
    incumbent, challenger = list(context.features)
    state.update(cash="900", positions={incumbent: "1"}, average_cost={incumbent: "100"},
                 stops={incumbent: "95"})
    account = model_account(state, context, market)
    provider = development_provider(market, "relative_rotation")
    for advantage, selected in ((0.005, incumbent), (0.02, challenger)):
        changed = replace(context, features={**context.features,
                          challenger: {**context.features[challenger], "momentum": 0.1 + advantage}})
        result = weights(provider, changed, account, config)
        assert result[selected] > 0 and sum(value > 0 for value in result.values()) == 1
    weak = replace(context, features={symbol: {**values, "momentum": -0.01}
                                     for symbol, values in context.features.items()})
    assert all(value == 0 for value in weights(provider, weak, account, config).values())


def test_stock_rotation_waits_for_challenger_three_sessions_and_reloaded_state():
    config, context, state = inputs("stocks")
    config["strategy"]["stock_cooldown_bars"] = 3
    state.update(cash="9000", positions={"SPY": "10"}, average_cost={"SPY": "100"}, stops={"SPY": "95"},
                 last_exit_bar={"QQQ": "2024-05-24T20:00:00+00:00"})
    state = json.loads(json.dumps(state))
    context = replace(context, features={**context.features,
                      "QQQ": {**context.features["QQQ"], "momentum": 0.2}})
    provider = development_provider("stocks", "relative_rotation")
    for day in ("2024-05-24", "2024-05-28", "2024-05-29", "2024-05-30"):
        now = replace(context, as_of=datetime.fromisoformat(day + "T20:00:00+00:00"))
        account = model_account(state, now, "stocks")
        assert account.last_exit_bar == state["last_exit_bar"]
        decision = provider.decide(now, account, config)
        target = {a.symbol: a.target_weight for a in decision.allocations}
        plan = create_plan(state, now, decision, config, "stocks", "test")
        if day < "2024-05-30":
            assert target["SPY"] == account.payload(now)["current_weights"]["SPY"] and target["QQQ"] == 0
            assert plan["orders"] == []
        else:
            assert target["SPY"] == 0 and target["QQQ"] == config["risk"]["max_weight"]
            assert {(o["symbol"], o["side"]) for o in plan["orders"]} == {("SPY", "sell"), ("QQQ", "buy")}


def test_stock_rotation_still_exits_invalid_incumbent_when_challenger_cools():
    config, context, state = inputs("stocks")
    config["strategy"]["stock_cooldown_bars"] = 3
    state.update(cash="900", positions={"SPY": "1"}, average_cost={"SPY": "100"}, stops={"SPY": "95"},
                 last_exit_bar={"QQQ": "2024-05-24T20:00:00+00:00"})
    context = replace(context, as_of=datetime.fromisoformat("2024-05-28T20:00:00+00:00"),
                      features={**context.features, "SPY": {**context.features["SPY"], "channel_exit": True}})
    account = model_account(state, context, "stocks")
    decision = development_provider("stocks", "relative_rotation").decide(context, account, config)
    assert all(a.target_weight == 0 for a in decision.allocations)
    plan = create_plan(state, context, decision, config, "stocks", "test")
    assert [(o["symbol"], o["side"]) for o in plan["orders"]] == [("SPY", "sell")]


def test_stock_confirmed_rotation_requires_previous_eligibility_only_for_new_entry():
    config, context, state = inputs("stocks")
    context = replace(context, features={
        "SPY": {**context.features["SPY"], "momentum": -0.1},
        "QQQ": {**context.features["QQQ"], "previous_rotation_eligible": False},
    })
    account = model_account(state, context, "stocks")
    original = development_provider("stocks", "relative_rotation")
    assert weights(original, context, account, config)["QQQ"] == config["risk"]["max_weight"]
    assert weights(development_provider("stocks", "relative_rotation_confirmed"), context, account, config) \
        == {"SPY": 0, "QQQ": 0}
    confirmed = replace(context, features={**context.features,
                        "QQQ": {**context.features["QQQ"], "previous_rotation_eligible": True}})
    # 新建 provider 仍得到相同判断，不依赖此前调用或前一日排名。
    assert weights(development_provider("stocks", "relative_rotation_confirmed"), confirmed, account, config) \
        == {"SPY": 0, "QQQ": config["risk"]["max_weight"]}
    without_new_feature = replace(context, features={s: {k: v for k, v in values.items()
                                                        if k != "previous_rotation_eligible"}
                                                     for s, values in context.features.items()})
    assert original.decide(without_new_feature, account, config) == original.decide(context, account, config)


def test_stock_confirmed_rotation_delays_new_challenger_but_keeps_original_margin():
    config, context, state = inputs("stocks")
    state.update(cash="9000", positions={"SPY": "10"}, average_cost={"SPY": "100"}, stops={"SPY": "95"})
    context = replace(context, features={s: {**v, "previous_rotation_eligible": False}
                                        for s, v in context.features.items()})
    account = model_account(state, context, "stocks")
    provider = development_provider("stocks", "relative_rotation_confirmed")
    for previous, advantage, selected in ((False, 0.02, "SPY"), (True, 0.005, "SPY"), (True, 0.02, "QQQ")):
        changed = replace(context, features={**context.features,
                          "QQQ": {**context.features["QQQ"], "momentum": 0.1 + advantage,
                                  "previous_rotation_eligible": previous}})
        decision = provider.decide(changed, account, config)
        target = {a.symbol: a.target_weight for a in decision.allocations}
        assert target[selected] > 0 and sum(v > 0 for v in target.values()) == 1
        plan = create_plan(state, changed, decision, config, "stocks", "confirmation-test")
        assert {(o["symbol"], o["side"]) for o in plan["orders"]} == (
            set() if selected == "SPY" else {("SPY", "sell"), ("QQQ", "buy")})


@pytest.mark.parametrize("invalid", [{"momentum": 0}, {"uptrend": False}, {"channel_exit": True}])
def test_stock_confirmed_rotation_exits_invalid_position_without_confirmation(invalid):
    config, context, state = inputs("stocks")
    state.update(cash="900", positions={"SPY": "1"}, average_cost={"SPY": "100"}, stops={"SPY": "95"})
    context = replace(context, features={
        "SPY": {**context.features["SPY"], **invalid, "previous_rotation_eligible": True},
        "QQQ": {**context.features["QQQ"], "previous_rotation_eligible": False},
    })
    account = model_account(state, context, "stocks")
    decision = development_provider("stocks", "relative_rotation_confirmed").decide(context, account, config)
    assert all(a.target_weight == 0 for a in decision.allocations)
    plan = create_plan(state, context, decision, config, "stocks", "confirmation-test")
    assert [(o["symbol"], o["side"]) for o in plan["orders"]] == [("SPY", "sell")]


@pytest.mark.parametrize("previous_close", [170, 80])
def test_previous_rotation_feature_uses_previous_completed_session_not_current_or_future(previous_close):
    config, _, _ = inputs("stocks")
    dates = stock_calendar("2025-01-01", "2025-06-30").sessions_in_range("2025-01-01", "2025-06-30")[:80]
    rows = []
    for index, at in enumerate(dates):
        close = previous_close if index == 69 else 101 + index + 5 * sin(index * 2 * pi / 12)
        for symbol in config["market"]["symbols"]:
            rows.append({"symbol": symbol, "date": str(at.date()), "open": close, "high": close + 1,
                         "low": close - 1, "close": close, "volume": 1000000})
    market = DailyMarketData(rows, config)
    previous, now = market.context(dates[69].date()), market.context(dates[70].date())
    altered = deepcopy(rows)
    # 当前根和未来根都改动，前一根资格仍必须保持；两日前的输入用于指标原有滚动窗口。
    for row in altered[70 * len(config["market"]["symbols"]):]:
        row.update(open=20, high=21, low=19, close=20)
    changed = DailyMarketData(altered, config).context(dates[70].date())
    for symbol in config["market"]["symbols"]:
        old = previous.features[symbol]
        expected = old["momentum"] > 0 and old["uptrend"] and not old["channel_exit"]
        assert expected is (previous_close == 170)
        assert now.features[symbol]["previous_rotation_eligible"] is expected
        assert changed.features[symbol]["previous_rotation_eligible"] is expected


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_controls_are_explicit_and_keep_original_momentum_rebalancing(market):
    config, context, state = inputs(market)
    account = model_account(state, context, market)
    assert set(weights(development_provider(market, "cash"), context, account, config).values()) == {0}
    assert set(weights(development_provider(market, "momentum"), context, account, config).values()) \
        == {config["risk"]["max_weight"]}
    with pytest.raises(ValueError, match="市场"):
        development_provider("stocks" if market == "crypto" else "crypto", "cash").decide(context, account, config)


@pytest.mark.parametrize("market", ["crypto", "stocks"])
def test_future_rows_cannot_change_any_registered_decision(market):
    config, _, _ = inputs(market)
    symbols = config["market"]["symbols"]
    dates = stock_calendar("2025-01-01", "2025-06-30").sessions_in_range("2025-01-01", "2025-06-30")[:90]
    rows = []
    for index in range(90):
        for symbol in symbols:
            row = {"symbol": symbol, "open": 100 + index, "high": 102 + index,
                   "low": 99 + index, "close": 101 + index, "volume": 1000000}
            row.update({"date": str(dates[index].date())} if market == "stocks" else
                       {"timestamp": (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
                        "bar": "1d"})
            rows.append(row)
    revised = deepcopy(rows)
    for row in revised[71 * len(symbols):]:
        row.update(open=10000, high=11000, low=9000, close=10000)
    if market == "crypto":
        original, changed = (Snapshot(market, data, config, "csv") for data in (rows, revised))
        before, after = original.context(original.times[70]), changed.context(changed.times[70])
    else:
        before, after = (DailyMarketData(data, config).context(dates[70].date()) for data in (rows, revised))
    state = new_account(1000)
    state["prices"] = before.prices
    account = model_account(state, before, market)
    assert before.payload() == after.payload()
    for candidate in (*development_candidates(market), *development_controls(market)):
        provider = development_provider(market, candidate.id)
        assert provider.decide(before, account, config) == provider.decide(after, account, config)
