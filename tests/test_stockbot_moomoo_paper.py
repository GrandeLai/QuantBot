import fcntl
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.moomoo_paper import (
    EASTERN,
    DEFAULT_MOOMOO_CANARY_SYMBOLS,
    MoomooPaperCanary,
    MoomooPaperCanaryConfig,
    MoomooPaperCanaryHalt,
)


class MutableClock:
    def __init__(self, value):
        self.value = value
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class FakeMoomooClient:
    def __init__(
        self,
        clock,
        *,
        session_type="WHOLE",
        positions=None,
        orders=None,
        frozen_cash=0,
        equity=1_000_000,
        power=1_000_000,
        quote_age_seconds=0,
        buy_behavior="filled",
        sell_behavior="filled",
        cancel_behavior="acknowledged",
        state_path=None,
    ):
        self.clock = clock
        self.session_type = session_type
        self.positions = {row["code"]: dict(row) for row in (positions or [])}
        self.orders = {
            str(row["order_id"]): dict(row) for row in (orders or [])
        }
        self.frozen_cash = frozen_cash
        self.equity = equity
        self.power = power
        self.quote_age_seconds = quote_age_seconds
        self.buy_behavior = buy_behavior
        self.sell_behavior = sell_behavior
        self.cancel_behavior = cancel_behavior
        self.state_path = Path(state_path) if state_path else None
        self.place_calls = []
        self.cancel_calls = []
        self.query_calls = []
        self.reconcile_calls = []
        self.place_wal_states = []
        self.next_order_id = 1000
        self.reconcile_result = None

    def snapshot(self):
        return {
            "broker": "moomoo",
            "trd_env": "SIMULATE",
            "market": "US",
            "acc_id": 1234,
            "account": {
                "total_assets": self.equity,
                "power": self.power,
                "frozen_cash": self.frozen_cash,
            },
            "positions": [dict(row) for row in self.positions.values()],
            "orders": [dict(row) for row in self.orders.values()],
        }

    def account_id(self):
        return "1234"

    def trading_day(self, day):
        return {
            "time": day.isoformat(),
            "trade_date_type": self.session_type,
        }

    def market_snapshot(self, codes):
        observed = self.clock.now() - timedelta(seconds=self.quote_age_seconds)
        return [
            {
                "code": code if str(code).startswith("US.") else "US." + str(code),
                "update_time": observed.astimezone(EASTERN).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "last_price": 100.0,
                "bid_price": 99.99,
                "ask_price": 100.0,
                "price_spread": 0.01,
                "lot_size": 1,
                "suspension": False,
            }
            for code in codes
        ]

    def place_limit_order(
        self,
        code,
        side,
        quantity,
        price,
        *,
        submission_marker,
        pre_write_guard=None,
    ):
        if pre_write_guard is not None:
            pre_write_guard()
        if self.state_path is not None:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            matching = [
                row
                for row in state["orders"]
                if row["submission_marker"] == submission_marker
            ]
            self.place_wal_states.append(matching[0]["send_state"])
        self.place_calls.append(
            {
                "code": code,
                "side": side,
                "quantity": quantity,
                "price": price,
                "submission_marker": submission_marker,
            }
        )
        behavior = self.buy_behavior if side == "BUY" else self.sell_behavior
        if behavior == "unknown":
            raise TimeoutError("broker reply lost")
        self.next_order_id += 1
        status = (
            "FILLED_ALL"
            if behavior == "filled"
            else "TIMEOUT"
            if behavior == "timeout_status"
            else "SUBMITTED"
        )
        dealt = quantity if behavior == "filled" else 0
        order = _order(
            self.next_order_id,
            code,
            side,
            quantity,
            status=status,
            dealt_qty=dealt,
            remark=submission_marker,
        )
        self.orders[str(self.next_order_id)] = order
        if behavior == "timeout_status":
            order["outcome_uncertain"] = True
        if behavior == "filled":
            current = self.positions.get(
                code, {"code": code, "qty": 0, "can_sell_qty": 0}
            )
            delta = quantity if side == "BUY" else -quantity
            new_quantity = current["qty"] + delta
            if new_quantity:
                self.positions[code] = {
                    "code": code,
                    "qty": new_quantity,
                    "can_sell_qty": new_quantity,
                }
            else:
                self.positions.pop(code, None)
        return {**order, "submission_marker": submission_marker}

    def query_order(self, order_id):
        self.query_calls.append(str(order_id))
        return dict(self.orders[str(order_id)])

    def reconcile_submission(self, marker):
        self.reconcile_calls.append(marker)
        if self.reconcile_result is not None:
            return dict(self.reconcile_result)
        matches = [row for row in self.orders.values() if row.get("remark") == marker]
        if len(matches) != 1:
            raise RuntimeError("marker is unresolved")
        return dict(matches[0])

    def cancel_order(self, order_id, *, pre_write_guard=None):
        if pre_write_guard is not None:
            pre_write_guard()
        self.cancel_calls.append(str(order_id))
        if self.cancel_behavior == "unknown":
            raise TimeoutError("cancel reply lost")
        order = self.orders[str(order_id)]
        order["order_status"] = "CANCELLED_ALL"
        order["is_terminal"] = True
        return {"order_id": str(order_id), "action": "cancel_requested"}

    def close(self):
        pass

    def close_quote_context(self):
        pass


def _order(
    order_id,
    code="US.AAPL",
    side="BUY",
    quantity=1,
    *,
    status="SUBMITTED",
    dealt_qty=0,
    remark="manual",
):
    terminal = status in {
        "FILLED_ALL",
        "CANCELLED_ALL",
        "CANCELLED_PART",
        "FAILED",
    }
    return {
        "order_id": str(order_id),
        "code": code,
        "trd_side": side,
        "qty": quantity,
        "dealt_qty": dealt_qty,
        "order_status": status,
        "is_terminal": terminal,
        "remark": remark,
    }


class StockBotMoomooPaperCanaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state_path = self.root / "canary.json"
        self.lock_path = self.root / "canary.lock"

    def canary(self, client, clock, config=None, events=None):
        return MoomooPaperCanary(
            client,
            self.state_path,
            config,
            lock_path=self.lock_path,
            clock=clock.now,
            sleep=clock.sleep,
            event_sink=(events if events is not None else []).append,
        )

    def interrupt_after_unknown_write(self, client, clock, config):
        """Simulate a crash after WAL records an unresolved broker write."""

        def interrupting_sleep(seconds):
            if self.state_path.exists():
                state = json.loads(self.state_path.read_text(encoding="utf-8"))
                if seconds >= 1.0 and any(
                    order.get("send_state") == "SEND_STARTED"
                    or (
                        isinstance(order.get("cancel"), dict)
                        and order["cancel"].get("state") == "SEND_STARTED"
                    )
                    for order in state.get("orders", [])
                ):
                    raise RuntimeError("simulated process interruption")
            clock.sleep(seconds)

        runner = MoomooPaperCanary(
            client,
            self.state_path,
            config,
            lock_path=self.lock_path,
            clock=clock.now,
            sleep=interrupting_sleep,
            event_sink=[].append,
        )
        with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
            runner.run()
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_preflight_is_read_only_and_reports_half_day_liquidation(self):
        clock = MutableClock(datetime(2026, 11, 27, 12, 0, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, session_type="HALF")

        result = self.canary(client, clock).preflight()

        self.assertEqual(result["status"], "PREFLIGHT_OK")
        self.assertTrue(result["flat"])
        self.assertTrue(result["liquidation_at"].endswith("12:50:00-05:00"))
        self.assertEqual(client.place_calls, [])
        self.assertEqual(client.cancel_calls, [])
        self.assertFalse(self.state_path.exists())

    def test_preflight_rejects_stale_quote_without_writes(self):
        clock = MutableClock(datetime(2026, 8, 7, 10, 30, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, quote_age_seconds=6)
        config = MoomooPaperCanaryConfig(max_quote_age_seconds=5)

        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "stale"):
            self.canary(client, clock, config).preflight()

        self.assertEqual(client.place_calls, [])
        self.assertEqual(client.cancel_calls, [])

    def test_preflight_rejects_missing_frozen_cash_field(self):
        clock = MutableClock(datetime(2026, 8, 7, 10, 30, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_snapshot = client.snapshot

        def missing_frozen_cash():
            payload = original_snapshot()
            payload["account"].pop("frozen_cash")
            return payload

        client.snapshot = missing_frozen_cash
        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "frozen_cash"):
            self.canary(client, clock).preflight()
        self.assertEqual(client.place_calls, [])
        self.assertEqual(client.cancel_calls, [])

    def test_fresh_start_rejects_position_working_order_and_frozen_cash(self):
        clock = MutableClock(datetime(2026, 8, 7, 10, 30, tzinfo=EASTERN))
        cases = (
            (
                {"positions": [{"code": "US.AAPL", "qty": 1, "can_sell_qty": 1}]},
                "no positions",
            ),
            ({"orders": [_order(1)]}, "no non-terminal orders"),
            ({"frozen_cash": 0.01}, "frozen_cash=0"),
        )
        for kwargs, message in cases:
            with self.subTest(message=message):
                client = FakeMoomooClient(clock, **kwargs)
                with self.assertRaisesRegex(MoomooPaperCanaryHalt, message):
                    self.canary(client, clock).preflight()
                self.assertEqual(client.place_calls, [])
                self.assertEqual(client.cancel_calls, [])

    def test_lock_is_nonblocking_and_process_singleton(self):
        clock = MutableClock(datetime(2026, 8, 7, 10, 30, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(MoomooPaperCanaryHalt, "holds the lock"):
                self.canary(client, clock).preflight()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        self.assertEqual(client.place_calls, [])

    def test_full_default_basket_has_no_one_share_or_position_cap_and_flattens(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 48, 0, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, state_path=self.state_path)
        events = []

        result = self.canary(client, clock, events=events).run()

        buys = [call for call in client.place_calls if call["side"] == "BUY"]
        sells = [call for call in client.place_calls if call["side"] == "SELL"]
        self.assertEqual(len(buys), len(DEFAULT_MOOMOO_CANARY_SYMBOLS))
        self.assertEqual(len(sells), len(DEFAULT_MOOMOO_CANARY_SYMBOLS))
        self.assertLessEqual(
            sum(call["quantity"] * call["price"] for call in buys),
            1_000_000 * 0.10,
        )
        self.assertTrue(all(call["quantity"] > 1 for call in buys))
        self.assertEqual(
            [call["quantity"] for call in sells],
            [call["quantity"] for call in buys],
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["flat"])
        self.assertEqual(result["status"], "FLAT")
        self.assertEqual(client.positions, {})
        self.assertIn("flat", [event["event"] for event in events])

    def test_place_rate_limiter_survives_unknown_tenth_entry_and_flattens(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order
        write_times = []
        buy_count = 0

        def tenth_buy_loses_ack(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal buy_count
            write_times.append(clock.now())
            if side == "BUY":
                buy_count += 1
            result = original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )
            if side == "BUY" and buy_count == 10:
                raise TimeoutError("tenth acknowledgement was lost")
            return result

        client.place_limit_order = tenth_buy_loses_ack

        result = self.canary(client, clock).run()

        self.assertTrue(result["flat"])
        self.assertEqual(len([row for row in client.place_calls if row["side"] == "BUY"]), 10)
        self.assertEqual(len([row for row in client.place_calls if row["side"] == "SELL"]), 10)
        for observed in write_times:
            count = sum(
                0 <= (candidate - observed).total_seconds() < 30
                for candidate in write_times
            )
            self.assertLessEqual(count, 15)
        for previous, current in zip(write_times, write_times[1:]):
            self.assertGreaterEqual(
                (current - previous).total_seconds(),
                0.021,
            )

    def test_entry_requote_preserves_original_notional_budget(self):
        started_at = datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN)
        clock = MutableClock(started_at)
        client = FakeMoomooClient(clock)
        original_snapshot = client.market_snapshot

        def repriced_snapshot(codes):
            rows = original_snapshot(codes)
            if (clock.now() - started_at).total_seconds() >= 30:
                for row in rows:
                    row.update(
                        {
                            "last_price": 200.0,
                            "bid_price": 199.99,
                            "ask_price": 200.0,
                        }
                    )
            return rows

        client.market_snapshot = repriced_snapshot

        result = self.canary(client, clock).run()

        buys = [call for call in client.place_calls if call["side"] == "BUY"]
        self.assertEqual(len(buys), len(DEFAULT_MOOMOO_CANARY_SYMBOLS))
        self.assertLessEqual(
            sum(call["quantity"] * call["price"] for call in buys),
            1_000_000 * 0.10,
        )
        self.assertTrue(all(call["price"] > buys[0]["price"] for call in buys[-3:]))
        self.assertTrue(
            all(call["quantity"] < buys[0]["quantity"] for call in buys[-3:])
        )
        self.assertTrue(result["flat"])

    def test_entry_requote_read_failure_enters_containment_and_flattens(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_market_snapshot = client.market_snapshot
        quote_reads = 0

        def fail_first_rate_limited_requote(codes):
            nonlocal quote_reads
            quote_reads += 1
            if quote_reads == 2:
                raise TimeoutError("one entry requote failed")
            return original_market_snapshot(codes)

        client.market_snapshot = fail_first_rate_limited_requote

        result = self.canary(client, clock).run()

        buys = [call for call in client.place_calls if call["side"] == "BUY"]
        sells = [call for call in client.place_calls if call["side"] == "SELL"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(len(sells), 1)
        self.assertTrue(result["flat"])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["containment_mode"])

    def test_quote_delay_crossing_liquidation_gate_submits_no_entry(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_quotes = client.market_snapshot

        def delayed_quotes(codes):
            clock.value += timedelta(seconds=1)
            return original_quotes(codes)

        client.market_snapshot = delayed_quotes

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT")),
        ).run()

        self.assertTrue(result["flat"])
        self.assertEqual(client.place_calls, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(state["containment_mode"])

    def test_entry_deadline_is_rechecked_after_wal_and_event(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)

        def event_sink(event):
            if event["event"] == "send_started":
                clock.value = datetime(2026, 8, 7, 15, 50, 1, tzinfo=EASTERN)

        result = MoomooPaperCanary(
            client,
            self.state_path,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
            lock_path=self.lock_path,
            clock=clock.now,
            sleep=clock.sleep,
            event_sink=event_sink,
        ).run()

        self.assertTrue(result["flat"])
        self.assertEqual(client.place_calls, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"][0]["send_state"], "ABORTED")

        repeated = self.canary(
            FakeMoomooClient(clock),
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()
        self.assertTrue(repeated["flat"])
        self.assertEqual(repeated["run_id"], result["run_id"])

    def test_exit_deadline_is_rechecked_after_wal_and_event(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        persisted_count = 0

        def event_sink(event):
            nonlocal persisted_count
            if event["event"] == "send_started":
                persisted_count += 1
                if persisted_count == 2:
                    clock.value = datetime(
                        2026, 8, 7, 16, 1, tzinfo=EASTERN
                    )

        with self.assertRaisesRegex(
            MoomooPaperCanaryHalt,
            "after the regular session close",
        ):
            MoomooPaperCanary(
                client,
                self.state_path,
                MoomooPaperCanaryConfig(symbols=("AAPL",)),
                lock_path=self.lock_path,
                clock=clock.now,
                sleep=clock.sleep,
                event_sink=event_sink,
            ).run()

        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY"],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"][-1]["send_state"], "ABORTED")

        clock.value = datetime(2026, 8, 10, 15, 49, 0, tzinfo=EASTERN)
        recovered_client = FakeMoomooClient(
            clock,
            positions=list(client.positions.values()),
            orders=list(client.orders.values()),
        )
        recovered = self.canary(
            recovered_client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()
        self.assertTrue(recovered["flat"])
        self.assertEqual(
            [call["side"] for call in recovered_client.place_calls],
            ["SELL"],
        )

    def test_send_started_event_failure_cannot_strand_an_exit(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        send_started_count = 0

        def failing_event_sink(event):
            nonlocal send_started_count
            if event["event"] == "send_started":
                send_started_count += 1
                if send_started_count == 2:
                    raise OSError("event output unavailable")

        result = MoomooPaperCanary(
            client,
            self.state_path,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
            lock_path=self.lock_path,
            clock=clock.now,
            sleep=clock.sleep,
            event_sink=failing_event_sink,
        ).run()

        self.assertTrue(result["flat"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )

    def test_adapter_wait_guard_blocks_exit_after_market_close(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order

        def adapter_delayed_place(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            if side == "SELL":
                clock.value = datetime(2026, 8, 7, 16, 1, tzinfo=EASTERN)
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        client.place_limit_order = adapter_delayed_place

        with self.assertRaisesRegex(
            MoomooPaperCanaryHalt,
            "after the regular session close",
        ):
            self.canary(
                client,
                clock,
                MoomooPaperCanaryConfig(symbols=("AAPL",)),
            ).run()

        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY"],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"][-1]["send_state"], "ABORTED")

    def test_adapter_wait_stale_exit_quote_is_repriced_before_close(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order
        delayed = False

        def adapter_delayed_once(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal delayed
            if side == "SELL" and not delayed:
                delayed = True
                clock.value += timedelta(seconds=6)
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        client.place_limit_order = adapter_delayed_once

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        self.assertTrue(result["flat"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        aborted_exits = [
            order
            for order in state["orders"]
            if order["kind"] == "EXIT" and order["send_state"] == "ABORTED"
        ]
        self.assertEqual(len(aborted_exits), 1)

    def test_repriced_exit_with_lost_ack_reconciles_the_replacement(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order
        sell_attempts = 0

        def stale_then_filled_without_reply(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal sell_attempts
            if side == "SELL":
                sell_attempts += 1
                if sell_attempts == 1:
                    clock.value += timedelta(seconds=6)
            result = original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )
            if side == "SELL":
                raise TimeoutError("replacement exit acknowledgement was lost")
            return result

        client.place_limit_order = stale_then_filled_without_reply
        client.orders_snapshot = lambda: [
            dict(row) for row in client.orders.values()
        ]

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        self.assertTrue(result["flat"])
        self.assertEqual(sell_attempts, 2)
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        exits = [order for order in state["orders"] if order["kind"] == "EXIT"]
        self.assertEqual([order["send_state"] for order in exits], ["ABORTED", "ACKNOWLEDGED"])

    def test_wal_is_send_started_before_sdk_write_and_contains_order_contract(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, state_path=self.state_path)
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))

        result = self.canary(client, clock, config).run()

        self.assertEqual(result["status"], "FLAT")
        self.assertTrue(client.place_wal_states)
        self.assertEqual(set(client.place_wal_states), {"SEND_STARTED"})
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        first = state["orders"][0]
        self.assertEqual(first["symbol"], "US.AAPL")
        self.assertEqual(first["side"], "BUY")
        self.assertEqual(first["time_in_force"], "DAY")
        self.assertEqual(first["session"], "RTH")
        self.assertLessEqual(len(first["submission_marker"].encode("utf-8")), 64)
        self.assertTrue(first["submission_marker"].startswith("STOCKBOT:"))
        self.assertTrue(first["client_order_id"])

    def test_unknown_submission_is_sent_once_and_recovery_only_reconciles_marker(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(
            clock,
            buy_behavior="unknown",
            state_path=self.state_path,
        )

        state = self.interrupt_after_unknown_write(first, clock, config)

        self.assertEqual(state["status"], "RECOVERY_REQUIRED")
        self.assertTrue(state["reconciliation_required"])
        self.assertEqual(len(first.place_calls), 1)
        self.assertEqual(state["orders"][0]["send_state"], "SEND_STARTED")
        marker = state["orders"][0]["submission_marker"]
        persisted_quantity = state["orders"][0]["quantity"]

        clock.value = datetime(2026, 8, 7, 15, 50, tzinfo=EASTERN)
        second = FakeMoomooClient(clock)
        second.reconcile_result = {
            **_order(
                999,
                "US.AAPL",
                "BUY",
                persisted_quantity,
                status="CANCELLED_ALL",
                remark=marker,
            ),
            "submission_marker": marker,
        }
        second.orders["999"] = dict(second.reconcile_result)
        recovered = self.canary(second, clock, config).run()

        self.assertEqual(second.place_calls, [])
        self.assertEqual(second.reconcile_calls, [marker])
        self.assertEqual(recovered["status"], "FLAT")
        self.assertTrue(recovered["flat"])

    def test_recovery_retries_first_bulk_timeout_and_flattens_same_run(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="submitted")

        def crash_after_entry(_seconds):
            raise RuntimeError("process interrupted after entry acknowledgement")

        with self.assertRaisesRegex(RuntimeError, "process interrupted"):
            MoomooPaperCanary(
                first,
                self.state_path,
                config,
                lock_path=self.lock_path,
                clock=clock.now,
                sleep=crash_after_entry,
            ).run()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        entry = state["orders"][0]
        broker_entry = dict(first.orders[str(entry["broker_order_id"])])
        broker_entry["order_status"] = "FILLED_PART"
        broker_entry["dealt_qty"] = entry["quantity"]
        broker_entry["is_terminal"] = False
        second = FakeMoomooClient(
            clock,
            positions=[
                {
                    "code": entry["symbol"],
                    "qty": entry["quantity"],
                    "can_sell_qty": entry["quantity"],
                }
            ],
            orders=[broker_entry],
        )
        bulk_calls = 0
        session_reads = 0
        session_reads_at_cancel = []
        original_trading_day = second.trading_day
        original_cancel = second.cancel_order

        def first_bulk_read_times_out():
            nonlocal bulk_calls
            bulk_calls += 1
            if bulk_calls == 1:
                raise TimeoutError("one recovery bulk read timed out")
            return [dict(row) for row in second.orders.values()]

        def tracked_trading_day(day):
            nonlocal session_reads
            session_reads += 1
            return original_trading_day(day)

        def tracked_cancel(order_id, *, pre_write_guard=None):
            session_reads_at_cancel.append(session_reads)
            return original_cancel(
                order_id,
                pre_write_guard=pre_write_guard,
            )

        second.orders_snapshot = first_bulk_read_times_out
        second.trading_day = tracked_trading_day
        second.cancel_order = tracked_cancel

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertGreaterEqual(bulk_calls, 2)
        self.assertEqual(second.cancel_calls, [broker_entry["order_id"]])
        self.assertTrue(session_reads_at_cancel)
        self.assertGreaterEqual(min(session_reads_at_cancel), 1)
        self.assertEqual(
            [call["side"] for call in second.place_calls],
            ["SELL"],
        )

    def test_recovery_converges_delayed_marker_without_resend_or_duplicate_cancel(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")

        state = self.interrupt_after_unknown_write(first, clock, config)

        self.assertTrue(state["reconciliation_required"])
        intent = state["orders"][0]
        matching = _order(
            1999,
            intent["symbol"],
            "BUY",
            intent["quantity"],
            status="SUBMITTED",
            remark=intent["submission_marker"],
        )
        second = FakeMoomooClient(clock, orders=[matching])
        bulk_calls = 0
        session_reads = 0
        session_reads_at_cancel = []
        original_trading_day = second.trading_day
        original_cancel = second.cancel_order

        def marker_appears_on_third_bulk_read():
            nonlocal bulk_calls
            bulk_calls += 1
            if bulk_calls <= 2:
                return []
            return [dict(row) for row in second.orders.values()]

        def tracked_trading_day(day):
            nonlocal session_reads
            session_reads += 1
            return original_trading_day(day)

        def tracked_cancel(order_id, *, pre_write_guard=None):
            session_reads_at_cancel.append(session_reads)
            return original_cancel(
                order_id,
                pre_write_guard=pre_write_guard,
            )

        second.orders_snapshot = marker_appears_on_third_bulk_read
        second.trading_day = tracked_trading_day
        second.cancel_order = tracked_cancel

        recovered = self.canary(second, clock, config).run()

        self.assertTrue(recovered["flat"])
        self.assertGreaterEqual(bulk_calls, 3)
        self.assertEqual(len(first.place_calls), 1)
        self.assertEqual(second.place_calls, [])
        self.assertEqual(second.cancel_calls, [matching["order_id"]])
        self.assertTrue(session_reads_at_cancel)
        self.assertGreaterEqual(min(session_reads_at_cancel), 1)

    def test_same_run_unknown_submission_converges_and_flattens(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order
        bulk_calls = 0

        def filled_but_reply_lost(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            result = original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )
            if side == "BUY":
                raise TimeoutError("broker filled the entry but its reply was lost")
            return result

        def marker_appears_on_third_bulk_read():
            nonlocal bulk_calls
            bulk_calls += 1
            if bulk_calls <= 2:
                return []
            return [dict(row) for row in client.orders.values()]

        client.place_limit_order = filled_but_reply_lost
        client.orders_snapshot = marker_appears_on_third_bulk_read

        result = self.canary(client, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertGreaterEqual(bulk_calls, 3)
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        self.assertEqual(client.positions, {})

    def test_hard_crash_after_entry_write_latches_immediate_containment(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order

        def filled_then_process_dies(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            result = original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )
            if side == "BUY":
                raise KeyboardInterrupt("hard process interruption")
            return result

        client.place_limit_order = filled_then_process_dies

        with self.assertRaises(KeyboardInterrupt):
            self.canary(client, clock, config).run()

        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertFalse(persisted["containment_mode"])
        self.assertEqual(persisted["orders"][0]["send_state"], "SEND_STARTED")
        client.orders_snapshot = lambda: [
            dict(row) for row in client.orders.values()
        ]

        recovered = self.canary(client, clock, config).run()

        self.assertTrue(recovered["flat"])
        recovered_state = json.loads(
            self.state_path.read_text(encoding="utf-8")
        )
        self.assertTrue(recovered_state["containment_mode"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        self.assertLess(clock.now().minute, 1)

    def test_restart_during_entry_batch_never_resumes_remaining_entries(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT"))
        client = FakeMoomooClient(clock)

        def crash_between_entries(seconds):
            if len(client.place_calls) == 1:
                raise KeyboardInterrupt("process died between entry orders")
            clock.sleep(seconds)

        with self.assertRaises(KeyboardInterrupt):
            MoomooPaperCanary(
                client,
                self.state_path,
                config,
                lock_path=self.lock_path,
                clock=clock.now,
                sleep=crash_between_entries,
                event_sink=[].append,
            ).run()

        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "ENTERING")
        self.assertFalse(persisted["containment_mode"])

        recovered = self.canary(client, clock, config).run()

        self.assertTrue(recovered["flat"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        self.assertLess(clock.now().minute, 1)

    def test_unknown_entry_then_unknown_cancel_converges_in_one_run(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        original_place = client.place_limit_order
        original_cancel = client.cancel_order
        bulk_calls = 0

        def accepted_entry_but_reply_lost(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            result = original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )
            if side == "BUY":
                raise TimeoutError("entry reply lost")
            return result

        def accepted_cancel_but_reply_lost(order_id, *, pre_write_guard=None):
            original_cancel(order_id, pre_write_guard=pre_write_guard)
            raise TimeoutError("cancel reply lost")

        def marker_is_delayed_once():
            nonlocal bulk_calls
            bulk_calls += 1
            if bulk_calls == 1:
                return []
            return [dict(row) for row in client.orders.values()]

        client.place_limit_order = accepted_entry_but_reply_lost
        client.cancel_order = accepted_cancel_but_reply_lost
        client.orders_snapshot = marker_is_delayed_once

        result = self.canary(client, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertGreaterEqual(bulk_calls, 3)
        self.assertEqual(len(client.place_calls), 1)
        self.assertEqual(len(client.cancel_calls), 1)

    def test_same_run_unknown_exit_converges_without_resubmission(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        client = FakeMoomooClient(clock)
        original_place = client.place_limit_order
        exit_write_started = False
        post_write_bulk_reads = 0

        def filled_exit_but_reply_lost(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal exit_write_started
            result = original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )
            if side == "SELL":
                exit_write_started = True
                raise TimeoutError("broker filled the exit but its reply was lost")
            return result

        def delayed_exit_marker():
            nonlocal post_write_bulk_reads
            rows = [dict(row) for row in client.orders.values()]
            if not exit_write_started:
                return rows
            post_write_bulk_reads += 1
            if post_write_bulk_reads <= 2:
                return [row for row in rows if row["trd_side"] != "SELL"]
            return rows

        client.place_limit_order = filled_exit_but_reply_lost
        client.orders_snapshot = delayed_exit_marker

        result = self.canary(client, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertGreaterEqual(post_write_bulk_reads, 3)
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        self.assertEqual(client.positions, {})

    def test_unknown_submission_contains_known_entries_without_funds_snapshot(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT"))
        first = FakeMoomooClient(clock, buy_behavior="submitted")
        original_place = first.place_limit_order
        buy_number = 0

        def second_buy_is_unknown(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal buy_number
            if side == "BUY":
                buy_number += 1
                first.buy_behavior = "submitted" if buy_number == 1 else "unknown"
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        first.place_limit_order = second_buy_is_unknown
        self.interrupt_after_unknown_write(first, clock, config)
        self.assertEqual(len(first.place_calls), 2)

        second = FakeMoomooClient(
            clock,
            orders=list(first.orders.values()),
        )
        snapshot_calls = 0

        def unavailable_snapshot():
            nonlocal snapshot_calls
            snapshot_calls += 1
            raise TimeoutError("funds snapshot unavailable")

        second.snapshot = unavailable_snapshot
        clock.value = datetime(2026, 8, 7, 15, 59, 59, tzinfo=EASTERN)
        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(second, clock, config).run()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(snapshot_calls, 0)
        self.assertEqual(second.place_calls, [])
        self.assertEqual(first.cancel_calls, ["1001"])
        self.assertEqual(second.cancel_calls, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"][0]["cancel"]["state"], "ACKNOWLEDGED")
        self.assertTrue(state["containment_mode"])

        with self.assertRaises(MoomooPaperCanaryHalt):
            self.canary(second, clock, config).run()
        self.assertEqual(first.cancel_calls, ["1001"])
        self.assertEqual(second.cancel_calls, [])

    def test_recovery_never_sends_persisted_unsent_intent(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        state["orders"][0]["send_state"] = "INTENT_PERSISTED"
        state["orders"][0].pop("send_started_at", None)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        second = FakeMoomooClient(clock)

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertFalse(result["reconciliation_required"])
        self.assertEqual(second.place_calls, [])
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertTrue(persisted["containment_mode"])
        self.assertEqual(persisted["orders"][0]["send_state"], "ABORTED")

    def test_preopen_recovery_refresh_flattens_newly_resolved_fill(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        self.assertTrue(state["reconciliation_required"])
        entry = state["orders"][0]
        filled = _order(
            889,
            "US.AAPL",
            "BUY",
            entry["quantity"],
            status="FILLED_ALL",
            dealt_qty=entry["quantity"],
            remark=entry["submission_marker"],
        )

        clock.value = datetime(2026, 8, 10, 9, 29, tzinfo=EASTERN)
        second = FakeMoomooClient(
            clock,
            positions=[
                {
                    "code": "US.AAPL",
                    "qty": entry["quantity"],
                    "can_sell_qty": entry["quantity"],
                }
            ],
            orders=[filled],
        )
        second.orders_snapshot = lambda: []
        second.history_orders_snapshot = lambda _start, _end: (
            [dict(filled)]
            if clock.now().time() >= datetime(2026, 8, 10, 9, 30).time()
            else []
        )

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertFalse(result["reconciliation_required"])
        self.assertEqual(
            [call["side"] for call in second.place_calls],
            ["SELL"],
        )

    def test_known_query_failure_still_contains_entry_once(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT"))
        first = FakeMoomooClient(clock, buy_behavior="submitted")
        original_place = first.place_limit_order
        buy_number = 0

        def second_buy_is_unknown(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal buy_number
            if side == "BUY":
                buy_number += 1
                first.buy_behavior = "submitted" if buy_number == 1 else "unknown"
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        first.place_limit_order = second_buy_is_unknown
        self.interrupt_after_unknown_write(first, clock, config)
        second = FakeMoomooClient(clock, orders=list(first.orders.values()))
        second.query_order = lambda _order_id: (_ for _ in ()).throw(
            TimeoutError("query unavailable")
        )
        clock.value = datetime(2026, 8, 7, 15, 59, 59, tzinfo=EASTERN)

        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(second, clock, config).run()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(first.cancel_calls, ["1001"])
        self.assertEqual(second.cancel_calls, [])

    def test_containment_continues_after_one_cancel_is_unknown(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT", "NVDA"))
        first = FakeMoomooClient(
            clock,
            buy_behavior="submitted",
            cancel_behavior="unknown",
        )
        original_place = first.place_limit_order
        buy_number = 0

        def third_buy_is_unknown(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal buy_number
            if side == "BUY":
                buy_number += 1
                first.buy_behavior = "submitted" if buy_number < 3 else "unknown"
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        first.place_limit_order = third_buy_is_unknown
        self.interrupt_after_unknown_write(first, clock, config)
        second = FakeMoomooClient(
            clock,
            orders=list(first.orders.values()),
            cancel_behavior="unknown",
        )
        clock.value = datetime(2026, 8, 7, 15, 59, 59, tzinfo=EASTERN)

        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(second, clock, config).run()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(first.cancel_calls, ["1001", "1002"])
        self.assertEqual(second.cancel_calls, [])

    def test_reconciled_orders_do_not_clear_recovery_before_fresh_snapshot(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        marker = state["orders"][0]["submission_marker"]
        quantity = state["orders"][0]["quantity"]
        second = FakeMoomooClient(clock)
        second.reconcile_result = {
            **_order(
                999,
                "US.AAPL",
                "BUY",
                quantity,
                status="CANCELLED_ALL",
                remark=marker,
            ),
            "submission_marker": marker,
        }
        second.orders["999"] = dict(second.reconcile_result)
        second.snapshot = lambda: (_ for _ in ()).throw(
            TimeoutError("fresh funds unavailable")
        )

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["reconciliation_required"])
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "RECOVERY_REQUIRED")
        self.assertTrue(persisted["reconciliation_required"])

    def test_reconciled_unknown_entry_is_cancelled_before_funds_snapshot(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        intent = state["orders"][0]
        matching = _order(
            1999,
            intent["symbol"],
            "BUY",
            intent["quantity"],
            status="SUBMITTED",
            remark=intent["submission_marker"],
        )
        second = FakeMoomooClient(clock, orders=[matching])
        second.orders_snapshot = lambda: [dict(matching)]
        second.snapshot = lambda: (_ for _ in ()).throw(
            TimeoutError("funds snapshot unavailable")
        )

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(second.cancel_calls, ["1999"])
        self.assertEqual(second.place_calls, [])

    def test_quote_isolation_failure_stops_before_containment_write(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        self.interrupt_after_unknown_write(first, clock, config)
        second = FakeMoomooClient(clock)
        second.close_quote_context = lambda: (_ for _ in ()).throw(
            RuntimeError("quote socket close failed")
        )

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(second.place_calls, [])
        self.assertEqual(second.cancel_calls, [])

    def test_reconciled_prior_containment_flattens_immediately(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT"))
        first = FakeMoomooClient(clock, buy_behavior="submitted")
        original_place = first.place_limit_order
        buy_number = 0

        def second_buy_is_unknown(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal buy_number
            if side == "BUY":
                buy_number += 1
                first.buy_behavior = "submitted" if buy_number == 1 else "unknown"
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        first.place_limit_order = second_buy_is_unknown
        state = self.interrupt_after_unknown_write(first, clock, config)
        first_order, unknown_order = state["orders"]
        first_order["cancel"] = {
            "client_action_id": "a" * 32,
            "state": "CONFIRMED",
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

        first_filled = _order(
            first_order["broker_order_id"],
            first_order["symbol"],
            "BUY",
            first_order["quantity"],
            status="FILLED_ALL",
            dealt_qty=first_order["quantity"],
            remark=first_order["submission_marker"],
        )
        unknown_filled = {
            **_order(
                2002,
                unknown_order["symbol"],
                "BUY",
                unknown_order["quantity"],
                status="FILLED_ALL",
                dealt_qty=unknown_order["quantity"],
                remark=unknown_order["submission_marker"],
            ),
            "submission_marker": unknown_order["submission_marker"],
        }
        second = FakeMoomooClient(
            clock,
            positions=[
                {
                    "code": first_order["symbol"],
                    "qty": first_order["quantity"],
                    "can_sell_qty": first_order["quantity"],
                },
                {
                    "code": unknown_order["symbol"],
                    "qty": unknown_order["quantity"],
                    "can_sell_qty": unknown_order["quantity"],
                },
            ],
            orders=[first_filled, unknown_filled],
        )
        second.reconcile_result = unknown_filled

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertEqual(result["status"], "FLAT")
        self.assertEqual(
            [call["side"] for call in second.place_calls],
            ["SELL", "SELL"],
        )
        self.assertEqual(second.query_calls, [str(first_order["broker_order_id"])])
        self.assertLess(
            (
                clock.value
                - datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN)
            ).total_seconds(),
            1.0,
        )

    def test_timeout_order_status_requires_reconciliation_and_no_next_submission(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="timeout_status")
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT"))

        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(client, clock, config).run()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(len(client.place_calls), 1)

    def test_current_day_flat_state_is_revalidated_against_broker(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        self.assertTrue(self.canary(client, clock, config).run()["flat"])
        client.positions["US.MSFT"] = {
            "code": "US.MSFT",
            "qty": 1,
            "can_sell_qty": 1,
        }

        with self.assertRaises(MoomooPaperCanaryHalt):
            self.canary(client, clock, config).run()
        self.assertEqual(
            [call for call in client.place_calls if call["side"] == "SELL"][-1]["code"],
            "US.AAPL",
        )

    def test_preflight_revalidates_persisted_flat_state_against_broker(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        self.assertTrue(self.canary(client, clock, config).run()["flat"])
        client.positions["US.MSFT"] = {
            "code": "US.MSFT",
            "qty": 1,
            "can_sell_qty": 1,
        }

        with self.assertRaises(MoomooPaperCanaryHalt):
            self.canary(client, clock, config).preflight()
        self.assertEqual(
            [call for call in client.place_calls if call["code"] == "US.MSFT"], []
        )

    def test_preflight_does_not_mask_persisted_recovery_requirement(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        self.assertTrue(state["reconciliation_required"])
        second = FakeMoomooClient(clock)

        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(second, clock, config).preflight()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(second.place_calls, [])
        self.assertEqual(second.cancel_calls, [])

    def test_state_binds_complete_safety_config(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        first_config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, first_config)
        self.assertTrue(state["reconciliation_required"])
        changed = MoomooPaperCanaryConfig(
            symbols=("AAPL",), marketable_limit_buffer_bps=75
        )
        second = FakeMoomooClient(clock)

        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "config"):
            self.canary(second, clock, changed).preflight()
        self.assertEqual(second.place_calls, [])

    def test_tampered_persisted_intent_halts_before_broker_write(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        state["orders"][0]["send_state"] = "INTENT_PERSISTED"
        state["orders"][0]["submission_marker"] = "forged-marker"
        self.state_path.write_text(json.dumps(state), encoding="utf-8")
        second = FakeMoomooClient(clock)

        with self.assertRaises(MoomooPaperCanaryHalt):
            self.canary(second, clock, config).run()
        self.assertEqual(second.place_calls, [])

    def test_recovery_never_duplicates_working_exit(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL",), poll_interval_seconds=600
        )
        client = FakeMoomooClient(clock, sell_behavior="unknown")
        first = self.interrupt_after_unknown_write(client, clock, config)
        self.assertTrue(first["reconciliation_required"])
        exit_marker = [
            row["submission_marker"]
            for row in json.loads(self.state_path.read_text())["orders"]
            if row["kind"] == "EXIT"
        ][0]
        working_exit = _order(
            888,
            "US.AAPL",
            "SELL",
            995,
            status="SUBMITTED",
            remark=exit_marker,
        )
        client.orders["888"] = working_exit
        client.reconcile_result = {
            **working_exit,
            "submission_marker": exit_marker,
        }
        client.sell_behavior = "submitted"

        with self.assertRaises(MoomooPaperCanaryHalt):
            self.canary(client, clock, config).run()

        self.assertEqual(
            len([call for call in client.place_calls if call["side"] == "SELL"]),
            1,
        )

    def test_cancel_send_started_is_never_resent_on_recovery(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL",), poll_interval_seconds=600
        )
        client = FakeMoomooClient(
            clock,
            buy_behavior="submitted",
            cancel_behavior="unknown",
        )
        first = self.interrupt_after_unknown_write(client, clock, config)
        self.assertTrue(first["reconciliation_required"])
        self.assertEqual(len(client.cancel_calls), 1)
        clock.value = datetime(2026, 8, 7, 15, 59, 59, tzinfo=EASTERN)

        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(client, clock, config).run()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(len(client.cancel_calls), 1)

    def test_eod_cancels_unfilled_entry_then_sells_filled_position(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "MSFT"))
        original_place = client.place_limit_order
        buy_number = 0

        def mixed_place(
            code,
            side,
            quantity,
            price,
            *,
            submission_marker,
            pre_write_guard=None,
        ):
            nonlocal buy_number
            if side == "BUY":
                buy_number += 1
                client.buy_behavior = "submitted" if buy_number == 1 else "filled"
            return original_place(
                code,
                side,
                quantity,
                price,
                submission_marker=submission_marker,
                pre_write_guard=pre_write_guard,
            )

        client.place_limit_order = mixed_place

        result = self.canary(client, clock, config).run()

        self.assertEqual(len(client.cancel_calls), 1)
        self.assertEqual(
            [(row["code"], row["side"]) for row in client.place_calls],
            [("US.AAPL", "BUY"), ("US.MSFT", "BUY"), ("US.MSFT", "SELL")],
        )
        self.assertTrue(result["flat"])

    def test_unowned_position_halts_instead_of_liquidating_it(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        original_snapshot = client.snapshot

        def snapshot_with_foreign_position():
            payload = original_snapshot()
            if client.place_calls:
                payload["positions"].append(
                    {"code": "US.MSFT", "qty": 1, "can_sell_qty": 1}
                )
            return payload

        client.snapshot = snapshot_with_foreign_position

        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "canary-owned"):
            self.canary(client, clock, config).run()

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "HALT")
        self.assertEqual(
            [call for call in client.place_calls if call["side"] == "SELL"], []
        )

    def test_unowned_working_order_halts_before_any_exit_submission(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL",), poll_interval_seconds=600
        )
        original_snapshot = client.snapshot

        def snapshot_with_foreign_working_order():
            payload = original_snapshot()
            if client.place_calls:
                payload["orders"].append(_order(777, "US.MSFT", "BUY", 1))
            return payload

        client.snapshot = snapshot_with_foreign_working_order

        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "unowned working"):
            self.canary(client, clock, config).run()

        self.assertEqual(
            [call for call in client.place_calls if call["side"] == "SELL"], []
        )

    def test_unfilled_exit_at_close_persists_halt(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, sell_behavior="submitted")
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL",), poll_interval_seconds=600
        )

        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "not flat") as raised:
            self.canary(client, clock, config).run()

        self.assertTrue(raised.exception.reconciliation_required)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "HALT")
        self.assertFalse(state["flat"])

    def test_working_exit_is_cancelled_then_replaced_only_after_terminal(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, sell_behavior="submitted")
        config = MoomooPaperCanaryConfig(symbols=("AAPL",), poll_interval_seconds=5)
        original_cancel = client.cancel_order

        def cancel_then_allow_fill(order_id, *, pre_write_guard=None):
            result = original_cancel(
                order_id,
                pre_write_guard=pre_write_guard,
            )
            if client.orders[str(order_id)]["trd_side"] == "SELL":
                client.sell_behavior = "filled"
            return result

        client.cancel_order = cancel_then_allow_fill

        result = self.canary(client, clock, config).run()

        sells = [call for call in client.place_calls if call["side"] == "SELL"]
        self.assertEqual(len(sells), 2)
        self.assertEqual(sells[0]["quantity"], sells[1]["quantity"])
        self.assertEqual(len(client.cancel_calls), 1)
        self.assertTrue(result["flat"])

    def test_non_atomic_position_and_order_snapshot_is_retried(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, sell_behavior="submitted")
        original_snapshot = client.snapshot
        race_stage = 0

        def position_before_partial_exit_fill():
            nonlocal race_stage
            payload = original_snapshot()
            sell_orders = [
                row
                for row in payload["orders"]
                if row["trd_side"] == "SELL"
            ]
            if sell_orders and race_stage == 0:
                race_stage = 1
                sell = sell_orders[-1]
                sell["order_status"] = "CANCELLED_PART"
                sell["dealt_qty"] = 100
                sell["is_terminal"] = True
                client.orders[str(sell["order_id"])].update(sell)
                client.positions["US.AAPL"] = {
                    "code": "US.AAPL",
                    "qty": 895,
                    "can_sell_qty": 895,
                }
                client.sell_behavior = "filled"
            elif sell_orders and race_stage == 1:
                race_stage = 2
                for position in payload["positions"]:
                    if position["code"] == "US.AAPL":
                        position["qty"] = 995
                        position["can_sell_qty"] = 995
                sell = sell_orders[-1]
                sell["order_status"] = "CANCELLED_ALL"
                sell["dealt_qty"] = 0
                sell["is_terminal"] = True
            return payload

        client.snapshot = position_before_partial_exit_fill

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        sells = [call for call in client.place_calls if call["side"] == "SELL"]
        self.assertEqual(race_stage, 2)
        self.assertEqual([call["quantity"] for call in sells], [995, 895])
        self.assertTrue(result["flat"])

    def test_missing_position_row_cannot_create_false_flat(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, sell_behavior="submitted")
        original_snapshot = client.snapshot
        omitted_once = False

        def omit_position_after_terminal_unfilled_exit():
            nonlocal omitted_once
            payload = original_snapshot()
            sell_orders = [
                row
                for row in payload["orders"]
                if row["trd_side"] == "SELL"
            ]
            if sell_orders and not omitted_once:
                omitted_once = True
                sell = sell_orders[-1]
                sell["order_status"] = "CANCELLED_ALL"
                sell["dealt_qty"] = 0
                sell["is_terminal"] = True
                client.orders[str(sell["order_id"])].update(sell)
                payload["positions"] = []
                client.sell_behavior = "filled"
            return payload

        client.snapshot = omit_position_after_terminal_unfilled_exit

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        sells = [call for call in client.place_calls if call["side"] == "SELL"]
        self.assertTrue(omitted_once)
        self.assertEqual([call["quantity"] for call in sells], [995, 995])
        self.assertTrue(result["flat"])
        self.assertEqual(client.positions, {})

    def test_same_run_unknown_cancel_converges_then_sells_partial_fill(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))

        def cancelled_part_but_reply_lost(order_id, *, pre_write_guard=None):
            if pre_write_guard is not None:
                pre_write_guard()
            client.cancel_calls.append(str(order_id))
            order = client.orders[str(order_id)]
            filled = int(order["qty"]) // 2
            order["order_status"] = "CANCELLED_PART"
            order["dealt_qty"] = filled
            order["is_terminal"] = True
            client.positions[order["code"]] = {
                "code": order["code"],
                "qty": filled,
                "can_sell_qty": filled,
            }
            raise TimeoutError("broker accepted cancel but its reply was lost")

        client.cancel_order = cancelled_part_but_reply_lost

        result = self.canary(client, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertEqual(len(client.cancel_calls), 1)
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )
        self.assertEqual(client.positions, {})

    def test_active_monitor_reconciles_working_entries_before_liquidation(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 40, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        config = MoomooPaperCanaryConfig(symbols=("AAPL",), poll_interval_seconds=5)

        result = self.canary(client, clock, config).run()

        self.assertGreaterEqual(len(client.query_calls), 2)
        self.assertEqual(len(client.cancel_calls), 1)
        self.assertTrue(result["flat"])

    def test_monitor_retries_a_temporarily_missing_acknowledged_order(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 40, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        bulk_reads = 0

        def orders_snapshot():
            nonlocal bulk_reads
            bulk_reads += 1
            if bulk_reads == 1:
                return []
            return [dict(row) for row in client.orders.values()]

        client.orders_snapshot = orders_snapshot

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        self.assertGreaterEqual(bulk_reads, 2)
        self.assertTrue(result["flat"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )

    def test_monitor_read_recovery_continues_into_same_run_liquidation(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 40, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_snapshot = client.snapshot
        original_trading_day = client.trading_day
        failures = 0
        calendar_reads = 0

        def four_failures_then_recover():
            nonlocal failures
            if client.place_calls and failures < 4:
                failures += 1
                raise TimeoutError("temporary account read failure")
            return original_snapshot()

        client.snapshot = four_failures_then_recover

        def one_containment_calendar_failure(day):
            nonlocal calendar_reads
            calendar_reads += 1
            if calendar_reads == 2:
                raise TimeoutError("temporary calendar read failure")
            return original_trading_day(day)

        client.trading_day = one_containment_calendar_failure

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        self.assertEqual(failures, 4)
        self.assertGreaterEqual(calendar_reads, 3)
        self.assertTrue(result["flat"])
        self.assertFalse(result["reconciliation_required"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )

    def test_transient_liquidation_snapshot_failure_is_retried(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_snapshot = client.snapshot
        post_entry_reads = 0

        def fail_first_liquidation_snapshot():
            nonlocal post_entry_reads
            if client.place_calls:
                post_entry_reads += 1
                if post_entry_reads == 2:
                    raise TimeoutError("one liquidation read failed")
            return original_snapshot()

        client.snapshot = fail_first_liquidation_snapshot

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        self.assertGreaterEqual(post_entry_reads, 3)
        self.assertTrue(result["flat"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )

    def test_transient_stale_exit_quote_is_retried(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_market_snapshot = client.market_snapshot
        quote_reads = 0

        def stale_once(codes):
            nonlocal quote_reads
            quote_reads += 1
            rows = original_market_snapshot(codes)
            if quote_reads == 2:
                stale_time = clock.now() - timedelta(seconds=6)
                for row in rows:
                    row["update_time"] = stale_time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
            return rows

        client.market_snapshot = stale_once

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(
                symbols=("AAPL",),
                max_quote_age_seconds=5,
            ),
        ).run()

        self.assertGreaterEqual(quote_reads, 3)
        self.assertTrue(result["flat"])
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY", "SELL"],
        )

    def test_persistently_stale_exit_quote_halts_at_close_without_false_flat(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        original_market_snapshot = client.market_snapshot
        quote_reads = 0

        def stale_after_entry(codes):
            nonlocal quote_reads
            quote_reads += 1
            rows = original_market_snapshot(codes)
            if quote_reads >= 2:
                stale_time = clock.now() - timedelta(seconds=6)
                for row in rows:
                    row["update_time"] = stale_time.strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
            return rows

        client.market_snapshot = stale_after_entry

        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(
                client,
                clock,
                MoomooPaperCanaryConfig(
                    symbols=("AAPL",),
                    max_quote_age_seconds=5,
                ),
            ).run()

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(
            [call["side"] for call in client.place_calls],
            ["BUY"],
        )
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "HALT")
        self.assertFalse(state["flat"])
        self.assertTrue(state["reconciliation_required"])

    def test_symbols_normalize_and_reject_duplicate_or_non_us_values(self):
        config = MoomooPaperCanaryConfig(symbols=("AAPL", "us.msft"))
        self.assertEqual(config.symbols, ("US.AAPL", "US.MSFT"))
        with self.assertRaisesRegex(ValueError, "unique"):
            MoomooPaperCanaryConfig(symbols=("AAPL", "US.AAPL"))
        with self.assertRaisesRegex(ValueError, "only accepts US"):
            MoomooPaperCanaryConfig(symbols=("HK.00700",))
        with self.assertRaisesRegex(ValueError, "broker query limits"):
            MoomooPaperCanaryConfig(poll_interval_seconds=0.1)
        large_basket = tuple("S%03d" % index for index in range(300))
        with self.assertRaisesRegex(ValueError, "worst-case cancellation"):
            MoomooPaperCanaryConfig(
                symbols=large_basket,
                liquidation_lead_minutes=10,
            )
        self.assertEqual(
            len(
                MoomooPaperCanaryConfig(
                    symbols=large_basket,
                    liquidation_lead_minutes=20,
                ).symbols
            ),
            300,
        )

    def test_open_wait_never_enters_after_liquidation_or_on_a_later_day(self):
        for index, wake_at in enumerate(
            (
                datetime(2026, 8, 7, 15, 51, tzinfo=EASTERN),
                datetime(2026, 8, 8, 9, 31, tzinfo=EASTERN),
            )
        ):
            with self.subTest(wake_at=wake_at):
                clock = MutableClock(
                    datetime(2026, 8, 7, 9, 29, tzinfo=EASTERN)
                )
                client = FakeMoomooClient(clock)
                state_path = self.root / ("wait-%d.json" % index)
                lock_path = self.root / ("wait-%d.lock" % index)

                def oversleep(_seconds):
                    clock.value = wake_at

                result = MoomooPaperCanary(
                    client,
                    state_path,
                    MoomooPaperCanaryConfig(symbols=("AAPL",)),
                    lock_path=lock_path,
                    clock=clock.now,
                    sleep=oversleep,
                ).run()

                self.assertEqual(result["status"], "SKIPPED")
                self.assertTrue(result["flat"])
                self.assertEqual(client.place_calls, [])

    def test_completed_session_archives_and_rolls_to_next_trading_day(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock)
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = self.canary(client, clock, config).run()
        first_state = json.loads(self.state_path.read_text(encoding="utf-8"))

        clock.value = datetime(2026, 8, 10, 15, 49, 59, tzinfo=EASTERN)
        second = self.canary(client, clock, config).run()

        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(second["trading_day"], "2026-08-10")
        archive = (
            self.state_path.parent
            / (self.state_path.stem + "_sessions")
            / ("2026-08-07_%s.json" % first["run_id"])
        )
        self.assertEqual(
            json.loads(archive.read_text(encoding="utf-8")),
            first_state,
        )

    def test_unfinished_previous_day_enters_current_day_containment_and_flattens(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, sell_behavior="unknown")
        state = self.interrupt_after_unknown_write(first, clock, config)
        self.assertTrue(state["reconciliation_required"])
        exit_order = [row for row in state["orders"] if row["kind"] == "EXIT"][0]

        clock.value = datetime(2026, 8, 10, 15, 49, 59, tzinfo=EASTERN)
        cancelled_exit = {
            **_order(
                2001,
                "US.AAPL",
                "SELL",
                exit_order["quantity"],
                status="CANCELLED_ALL",
                dealt_qty=0,
                remark=exit_order["submission_marker"],
            ),
            "submission_marker": exit_order["submission_marker"],
        }
        second = FakeMoomooClient(
            clock,
            positions=[
                {
                    "code": "US.AAPL",
                    "qty": exit_order["quantity"],
                    "can_sell_qty": exit_order["quantity"],
                }
            ],
            orders=list(first.orders.values()),
        )
        second.reconcile_result = cancelled_exit
        second.orders["2001"] = dict(cancelled_exit)
        second.next_order_id = 3000

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertEqual(result["trading_day"], "2026-08-07")
        self.assertEqual(result["effective_trading_day"], "2026-08-10")
        self.assertEqual(
            [call["side"] for call in second.place_calls],
            ["SELL"],
        )

        calls_after_flat = len(second.place_calls)
        repeated = self.canary(second, clock, config).run()
        self.assertEqual(repeated["run_id"], result["run_id"])
        self.assertEqual(len(second.place_calls), calls_after_flat)

    def test_previous_day_halt_uses_one_history_batch_then_flattens(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL"),
            poll_interval_seconds=600,
        )
        first = FakeMoomooClient(clock, sell_behavior="submitted")
        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "not flat"):
            self.canary(first, clock, config).run()
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "HALT")
        self.assertEqual(len(persisted["orders"]), 12)

        historical = []
        for raw in first.orders.values():
            row = dict(raw)
            if row["trd_side"] == "SELL":
                row["order_status"] = "CANCELLED_ALL"
                row["is_terminal"] = True
            historical.append(row)
        positions = [dict(row) for row in first.positions.values()]

        clock.value = datetime(2026, 8, 10, 15, 49, 59, tzinfo=EASTERN)
        second = FakeMoomooClient(clock, positions=positions)
        second.next_order_id = 2000
        history_calls = []
        second.orders_snapshot = lambda: [dict(row) for row in historical]

        def history_orders_snapshot(start, end):
            history_calls.append((start, end))
            return [dict(row) for row in historical]

        second.history_orders_snapshot = history_orders_snapshot
        second.query_order = lambda _order_id: (_ for _ in ()).throw(
            AssertionError("bulk cross-day recovery must not query per order")
        )

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertEqual(result["effective_trading_day"], "2026-08-10")
        self.assertEqual(len(history_calls), 1)
        self.assertEqual(history_calls[0][0].isoformat(), "2026-08-07")
        self.assertEqual(history_calls[0][1].isoformat(), "2026-08-09")
        self.assertEqual(
            len([call for call in second.place_calls if call["side"] == "SELL"]),
            6,
        )

    def test_old_nonterminal_entry_cancel_is_confirmed_by_history_batch(self):
        clock = MutableClock(datetime(2026, 8, 7, 11, 0, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(symbols=("AAPL",))
        first = FakeMoomooClient(clock, buy_behavior="submitted")
        first.query_order = lambda _order_id: (_ for _ in ()).throw(
            TimeoutError("monitor read unavailable")
        )
        with self.assertRaises(MoomooPaperCanaryHalt) as raised:
            self.canary(first, clock, config).run()
        self.assertTrue(raised.exception.reconciliation_required)
        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        persisted_order = persisted["orders"][0]
        persisted_order["cancel"] = None
        persisted_order["order_status"] = "SUBMITTED"
        persisted_order["is_terminal"] = False
        self.state_path.write_text(json.dumps(persisted), encoding="utf-8")
        old_order = _order(
            persisted_order["broker_order_id"],
            persisted_order["symbol"],
            "BUY",
            persisted_order["quantity"],
            status="SUBMITTED",
            remark=persisted_order["submission_marker"],
        )

        clock.value = datetime(2026, 8, 10, 10, 0, tzinfo=EASTERN)
        second = FakeMoomooClient(clock, orders=[old_order])
        original_snapshot = second.snapshot
        history_calls = 0
        second.orders_snapshot = lambda: []

        def history_orders_snapshot(_start, _end):
            nonlocal history_calls
            history_calls += 1
            return [dict(row) for row in second.orders.values()]

        def today_snapshot():
            payload = original_snapshot()
            payload["orders"] = []
            return payload

        second.history_orders_snapshot = history_orders_snapshot
        second.snapshot = today_snapshot
        second.query_order = lambda _order_id: (_ for _ in ()).throw(
            AssertionError("old entry recovery must remain bulk-only")
        )

        result = self.canary(second, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertEqual(second.cancel_calls, [old_order["order_id"]])
        self.assertGreaterEqual(history_calls, 2)

    def test_containment_open_wait_oversleep_never_writes_after_close(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL",),
            poll_interval_seconds=600,
        )
        first = FakeMoomooClient(clock, sell_behavior="submitted")
        with self.assertRaisesRegex(MoomooPaperCanaryHalt, "not flat"):
            self.canary(first, clock, config).run()
        historical = []
        for raw in first.orders.values():
            row = dict(raw)
            if row["trd_side"] == "SELL":
                row["order_status"] = "CANCELLED_ALL"
                row["is_terminal"] = True
            historical.append(row)

        clock.value = datetime(2026, 8, 10, 9, 29, tzinfo=EASTERN)
        second = FakeMoomooClient(
            clock,
            positions=[dict(row) for row in first.positions.values()],
        )
        second.orders_snapshot = lambda: []
        second.history_orders_snapshot = lambda _start, _end: [
            dict(row) for row in historical
        ]

        def oversleep(_seconds):
            clock.value = datetime(2026, 8, 10, 16, 1, tzinfo=EASTERN)

        result = MoomooPaperCanary(
            second,
            self.state_path,
            config,
            lock_path=self.lock_path,
            clock=clock.now,
            sleep=oversleep,
        ).run()

        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(second.place_calls, [])
        self.assertEqual(second.cancel_calls, [])

    def test_crossday_halt_survives_crash_while_waiting_for_open(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL",),
            poll_interval_seconds=600,
        )
        first = FakeMoomooClient(clock, sell_behavior="submitted")
        with self.assertRaises(MoomooPaperCanaryHalt):
            self.canary(first, clock, config).run()
        historical = []
        for raw in first.orders.values():
            row = dict(raw)
            if row["trd_side"] == "SELL":
                row["order_status"] = "CANCELLED_ALL"
                row["is_terminal"] = True
            historical.append(row)
        positions = [dict(row) for row in first.positions.values()]

        clock.value = datetime(2026, 8, 10, 9, 29, tzinfo=EASTERN)
        interrupted = FakeMoomooClient(clock, positions=positions)
        interrupted.orders_snapshot = lambda: []
        interrupted.history_orders_snapshot = lambda _start, _end: [
            dict(row) for row in historical
        ]

        def crash_during_open_wait(_seconds):
            raise RuntimeError("process interrupted while waiting for open")

        with self.assertRaisesRegex(RuntimeError, "process interrupted"):
            MoomooPaperCanary(
                interrupted,
                self.state_path,
                config,
                lock_path=self.lock_path,
                clock=clock.now,
                sleep=crash_during_open_wait,
            ).run()

        persisted = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["status"], "HALT")
        self.assertEqual(
            persisted["recovery_session"]["trading_day"],
            "2026-08-10",
        )

        clock.value = datetime(2026, 8, 10, 9, 31, tzinfo=EASTERN)
        recovered_client = FakeMoomooClient(clock, positions=positions)
        recovered_client.orders_snapshot = lambda: []
        recovered_client.history_orders_snapshot = lambda _start, _end: [
            dict(row) for row in historical
        ]

        recovered = self.canary(recovered_client, clock, config).run()

        self.assertTrue(recovered["flat"])
        self.assertEqual(
            [call["side"] for call in recovered_client.place_calls],
            ["SELL"],
        )

    def test_monitor_uses_one_bulk_order_read_per_poll_when_available(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 40, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        bulk_calls = 0

        def orders_snapshot():
            nonlocal bulk_calls
            bulk_calls += 1
            for order in client.orders.values():
                if order["trd_side"] == "BUY":
                    order["order_status"] = "FILLED_ALL"
                    order["dealt_qty"] = order["qty"]
                    order["is_terminal"] = True
                    client.positions[order["code"]] = {
                        "code": order["code"],
                        "qty": order["qty"],
                        "can_sell_qty": order["qty"],
                    }
            return [dict(row) for row in client.orders.values()]

        client.orders_snapshot = orders_snapshot
        client.query_order = lambda _order_id: (_ for _ in ()).throw(
            AssertionError("bulk-capable monitor must not query per order")
        )
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL", "MSFT"),
            poll_interval_seconds=5,
        )

        result = self.canary(client, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertGreaterEqual(bulk_calls, 1)

    def test_liquidation_bulk_cancels_working_entries_without_exact_queries(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        bulk_calls = 0

        def orders_snapshot():
            nonlocal bulk_calls
            bulk_calls += 1
            return [dict(row) for row in client.orders.values()]

        client.orders_snapshot = orders_snapshot
        original_cancel = client.cancel_order
        cancel_times = []

        def timed_cancel(order_id, *, pre_write_guard=None):
            cancel_times.append(clock.now())
            return original_cancel(
                order_id,
                pre_write_guard=pre_write_guard,
            )

        client.cancel_order = timed_cancel
        client.query_order = lambda _order_id: (_ for _ in ()).throw(
            AssertionError("bulk-capable liquidation must not query per order")
        )
        config = MoomooPaperCanaryConfig(
            symbols=("AAPL", "MSFT"),
            poll_interval_seconds=5,
        )

        result = self.canary(client, clock, config).run()

        self.assertTrue(result["flat"])
        self.assertEqual(len(client.cancel_calls), 2)
        self.assertGreaterEqual(bulk_calls, 2)
        self.assertGreaterEqual(
            (cancel_times[1] - cancel_times[0]).total_seconds(),
            0.041,
        )

    def test_cancel_wal_crossing_close_never_calls_broker(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        canary = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        )
        original_persist = canary._persist

        def persist_then_cross_close():
            original_persist()
            if canary._state is None:
                return
            if any(
                isinstance(order.get("cancel"), dict)
                and order["cancel"].get("state") == "SEND_STARTED"
                for order in canary._state["orders"]
            ):
                clock.value = datetime(2026, 8, 7, 16, 1, tzinfo=EASTERN)

        canary._persist = persist_then_cross_close

        result = canary.run()

        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(client.cancel_calls, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"][0]["cancel"]["state"], "INTENT_PERSISTED")
        self.assertNotIn("send_started_at", state["orders"][0]["cancel"])

    def test_adapter_wait_guard_blocks_cancel_after_market_close(self):
        clock = MutableClock(datetime(2026, 8, 7, 15, 49, 59, tzinfo=EASTERN))
        client = FakeMoomooClient(clock, buy_behavior="submitted")
        original_cancel = client.cancel_order

        def adapter_delayed_cancel(order_id, *, pre_write_guard=None):
            clock.value = datetime(2026, 8, 7, 16, 1, tzinfo=EASTERN)
            return original_cancel(
                order_id,
                pre_write_guard=pre_write_guard,
            )

        client.cancel_order = adapter_delayed_cancel

        result = self.canary(
            client,
            clock,
            MoomooPaperCanaryConfig(symbols=("AAPL",)),
        ).run()

        self.assertTrue(result["reconciliation_required"])
        self.assertEqual(client.cancel_calls, [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["orders"][0]["cancel"]["state"], "INTENT_PERSISTED")
        self.assertNotIn("send_started_at", state["orders"][0]["cancel"])


if __name__ == "__main__":
    unittest.main()
