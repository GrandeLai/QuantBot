"""SimBroker（本地模拟券商）单元测试：撮合、记账、持久化。"""

from __future__ import annotations

import pandas as pd
import pytest

from quantbot.price_action.broker.sim_broker import SimBroker, SLIPPAGE

from .conftest import make_cfg, make_trending_ohlcv


@pytest.fixture()
def sim(tmp_path):
    """带一只可撮合股票（TST）的 SimBroker。"""
    cfg = make_cfg(tmp_path)
    cache = tmp_path / "live_cache"
    cache.mkdir()
    cfg["live"]["cache_dir"] = str(cache)
    df = make_trending_ohlcv(60, start_price=100.0)
    df.to_parquet(cache / "TST.parquet")
    broker = SimBroker(cfg, state_path=tmp_path / "sim_state.json")
    return broker, float(df["close"].iloc[-1])


def test_buy_fills_with_slippage_and_books_cash(sim):
    """买入按最新收盘 +5bp 成交，现金与持仓同步记账。"""
    broker, last_close = sim
    oid = broker.market_buy("TST", 100)
    assert broker.wait_order(oid)
    qty, px = broker.order_fill(oid)
    assert qty == 100
    assert px == pytest.approx(last_close * (1 + SLIPPAGE), rel=1e-6)
    acct = broker.get_account()
    assert acct.cash == pytest.approx(100000 - 100 * px, abs=0.01)
    assert broker.get_positions()["TST"].shares == 100


def test_sell_roundtrip_and_position_removal(sim):
    """买入后全部卖出：持仓清空，现金损失 ≈ 双边滑点。"""
    broker, last_close = sim
    broker.market_buy("TST", 50)
    broker.market_sell("TST", 50)
    assert "TST" not in broker.get_positions()
    loss = 100000 - broker.get_account().cash
    assert loss == pytest.approx(50 * last_close * 2 * SLIPPAGE, rel=0.01)


def test_state_persists_across_instances(sim, tmp_path):
    """状态持久化：重建实例后持仓与现金完整恢复。"""
    broker, _ = sim
    broker.market_buy("TST", 30)
    cash_before = broker.cash
    broker2 = SimBroker(broker.cfg, state_path=tmp_path / "sim_state.json")
    assert broker2.get_positions()["TST"].shares == 30
    assert broker2.cash == pytest.approx(cash_before, abs=0.01)


def test_no_quote_rejects_order(sim):
    """实盘缓存中无行情的代码：拒单（wait_order 为 False），不动账。"""
    broker, _ = sim
    cash0 = broker.cash
    oid = broker.market_buy("GHOST", 10)
    assert not broker.wait_order(oid)
    assert broker.cash == cash0
    assert "GHOST" not in broker.get_positions()


def test_short_position_bookkeeping(sim):
    """卖空开仓：负持仓、卖出所得计入现金。"""
    broker, last_close = sim
    broker.market_sell("TST", 40)
    pos = broker.get_positions()["TST"]
    assert pos.shares == -40
    assert broker.cash > 100000  # 卖空所得
