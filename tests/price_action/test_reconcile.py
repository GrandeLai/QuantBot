"""影子对账的三态、数量比较与数据新鲜度测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

import quantbot.price_action.live.reconcile as reconcile_mod
from quantbot.price_action.live.reconcile import (
    DRIFT,
    MATCH,
    UNKNOWN,
    _get_actual_positions,
    compare_positions,
    validate_cache_freshness,
)


def test_compare_positions_checks_symbol_direction_and_exact_quantity():
    theory = {
        "MATCHED": {"direction": 1, "shares": 10},
        "WRONG_DIR": {"direction": -1, "shares": 5},
        "WRONG_QTY": {"direction": 1, "shares": 7},
        "MISSING": {"direction": 1, "shares": 3},
    }
    actual = {"MATCHED": 10, "WRONG_DIR": 5, "WRONG_QTY": 9, "EXTRA": -2}

    result = compare_positions(theory, actual, asof="2026-08-07")

    assert result.status == DRIFT
    assert result.matched_symbols == ["MATCHED"]
    assert result.only_theory == ["MISSING"]
    assert result.only_actual == ["EXTRA"]
    assert [item.symbol for item in result.direction_mismatches] == ["WRONG_DIR"]
    assert [item.symbol for item in result.quantity_mismatches] == ["WRONG_QTY"]
    assert result.to_dict()["quantity_mismatches"][0] == {
        "symbol": "WRONG_QTY",
        "theory_shares": 7,
        "actual_shares": 9,
    }


def test_compare_positions_reports_match_only_for_exact_signed_quantities():
    theory = {
        "LONG": {"direction": 1, "shares": 4},
        "SHORT": {"direction": -1, "shares": 2},
    }
    result = compare_positions(theory, {"LONG": 4, "SHORT": -2}, asof="2026-08-07")
    assert result.status == MATCH
    assert result.matched_symbols == ["LONG", "SHORT"]


class _UnavailableBroker:
    def get_positions(self):
        raise ConnectionError("network down")


def test_broker_unreachable_is_unknown_even_when_local_state_exists(tmp_path):
    """券商失败不得退化成本地持仓，从而制造假的一致结论。"""
    state = tmp_path / "live_state.json"
    state.write_text('{"positions": {"TST": {"direction": 1, "qty": 10}}}')
    cfg = {
        "broker": {"paper": True},
        "live": {"state_file": str(state)},
    }

    snapshot = _get_actual_positions(cfg, broker=_UnavailableBroker())

    assert snapshot.status == UNKNOWN
    assert snapshot.positions is None
    assert "network down" in (snapshot.error or "")


def test_stale_or_partial_cache_is_rejected():
    cfg = {
        "regime": {"symbol": "SPY"},
        "live": {"reconcile_max_cache_age_days": 4},
    }
    stale = {"SPY": pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp("2026-08-01")])}
    with pytest.raises(RuntimeError, match="缓存陈旧"):
        validate_cache_freshness(
            stale,
            cfg,
            now=pd.Timestamp("2026-08-09 17:00", tz="America/New_York"),
        )

    partial = {
        "SPY": pd.DataFrame({"close": [100.0]}, index=[pd.Timestamp("2026-08-09")])
    }
    with pytest.raises(RuntimeError, match="盘中未完成"):
        validate_cache_freshness(
            partial,
            cfg,
            now=pd.Timestamp("2026-08-09 10:00", tz="America/New_York"),
        )


def test_cmd_reconcile_returns_structured_unknown_on_broker_failure(monkeypatch):
    """理论侧成功但券商失败时，命令返回 UNKNOWN，actual 明确为 None。"""
    asof = pd.Timestamp("2026-08-07")
    data = {"SPY": pd.DataFrame({"close": [100.0]}, index=[asof])}
    monkeypatch.setattr(reconcile_mod, "load_market_data", lambda *args, **kwargs: data)
    monkeypatch.setattr(reconcile_mod, "notify", lambda text: True)

    class _FakeEngine:
        def __init__(self, loaded_data, cfg):
            assert loaded_data is data

        def run(self):
            return SimpleNamespace(
                equity=pd.Series([1000.0], index=[asof]),
                open_positions={"TST": {"direction": 1, "shares": 10}},
            )

    monkeypatch.setattr(reconcile_mod, "BacktestEngine", _FakeEngine)
    cfg = {
        "data": {"snapshot": "frozen"},
        "backtest": {"end": "2026-01-01"},
        "live": {"cache_dir": "unused"},
        "regime": {"symbol": "SPY"},
        "broker": {"paper": True},
        "base_position": {"enabled": False},
    }

    result = reconcile_mod.cmd_reconcile(
        cfg,
        broker=_UnavailableBroker(),
        now=pd.Timestamp("2026-08-09 17:00", tz="America/New_York"),
    )

    assert result.status == UNKNOWN
    assert result.theory_positions == {"TST": 10}
    assert result.actual_positions is None
    assert result.to_dict()["status"] == UNKNOWN
