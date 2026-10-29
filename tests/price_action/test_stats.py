"""统计尺子（stats.py）单元测试：灵敏度与特异度双向验证。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantbot.price_action.backtest.stats import bootstrap_metrics, concentration, paired_test


@pytest.fixture(scope="module")
def equity() -> pd.Series:
    """确定性的合成权益曲线：温和上涨 + 噪声。"""
    rng = np.random.default_rng(7)
    rets = rng.normal(0.0006, 0.01, 1500)
    idx = pd.bdate_range("2019-01-01", periods=1500)
    return pd.Series(100000 * np.cumprod(1 + rets), index=idx)


def test_bootstrap_interval_contains_point(equity):
    """点估计 CAGR 应落在 bootstrap 90% 区间内。"""
    n_years = len(equity) / 252
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / n_years) - 1
    b = bootstrap_metrics(equity, n_paths=500)
    assert b["cagr_p5"] <= cagr <= b["cagr_p95"]
    assert b["maxdd_p5"] <= b["maxdd_p50"] <= b["maxdd_p95"] <= 0


def test_paired_test_specificity(equity):
    """特异度：叠加 iid 零均值噪声 → 不可采纳（不误报）。"""
    rng = np.random.default_rng(0)
    rets = equity.pct_change().fillna(0)
    noisy = (1 + rets + rng.normal(0, 0.0005, len(rets))).cumprod() * equity.iloc[0]
    noisy.index = equity.index
    r = paired_test(equity, noisy, n_paths=500)
    assert not r["actionable"]


def test_paired_test_sensitivity(equity):
    """灵敏度：系统性 +2bp/日 优势 → 显著且可采纳（不漏报）。"""
    rets = equity.pct_change().fillna(0)
    better = (1 + rets + 0.0002).cumprod() * equity.iloc[0]
    better.index = equity.index
    r = paired_test(better, equity, n_paths=500)
    assert r["actionable"]
    assert r["diff_annual"] > 0.04  # 年化差约 +5%


def test_block_bootstrap_covers_last_day():
    """回归测试：最后一天的收益必须可能被采样（曾有 off-by-one 漏采）。"""
    from quantbot.price_action.backtest.stats import _block_bootstrap_paths

    # 最后一天收益是唯一的非零值：若被漏采，所有路径将全为零
    rets = np.zeros(100)
    rets[-1] = 0.5
    rng = np.random.default_rng(1)
    paths = _block_bootstrap_paths(rets, n_paths=200, block=21, rng=rng)
    assert (paths == 0.5).any(), "最后一天的收益从未被任何路径采样"


def test_bootstrap_short_series_no_crash():
    """序列短于 block 长度时不越界（退化为逐日重抽样）。"""
    idx = pd.bdate_range("2024-01-01", periods=10)
    eq = pd.Series(np.linspace(100, 110, 10), index=idx)
    b = bootstrap_metrics(eq, n_paths=100, block=21)
    assert np.isfinite(b["cagr_p50"])


def test_concentration_math():
    """利润集中度：手工构造交易验证 top-N 占比。"""
    trades = pd.DataFrame({"pnl": [100.0, 50.0, -30.0, 20.0, -10.0]})
    idx = pd.bdate_range("2020-01-01", periods=504)  # 2 年
    eq = pd.Series(np.linspace(1000, 1130, 504), index=idx)
    c = concentration(trades, eq)
    assert c["top1_share"] == pytest.approx(100 / 130)
    assert c["cagr_ex_top1"] < c["cagr_full"]
