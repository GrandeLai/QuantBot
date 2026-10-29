from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import io
import json
import sqlite3
import threading

from fastapi.testclient import TestClient
import pytest

from quantbot.ai.config import load_config
from quantbot.ai.domain import Allocation, DecisionBatch
from quantbot.ai.evaluation import evaluate, replay
from quantbot.ai.feeds import fetch_snapshot, stock_calendar
from quantbot.ai.local_execution import account_view, advance, create_plan, new_account, queue_plan
from quantbot.ai.models import DemoProvider
from quantbot.ai.runtime import RuntimeStore, tick
from quantbot.ai.runtime_cli import main
from quantbot.ai.transport import ProviderError
from quantbot.web.app import create_app


def write_rows(path, rows):
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


def rows_for(market, count=84):
    if market == "stocks":
        dates = stock_calendar("2025-01-01", "2025-12-31").sessions_in_range("2025-01-01", "2025-12-31")[
            :count
        ]
        return [
            {
                "date": str(at.date()),
                "symbol": "SPY",
                "open": 100 + i * 0.1,
                "high": 101 + i * 0.1,
                "low": 99 + i * 0.1,
                "close": 100.1 + i * 0.1,
                "volume": 1000000,
            }
            for i, at in enumerate(dates)
        ]
    return [
        {
            "timestamp": (datetime(2025, 1, 2, tzinfo=UTC) + timedelta(minutes=15 * i)).isoformat(),
            "symbol": "BTC/USDT",
            "bar": "15m",
            "open": 30000 + i * 10,
            "high": 30050 + i * 10,
            "low": 29950 + i * 10,
            "close": 30010 + i * 10,
            "volume": 100,
        }
        for i in range(count)
    ]


def setup(tmp_path, market="stocks", count=84):
    config = load_config(environ={})
    config["strategy"]["momentum_window"] = 20
    path = tmp_path / (market + ".csv")
    rows = rows_for(market, count)
    write_rows(path, rows)
    store = RuntimeStore(tmp_path / "runtime")
    job = store.create(
        {
            "name": "持续模拟",
            "market": market,
            "symbols": ["SPY"] if market == "stocks" else ["BTC/USDT"],
            "provider": "demo",
            "source": "csv",
            "data_path": str(path),
            "bar": "1d" if market == "stocks" else "15m",
            "interval_seconds": 1,
            "data_delay_seconds": 0,
        },
        config,
    )
    return store, job, path, rows


def close_time(market, row):
    if market == "stocks":
        return stock_calendar("2025-01-01", "2025-12-31").session_close(row["date"]).to_pydatetime()
    return datetime.fromisoformat(row["timestamp"]) + timedelta(minutes=15)


def test_runtime_serializes_concurrent_wal_initialization(tmp_path, monkeypatch):
    first_pragma = threading.Event()
    release_first = threading.Event()
    second_connected = threading.Event()
    second_pragma = threading.Event()
    connect = sqlite3.connect
    connections = []

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        connections.append(db)
        first = len(connections) == 1

        def trace(statement):
            if statement == "PRAGMA journal_mode=WAL":
                if first:
                    first_pragma.set()
                    release_first.wait(5)
                else:
                    second_pragma.set()

        db.set_trace_callback(trace)
        if not first:
            second_connected.set()
        return db

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    root = tmp_path / "concurrent-init"
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(RuntimeStore, root)
        try:
            assert first_pragma.wait(5)
            second = pool.submit(RuntimeStore, root)
            assert second_connected.wait(5)
            # The second connection exists, but must not attempt the WAL mode
            # transition while another constructor is initializing the schema.
            assert not second_pragma.wait(0.1)
        finally:
            release_first.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert second_pragma.is_set()
    with connect(root / "runtime.sqlite") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("SELECT count(*) FROM research_queue").fetchone()[0] == 0


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_runtime_preserves_unfilled_orders_without_new_provider_decision(tmp_path, market, monkeypatch):
    from quantbot.ai.runtime import _process, encode

    store, job, path, rows = setup(tmp_path, market)
    rows[-1]["volume"] = 0
    write_rows(path, rows)
    now = close_time(market, rows[-1])
    snap = fetch_snapshot(job["spec"], job["config"], now, store.root)
    previous = snap.context(snap.times[-2])
    state = new_account(job["config"]["execution"]["initial_cash"])
    advance(state, snap, previous, job["config"], job["spec"])
    symbol = job["spec"]["symbols"][0]
    decision = DecisionBatch((Allocation(symbol, 0.1, 0.8, "synthetic fixture"),), "demo", "fixture")
    plan = create_plan(state, previous, decision, job["config"], market, job["id"])
    queue_plan(state, plan)
    identifiers = [o["client_order_id"] for o in state["pending"]]
    assert identifiers
    with store.connect() as db:
        db.execute("UPDATE jobs SET account=?,stage='paper',status='active' WHERE id=?",
                   (encode(state), job["id"]))

    def forbidden(*args, **kwargs):
        raise AssertionError("a pending order must finish or expire before a new decision")

    monkeypatch.setattr("quantbot.ai.runtime.provider_for", forbidden)
    result = _process(store, job["id"], snap, now)
    assert result["status"] == "awaiting_fills"
    assert result["provider_calls"] == 0
    assert [o["client_order_id"] for o in store.get(job["id"])["account"]["pending"]] == identifiers
    assert store.history(job["id"])["cycles"][0]["status"] == "awaiting_fills"


def test_automatic_runtime_hold_cancels_entries_but_preserves_exit_orders(tmp_path):
    from quantbot.ai.runtime import encode

    store, job, _, _ = setup(tmp_path)
    state = job["account"]
    state["pending"] = [
        {"client_order_id": "entry", "symbol": "SPY", "side": "buy", "quantity": "1"},
        {"client_order_id": "protection", "symbol": "SPY", "side": "sell", "quantity": "1"},
    ]
    with store.connect() as db:
        db.execute("UPDATE jobs SET account=? WHERE id=?", (encode(state), job["id"]))
    store.hold(job["id"], "synthetic runtime failure")
    held = store.get(job["id"])
    assert held["status"] == "held"
    assert [o["client_order_id"] for o in held["account"]["pending"]] == ["protection"]


@pytest.mark.parametrize("market", ["stocks", "crypto"])
@pytest.mark.parametrize("action", ["buy", "sell", "stop"])
def test_zero_volume_bar_cannot_fill_orders_or_protective_stops(tmp_path, market, action):
    store, job, path, rows = setup(tmp_path, market)
    rows[-1]["volume"] = 0
    write_rows(path, rows)
    snap = fetch_snapshot(job["spec"], job["config"], close_time(market, rows[-1]), store.root)
    previous = snap.context(snap.times[-2])
    state = new_account(job["config"]["execution"]["initial_cash"])
    advance(state, snap, previous, job["config"], job["spec"])
    symbol = job["spec"]["symbols"][0]
    quantity = "1" if market == "stocks" else "0.01"
    if action in {"sell", "stop"}:
        basis = Decimal(str(previous.prices[symbol]))
        state["positions"] = {symbol: quantity}
        state["average_cost"] = {symbol: str(basis)}
        state["cash"] = str(Decimal(state["cash"]) - basis * Decimal(quantity))
        state["stops"] = {symbol: str(rows[-1]["low"] + 0.1 if action == "stop" else 1)}
    if action != "stop":
        state["pending"] = [{"client_order_id": "zero-volume-test", "symbol": symbol, "side": action,
                             "quantity": quantity, "limit_price": str(rows[-1]["open"] * 2), "stop": "1"}]
    before = deepcopy(state)
    events = advance(state, snap, snap.context(), job["config"], job["spec"])
    assert not events
    for field in ("cash", "positions", "average_cost", "stops", "realized_pnl", "costs"):
        assert state[field] == before[field]
    assert len(state["pending"]) == 1
    assert state["pending"][0]["remaining"] == quantity


@pytest.mark.parametrize("market", ["stocks", "crypto"])
def test_continuous_next_bar_restart_idempotency_and_pnl(tmp_path, monkeypatch, market):
    store, job, path, rows = setup(tmp_path, market)
    report = evaluate(store, job["id"], last_bars=4)
    assert report["passed"] and report["candidate"]["provider_calls"] == 3
    store.control(job["id"], "resume")
    calls = []
    original = DemoProvider.decide

    def counted(self, *args):
        calls.append(args[0].as_of)
        return original(self, *args)

    monkeypatch.setattr(DemoProvider, "decide", counted)
    now = close_time(market, rows[-1]) + timedelta(seconds=1)
    assert tick(store, now=now)[0]["account"]["positions"] == {}
    first = store.get(job["id"])
    assert first["account"]["pending"]
    assert tick(RuntimeStore(store.root), now=now + timedelta(seconds=1)) == []
    assert len(calls) == 1
    rows = rows_for(market, 85)
    write_rows(path, rows)
    second = tick(RuntimeStore(store.root), now=close_time(market, rows[-1]) + timedelta(seconds=1))[0]
    assert len(calls) == 2
    fills = [o for o in second["orders"] if o["status"] == "filled"]
    opening = (
        stock_calendar("2025-01-01", "2025-12-31").session_open(rows[-1]["date"]).to_pydatetime()
        if market == "stocks"
        else datetime.fromisoformat(rows[-1]["timestamp"])
    )
    assert fills and fills[0]["at"] == opening.isoformat()
    view = store.get(job["id"])["account_view"]
    assert view["positions"]
    if market == "stocks":
        assert all(Decimal(q) == int(Decimal(q)) for q in view["positions"].values())
    else:
        assert Decimal(view["positions"]["BTC/USDT"]) % Decimal("0.0001") == 0
    assert view["equity"] == pytest.approx(100000 + view["realized_pnl"] + view["unrealized_pnl"])
    assert view["estimated_execution_cost"] > 0


def test_stock_calendar_early_close_holiday_and_stale_gap(tmp_path):
    store, job, path, _ = setup(tmp_path)
    dates = stock_calendar("2025-01-01", "2025-12-31").sessions_in_range("2025-01-01", "2025-07-03")
    rows = rows_for("stocks", len(dates))
    write_rows(path, rows)
    now = datetime(2025, 7, 4, 19, tzinfo=UTC)
    snapshot = fetch_snapshot(job["spec"], job["config"], now, store.root)
    assert snapshot.context().as_of == datetime(2025, 7, 3, 17, tzinfo=UTC)
    with pytest.raises(ValueError, match="过期"):
        fetch_snapshot(job["spec"], job["config"], datetime(2025, 7, 7, 21, tzinfo=UTC), store.root)
    write_rows(path, rows[:70] + rows[71:])
    with pytest.raises(ValueError, match="缺少交易日"):
        fetch_snapshot(job["spec"], job["config"], now, store.root)
    write_rows(path, rows)
    with pytest.raises(ValueError, match="尚未收盘"):
        fetch_snapshot(job["spec"], job["config"], datetime(2025, 7, 3, 16, tzinfo=UTC), store.root)


def test_crypto_stale_gap_and_unclosed_bars(tmp_path):
    store, job, path, rows = setup(tmp_path, "crypto")
    at = close_time("crypto", rows[-1])
    with pytest.raises(ValueError, match="过期"):
        fetch_snapshot(job["spec"], job["config"], at + timedelta(minutes=20), store.root)
    with pytest.raises(ValueError, match="尚未收盘"):
        fetch_snapshot(job["spec"], job["config"], at - timedelta(seconds=1), store.root)
    write_rows(path, rows[:40] + rows[41:])
    with pytest.raises(ValueError, match="不连续"):
        fetch_snapshot(job["spec"], job["config"], at, store.root)


def test_pause_during_inference_discards_plan(tmp_path, monkeypatch):
    store, job, _, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")
    original = DemoProvider.decide

    def pause(self, *args):
        store.control(job["id"], "pause")
        return original(self, *args)

    monkeypatch.setattr(DemoProvider, "decide", pause)
    tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    assert store.get(job["id"])["status"] == "paused"
    assert not store.get(job["id"])["account"]["pending"]
    assert store.history(job["id"])["cycles"][0]["status"] == "discarded"


def test_interrupted_model_recovery_is_held_not_retried(tmp_path, monkeypatch):
    store, job, _, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")

    def crash(*args):
        raise SystemExit("process terminated")

    monkeypatch.setattr(DemoProvider, "decide", crash)
    at = close_time("stocks", rows[-1]) + timedelta(seconds=1)
    with pytest.raises(SystemExit):
        tick(store, now=at)
    assert store.history(job["id"])["cycles"][0]["status"] == "started"
    with monkeypatch.context() as interrupted_recovery:
        interrupted_recovery.setattr(RuntimeStore, "event", staticmethod(crash))
        with pytest.raises(SystemExit):
            tick(store, now=at + timedelta(seconds=2))
    assert store.history(job["id"])["cycles"][0]["status"] == "started"
    assert store.get(job["id"])["status"] == "active"
    tick(RuntimeStore(store.root), now=at + timedelta(seconds=2))
    recovered = store.get(job["id"])
    assert recovered["status"] == "held" and "中断" in recovered["last_error"]
    assert not recovered["account"]["pending"]
    assert store.history(job["id"])["cycles"][0]["status"] == "interrupted"


def test_model_failure_circuit_and_resume_authorization(tmp_path, monkeypatch):
    store, job, path, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")

    def fail(*args):
        raise ProviderError("测试模型失败")

    monkeypatch.setattr(DemoProvider, "decide", fail)
    for count in (84, 85, 86):
        rows = rows_for("stocks", count)
        write_rows(path, rows)
        tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    current = store.get(job["id"])
    assert current["failures"] == 3 and current["status"] == "held"
    assert not current["account"]["pending"]
    with pytest.raises(ValueError, match="确认"):
        store.control(job["id"], "resume")
    store.control(job["id"], "resume", acknowledge_risk=True)
    assert store.get(job["id"])["status"] == "active"


def test_data_revision_and_disconnect_cancel_pending_and_alert_once(tmp_path):
    store, job, path, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")
    now = close_time("stocks", rows[-1]) + timedelta(seconds=1)
    tick(store, now=now)
    rows[-1]["close"] += 0.01
    write_rows(path, rows)
    tick(store, now=now + timedelta(seconds=1))
    assert store.get(job["id"])["status"] == "held"
    assert not store.get(job["id"])["account"]["pending"]
    alerts = [e for e in store.history(job["id"])["events"] if e["kind"] == "alert"]
    tick(store, now=now + timedelta(seconds=2))
    assert len([e for e in store.history(job["id"])["events"] if e["kind"] == "alert"]) == len(alerts)
    path.unlink()
    tick(store, now=now + timedelta(seconds=3))
    assert store.get(job["id"])["last_error"] == "FileNotFoundError"


def test_risk_limits_cancel_new_orders_but_keep_stops(tmp_path):
    store, job, path, rows = setup(tmp_path)
    spec = {**job["spec"], "max_daily_loss": 0.0001}
    job = store.create(spec, load_config(environ={}) | {"strategy": job["config"]["strategy"]})
    # 使用相同的风险阈值评估；无成交成本导致的下跌区间才准入。
    config = job["config"]
    snap = fetch_snapshot(job["spec"], config, close_time("stocks", rows[-1]), store.root)
    state = new_account(config["execution"]["initial_cash"])
    context = snap.context()
    advance(state, snap, context, config, spec)
    plan = create_plan(state, context, DemoProvider().decide(context, None, config), config, "stocks", "test")
    queue_plan(state, plan)
    newer = rows_for("stocks", 85)
    newer[-1].update(open=rows[-1]["close"], low=95, high=110, close=96)
    write_rows(path, newer)
    snap = fetch_snapshot(job["spec"], config, close_time("stocks", newer[-1]), store.root)
    events = advance(state, snap, snap.context(), config, spec)
    assert any(e.get("reason") == "protective_stop" for e in events)
    assert state["risk_latched"] == "daily_loss_limit"
    assert account_view(state)["equity"] < config["execution"]["initial_cash"]


def test_manual_exit_next_bar_and_lifecycle_rollback(tmp_path):
    store, job, path, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")
    tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    rows = rows_for("stocks", 85)
    write_rows(path, rows)
    tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    exit_job = store.control(job["id"], "exit")
    assert exit_job["status"] == "closing" and exit_job["account"]["positions"]
    canceled = store.cancel_order(job["id"], exit_job["account"]["pending"][0]["client_order_id"])
    assert canceled["status"] == "paused" and canceled["account"]["positions"]
    store.control(job["id"], "exit")
    rows = rows_for("stocks", 86)
    write_rows(path, rows)
    tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    assert store.get(job["id"])["status"] == "paused"
    assert not store.get(job["id"])["account"]["positions"]
    child = store.create({**job["spec"], "name": "修订"}, job["config"], parent=job["id"])
    assert child["account"]["positions"] == {} and child["stage"] == "candidate"
    evaluate(store, child["id"], last_bars=3)
    store.control(child["id"], "resume")
    store.control(job["id"], "resume")
    assert store.get(child["id"])["status"] == "paused"
    assert store.get(job["id"])["status"] == "active"


def test_replay_no_future_data_and_crypto_baseline(tmp_path):
    store, job, _, rows = setup(tmp_path, "crypto")
    snapshot = fetch_snapshot(job["spec"], job["config"], close_time("crypto", rows[-1]), store.root)

    class CheckedProvider:
        def decide(self, context, account, config):
            index = snapshot.times.index(context.as_of)
            assert context.prices["BTC/USDT"] == rows[index]["close"]
            return DecisionBatch(
                (Allocation("BTC/USDT", 0.2, 0.9, "controlled allocation"),), "controlled", "fixture"
            )

    report = replay(snapshot, job["spec"], job["config"], CheckedProvider(), last_bars=5)
    assert report["provider_calls"] == 4 and len(report["equity_curve"]) == 5
    fills = [e for e in report["orders"] if e["status"] == "filled"]
    assert fills and fills[0]["at"] >= report["decisions"][0]["as_of"]
    assert report["account"]["pending_orders"] == []
    frozen = snapshot.freeze(store.root / "snapshots")
    assert snapshot.freeze(store.root / "snapshots") == frozen
    from pathlib import Path

    Path(frozen["path"]).write_text("corrupt")
    with pytest.raises(ValueError, match="指纹"):
        snapshot.freeze(store.root / "snapshots")


def test_ai_evaluation_requires_explicit_authorization(tmp_path):
    store, job, _, _ = setup(tmp_path)
    ai = store.create({**job["spec"], "provider": "ai"}, job["config"])
    with pytest.raises(ValueError, match="明确允许"):
        evaluate(store, ai["id"], last_bars=3)
    with pytest.raises(ValueError, match="历史评估"):
        store.control(ai["id"], "resume", allow_model_calls=True)


def test_http_lifecycle_and_cli_read_same_store(tmp_path):
    _, job, path, _ = setup(tmp_path)
    config = load_config(environ={})
    config["strategy"]["momentum_window"] = 20
    with TestClient(create_app(config=config, runtime_dir=tmp_path / "web")) as client:
        created = client.post("/api/quantbot/runtime/jobs", json={"spec": job["spec"], "initial_cash": 10000})
        assert created.status_code == 200
        job_id = created.json()["id"]
        assert (
            client.post(f"/api/quantbot/runtime/jobs/{job_id}/control", json={"action": "resume"}).status_code
            == 400
        )
        report = client.post(f"/api/quantbot/runtime/jobs/{job_id}/evaluate", json={"last_bars": 3})
        assert report.status_code == 200 and report.json()["passed"]
        assert (
            client.post(f"/api/quantbot/runtime/jobs/{job_id}/control", json={"action": "resume"}).status_code
            == 200
        )
        detail = client.get(f"/api/quantbot/runtime/jobs/{job_id}").json()
        assert len(detail["evaluations"]) == 1
        assert client.get("/api/quantbot/runtime").json()["live_enabled"] is False
    output = io.StringIO()
    assert main(["--root", str(tmp_path / "web" / "automation"), "list"], stdout=output) == 0
    assert json.loads(output.getvalue())["jobs"][0]["id"] == job_id


def test_public_crypto_feed_filters_incomplete_and_http_error_holds(tmp_path, monkeypatch):
    store, job, _, rows = setup(tmp_path, "crypto")
    spec = {**job["spec"], "source": "okx_public"}
    raw = [
        [
            str(int(datetime.fromisoformat(r["timestamp"]).timestamp() * 1000)),
            *[str(r[k]) for k in ("open", "high", "low", "close", "volume")],
            "0",
            "0",
            "1",
        ]
        for r in rows
    ]
    incomplete = deepcopy(raw[-1])
    incomplete[0] = str(int(incomplete[0]) + 900000)
    incomplete[-1] = "0"
    from quantbot import __version__
    from quantbot.ai.transport import JsonTransport

    def public_response(self, url, headers=None, params=None):
        # 公开端点会拒绝缺少应用标识的默认 Python 客户端请求。
        if headers != {"User-Agent": f"QuantBot/{__version__}"}:
            raise ProviderError("接口 HTTP 403", status=403)
        assert url == "https://www.okx.com/api/v5/market/candles"
        assert params == {"instId": "BTC-USDT", "bar": "15m", "limit": 300}
        return {"code": "0", "data": [incomplete, *reversed(raw)]}

    monkeypatch.setattr(JsonTransport, "get_json", public_response)
    snapshot = fetch_snapshot(spec, job["config"], close_time("crypto", rows[-1]), store.root)
    assert len(snapshot.rows) == len(rows)
    monkeypatch.setattr(JsonTransport, "get_json", lambda *a, **kw: {"code": "50000", "data": []})
    with pytest.raises(ValueError, match="返回失败"):
        fetch_snapshot(spec, job["config"], close_time("crypto", rows[-1]), store.root)


def test_cancel_pending_order_prevents_later_fill(tmp_path):
    store, job, path, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")
    tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    client_id = store.get(job["id"])["account"]["pending"][0]["client_order_id"]
    store.cancel_order(job["id"], client_id)
    with pytest.raises(ValueError, match="已取消"):
        store.cancel_order(job["id"], client_id)
    rows = rows_for("stocks", 85)
    write_rows(path, rows)
    results = tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    assert not any(o["status"] == "filled" for o in results[0]["orders"])
    assert store.get(job["id"])["account"]["positions"] == {}


def test_runtime_drawdown_latches_and_resume_preserves_high_water_mark(tmp_path):
    store, initial, path, rows = setup(tmp_path)
    job = store.create({**initial["spec"], "max_drawdown": 0.001}, initial["config"])
    assert evaluate(store, job["id"], last_bars=3)["passed"]
    store.control(job["id"], "resume")
    for count in (84, 85):
        rows = rows_for("stocks", count)
        write_rows(path, rows)
        tick(store, now=close_time("stocks", rows[-1]) + timedelta(seconds=1))
    newer = rows_for("stocks", 86)
    newer[-1].update(open=100, high=101, low=95, close=96)
    write_rows(path, newer)
    tick(store, now=close_time("stocks", newer[-1]) + timedelta(seconds=1))
    held = store.get(job["id"])
    assert held["status"] == "held" and held["last_error"] == "drawdown_limit"
    assert held["account"]["pending"] == []
    peak = held["account"]["peak"]
    store.control(job["id"], "resume", acknowledge_risk=True)
    assert store.get(job["id"])["account"]["peak"] == peak
    newer.append(rows_for("stocks", 87)[-1])
    write_rows(path, newer)
    tick(store, now=close_time("stocks", newer[-1]) + timedelta(seconds=1))
    assert store.get(job["id"])["status"] == "held"


def test_worker_lock_blocks_duplicate_cycle_and_allows_read(tmp_path):
    from quantbot.ai.journal import runtime_lock

    store, job, _, rows = setup(tmp_path)
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")
    with runtime_lock(store.root / "worker-lock"):
        assert store.get(job["id"])["status"] == "active"
        with pytest.raises(RuntimeError, match="已有 QuantBot 进程"):
            tick(store, now=close_time("stocks", rows[-1]))
    assert store.history(job["id"])["cycles"] == []


def test_csv_crypto_timezone_required_before_normalization(tmp_path):
    store, job, path, rows = setup(tmp_path, "crypto")
    now = close_time("crypto", rows[-1])
    rows[-1]["timestamp"] = rows[-1]["timestamp"].replace("+00:00", "")
    write_rows(path, rows)
    with pytest.raises(ValueError, match="必须带时区"):
        fetch_snapshot(job["spec"], job["config"], now, store.root)


def test_ai_protocol_flows_through_evaluation_and_worker_with_authorization(tmp_path, monkeypatch):
    from quantbot.ai.transport import JsonTransport

    store, prior, _, rows = setup(tmp_path, "crypto")
    config = prior["config"]
    config["llm"].update(base_url="http://127.0.0.1:9999/v1", model="controlled-model")
    job = store.create({**prior["spec"], "provider": "ai"}, config)
    requests = []

    def respond(self, url, headers=None, payload=None):
        market = json.loads(payload["messages"][1]["content"])["market"]
        requests.append(payload["model"])
        content = {
            "snapshot_id": market["snapshot_id"],
            "as_of": market["as_of"],
            "decisions": [
                {"symbol": s, "target_weight": 0.2, "confidence": 0.9, "rationale": "受控协议测试"}
                for s in market["features"]
            ],
        }
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
            "usage": {"total_tokens": 17},
        }

    monkeypatch.setattr(JsonTransport, "post_json", respond)
    report = evaluate(store, job["id"], last_bars=3, allow_model_calls=True)
    assert report["passed"] and report["candidate"]["reported_tokens"] == 34
    with pytest.raises(ValueError, match="明确允许"):
        store.control(job["id"], "resume")
    store.control(job["id"], "resume", allow_model_calls=True)
    result = tick(store, now=close_time("crypto", rows[-1]) + timedelta(seconds=1))[0]
    assert result["plan"]["model"] == "controlled-model"
    assert len(requests) == 3 and store.get(job["id"])["account"]["pending"]


def test_worker_heartbeat_and_graceful_stop(tmp_path):
    import threading
    import time
    from quantbot.ai.runtime import run_worker

    store = RuntimeStore(tmp_path / "worker")
    stop = threading.Event()
    worker = threading.Thread(target=run_worker, args=(store, stop), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not store.overview()["worker_online"] and time.monotonic() < deadline:
            stop.wait(0.02)
        assert store.overview()["worker_online"]
    finally:
        stop.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not store.overview()["worker_online"]


def test_worker_once_failure_exit_code_matches_held_account(tmp_path):
    store, job, _, _ = setup(tmp_path, "crypto")
    evaluate(store, job["id"], last_bars=3)
    store.control(job["id"], "resume")
    output = io.StringIO()
    assert main(["--root", str(store.root), "worker", "--once"], stdout=output) == 2
    result = json.loads(output.getvalue())
    assert result[0]["status"] == "held"
    assert "过期" in result[0]["error"]
