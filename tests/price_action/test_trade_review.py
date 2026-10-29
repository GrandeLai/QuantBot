"""回测交易应携带逐笔价格行为复盘字段。"""

from quantbot.price_action.backtest.engine import BacktestEngine

from .conftest import make_cfg, make_trending_ohlcv


def test_completed_trade_contains_point_in_time_review_metrics():
    spy = make_trending_ohlcv(420, start_price=300.0)
    stock = make_trending_ohlcv(420, start_price=100.0)
    cfg = make_cfg()
    cfg["backtest"]["start"] = str(spy.index[280].date())
    cfg["backtest"]["end"] = str(spy.index[-1].date())

    result = BacktestEngine({"SPY": spy, "TST": stock}, cfg).run()

    assert result.trades
    trade = result.trades[0]
    assert trade.signal_date < trade.entry_date
    assert trade.initial_entry_price > 0
    assert trade.init_risk > 0
    assert trade.initial_stop != trade.initial_entry_price
    assert trade.holding_days >= 0
    assert trade.mfe_r >= 0
    assert trade.mae_r >= 0
