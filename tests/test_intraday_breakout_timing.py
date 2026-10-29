"""Fixed one-bar confirmation checks using only synthetic prices."""

import pandas as pd
import pytest

from quantbot.intraday import data, mining, research
from tests.test_intraday_execution import model, section  # noqa: F401


RECIPE = {"version": 1, "seed": "breakout_next_bar", "all": [
    {"feature": "pair_net_move_cost_room", "op": "gte", "threshold": 1.0}]}


@pytest.fixture(params=[["BTC/USDT", "ETH/USDT"], ["SPY", "QQQ"]])
def symbols(request):
    return request.param


def history(symbols, paths, *, days=1, sessions=None):
    if sessions is None:
        sessions = {}
        for day in range(days):
            opening = pd.Timestamp("2025-01-02T14:30:00Z") + pd.Timedelta(days=day)
            sessions[opening.date().isoformat()] = (opening, opening + pd.Timedelta(hours=4))
    rows = []
    for opening, closing in sessions.values():
        for i, stamp in enumerate(pd.date_range(opening, closing, freq="5min", inclusive="left")):
            for symbol, path in zip(symbols, paths, strict=True):
                close = path[min(i // 3, len(path) - 1)]
                rows.append({"timestamp": stamp, "symbol": symbol, "open": close,
                             "high": close + 0.05, "low": close - 0.05,
                             "close": close, "volume": 100000})
    return pd.DataFrame(rows), sessions


def selected(value, symbol):
    return research.select_signal("mined", value, symbol, None, {}, 70, recipe=RECIPE)


def test_original_cost_failure_does_not_consume_fixed_next_bar(symbols):
    frame, sessions = history(symbols, [[100, 100, 100.3, 101, 102]] * 2, days=2)
    prepared = research.prepare_sessions(frame, sessions, symbols)
    for day, (opening, _) in sessions.items():
        signals = prepared[day]["signals"]
        original, confirmation, later = [signals[opening + pd.Timedelta(minutes=m)] for m in (45, 60, 75)]
        for symbol in symbols:
            assert original[symbol]["base"] and not original[symbol]["breakout_next_bar"]
            assert not mining.matches(RECIPE, original[symbol], 70)
            assert not selected(original[symbol], symbol)
            assert confirmation[symbol]["breakout_next_bar"] and selected(confirmation[symbol], symbol)
            assert confirmation[symbol]["pair_net_move_30m_bps"] == pytest.approx(100)
            assert not later[symbol]["base"] and not selected(later[symbol], symbol)
        # The two initial bars cannot inherit the previous session's breakout.
        assert all(not v["breakout_next_bar"] for values in list(signals.values())[:2] for v in values.values())


def test_each_symbol_has_its_own_breakout_time_and_no_retest_is_required(symbols):
    frame, sessions = history(symbols, [[100, 100, 102, 101, 103], [100, 100, 100, 101, 101.2]])
    prepared = research.prepare_sessions(frame, sessions, symbols)
    signals = prepared["2025-01-02"]["signals"]
    opening = sessions["2025-01-02"][0]
    at60, at75 = [signals[opening + pd.Timedelta(minutes=m)] for m in (60, 75)]
    first, second = symbols
    assert at60[first]["close"] < 102 and not at60[first]["retest"]
    assert selected(at60[first], first)  # May be below the original breakout close.
    assert at60[second]["base"] and not selected(at60[second], second)
    assert not selected(at75[first], first) and selected(at75[second], second)
    reversed_signals = research.prepare_sessions(frame, sessions, list(reversed(symbols)))
    assert reversed_signals["2025-01-02"]["signals"] == signals


@pytest.mark.parametrize("failure", ["price", "pair"])
def test_failed_confirmation_cannot_be_replaced_by_later_winner(symbols, failure):
    first = [100, 100, 100.3, 100.05 if failure == "price" else 101, 102]
    second = [100, 100, 100.3, 100.5 if failure == "pair" else 101, 102]
    frame, sessions = history(symbols, [first, second])
    signals = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    opening = sessions["2025-01-02"][0]
    confirmation = signals[opening + pd.Timedelta(minutes=60)][symbols[0]]
    later = signals[opening + pd.Timedelta(minutes=75)][symbols[0]]
    assert confirmation["breakout_next_bar"] == (failure == "pair")
    assert not selected(confirmation, symbols[0])
    assert mining.matches(RECIPE, later, 70) and not selected(later, symbols[0])
    missing = confirmation | {"breakout_next_bar": True, "pair_net_move_30m_bps": None}
    assert not selected(missing, symbols[0])


def test_confirmation_must_not_extend_original_entry_cutoff(symbols):
    # Four-hour session: original cutoff is minute 105.
    frame, sessions = history(symbols, [[100] * 5 + [101, 102], [100] * 6 + [101, 102]])
    signals = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    opening = sessions["2025-01-02"][0]
    at105, at120 = [signals[opening + pd.Timedelta(minutes=m)] for m in (105, 120)]
    assert selected(at105[symbols[0]], symbols[0])
    assert at105[symbols[1]]["base"] and not selected(at105[symbols[1]], symbols[1])
    assert not at120[symbols[1]]["breakout_next_bar"] and not selected(at120[symbols[1]], symbols[1])


def test_future_bars_cannot_change_completed_breakout_confirmation(symbols):
    frame, sessions = history(symbols, [[100, 100, 100.3, 101, 102]] * 2)
    before = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    boundary = sessions["2025-01-02"][0] + pd.Timedelta(minutes=60)
    for field in ("open", "high", "low", "close"):
        frame.loc[frame["timestamp"] >= boundary, field] *= 10
    after = research.prepare_sessions(frame, sessions, symbols)["2025-01-02"]["signals"]
    assert all(before[stamp] == after[stamp] for stamp in before if stamp <= boundary)
    assert after[boundary + pd.Timedelta(minutes=15)] != before[boundary + pd.Timedelta(minutes=15)]


@pytest.mark.parametrize("latency,fill_delay", [(1, 5), (301, 10)])
def test_delayed_seed_and_recipe_keep_next_bar_execution_costs_and_ledger(symbols, section, model, latency, fill_delay):  # noqa: F811
    market = "stocks" if symbols[0] == "SPY" else "crypto"
    protocol = {"range": {"start": "2025-01-02", "end_exclusive": "2025-02-15"},
                "markets": {market: section | {"symbols": symbols}}, "execution_model": model,
                "statistics": {"purge_sessions": 0, "embargo_sessions": 0}}
    sessions = data.session_grid(protocol, market)
    frame, _ = history(symbols, [[100, 100, 100.3, 101]] * 2, sessions=sessions)
    prepared = research.prepare_sessions(frame, sessions, symbols)
    last = list(sessions)[-1]
    window = {"id": "fixture", "start": last, "end": last}
    signal_at = sessions[last][0] + pd.Timedelta(minutes=60)
    for recipe in (None, RECIPE):
        result = research.replay(protocol, market, prepared, window, "breakout_next_bar", 15, latency,
                                 recipe=recipe)
        assert result["entry_orders_filled"] == 2
        assert len(result["attempts"]) == 2 and all(a["at"] == signal_at.isoformat() for a in result["attempts"])
        assert result["net_return"] < 0 and result["account"]["positions"] == {}
        assert research.audit_report(result)["status"] == "reconciled"
        buys = [e for e in result["events"] if e["status"] == "filled" and e["side"] == "buy"]
        assert len(buys) == 2
        assert all(pd.Timestamp(e["created_at"]) == signal_at for e in buys)
        assert all(pd.Timestamp(e["at"]) == signal_at + pd.Timedelta(minutes=fill_delay) for e in buys)
