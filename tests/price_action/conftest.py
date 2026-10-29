"""测试共享夹具：合成行情数据、最小配置、脚本化 MockBroker。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import pytest

from quantbot.price_action.broker.base import AccountInfo, Broker, BrokerOrderSnapshot, BrokerPosition
from quantbot.price_action.data.session import EASTERN, MarketSession
from quantbot.price_action.live.paths import runtime_paths


TEST_NOW = datetime(2026, 9, 11, 9, 35, tzinfo=EASTERN)


@dataclass
class MarketClock:
    now: datetime = TEST_NOW
    ticks: int = 0

    def recorded_at(self) -> str:
        self.ticks += 1
        return (self.now + timedelta(microseconds=self.ticks)).astimezone(timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def market_clock(monkeypatch, tmp_path):
    """固定交易日并把运行文件限制到临时目录，测试不污染用户日志。"""
    monkeypatch.chdir(tmp_path)
    clock = MarketClock()
    monkeypatch.setattr("quantbot.price_action.live.runner.market_now", lambda: clock.now)
    monkeypatch.setattr("quantbot.price_action.live.closing.market_now", lambda: clock.now)
    monkeypatch.setattr("quantbot.price_action.review.journal._utc_now", clock.recorded_at)
    monkeypatch.setattr("quantbot.price_action.live.runner.notify", lambda _: True)
    monkeypatch.setattr("quantbot.price_action.live.runner.heartbeat", lambda: True)
    monkeypatch.setattr("quantbot.price_action.live.notify.notify", lambda _: True)
    monkeypatch.setattr("quantbot.price_action.live.reconcile.notify", lambda _: True)
    return clock


def mock_runtime_paths(cfg: dict, mode: str = "dry-run"):
    paths = runtime_paths(cfg, mode, MockBroker().account_fingerprint())
    paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    return paths


def make_trending_ohlcv(
    n: int = 400,
    start_price: float = 100.0,
    end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """构造确定性的锯齿上升行情：7 天涨 1.0、3 天跌 0.4 循环。

    该形态保证：
      - 每个周期产生一个分形摆动高点与低点，且逐周期抬高（HH+HL 上升结构）
      - 收盘持续创出 55 日新高（突破触发充足）
      - 6 个月动量为正
    """
    if end is None:
        # 固定于执行日的上一交易日，禁止把当日半根 K 线当信号。
        end = pd.Timestamp(TEST_NOW).tz_localize(None).normalize() - pd.offsets.BDay(1)
    dates = pd.bdate_range(end=end, periods=n)
    closes = []
    c = start_price
    for i in range(n):
        c = c + (1.0 if i % 10 < 7 else -0.4)
        closes.append(round(c, 2))
    close = np.array(closes)
    open_ = np.concatenate([[start_price], close[:-1]])  # 开盘 = 昨收（无跳空）
    high = np.maximum(open_, close) + 0.5
    low = np.minimum(open_, close) - 0.5
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close,
         "volume": np.full(n, 1e6)},
        index=dates,
    )


def make_cfg(tmp_path=None) -> dict:
    """最小可运行配置：static 池、全部研究模块关闭、零滑点零佣金。"""
    cfg = {
        "data": {"cache_dir": "unused_cache", "start": "2020-01-01",
                 "end": "2030-12-31", "snapshot": None},
        "universe": {"mode": "static", "top_adv": 0},
        "backtest": {"start": "2020-01-01", "end": "2030-12-31",
                     "initial_capital": 100000, "slippage_bps": 0,
                     "commission_per_share": 0.0, "cash_yield": False},
        "strategy": {"breakout_window": 55, "exit_window": 45, "atr_window": 14,
                     "atr_stop_mult": 2.5, "atr_trail_mult": 4.5, "swing_k": 3,
                     "momentum_window": 126, "momentum_filter": "absolute",
                     "rank_by": "momentum", "pullback_depth_atr": 1.5,
                     "earnings_avoid_days": 0, "pyramid_max_adds": 0,
                     "pyramid_trigger_r": 2.0, "pyramid_size_frac": 0.5},
        "risk": {"risk_per_trade": 0.025, "max_positions": 10,
                 "max_weight": 0.20, "max_per_sector": 0},
        "regime": {"symbol": "SPY", "momentum_window": 126,
                   "recovery_window": None},
        "shorts": {"enabled": False},
        "base_position": {"enabled": False},
        "leverage": {"enabled": False},
        "live": {"state_file": "live_state.json", "cache_dir": "unused_live"},
    }
    if tmp_path is not None:
        cfg["live"]["state_file"] = str(tmp_path / "live_state.json")
    return cfg


class MockBroker(Broker):
    """脚本化券商：预设持仓/成交结果，记录全部下单动作。

    用法::

        broker = MockBroker(positions={"TST": 100})
        broker.wait_results["order-1"] = False   # 让第 1 笔订单失败
    """

    def __init__(self, equity: float = 100000.0, cash: float = 100000.0,
                 positions: dict[str, int] | None = None,
                 avg_prices: dict[str, float] | None = None):
        self.equity = equity
        self.cash = cash
        # {代码: 带方向股数}（负数 = 空头），模拟券商侧持仓
        self.positions: dict[str, int] = dict(positions or {})
        # {代码: 持仓均价}，未指定的默认 100.0（拆股交叉验证用）
        self.avg_prices: dict[str, float] = dict(avg_prices or {})
        self.orders: list[tuple[str, str, int]] = []  # (buy/sell, 代码, 股数)
        self.client_order_ids: list[str | None] = []
        self._orders_by_client_id: dict[str, tuple[str, str, str, int]] = {}
        self._order_status: dict[str, str] = {}
        self._order_intents: dict[str, tuple[str | None, str, str, int]] = {}
        self.external_open_orders: list[BrokerOrderSnapshot] = []
        self._next_id = 0
        # 按订单号定制的结果；未指定时 wait 默认成功、fill 默认 None
        self.wait_results: dict[str, bool] = {}
        self.fill_results: dict[str, tuple[int, float]] = {}
        self.last_prices: dict[str, float] = {}
        self.cancelled: list[str] = []           # 被撤销的订单号
        self.cancel_results: dict[str, bool] = {}

    # ---- 下单 ----
    def _submit(
        self,
        side: str,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        if client_order_id is not None and client_order_id in self._orders_by_client_id:
            oid, old_side, old_symbol, old_shares = self._orders_by_client_id[client_order_id]
            if (old_side, old_symbol, old_shares) != (side, symbol, shares):
                raise ValueError("client_order_id 被用于不同订单意图")
            return oid
        self._next_id += 1
        oid = f"order-{self._next_id}"
        self.orders.append((side, symbol, shares))
        self.client_order_ids.append(client_order_id)
        self._order_status[oid] = "submitted"
        self._order_intents[oid] = (client_order_id, side, symbol, shares)
        if client_order_id is not None:
            self._orders_by_client_id[client_order_id] = (
                oid, side, symbol, shares,
            )
        return oid

    def market_buy(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        return self._submit("buy", symbol, shares, client_order_id)

    def market_sell(
        self,
        symbol: str,
        shares: int,
        client_order_id: str | None = None,
    ) -> str:
        return self._submit("sell", symbol, shares, client_order_id)

    # ---- 查询 ----
    def get_account(self) -> AccountInfo:
        return AccountInfo(equity=self.equity, cash=self.cash,
                           buying_power=self.cash)

    def account_fingerprint(self) -> str:
        return hashlib.sha256(b"quantbot.price_action:test:mock-account").hexdigest()

    def get_market_session(self, trading_date: str) -> MarketSession:
        day = pd.Timestamp(trading_date)
        previous = (day - pd.offsets.BDay(1)).date().isoformat()
        return MarketSession(
            trading_date=trading_date, signal_date=previous,
            opens_at=datetime.combine(day.date(), datetime.min.time(), EASTERN).replace(
                hour=9, minute=30
            ).isoformat(),
            closes_at=datetime.combine(day.date(), datetime.min.time(), EASTERN).replace(
                hour=16
            ).isoformat(),
            source="alpaca:calendar",
        )

    def get_positions(self) -> dict[str, BrokerPosition]:
        return {
            s: BrokerPosition(symbol=s, shares=q,
                              avg_price=self.avg_prices.get(s, 100.0),
                              market_value=abs(q) * self.avg_prices.get(s, 100.0))
            for s, q in self.positions.items()
        }

    def _order_snapshot(self, order_id: str) -> BrokerOrderSnapshot:
        client_order_id, side, symbol, shares = self._order_intents[order_id]
        fill = self.fill_results.get(order_id)
        return BrokerOrderSnapshot(
            order_id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            quantity=shares,
            filled_quantity=fill[0] if fill else (
                shares if self._order_status[order_id] == "filled" else 0
            ),
            filled_avg_price=fill[1] if fill else None,
            status=self._order_status[order_id],
        )

    def get_order_by_client_id(
        self,
        client_order_id: str,
    ) -> BrokerOrderSnapshot | None:
        record = self._orders_by_client_id.get(client_order_id)
        if record is None:
            return None
        return self._order_snapshot(record[0])

    def list_open_orders(self) -> list[BrokerOrderSnapshot]:
        owned = [
            self._order_snapshot(order_id)
            for order_id in self._order_intents
            if self._order_snapshot(order_id).is_open
        ]
        return owned + list(self.external_open_orders)

    def wait_order(self, order_id: str, timeout: float = 60.0) -> bool:
        result = self.wait_results.get(order_id, True)
        if result and order_id in self._order_status:
            self._order_status[order_id] = "filled"
        return result

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled.append(order_id)
        result = self.cancel_results.get(order_id, True)
        if result and order_id in self._order_status:
            self._order_status[order_id] = "canceled"
        return result

    def order_fill(self, order_id: str) -> tuple[int, float] | None:
        return self.fill_results.get(order_id)

    def get_last_price(self, symbol: str) -> float | None:
        return self.last_prices.get(symbol)

    def is_market_open(self) -> bool:
        return True


@pytest.fixture
def market_data() -> dict[str, pd.DataFrame]:
    """两只合成标的：SPY 基准（开关必开）+ TST 交易标的。"""
    return {
        "SPY": make_trending_ohlcv(400, start_price=300.0),
        "TST": make_trending_ohlcv(400, start_price=100.0),
    }
