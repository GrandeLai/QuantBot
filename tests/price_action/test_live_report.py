"""报告按实际样本数决定是否准备影子回测，保留五日边界与失败说明。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pandas as pd
import pytest

from quantbot.price_action.live.paths import read_runtime_paths
from quantbot.price_action.live.report import cmd_report

from .conftest import make_cfg


def _write_equity(paths, days: int, *, duplicate_last: bool = False):
    dates = pd.bdate_range(end="2026-09-10", periods=days)
    frame = pd.DataFrame({
        "date": dates, "equity": [100000 + i * 100 for i in range(days)],
        "cash": 80000, "n_positions": 1, "level": 2,
    })
    if duplicate_last:
        frame = pd.concat([frame, frame.iloc[[-1]]], ignore_index=True)
    paths.logs_dir.mkdir(parents=True)
    frame.to_csv(paths.logs_dir / "equity_history.csv", index=False)
    return dates


@pytest.mark.parametrize("days,duplicate_last", [(1, False), (4, False), (4, True)])
def test_short_report_uses_only_recorded_equity_and_fills(
    tmp_path, monkeypatch, days, duplicate_last,
):
    cfg = make_cfg(tmp_path)
    paths = read_runtime_paths(cfg, "sim")
    _write_equity(paths, days, duplicate_last=duplicate_last)
    (paths.logs_dir / "fills.csv").write_text("slip_bps\n5.0\n", encoding="utf-8")
    before = copy.deepcopy(cfg)
    attempted = []

    def forbidden(*args, **kwargs):
        attempted.append("shadow")
        raise AssertionError("不足五个交易日不能发起补数或影子回测")

    monkeypatch.setattr("quantbot.price_action.data.downloader.load_market_data", forbidden)
    monkeypatch.setattr("quantbot.price_action.backtest.engine.BacktestEngine", forbidden)

    cmd_report(cfg, paths=paths)

    report = (paths.logs_dir / "live_report.txt").read_text()
    assert attempted == []
    assert f"（{days} 个交易日）" in report
    assert "实测滑点（1 笔成交" in report
    assert "重叠交易日不足 5 天" in report
    assert "影子回测对比失败" not in report
    assert cfg == before


@pytest.mark.parametrize("overlap", [4, 5])
def test_five_days_prepare_shadow_but_still_require_five_overlapping_days(
    tmp_path, monkeypatch, overlap,
):
    cfg = make_cfg(tmp_path)
    cfg["data"]["snapshot"] = "research-identity"
    paths = read_runtime_paths(cfg, "sim")
    dates = _write_equity(paths, 5)
    before = copy.deepcopy(cfg)
    calls = []
    market = object()

    def load(shadow_cfg, **kwargs):
        calls.append("load")
        assert kwargs["cache_dir_override"] == str(paths.cache_dir)
        assert shadow_cfg["data"]["snapshot"] is None
        return market

    def engine(data, shadow_cfg):
        calls.append("engine")
        assert data is market
        assert shadow_cfg["backtest"]["start"] == str(dates[0].date())
        assert shadow_cfg["backtest"]["end"] == str(dates[-1].date())
        assert shadow_cfg["backtest"]["initial_capital"] == 100000
        equity = pd.Series([100000 + i * 100 for i in range(overlap)], index=dates[:overlap])
        return SimpleNamespace(run=lambda: SimpleNamespace(equity=equity))

    monkeypatch.setattr("quantbot.price_action.data.downloader.load_market_data", load)
    monkeypatch.setattr("quantbot.price_action.backtest.engine.BacktestEngine", engine)

    cmd_report(cfg, paths=paths)

    report = (paths.logs_dir / "live_report.txt").read_text()
    assert calls == ["load", "engine"]
    if overlap == 5:
        assert "日收益跟踪误差(std): 0.00%/年化" in report
        assert "实际 +0.40% vs 影子 +0.40%" in report
    else:
        assert "重叠交易日不足 5 天" in report
        assert "日收益跟踪误差" not in report
    assert cfg == before


def test_shadow_failure_after_five_days_remains_visible(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    paths = read_runtime_paths(cfg, "sim")
    _write_equity(paths, 5)

    def unavailable(*args, **kwargs):
        raise RuntimeError("行情暂不可用")

    monkeypatch.setattr("quantbot.price_action.data.downloader.load_market_data", unavailable)
    cmd_report(cfg, paths=paths)
    report = (paths.logs_dir / "live_report.txt").read_text()
    assert "$100,400" in report
    assert "影子回测对比失败（行情暂不可用）" in report
