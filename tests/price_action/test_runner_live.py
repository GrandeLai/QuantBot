"""Runner 状态迁移测试：MockBroker 脚本化真金白银路径的失败场景。

覆盖此前零测试的关键路径：
    - 止损离场：成交确认后才删状态
    - 卖单被拒：状态与止损原样保留 + 告警
    - 买单被拒：不产生幽灵持仓
    - 成交价回填：state 记录真实成交均价而非报价
    - 当日幂等：重复运行不重复下单
    - 拆股侦测：股数突变按比例缩放价格状态
    - 持仓数据缺失：告警且不误动持仓
    - 数据陈旧：整体熔断不交易
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

import quantbot.price_action.live.runner as runner_mod
from quantbot.price_action.broker.base import BrokerOrderSnapshot
from quantbot.price_action.live.recovery import RecoveryHoldError
from quantbot.price_action.live.runner import LiveRunner
from quantbot.price_action.strategy.trend_breakout import EntrySignal

from .conftest import TEST_NOW, MockBroker, make_cfg, make_trending_ohlcv, mock_runtime_paths


@pytest.fixture
def notifications(monkeypatch):
    """捕获 notify 调用（runner 模块内引用）。"""
    sent: list[str] = []
    monkeypatch.setattr(runner_mod, "notify", lambda text: sent.append(text) or True)
    monkeypatch.setattr(runner_mod, "heartbeat", lambda: True)
    return sent


def run(broker, cfg, data, force=True):
    """跑一次 runner（force 跳过交易时段检查，数据注入跳过下载）。"""
    LiveRunner(broker, cfg, mode="dry-run").run_once(force=force, data=data)


def read_state(cfg) -> tuple[dict, dict]:
    raw = json.loads(mock_runtime_paths(cfg).state_file.read_text(encoding="utf-8"))
    return raw["meta"], raw["positions"]


def stop_above_low(data, sym="TST") -> float:
    """构造一个必然触发硬止损的止损价（高于昨日最低价）。"""
    return float(data[sym].iloc[-1]["low"]) + 0.1


def stop_below_low(data, sym="TST") -> float:
    """构造一个不会触发的止损价（远低于昨日最低价）。"""
    return float(data[sym].iloc[-1]["low"]) - 50.0


def base_state(stop: float, qty: int = 100) -> dict:
    return {
        "stop": stop, "highest_close": 120.0, "entry_price": 100.0,
        "entry_date": "2025-01-01", "init_risk": 5.0, "adds": 0,
        "direction": 1, "qty": qty,
    }


def write_state(cfg, positions: dict, meta: dict | None = None) -> None:
    with mock_runtime_paths(cfg).state_file.open("w", encoding="utf-8") as f:
        json.dump({"meta": meta or {}, "positions": positions}, f)


def test_state_save_does_not_claim_commit_when_directory_fsync_cannot_start(
    tmp_path, monkeypatch
):
    cfg = make_cfg(tmp_path)
    runner = LiveRunner(MockBroker(), cfg, mode="dry-run")
    runner.state_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runner_mod.os,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("directory fsync unavailable")
        ),
    )

    with pytest.raises(OSError, match="directory fsync unavailable"):
        runner._save_state({}, {})


# ---------------- 离场路径 ----------------

def test_stop_exit_removes_state_after_fill(tmp_path, market_data, notifications):
    """止损触发 → 卖单成交 → 状态删除。"""
    cfg = make_cfg(tmp_path)
    write_state(cfg, {"TST": base_state(stop_above_low(market_data))})
    broker = MockBroker(positions={"TST": 100})
    run(broker, cfg, market_data)
    assert ("sell", "TST", 100) in broker.orders
    _, positions = read_state(cfg)
    assert "TST" not in positions


def test_rejected_sell_keeps_state_and_alerts(tmp_path, market_data, notifications):
    """卖单被拒 → 状态与原止损保留 + 推送告警（不静默放宽风控）。"""
    cfg = make_cfg(tmp_path)
    stop = stop_above_low(market_data)
    write_state(cfg, {"TST": base_state(stop)})
    broker = MockBroker(positions={"TST": 100})
    broker.wait_results["order-1"] = False  # 第一笔（卖单）失败
    run(broker, cfg, market_data)
    _, positions = read_state(cfg)
    assert "TST" in positions, "卖单失败时状态不得删除"
    assert positions["TST"]["stop"] == stop, "止损不得被重建放宽"
    assert any("未成交" in m for m in notifications)


def test_no_exit_updates_trailing_stop(tmp_path, market_data, notifications):
    """未触发离场时吊灯止损只上移。"""
    cfg = make_cfg(tmp_path)
    stop = stop_below_low(market_data)
    write_state(cfg, {"TST": base_state(stop)})
    broker = MockBroker(positions={"TST": 100})
    run(broker, cfg, market_data)
    _, positions = read_state(cfg)
    assert positions["TST"]["stop"] >= stop
    assert broker.orders == []  # 无任何下单


# ---------------- 入场路径 ----------------

def _force_entry_signal(monkeypatch, stop: float):
    """让 TST 必然产生入场信号（聚焦测试订单生命周期而非信号生成）。"""
    monkeypatch.setattr(
        runner_mod, "entry_signal",
        lambda row, level, cfg, bench_mom=None: EntrySignal(
            rank=1.0, stop=stop, trigger="breakout"),
    )
    monkeypatch.setattr(runner_mod, "UNIVERSE", ["TST"])


def test_rejected_buy_leaves_no_ghost_state(tmp_path, market_data,
                                            notifications, monkeypatch):
    """买单被拒 → 不写入状态（无幽灵持仓）+ 告警。"""
    cfg = make_cfg(tmp_path)
    _force_entry_signal(monkeypatch, stop=50.0)
    broker = MockBroker()
    broker.wait_results["order-1"] = False
    run(broker, cfg, market_data)
    _, positions = read_state(cfg)
    assert positions == {}
    assert any("未成交" in m for m in notifications)


def test_filled_buy_backfills_actual_price(tmp_path, market_data,
                                           notifications, monkeypatch):
    """买单成交 → state 记录真实成交均价与股数，而非下单时的报价。"""
    cfg = make_cfg(tmp_path)
    _force_entry_signal(monkeypatch, stop=50.0)
    broker = MockBroker()
    broker.fill_results["order-1"] = (37, 123.45)  # 实际成交 37 股 @123.45
    run(broker, cfg, market_data)
    _, positions = read_state(cfg)
    assert "TST" in positions
    assert positions["TST"]["entry_price"] == 123.45
    assert positions["TST"]["qty"] == 37
    assert positions["TST"]["init_risk"] == pytest.approx(123.45 - 50.0)


def test_confirmed_fill_without_fill_details_is_uncertain(tmp_path):
    cfg = make_cfg(tmp_path)
    broker = MockBroker()
    order_id = broker.market_buy("TST", 10, "client-missing-fill")

    status, fill = LiveRunner(broker, cfg, mode="paper")._settle_order(
        order_id, "client-missing-fill"
    )

    assert (status, fill) == ("uncertain", None)


def test_canceled_order_with_quantity_but_missing_average_is_uncertain(tmp_path):
    cfg = make_cfg(tmp_path)

    class MissingAverageBroker(MockBroker):
        def get_order_by_client_id(self, client_order_id):
            return BrokerOrderSnapshot(
                order_id="order-1",
                client_order_id=client_order_id,
                symbol="TST",
                side="buy",
                quantity=10,
                filled_quantity=3,
                filled_avg_price=None,
                status="canceled",
            )

    broker = MissingAverageBroker()
    order_id = broker.market_buy("TST", 10, "client-partial-unknown")
    broker.wait_results[order_id] = False

    status, fill = LiveRunner(broker, cfg, mode="paper")._settle_order(
        order_id, "client-partial-unknown"
    )

    assert (status, fill) == ("uncertain", None)


def test_gap_protection_skips_entry(tmp_path, market_data,
                                    notifications, monkeypatch):
    """现价已低于止损（跳空）→ 放弃入场，不下单。"""
    cfg = make_cfg(tmp_path)
    _force_entry_signal(monkeypatch, stop=50.0)
    broker = MockBroker()
    broker.last_prices["TST"] = 49.0  # 现价低于止损
    run(broker, cfg, market_data)
    assert broker.orders == []


# ---------------- 幂等与熔断 ----------------

def test_idempotent_second_run(tmp_path, market_data, notifications):
    """同一信号基准日第二次运行：不下单、不改状态。"""
    cfg = make_cfg(tmp_path)
    write_state(cfg, {"TST": base_state(stop_above_low(market_data))})
    broker = MockBroker(positions={"TST": 100})
    run(broker, cfg, market_data)          # 第一次：触发止损卖出
    n_orders = len(broker.orders)
    broker.positions = {"TST": 100}        # 假设仍持仓（考验幂等而非持仓同步）
    run(broker, cfg, market_data, force=False)  # 第二次：应被幂等保护跳过
    assert len(broker.orders) == n_orders, "第二次运行不得重复下单"


def test_stale_data_circuit_breaker(tmp_path, notifications):
    """数据陈旧（基准日距今 > 4 天）→ 熔断不交易 + 告警。"""
    cfg = make_cfg(tmp_path)
    old_end = pd.Timestamp(TEST_NOW).tz_localize(None).normalize() - pd.Timedelta(days=30)
    stale = {
        "SPY": make_trending_ohlcv(400, 300.0, end=old_end),
        "TST": make_trending_ohlcv(400, 100.0, end=old_end),
    }
    broker = MockBroker(positions={"TST": 100})
    run(broker, cfg, stale)
    assert broker.orders == []
    assert any("陈旧" in m for m in notifications)


# ---------------- 拆股与数据缺失 ----------------

def test_split_detection_scales_stop(tmp_path, market_data, notifications):
    """真实 4:1 拆股（股数 ×4 且券商均价同比 ÷4）→ 价格状态缩为 1/4。"""
    cfg = make_cfg(tmp_path)
    st = base_state(stop=stop_below_low(market_data) + 40.0, qty=100)
    orig_stop = st["stop"]
    write_state(cfg, {"TST": st})
    # 拆股特征：均价 100 → 25，与股数 100 → 400 同比例反向
    broker = MockBroker(positions={"TST": 400}, avg_prices={"TST": 25.0})
    run(broker, cfg, market_data)
    _, positions = read_state(cfg)
    assert positions["TST"]["entry_price"] == pytest.approx(100.0 / 4)
    assert positions["TST"]["qty"] == 400
    assert positions["TST"]["stop"] >= orig_stop / 4 - 1e-9
    assert any("拆股" in m for m in notifications)


def test_qty_jump_without_split_freezes(tmp_path, market_data, notifications):
    """股数突变但均价不符合拆股特征（如订单晚成交）→ 冻结不缩放不下单。"""
    cfg = make_cfg(tmp_path)
    st = base_state(stop=stop_above_low(market_data), qty=100)  # 止损本应触发
    orig = dict(st)
    write_state(cfg, {"TST": st})
    # 股数 100 → 160 但均价仍 100（非拆股：疑似昨日加仓单晚成交）
    broker = MockBroker(positions={"TST": 160}, avg_prices={"TST": 100.0})
    run(broker, cfg, market_data)
    _, positions = read_state(cfg)
    # 价格状态不得被缩放，且冻结当日不下任何单（哪怕止损条件满足）
    assert positions["TST"]["entry_price"] == orig["entry_price"]
    assert positions["TST"]["stop"] == orig["stop"]
    assert broker.orders == []
    assert any("股数异常" in m for m in notifications)


def test_failed_exit_blocks_pyramid_add(tmp_path, market_data, notifications):
    """离场单失败的持仓当日绝不允许金字塔加仓（防对必离场股票反向买入）。"""
    cfg = make_cfg(tmp_path)
    cfg["strategy"]["pyramid_max_adds"] = 2
    # 止损触发（低于昨日低点），同时浮盈满足加仓条件（entry 远低于现价）
    st = base_state(stop=stop_above_low(market_data), qty=100)
    st["entry_price"] = 10.0
    st["init_risk"] = 5.0
    write_state(cfg, {"TST": st})
    broker = MockBroker(positions={"TST": 100})
    broker.wait_results["order-1"] = False   # 离场卖单失败（撤单成功=终态未成交）
    run(broker, cfg, market_data)
    buys = [o for o in broker.orders if o[0] == "buy" and o[1] == "TST"]
    assert buys == [], "离场失败的持仓不得被加仓买入"


def test_entry_timeout_cancels_and_no_double_size(tmp_path, market_data,
                                                  notifications, monkeypatch):
    """入场单超时 → 先撤单；撤单失败且查不到成交（状态不明）→ 停止后续入场。"""
    cfg = make_cfg(tmp_path)
    _force_entry_signal(monkeypatch, stop=50.0)
    monkeypatch.setattr(runner_mod, "UNIVERSE", ["TST", "TS2"])
    data = dict(market_data)
    data["TS2"] = market_data["TST"].copy()   # 第二个候选（正常应两笔都买）
    broker = MockBroker()
    broker.wait_results["order-1"] = False    # 第一笔超时
    broker.cancel_results["order-1"] = False  # 撤单也失败 → 状态不明
    with pytest.raises(RecoveryHoldError, match="启动恢复门禁 HOLD"):
        run(broker, cfg, data)
    assert "order-1" in broker.cancelled, "超时订单必须先尝试撤单"
    assert len([o for o in broker.orders if o[0] == "buy"]) == 1, \
        "状态不明时必须停止后续入场（防双倍建仓）"
    assert any("状态不明" in m for m in notifications)


def test_missing_data_position_alerts(tmp_path, market_data, notifications):
    """持仓股票无行情数据 → 告警且不对其下任何单。"""
    cfg = make_cfg(tmp_path)
    write_state(cfg, {"GHOST": base_state(stop=999.0)})
    broker = MockBroker(positions={"GHOST": 100})
    run(broker, cfg, market_data)
    assert all(sym != "GHOST" for _, sym, _ in broker.orders)
    assert any("数据缺失" in m for m in notifications)
