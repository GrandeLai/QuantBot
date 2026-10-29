import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantbot.trading.domain import (
    Bar,
    FillEvent,
    OrderIntent,
    OrderReconciliation,
    OrderState,
    PointInTimeSnapshot,
    TargetPortfolio,
)
from quantbot.trading.ledger import PortfolioLedger


NOW = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)


def bar(offset_minutes, open_price=100.0, close_price=101.0, volume=100.0):
    return Bar(
        symbol="AAPL",
        timestamp=NOW + timedelta(minutes=offset_minutes),
        open=open_price,
        high=max(open_price, close_price) + 1.0,
        low=min(open_price, close_price) - 1.0,
        close=close_price,
        volume=volume,
    )


def point_snapshot(offset_minutes, symbol_specs):
    observed_at = NOW + timedelta(minutes=offset_minutes)
    bars = {}
    for symbol, (open_price, close_price, volume) in symbol_specs.items():
        bars[symbol] = Bar(
            symbol=symbol,
            timestamp=observed_at,
            open=open_price,
            high=max(open_price, close_price) + 1.0,
            low=min(open_price, close_price) - 1.0,
            close=close_price,
            volume=volume,
        )
    return PointInTimeSnapshot(observed_at=observed_at, bars=bars)


class BuyOnceStrategy:
    def __init__(self, quantity=10.0):
        self.quantity = quantity
        self.called = False

    def on_event(self, state, event):
        del state
        if self.called:
            return ()
        self.called = True
        return (
            OrderIntent(
                client_order_id="buy-aapl-once",
                symbol=event.symbol,
                side="buy",
                quantity=self.quantity,
                created_at=event.timestamp,
            ),
        )


class FixedDailyStrategy:
    def __init__(self, weights):
        self.weights = weights

    def generate_targets(self, snapshot):
        return TargetPortfolio(snapshot.observed_at, self.weights)


class ScheduledDailyStrategy:
    def __init__(self, weights_by_time):
        self.weights_by_time = dict(weights_by_time)
        self.calls = []

    def generate_targets(self, snapshot):
        self.calls.append(snapshot.observed_at)
        return TargetPortfolio(
            snapshot.observed_at,
            self.weights_by_time[snapshot.observed_at],
        )


class SnapshotBuyOnceStrategy:
    def __init__(self):
        self.called = False

    def on_event(self, state, event):
        del state
        if self.called:
            return ()
        self.called = True
        symbol = next(iter(event.bars))
        return (
            OrderIntent(
                "snapshot-buy-once",
                symbol,
                "buy",
                1.0,
                event.observed_at,
            ),
        )


class RepeatSameIntentStrategy:
    def __init__(self, side="buy", quantity=1.0, client_order_id="repeated"):
        self.intent = OrderIntent(
            client_order_id,
            "AAPL",
            side,
            quantity,
            NOW,
        )

    def on_event(self, state, event):
        del state, event
        return (self.intent,)


class SubmitOnceStrategy:
    def __init__(self, intents):
        self.intents = tuple(intents)
        self.called = False

    def on_event(self, state, event):
        del state, event
        if self.called:
            return ()
        self.called = True
        return self.intents


class TimestampedIntentStrategy:
    def __init__(self, created_at, client_order_id="timestamped"):
        self.intent = OrderIntent(
            client_order_id,
            "AAPL",
            "buy",
            1.0,
            created_at,
        )

    def on_event(self, state, event):
        del state, event
        return (self.intent,)


class TradingEngineTest(unittest.TestCase):
    def test_same_timestamp_candidates_are_processed_in_fill_id_order(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        first_by_id = OrderIntent("tie-order-a", "AAPL", "buy", 1.0, NOW)
        second_by_id = OrderIntent("tie-order-z", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        for intent in (first_by_id, second_by_id):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        fill_a = FillEvent(
            "a-fill",
            first_by_id.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )
        fill_z = FillEvent(
            "z-fill",
            second_by_id.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )

        class ReverseTiePort:
            def __init__(self):
                self.returned = False

            def on_event(self, ignored):
                del ignored
                if self.returned:
                    return ()
                self.returned = True
                return (fill_z, fill_a)

            def acknowledge_fill(self, ignored):
                del ignored
                return OrderState.FILLED

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            ReverseTiePort(),
        )

        engine.process_event(event)

        self.assertEqual(
            tuple(executed.fill_id for executed in ledger.fills),
            ("a-fill", "z-fill"),
        )

    def test_submit_timeout_recovers_partial_and_filled_broker_economics(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        for state, quantity in (
            (OrderState.PARTIALLY_FILLED, 0.5),
            (OrderState.FILLED, 1.0),
        ):
            with self.subTest(state=state):
                intent = OrderIntent(
                    "submit-query-" + state.value,
                    "AAPL",
                    "buy",
                    1.0,
                    NOW,
                )
                broker_fill = FillEvent(
                    "submit-query-fill-" + state.value,
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    quantity,
                    10.0,
                    0.25,
                    NOW,
                )
                result = OrderReconciliation(
                    intent.client_order_id,
                    state,
                    quantity,
                    (broker_fill,),
                    observed_at=NOW,
                    revision=2,
                )

                class SubmitTimeoutPort:
                    def __init__(self):
                        self.query_calls = 0

                    def on_event(self, event):
                        del event
                        return ()

                    def preview_submit_order(self, submitted):
                        del submitted
                        return OrderState.ACCEPTED

                    def submit_order(self, submitted):
                        del submitted
                        raise TimeoutError("submit outcome unknown")

                    def cancel_order(self, client_order_id):
                        del client_order_id
                        raise TimeoutError("cancel outcome unknown")

                    def query_order(self, client_order_id):
                        del client_order_id
                        self.query_calls += 1
                        return result

                ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
                execution = SubmitTimeoutPort()
                engine = EventDrivenTradingEngine(
                    ledger,
                    SubmitOnceStrategy((intent,)),
                    execution,
                )

                with self.assertRaises(Exception):
                    engine.process_event(
                        bar(0, open_price=10.0, close_price=10.0)
                    )

                snapshot = ledger.snapshot()
                self.assertEqual(execution.query_calls, 1)
                self.assertEqual(snapshot.orders[intent.client_order_id], state)
                self.assertEqual(snapshot.fills, (broker_fill,))
                self.assertEqual(snapshot.positions["AAPL"].quantity, quantity)
                self.assertEqual(snapshot.cash, 100.0 - quantity * 10.0 - 0.25)
                self.assertEqual(snapshot.fees, 0.25)
                self.assertFalse(snapshot.requires_reconciliation)

    def test_working_cancel_queries_and_imports_partial_canceled_fill(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("cancel-query-partial", "AAPL", "buy", 1.0, NOW)
        broker_fill = FillEvent(
            "cancel-query-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            0.5,
            10.0,
            0.25,
            NOW,
        )
        result = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.5,
            (broker_fill,),
            observed_at=NOW,
            revision=2,
        )

        class CancelQueryPort:
            def __init__(self):
                self.query_calls = 0

            def on_event(self, event):
                del event
                return ()

            def preview_cancel_order(self, client_order_id):
                del client_order_id
                return OrderState.CANCELED

            def cancel_order(self, client_order_id):
                del client_order_id
                return OrderState.CANCELED

            def query_order(self, client_order_id):
                del client_order_id
                self.query_calls += 1
                return result

        class CancelStrategy:
            def cancel_order_ids(self, state, event):
                del state, event
                return (intent.client_order_id,)

            def on_event(self, state, event):
                del state, event
                return ()

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        execution = CancelQueryPort()
        engine = EventDrivenTradingEngine(ledger, CancelStrategy(), execution)

        engine.process_event(bar(0, open_price=10.0, close_price=10.0))

        snapshot = ledger.snapshot()
        self.assertEqual(execution.query_calls, 1)
        self.assertEqual(
            snapshot.orders[intent.client_order_id], OrderState.CANCELED
        )
        self.assertEqual(snapshot.fills, (broker_fill,))
        self.assertEqual(snapshot.positions["AAPL"].quantity, 0.5)
        self.assertEqual(snapshot.cash, 94.75)

    def test_working_cancel_active_query_persists_terminal_cursor_floor(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("working-cancel-conflict", "AAPL", "buy", 1.0, NOW)

        class WorkingCancelConflictPort:
            def __init__(self):
                self.result = OrderReconciliation(
                    intent.client_order_id,
                    OrderState.ACCEPTED,
                    0.0,
                    observed_at=NOW,
                    revision=10,
                )

            def on_event(self, event):
                del event
                return ()

            def preview_cancel_order(self, client_order_id):
                del client_order_id
                return OrderState.CANCELED

            def cancel_order(self, client_order_id):
                del client_order_id
                return OrderState.CANCELED

            def query_order(self, client_order_id):
                self.queried = client_order_id
                return self.result

        class CancelWorkingStrategy:
            def cancel_order_ids(self, state, event):
                del state, event
                return (intent.client_order_id,)

            def on_event(self, state, event):
                del state, event
                return ()

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        execution = WorkingCancelConflictPort()
        engine = EventDrivenTradingEngine(
            ledger,
            CancelWorkingStrategy(),
            execution,
        )

        with self.assertRaisesRegex(ValueError, "cancel working order"):
            engine.process_event(bar(0, open_price=10.0, close_price=10.0))

        detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(detail.state, OrderState.RECONCILIATION_REQUIRED)
        self.assertTrue(detail.terminal_required)
        self.assertEqual(detail.reconciliation_cursor, execution.result)
        self.assertEqual(detail.reconciliation_revision, 10)
        self.assertEqual(ledger.snapshot().cash, 100.0)
        self.assertEqual(ledger.snapshot().fills, ())

        active_at = NOW + timedelta(seconds=1)
        execution.result = OrderReconciliation(
            intent.client_order_id,
            OrderState.ACCEPTED,
            0.0,
            observed_at=active_at,
            revision=11,
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            engine.reconcile_order(intent.client_order_id)
        active_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(active_detail.state, OrderState.RECONCILIATION_REQUIRED)
        self.assertTrue(active_detail.terminal_required)
        self.assertEqual(active_detail.reconciliation_cursor, execution.result)

        terminal_at = NOW + timedelta(seconds=2)
        execution.result = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.0,
            observed_at=terminal_at,
            revision=12,
        )
        self.assertEqual(
            engine.reconcile_order(intent.client_order_id),
            OrderState.CANCELED,
        )
        terminal_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertFalse(terminal_detail.terminal_required)
        self.assertEqual(terminal_detail.reconciliation_cursor, execution.result)

    def test_engine_reconcile_order_queries_while_trading_is_blocked(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("manual-engine-query", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.require_reconciliation(intent.client_order_id)
        result = OrderReconciliation(
            intent.client_order_id,
            OrderState.ACCEPTED,
            0.0,
            observed_at=NOW,
            revision=1,
        )

        class QueryPort:
            def query_order(self, client_order_id):
                self.queried = client_order_id
                return result

        execution = QueryPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
        )
        with self.assertRaises(ValueError):
            engine.process_event(bar(0))

        self.assertEqual(
            engine.reconcile_order(intent.client_order_id),
            OrderState.ACCEPTED,
        )
        self.assertEqual(execution.queried, intent.client_order_id)
        self.assertFalse(ledger.requires_reconciliation)

    def test_engine_reconcile_rejects_unversioned_query_result(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("unversioned-query", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.require_reconciliation(intent.client_order_id)

        class LegacyQueryPort:
            def query_order(self, client_order_id):
                return OrderReconciliation(
                    client_order_id,
                    OrderState.ACCEPTED,
                    0.0,
                )

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            LegacyQueryPort(),
        )

        with self.assertRaisesRegex(ValueError, "observed_at.*revision"):
            engine.reconcile_order(intent.client_order_id)
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_terminal_cancel_conflicting_with_active_query_stays_unresolved(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("cancel-query-conflict", "AAPL", "buy", 1.0, NOW)

        class ConflictingSubmitPort:
            def __init__(self):
                self.result = OrderReconciliation(
                    intent.client_order_id,
                    OrderState.ACCEPTED,
                    0.0,
                    observed_at=NOW,
                    revision=10,
                )

            def on_event(self, event):
                del event
                return ()

            def preview_submit_order(self, submitted):
                del submitted
                return OrderState.ACCEPTED

            def submit_order(self, submitted):
                del submitted
                raise TimeoutError("submit response lost")

            def cancel_order(self, client_order_id):
                del client_order_id
                return OrderState.CANCELED

            def query_order(self, client_order_id):
                self.asserted_order_id = client_order_id
                return self.result

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        execution = ConflictingSubmitPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((intent,)),
            execution,
        )

        with self.assertRaises(Exception):
            engine.process_event(bar(0, open_price=10.0, close_price=10.0))
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )
        conflicted = ledger.snapshot()
        conflicted_detail = conflicted.order_details[intent.client_order_id]
        self.assertTrue(conflicted_detail.terminal_required)
        self.assertEqual(conflicted_detail.reconciliation_cursor, execution.result)
        self.assertEqual(conflicted_detail.reconciliation_revision, 10)
        self.assertEqual(conflicted.cash, 100.0)
        self.assertEqual(conflicted.fills, ())

        execution.result = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.0,
            observed_at=NOW,
            revision=1,
        )
        with self.assertRaisesRegex(ValueError, "stale"):
            engine.reconcile_order(intent.client_order_id)
        self.assertEqual(
            ledger.snapshot().order_details[intent.client_order_id].reconciliation_revision,
            10,
        )

        active_at = NOW + timedelta(seconds=1)
        execution.result = OrderReconciliation(
            intent.client_order_id,
            OrderState.ACCEPTED,
            0.0,
            observed_at=active_at,
            revision=11,
        )
        with self.assertRaisesRegex(ValueError, "terminal"):
            engine.reconcile_order(intent.client_order_id)
        active_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertTrue(active_detail.terminal_required)
        self.assertEqual(active_detail.reconciliation_cursor, execution.result)
        self.assertEqual(active_detail.reconciliation_revision, 11)
        self.assertEqual(ledger.snapshot().cash, 100.0)
        self.assertEqual(ledger.snapshot().fills, ())

        resolved_at = NOW + timedelta(seconds=2)
        execution.result = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.0,
            observed_at=resolved_at,
            revision=12,
        )
        self.assertEqual(
            engine.reconcile_order(intent.client_order_id),
            OrderState.CANCELED,
        )
        detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertFalse(detail.terminal_required)
        self.assertEqual(detail.reconciliation_cursor, execution.result)
        self.assertEqual(detail.reconciliation_revision, 12)
        self.assertEqual(detail.reconciliation_observed_at, resolved_at)

    def test_exact_replay_confirmation_timeout_requires_reconciliation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("replay-confirm-timeout", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        committed = FillEvent(
            "replay-confirm-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )
        ledger.apply_fill(committed)

        class ConfirmationTimeoutPort:
            def __init__(self):
                self.calls = 0

            def on_event(self, ignored):
                del ignored
                self.calls += 1
                if self.calls == 1:
                    return (committed,)
                raise TimeoutError("cannot confirm candidate disappeared")

            def acknowledge_fill(self, fill_event):
                del fill_event
                return OrderState.FILLED

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            ConfirmationTimeoutPort(),
        )

        with self.assertRaises(Exception):
            engine.process_event(event)
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_working_order_cancel_timeout_requires_reconciliation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("cancel-timeout", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)

        class CancelTimeoutPort:
            def on_event(self, event):
                del event
                return ()

            def preview_cancel_order(self, client_order_id):
                del client_order_id
                return OrderState.CANCELED

            def cancel_order(self, client_order_id):
                del client_order_id
                raise TimeoutError("cancel outcome unknown")

        class CancelStrategy:
            def cancel_order_ids(self, state, event):
                del state, event
                return (intent.client_order_id,)

            def on_event(self, state, event):
                del state, event
                return ()

        engine = EventDrivenTradingEngine(
            ledger,
            CancelStrategy(),
            CancelTimeoutPort(),
        )

        with self.assertRaises(Exception):
            engine.process_event(bar(0))
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_unconverged_exact_replay_at_round_limit_requires_reconciliation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("unconverged-replay", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        committed = FillEvent(
            "unconverged-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )
        ledger.apply_fill(committed)

        class NeverConvergingPort:
            def on_event(self, ignored):
                del ignored
                return (committed,)

            def acknowledge_fill(self, fill_event):
                del fill_event
                return OrderState.FILLED

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            NeverConvergingPort(),
            max_fill_rounds=1,
        )

        with self.assertRaises(ValueError):
            engine.process_event(event)
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_execution_ports_query_explicit_reconciliation_results(self):
        from quantbot.trading import ExecutionPort, OrderReconciliation
        from quantbot.trading.engine import BacktestExecutionPort, RecordingExecutionPort

        intent = OrderIntent("query-order", "AAPL", "buy", 10.0, NOW)
        backtest = BacktestExecutionPort()
        recording = RecordingExecutionPort()
        for execution in (backtest, recording):
            execution.submit_order(intent)
            self.assertIsInstance(execution, ExecutionPort)
            self.assertEqual(
                execution.query_order(intent.client_order_id),
                OrderReconciliation(
                    intent.client_order_id,
                    OrderState.ACCEPTED,
                    0.0,
                    observed_at=NOW,
                    revision=1,
                ),
            )

        candidate = backtest.on_event(
            bar(1, open_price=10.0, close_price=10.0, volume=4.0)
        )[0]
        self.assertEqual(
            backtest.acknowledge_fill(candidate),
            OrderState.PARTIALLY_FILLED,
        )
        self.assertEqual(
            backtest.query_order(intent.client_order_id),
            OrderReconciliation(
                intent.client_order_id,
                OrderState.PARTIALLY_FILLED,
                4.0,
                (candidate,),
                observed_at=candidate.filled_at,
                revision=2,
            ),
        )
        backtest.cancel_order(intent.client_order_id)
        self.assertEqual(
            backtest.query_order(intent.client_order_id),
            OrderReconciliation(
                intent.client_order_id,
                OrderState.CANCELED,
                4.0,
                (candidate,),
                observed_at=candidate.filled_at,
                revision=3,
            ),
        )

        self.assertEqual(recording.cancel_order(intent.client_order_id), OrderState.CANCELED)
        self.assertEqual(
            recording.query_order(intent.client_order_id),
            OrderReconciliation(
                intent.client_order_id,
                OrderState.CANCELED,
                0.0,
                observed_at=NOW,
                revision=2,
            ),
        )

    def test_backtest_ack_keeps_nextafter_underfill_pending(self):
        import math
        from quantbot.trading.engine import BacktestExecutionPort

        execution = BacktestExecutionPort()
        intent = OrderIntent("port-underfill", "AAPL", "buy", 1.0, NOW)
        execution.submit_order(intent)
        underfill = math.nextafter(1.0, 0.0)
        candidate = execution.on_event(
            bar(1, open_price=10.0, close_price=10.0, volume=underfill)
        )[0]

        state = execution.acknowledge_fill(candidate)

        self.assertEqual(state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(execution.pending_order_ids, (intent.client_order_id,))

    def test_point_in_time_bars_fill_in_timestamp_then_symbol_order(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        aapl = OrderIntent("async-aapl", "AAPL", "buy", 1.0, NOW)
        msft = OrderIntent("async-msft", "MSFT", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        for intent in (aapl, msft):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
            execution.submit_order(intent)
        earlier = Bar(
            "MSFT",
            NOW + timedelta(minutes=1),
            20.0,
            21.0,
            19.0,
            20.0,
            10.0,
        )
        later = Bar(
            "AAPL",
            NOW + timedelta(minutes=2),
            10.0,
            11.0,
            9.0,
            10.0,
            10.0,
        )
        snapshot = PointInTimeSnapshot(
            observed_at=later.timestamp,
            bars={"AAPL": later, "MSFT": earlier},
        )
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
        )

        engine.process_event(snapshot)

        self.assertEqual(
            [(fill.symbol, fill.filled_at) for fill in ledger.fills],
            [("MSFT", earlier.timestamp), ("AAPL", later.timestamp)],
        )
        self.assertEqual(ledger.order_state(aapl.client_order_id), OrderState.FILLED)
        self.assertEqual(ledger.order_state(msft.client_order_id), OrderState.FILLED)

    def test_max_one_round_accepts_one_normal_nonempty_batch(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        intent = OrderIntent("one-round-fill", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        class CountingBacktestPort(BacktestExecutionPort):
            def __init__(self):
                super().__init__()
                self.event_calls = 0

            def on_event(self, event):
                self.event_calls += 1
                return super().on_event(event)

        execution = CountingBacktestPort()
        execution.submit_order(intent)
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
            max_fill_rounds=1,
        )

        engine.process_event(bar(1, open_price=10.0, close_price=10.0))

        self.assertEqual(execution.event_calls, 2)
        self.assertEqual(ledger.order_state(intent.client_order_id), OrderState.FILLED)

    def test_max_one_round_allows_empty_confirmation_after_capacity_release(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        intent = OrderIntent("one-round-cancel", "AAPL", "sell", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)

        class CountingBacktestPort(BacktestExecutionPort):
            def __init__(self):
                super().__init__()
                self.event_calls = 0

            def on_event(self, event):
                self.event_calls += 1
                return super().on_event(event)

        execution = CountingBacktestPort()
        execution.submit_order(intent)
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
            max_fill_rounds=1,
        )

        engine.process_event(bar(1, open_price=10.0, close_price=10.0))

        self.assertEqual(execution.event_calls, 2)
        self.assertEqual(ledger.order_state(intent.client_order_id), OrderState.CANCELED)

    def test_max_one_round_raises_only_after_observing_second_nonempty_batch(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        blocked = OrderIntent("round-limit-blocked", "AAPL", "sell", 1.0, NOW)
        waiting = OrderIntent("round-limit-waiting", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution_base = BacktestExecutionPort()
        for intent in (blocked, waiting):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
            execution_base.submit_order(intent)

        class CountingPort:
            def __init__(self, delegate):
                self.delegate = delegate
                self.event_calls = 0

            def on_event(self, event):
                self.event_calls += 1
                return self.delegate.on_event(event)

            def acknowledge_fill(self, fill_event):
                return self.delegate.acknowledge_fill(fill_event)

            def cancel_order(self, client_order_id):
                return self.delegate.cancel_order(client_order_id)

            def query_order(self, client_order_id):
                return self.delegate.query_order(client_order_id)

            def resize_fill(self, fill_event, quantity):
                return self.delegate.resize_fill(fill_event, quantity)

        execution = CountingPort(execution_base)
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
            max_fill_rounds=1,
        )

        with self.assertRaisesRegex(ValueError, "max_fill_rounds"):
            engine.process_event(bar(1, open_price=10.0, close_price=10.0, volume=1.0))

        self.assertEqual(execution.event_calls, 2)
        self.assertEqual(ledger.order_state(blocked.client_order_id), OrderState.CANCELED)
        self.assertEqual(ledger.order_state(waiting.client_order_id), OrderState.ACCEPTED)

    def test_same_order_multiple_candidates_are_rejected_even_within_remaining(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("multi-candidate", "AAPL", "buy", 10.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidates = (
            FillEvent("multi-one", intent.client_order_id, "AAPL", "buy", 4.0, 10.0, 0.0, event.timestamp),
            FillEvent("multi-two", intent.client_order_id, "AAPL", "buy", 4.0, 10.0, 0.0, event.timestamp),
        )

        class MultiCandidatePort:
            def __init__(self):
                self.acked = []

            def on_event(self, ignored):
                del ignored
                return candidates

            def acknowledge_fill(self, fill_event):
                self.acked.append(fill_event)
                return OrderState.PARTIALLY_FILLED

        execution = MultiCandidatePort()
        engine = EventDrivenTradingEngine(ledger, SubmitOnceStrategy(()), execution)
        before = ledger.snapshot()

        with self.assertRaisesRegex(ValueError, "one candidate"):
            engine.process_event(event)
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual(execution.acked, [])

    def test_submit_and_cancel_timeouts_require_explicit_reconciliation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine, RecordingExecutionPort

        intent = OrderIntent("uncertain-submit", "AAPL", "buy", 1.0, NOW)

        class AcceptedButTimedOutPort(RecordingExecutionPort):
            def __init__(self):
                super().__init__()
                self.event_calls = 0

            def on_event(self, event):
                self.event_calls += 1
                return super().on_event(event)

            def submit_order(self, submitted):
                super().submit_order(submitted)
                raise TimeoutError("submit response lost")

            def cancel_order(self, client_order_id):
                del client_order_id
                raise TimeoutError("cancel response lost")

            def query_order(self, client_order_id):
                del client_order_id
                raise TimeoutError("query response lost")

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = AcceptedButTimedOutPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((intent,)),
            execution,
        )

        with self.assertRaises(Exception):
            engine.process_event(bar(0))

        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )
        state = ledger.snapshot()
        self.assertTrue(state.requires_reconciliation)
        detail = state.reconciliation_orders[intent.client_order_id]
        self.assertEqual(detail.intent, intent)
        self.assertEqual(detail.filled_quantity, 0.0)
        self.assertEqual(detail.remaining_quantity, 1.0)
        calls_before_block = execution.event_calls
        with self.assertRaisesRegex(ValueError, "reconciliation"):
            engine.process_event(bar(1))
        self.assertEqual(execution.event_calls, calls_before_block)

        resolved = ledger.reconcile_order(
            OrderReconciliation(
                intent.client_order_id,
                OrderState.ACCEPTED,
                0.0,
            )
        )
        self.assertEqual(resolved, OrderState.ACCEPTED)
        self.assertFalse(ledger.requires_reconciliation)

    def test_ack_state_mismatch_marks_order_for_reconciliation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("ack-mismatch", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidate = FillEvent(
            "ack-mismatch-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )

        class MismatchingAckPort:
            def __init__(self):
                self.returned = False

            def on_event(self, ignored):
                del ignored
                if self.returned:
                    return ()
                self.returned = True
                return (candidate,)

            def acknowledge_fill(self, fill_event):
                del fill_event
                return OrderState.FILLED.value

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            MismatchingAckPort(),
        )

        with self.assertRaisesRegex(ValueError, "acknowledgement"):
            engine.process_event(event)
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )
        self.assertTrue(ledger.requires_reconciliation)

    def test_ack_timeout_marks_order_for_reconciliation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("ack-timeout", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidate = FillEvent(
            "ack-timeout-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )

        class TimeoutAckPort:
            def __init__(self):
                self.returned = False

            def on_event(self, ignored):
                del ignored
                if self.returned:
                    return ()
                self.returned = True
                return (candidate,)

            def acknowledge_fill(self, fill_event):
                del fill_event
                raise TimeoutError("ack response lost")

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            TimeoutAckPort(),
        )

        with self.assertRaises(Exception):
            engine.process_event(event)
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_repeated_committed_candidate_is_reacked_and_validated(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("repeat-ack", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidate = FillEvent(
            "repeat-ack-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            event.timestamp,
        )

        class RepeatedCandidatePort:
            def __init__(self):
                self.event_calls = 0
                self.ack_calls = 0

            def on_event(self, ignored):
                del ignored
                self.event_calls += 1
                if self.event_calls <= 2:
                    return (candidate,)
                return ()

            def acknowledge_fill(self, fill_event):
                del fill_event
                self.ack_calls += 1
                if self.ack_calls == 1:
                    return OrderState.FILLED
                return OrderState.ACCEPTED

        execution = RepeatedCandidatePort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
        )

        with self.assertRaisesRegex(ValueError, "acknowledgement"):
            engine.process_event(event)
        self.assertEqual(execution.ack_calls, 2)
        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_engine_resizes_rounded_candidate_to_exact_order_remainder(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("rounded-remainder", "AAPL", "buy", 0.3, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.apply_fill(
            FillEvent(
                "rounded-first",
                intent.client_order_id,
                "AAPL",
                "buy",
                0.1,
                10.0,
                0.0,
                NOW + timedelta(seconds=1),
            )
        )
        event = bar(1, open_price=10.0, close_price=10.0)
        candidate = FillEvent(
            "rounded-second",
            intent.client_order_id,
            "AAPL",
            "buy",
            0.2,
            10.0,
            0.0,
            event.timestamp,
        )

        class RoundedCandidatePort:
            def __init__(self):
                self.returned = False
                self.resized = None
                self.acked = None
                self.cancel_called = False

            def on_event(self, ignored):
                del ignored
                if self.returned:
                    return ()
                self.returned = True
                return (candidate,)

            def resize_fill(self, original, quantity):
                self.resized = FillEvent(
                    original.fill_id,
                    original.client_order_id,
                    original.symbol,
                    original.side,
                    quantity,
                    original.price,
                    original.fee,
                    original.filled_at,
                )
                return self.resized

            def acknowledge_fill(self, fill_event):
                self.acked = fill_event
                return OrderState.FILLED

            def cancel_order(self, client_order_id):
                del client_order_id
                self.cancel_called = True
                raise AssertionError("order-rounding resize must not trigger risk cancel")

        execution = RoundedCandidatePort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            execution,
        )
        exact_remaining = intent.quantity - 0.1

        engine.process_event(event)

        self.assertIsNotNone(execution.resized)
        self.assertEqual(execution.resized.quantity, exact_remaining)
        self.assertEqual(execution.acked, execution.resized)
        self.assertFalse(execution.cancel_called)
        self.assertEqual(ledger.order_state(intent.client_order_id), OrderState.FILLED)
        self.assertEqual(ledger.snapshot().fills[-1].quantity, exact_remaining)

    def test_invalid_submit_state_is_canceled_in_port_and_ledger(self):
        from quantbot.trading.engine import EventDrivenTradingEngine, RecordingExecutionPort

        intent = OrderIntent("invalid-submit-state", "AAPL", "buy", 1.0, NOW)

        class InvalidStatePort(RecordingExecutionPort):
            def submit_order(self, submitted):
                super().submit_order(submitted)
                return OrderState.PARTIALLY_FILLED

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = InvalidStatePort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((intent,)),
            execution,
        )

        with self.assertRaisesRegex(ValueError, "accept or reject"):
            engine.process_event(bar(0))

        self.assertEqual(
            ledger.order_state(intent.client_order_id),
            OrderState.CANCELED,
        )
        self.assertEqual(
            execution.preview_cancel_order(intent.client_order_id),
            OrderState.CANCELED,
        )
        self.assertEqual(ledger.snapshot().open_orders, {})

    def test_cancellation_batch_is_prevalidated_before_first_cancel(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        working = OrderIntent("working-cancel", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(working)
        ledger.accept_order(working.client_order_id)
        execution = BacktestExecutionPort()
        execution.submit_order(working)

        class CancellationStrategy:
            def cancel_order_ids(self, state, event):
                del state, event
                return (working.client_order_id, "unknown-cancel")

            def on_event(self, state, event):
                del state, event
                return ()

        engine = EventDrivenTradingEngine(ledger, CancellationStrategy(), execution)
        before = ledger.snapshot()
        before_pending = execution.pending_order_ids
        current_bar = bar(0)

        with self.assertRaises(ValueError):
            engine.process_event(current_bar)

        after = ledger.snapshot()
        self.assertEqual(after.cash, before.cash)
        self.assertEqual(after.positions, before.positions)
        self.assertEqual(after.orders, before.orders)
        self.assertEqual(after.fills, before.fills)
        self.assertEqual(after.effective_fill_quantities, before.effective_fill_quantities)
        self.assertEqual(after.fees, before.fees)
        self.assertEqual(after.marks["AAPL"].price, current_bar.close)
        self.assertEqual(after.marks["AAPL"].timestamp, current_bar.timestamp)
        self.assertEqual(execution.pending_order_ids, before_pending)
        self.assertEqual(
            execution.submit_order(working),
            OrderState.ACCEPTED,
        )

    def test_intent_batch_wrong_type_is_rejected_before_first_submit(self):
        from quantbot.trading.engine import EventDrivenTradingEngine, RecordingExecutionPort

        valid = OrderIntent("valid-before-type-error", "AAPL", "buy", 1.0, NOW)

        class InvalidBatchStrategy:
            def on_event(self, state, event):
                del state, event
                return (valid, object())

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = RecordingExecutionPort()
        engine = EventDrivenTradingEngine(ledger, InvalidBatchStrategy(), execution)

        with self.assertRaises(ValueError):
            engine.process_event(bar(0))

        self.assertEqual(ledger.snapshot().orders, {})
        self.assertEqual(execution.recorded_intents, ())

    def test_intent_batch_port_conflict_is_rejected_before_first_submit(self):
        from quantbot.trading.engine import EventDrivenTradingEngine, RecordingExecutionPort

        valid = OrderIntent("valid-before-port-conflict", "AAPL", "buy", 1.0, NOW)
        existing = OrderIntent("port-conflict", "AAPL", "buy", 2.0, NOW)
        conflicting = OrderIntent("port-conflict", "AAPL", "buy", 3.0, NOW)
        execution = RecordingExecutionPort()
        execution.submit_order(existing)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((valid, conflicting)),
            execution,
        )

        with self.assertRaises(ValueError):
            engine.process_event(bar(0))

        self.assertEqual(ledger.snapshot().orders, {})
        self.assertEqual(execution.recorded_intents, (existing,))

    def test_intent_batch_invalid_preview_state_is_rejected_atomically(self):
        from quantbot.trading.engine import EventDrivenTradingEngine, RecordingExecutionPort

        first = OrderIntent("valid-preview-state", "AAPL", "buy", 1.0, NOW)
        invalid = OrderIntent("invalid-preview-state", "AAPL", "buy", 1.0, NOW)

        class InvalidPreviewPort(RecordingExecutionPort):
            def preview_submit_order(self, intent):
                if intent.client_order_id == invalid.client_order_id:
                    return OrderState.PARTIALLY_FILLED
                return super().preview_submit_order(intent)

            def submit_order(self, intent):
                state = super().submit_order(intent)
                if intent.client_order_id == invalid.client_order_id:
                    return OrderState.PARTIALLY_FILLED
                return state

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = InvalidPreviewPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((first, invalid)),
            execution,
        )

        with self.assertRaises(ValueError):
            engine.process_event(bar(0))

        self.assertEqual(ledger.snapshot().orders, {})
        self.assertEqual(execution.recorded_intents, ())

    def test_duplicate_intent_ids_in_one_batch_are_rejected_atomically(self):
        from quantbot.trading.engine import EventDrivenTradingEngine, RecordingExecutionPort

        repeated = OrderIntent("same-batch-id", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = RecordingExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((repeated, repeated)),
            execution,
        )

        with self.assertRaises(ValueError):
            engine.process_event(bar(0))

        self.assertEqual(ledger.snapshot().orders, {})
        self.assertEqual(execution.recorded_intents, ())

    def test_same_event_reuses_volume_released_by_zero_risk_candidate(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        sell = OrderIntent("blocked-sell", "AAPL", "sell", 5.0, NOW)
        buy = OrderIntent("funded-buy", "AAPL", "buy", 5.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger, SubmitOnceStrategy((sell, buy)), execution
        )
        engine.process_event(bar(0, open_price=10.0, close_price=10.0, volume=5.0))
        fill_event = bar(1, open_price=10.0, close_price=10.0, volume=5.0)

        engine.process_event(fill_event)

        state = ledger.snapshot()
        self.assertEqual(state.orders[sell.client_order_id], OrderState.CANCELED)
        self.assertEqual(state.orders[buy.client_order_id], OrderState.FILLED)
        self.assertEqual([fill.client_order_id for fill in state.fills], [buy.client_order_id])
        self.assertEqual(execution.pending_order_ids, ())
        before_replay = ledger.snapshot()
        engine.process_event(fill_event)
        self.assertEqual(ledger.snapshot(), before_replay)

    def test_same_event_reuses_volume_released_by_resized_candidate(self):
        from quantbot.trading.engine import BacktestExecutionPort, EventDrivenTradingEngine

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        seed = OrderIntent("seed-five", "AAPL", "buy", 5.0, NOW)
        ledger.record_order(seed)
        ledger.accept_order(seed.client_order_id)
        ledger.apply_fill(
            FillEvent("seed-five-fill", seed.client_order_id, "AAPL", "buy", 5.0, 10.0, 0.0, NOW + timedelta(seconds=1))
        )
        signal_time = NOW + timedelta(minutes=1)
        oversized_sell = OrderIntent("oversized-sell", "AAPL", "sell", 10.0, signal_time)
        replacement_buy = OrderIntent("replacement-buy", "AAPL", "buy", 5.0, signal_time)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((oversized_sell, replacement_buy)),
            execution,
        )
        engine.process_event(bar(1, open_price=10.0, close_price=10.0, volume=10.0))
        fill_event = bar(2, open_price=10.0, close_price=10.0, volume=10.0)

        engine.process_event(fill_event)

        state = ledger.snapshot()
        self.assertEqual(state.orders[oversized_sell.client_order_id], OrderState.CANCELED)
        self.assertEqual(state.orders[replacement_buy.client_order_id], OrderState.FILLED)
        event_fills = [fill for fill in state.fills if fill.filled_at == fill_event.timestamp]
        self.assertEqual([(fill.side, fill.quantity) for fill in event_fills], [("sell", 5.0), ("buy", 5.0)])
        self.assertEqual(state.positions["AAPL"].quantity, 5.0)
        self.assertEqual(execution.pending_order_ids, ())

    def test_future_candidate_rejects_entire_batch_before_ledger_or_ack(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        first = OrderIntent("batch-valid", "AAPL", "buy", 1.0, NOW)
        second = OrderIntent("batch-future", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        for intent in (first, second):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidates = (
            FillEvent("valid-candidate", first.client_order_id, "AAPL", "buy", 1.0, 10.0, 0.0, event.timestamp),
            FillEvent("future-candidate", second.client_order_id, "AAPL", "buy", 1.0, 10.0, 0.0, event.timestamp + timedelta(seconds=1)),
        )

        class CandidatePort:
            def __init__(self):
                self.acked = []

            def on_event(self, ignored):
                del ignored
                return candidates

            def acknowledge_fill(self, fill):
                self.acked.append(fill)
                return OrderState.FILLED

        execution = CandidatePort()
        engine = EventDrivenTradingEngine(ledger, SubmitOnceStrategy(()), execution)
        before = ledger.snapshot()

        with self.assertRaises(ValueError):
            engine.process_event(event)
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual(execution.acked, [])

    def test_late_candidate_prevalidation_fails_closed_without_financial_mutation(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("late-candidate", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.mark_to_market({}, observed_at=NOW + timedelta(minutes=2))
        candidate = FillEvent(
            "late-candidate-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.0,
            10.0,
            0.0,
            NOW + timedelta(minutes=1),
        )

        class LateCandidatePort:
            def on_event(self, event):
                del event
                return (candidate,)

        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            LateCandidatePort(),
        )
        before = ledger.snapshot()

        with self.assertRaisesRegex(ValueError, "precede"):
            engine.process_event(bar(3, open_price=10.0, close_price=10.0))

        after = ledger.snapshot()
        self.assertEqual(after.cash, before.cash)
        self.assertEqual(after.positions, before.positions)
        self.assertEqual(after.fees, before.fees)
        self.assertEqual(after.fills, before.fills)
        self.assertEqual(after.effective_fill_quantities, before.effective_fill_quantities)
        self.assertEqual(after.marks, before.marks)
        self.assertEqual(
            after.orders[intent.client_order_id],
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_descending_candidate_times_are_processed_chronologically(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        first = OrderIntent("later-first", "AAPL", "buy", 1.0, NOW)
        second = OrderIntent("earlier-second", "AAPL", "buy", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        for intent in (first, second):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidates = (
            FillEvent("later-fill", first.client_order_id, "AAPL", "buy", 1.0, 10.0, 0.0, event.timestamp),
            FillEvent("earlier-fill", second.client_order_id, "AAPL", "buy", 1.0, 10.0, 0.0, event.timestamp - timedelta(seconds=1)),
        )

        class CandidatePort:
            def __init__(self):
                self.acked = []
                self.returned = False

            def on_event(self, ignored):
                del ignored
                if self.returned:
                    return ()
                self.returned = True
                return candidates

            def acknowledge_fill(self, fill_event):
                self.acked.append(fill_event)
                return OrderState.FILLED

        execution = CandidatePort()
        engine = EventDrivenTradingEngine(ledger, SubmitOnceStrategy(()), execution)

        engine.process_event(event)

        self.assertEqual(
            [fill_event.fill_id for fill_event in execution.acked],
            ["earlier-fill", "later-fill"],
        )
        self.assertEqual(
            [fill_event.fill_id for fill_event in ledger.fills],
            ["earlier-fill", "later-fill"],
        )
        self.assertEqual(ledger.snapshot().cash, 980.0)

    def test_batch_cumulative_overfill_rejects_before_first_fill_mutates(self):
        from quantbot.trading.engine import EventDrivenTradingEngine

        intent = OrderIntent("aggregate-order", "AAPL", "buy", 10.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        event = bar(1, open_price=10.0, close_price=10.0)
        candidates = (
            FillEvent("aggregate-one", intent.client_order_id, "AAPL", "buy", 6.0, 10.0, 0.0, event.timestamp),
            FillEvent("aggregate-two", intent.client_order_id, "AAPL", "buy", 6.0, 10.0, 0.0, event.timestamp),
        )

        class CandidatePort:
            def __init__(self):
                self.acked = []

            def on_event(self, ignored):
                del ignored
                return candidates

            def acknowledge_fill(self, fill):
                self.acked.append(fill)
                return OrderState.PARTIALLY_FILLED

        execution = CandidatePort()
        engine = EventDrivenTradingEngine(ledger, SubmitOnceStrategy(()), execution)
        before = ledger.snapshot()

        with self.assertRaises(ValueError):
            engine.process_event(event)
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual(execution.acked, [])

    def test_engine_marks_point_in_time_snapshot_with_shared_latest_price_rule(self):
        from quantbot.trading.domain import Quote
        from quantbot.trading.engine import (
            EventDrivenTradingEngine,
            RecordingExecutionPort,
        )

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        seed = OrderIntent("mark-seed", "AAPL", "buy", 1.0, NOW)
        ledger.record_order(seed)
        ledger.accept_order(seed.client_order_id)
        ledger.apply_fill(
            FillEvent(
                "mark-seed-fill",
                seed.client_order_id,
                "AAPL",
                "buy",
                1.0,
                100.0,
                0.0,
                NOW + timedelta(seconds=1),
            )
        )
        current_bar = Bar(
            "AAPL",
            NOW + timedelta(minutes=1),
            108.0,
            112.0,
            107.0,
            110.0,
            100.0,
        )
        stale_quote = Quote(
            "AAPL",
            NOW + timedelta(seconds=30),
            99.0,
            101.0,
        )
        snapshot = PointInTimeSnapshot(
            NOW + timedelta(minutes=1),
            bars={"AAPL": current_bar},
            quotes={"AAPL": stale_quote},
        )
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy(()),
            RecordingExecutionPort(),
        )

        engine.process_event(snapshot)

        self.assertEqual(ledger.snapshot().positions["AAPL"].market_price, 110.0)

    def test_engine_rejects_out_of_order_event_before_execution_side_effect(self):
        from quantbot.trading.engine import (
            EventDrivenTradingEngine,
            RecordingExecutionPort,
        )

        class CountingRecordingPort(RecordingExecutionPort):
            def __init__(self):
                super().__init__()
                self.event_calls = 0

            def on_event(self, event):
                self.event_calls += 1
                return super().on_event(event)

        execution = CountingRecordingPort()
        ledger = PortfolioLedger(
            initial_cash=1_000.0,
            observed_at=NOW + timedelta(minutes=1),
        )
        engine = EventDrivenTradingEngine(ledger, BuyOnceStrategy(), execution)

        with self.assertRaises(ValueError):
            engine.process_event(bar(0))
        self.assertEqual(execution.event_calls, 0)
        self.assertEqual(ledger.snapshot().orders, {})

    def test_engine_rejects_new_intent_outside_current_event_time_atomically(self):
        from quantbot.trading.engine import (
            EventDrivenTradingEngine,
            RecordingExecutionPort,
        )

        for label, created_at in (
            ("future", NOW + timedelta(seconds=1)),
            ("past", NOW - timedelta(seconds=1)),
        ):
            with self.subTest(label=label):
                ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
                execution = RecordingExecutionPort()
                strategy = TimestampedIntentStrategy(
                    created_at,
                    client_order_id="timestamped-" + label,
                )
                engine = EventDrivenTradingEngine(ledger, strategy, execution)

                with self.assertRaises(ValueError):
                    engine.process_event(bar(0))
                self.assertEqual(ledger.snapshot().orders, {})
                self.assertEqual(execution.recorded_intents, ())

    def test_backtest_port_itself_skips_events_at_or_before_submission_time(self):
        from quantbot.trading.engine import BacktestExecutionPort

        execution = BacktestExecutionPort()
        intent = OrderIntent("direct-order", "AAPL", "buy", 1.0, NOW)
        execution.submit_order(intent)

        self.assertEqual(execution.on_event(bar(0)), ())
        fills = execution.on_event(bar(1, open_price=103.0, close_price=104.0))
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].price, 103.0)

    def test_backtest_fills_at_next_event_open_not_trigger_event_price(self):
        try:
            from quantbot.trading.engine import (
                BacktestExecutionPort,
                EventDrivenTradingEngine,
            )
        except ImportError as error:
            self.fail(str(error))

        ledger = PortfolioLedger(initial_cash=10_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(ledger, BuyOnceStrategy(), execution)

        submitted = engine.process_event(bar(0, open_price=100.0, close_price=101.0))

        self.assertEqual(len(submitted), 1)
        after_signal = ledger.snapshot()
        self.assertEqual(after_signal.positions, {})
        self.assertEqual(after_signal.orders["buy-aapl-once"], OrderState.ACCEPTED)
        self.assertEqual(after_signal.fills, ())

        generated = engine.process_event(bar(1, open_price=105.0, close_price=106.0))

        self.assertEqual(generated, ())
        after_next_event = ledger.snapshot()
        self.assertEqual(after_next_event.orders["buy-aapl-once"], OrderState.FILLED)
        self.assertEqual(after_next_event.positions["AAPL"].quantity, 10.0)
        self.assertEqual(after_next_event.fills[0].price, 105.0)
        self.assertEqual(after_next_event.fills[0].filled_at, NOW + timedelta(minutes=1))

    def test_backtest_applies_cost_and_caps_each_event_volume(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        ledger = PortfolioLedger(initial_cash=10_000.0, observed_at=NOW)
        try:
            execution = BacktestExecutionPort(
                cost_bps=20.0,
                max_volume_participation=0.10,
            )
        except TypeError as error:
            self.fail(str(error))
        engine = EventDrivenTradingEngine(ledger, BuyOnceStrategy(), execution)

        engine.process_event(bar(0, volume=1_000.0))
        engine.process_event(
            bar(1, open_price=100.0, close_price=100.0, volume=50.0)
        )

        partial = ledger.snapshot()
        self.assertEqual(partial.orders["buy-aapl-once"], OrderState.PARTIALLY_FILLED)
        self.assertEqual(partial.positions["AAPL"].quantity, 5.0)
        self.assertAlmostEqual(partial.fills[0].price, 100.2)
        self.assertEqual(execution.pending_order_ids, ("buy-aapl-once",))

        engine.process_event(
            bar(2, open_price=102.0, close_price=102.0, volume=100.0)
        )
        completed = ledger.snapshot()
        self.assertEqual(completed.orders["buy-aapl-once"], OrderState.FILLED)
        self.assertEqual(completed.positions["AAPL"].quantity, 10.0)
        self.assertAlmostEqual(completed.fills[1].price, 102.204)
        self.assertEqual(execution.pending_order_ids, ())

        invalid_configs = (
            {"cost_bps": -1.0},
            {"cost_bps": 10_000.0},
            {"max_volume_participation": 0.0},
            {"max_volume_participation": 1.1},
        )
        for config in invalid_configs:
            with self.subTest(config=config), self.assertRaises(ValueError):
                BacktestExecutionPort(**config)

    def test_backtest_candidates_are_stable_until_acknowledged(self):
        from quantbot.trading.engine import BacktestExecutionPort

        execution = BacktestExecutionPort(max_volume_participation=0.10)
        intent = OrderIntent("staged", "AAPL", "buy", 10.0, NOW)
        execution.submit_order(intent)
        first_event = bar(1, open_price=100.0, close_price=100.0, volume=50.0)

        first_candidates = tuple(execution.on_event(first_event))
        retry_candidates = tuple(execution.on_event(first_event))

        self.assertEqual(len(first_candidates), 1)
        self.assertEqual(retry_candidates, first_candidates)
        self.assertEqual(first_candidates[0].quantity, 5.0)
        execution.acknowledge_fill(first_candidates[0])
        execution.acknowledge_fill(first_candidates[0])
        self.assertEqual(execution.pending_order_ids, ("staged",))
        self.assertEqual(execution.submit_order(intent), OrderState.PARTIALLY_FILLED)
        self.assertEqual(tuple(execution.on_event(first_event)), ())

        second_candidates = tuple(
            execution.on_event(
                bar(2, open_price=101.0, close_price=101.0, volume=50.0)
            )
        )
        self.assertEqual(len(second_candidates), 1)
        self.assertEqual(second_candidates[0].quantity, 5.0)
        execution.acknowledge_fill(second_candidates[0])
        self.assertEqual(execution.pending_order_ids, ())
        self.assertEqual(execution.submit_order(intent), OrderState.FILLED)

    def test_insufficient_cash_fill_is_resized_then_remainder_is_canceled(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        intent = OrderIntent("unfunded", "AAPL", "buy", 2.0, NOW)
        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((intent,)),
            execution,
        )
        engine.process_event(bar(0, open_price=40.0, close_price=40.0))
        fill_event = bar(1, open_price=60.0, close_price=60.0)

        try:
            engine.process_event(fill_event)
        except ValueError as error:
            self.fail("risk-limited fill should not remain staged: %s" % error)

        self.assertEqual(execution.pending_order_ids, ())
        self.assertEqual(ledger.order_state("unfunded"), OrderState.CANCELED)
        self.assertEqual(len(ledger.snapshot().fills), 1)
        self.assertAlmostEqual(ledger.snapshot().fills[0].quantity, 100.0 / 60.0)
        self.assertAlmostEqual(ledger.snapshot().cash, 0.0)
        self.assertEqual(tuple(execution.on_event(fill_event)), ())

    def test_zero_fillable_naked_short_is_canceled_without_fill(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        intent = OrderIntent("naked", "AAPL", "sell", 1.0, NOW)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((intent,)),
            execution,
        )
        engine.process_event(bar(0, open_price=100.0, close_price=100.0))
        fill_event = bar(1, open_price=101.0, close_price=101.0)

        try:
            engine.process_event(fill_event)
        except ValueError as error:
            self.fail("zero-fillable order should be canceled: %s" % error)

        self.assertEqual(execution.pending_order_ids, ())
        self.assertEqual(ledger.order_state("naked"), OrderState.CANCELED)
        self.assertEqual(ledger.snapshot().fills, ())
        self.assertEqual(tuple(execution.on_event(fill_event)), ())

    def test_batch_risk_limits_later_candidate_after_acknowledging_prior_fill(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        funded = OrderIntent("funded", "AAPL", "buy", 1.0, NOW)
        unfunded = OrderIntent("batch-unfunded", "AAPL", "buy", 2.0, NOW)
        ledger = PortfolioLedger(initial_cash=250.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((funded, unfunded)),
            execution,
        )
        engine.process_event(bar(0, open_price=100.0, close_price=100.0))
        fill_event = bar(1, open_price=100.0, close_price=100.0)

        try:
            engine.process_event(fill_event)
        except ValueError as error:
            self.fail("risk-limited batch should terminate cleanly: %s" % error)

        state = ledger.snapshot()
        self.assertEqual(state.orders["funded"], OrderState.FILLED)
        self.assertEqual(state.orders["batch-unfunded"], OrderState.CANCELED)
        self.assertEqual(len(state.fills), 2)
        self.assertEqual(state.fills[0].client_order_id, "funded")
        self.assertEqual(state.fills[1].client_order_id, "batch-unfunded")
        self.assertEqual(state.fills[1].quantity, 1.5)
        self.assertEqual(execution.pending_order_ids, ())
        self.assertEqual(execution.submit_order(funded), OrderState.FILLED)
        self.assertEqual(execution.submit_order(unfunded), OrderState.CANCELED)
        self.assertEqual(tuple(execution.on_event(fill_event)), ())

    def test_concurrent_sell_candidates_share_available_long_position(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        buy = OrderIntent("seed-long", "AAPL", "buy", 10.0, NOW)
        ledger.record_order(buy)
        ledger.accept_order(buy.client_order_id)
        ledger.apply_fill(
            FillEvent(
                "seed-fill",
                buy.client_order_id,
                "AAPL",
                "buy",
                10.0,
                10.0,
                0.0,
                NOW + timedelta(seconds=1),
            )
        )
        signal_time = NOW + timedelta(minutes=1)
        first_sell = OrderIntent("sell-one", "AAPL", "sell", 7.0, signal_time)
        second_sell = OrderIntent("sell-two", "AAPL", "sell", 7.0, signal_time)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger,
            SubmitOnceStrategy((first_sell, second_sell)),
            execution,
        )
        engine.process_event(bar(1, open_price=10.0, close_price=10.0))

        try:
            engine.process_event(bar(2, open_price=10.0, close_price=10.0))
        except ValueError as error:
            self.fail("concurrent sells should share fillable quantity: %s" % error)

        state = ledger.snapshot()
        self.assertEqual(state.positions, {})
        self.assertEqual(state.orders["sell-one"], OrderState.FILLED)
        self.assertEqual(state.orders["sell-two"], OrderState.CANCELED)
        sell_fills = [fill for fill in state.fills if fill.side == "sell"]
        self.assertEqual([fill.quantity for fill in sell_fills], [7.0, 3.0])
        self.assertEqual(execution.pending_order_ids, ())

    def test_same_strategy_generates_same_intent_for_backtest_and_recording_ports(self):
        try:
            from quantbot.trading.engine import (
                BacktestExecutionPort,
                EventDrivenTradingEngine,
                RecordingExecutionPort,
            )
        except ImportError as error:
            self.fail(str(error))

        backtest_ledger = PortfolioLedger(initial_cash=10_000.0, observed_at=NOW)
        recording_ledger = PortfolioLedger(initial_cash=10_000.0, observed_at=NOW)
        backtest = EventDrivenTradingEngine(
            backtest_ledger,
            BuyOnceStrategy(quantity=3.0),
            BacktestExecutionPort(),
        )
        recording_port = RecordingExecutionPort()
        recording = EventDrivenTradingEngine(
            recording_ledger,
            BuyOnceStrategy(quantity=3.0),
            recording_port,
        )
        event = bar(0)

        backtest_intents = backtest.process_event(event)
        recording_intents = recording.process_event(event)

        self.assertEqual(backtest_intents, recording_intents)
        self.assertEqual(recording_port.recorded_intents, recording_intents)
        recording.process_event(bar(1, open_price=110.0, close_price=111.0))
        self.assertEqual(recording_ledger.snapshot().positions, {})
        self.assertEqual(
            recording_ledger.snapshot().orders["buy-aapl-once"],
            OrderState.ACCEPTED,
        )

    def test_replayed_identical_intent_is_submitted_once_across_execution_ports(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
            RecordingExecutionPort,
        )

        class CountingBacktestExecutionPort(BacktestExecutionPort):
            def __init__(self):
                super().__init__()
                self.submit_calls = 0

            def submit_order(self, intent):
                self.submit_calls += 1
                return super().submit_order(intent)

        class CountingRecordingExecutionPort(RecordingExecutionPort):
            def __init__(self):
                super().__init__()
                self.submit_calls = 0

            def submit_order(self, intent):
                self.submit_calls += 1
                return super().submit_order(intent)

        backtest_ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        recording_ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        backtest_port = CountingBacktestExecutionPort()
        recording_port = CountingRecordingExecutionPort()
        backtest_strategy = RepeatSameIntentStrategy()
        recording_strategy = RepeatSameIntentStrategy()
        backtest = EventDrivenTradingEngine(
            backtest_ledger, backtest_strategy, backtest_port
        )
        recording = EventDrivenTradingEngine(
            recording_ledger, recording_strategy, recording_port
        )

        try:
            for event in (
                bar(0, open_price=100.0, close_price=101.0),
                bar(1, open_price=105.0, close_price=106.0),
                bar(2, open_price=110.0, close_price=111.0),
            ):
                backtest.process_event(event)
                recording.process_event(event)
        except ValueError as error:
            self.fail("identical replay raised ValueError: %s" % error)

        self.assertEqual(backtest_port.submit_calls, 1)
        self.assertEqual(recording_port.submit_calls, 1)
        self.assertEqual(len(backtest_ledger.snapshot().fills), 1)
        self.assertEqual(backtest_port.pending_order_ids, ())
        self.assertEqual(recording_port.recorded_intents, (recording_strategy.intent,))

        self.assertEqual(
            backtest_port.submit_order(backtest_strategy.intent),
            OrderState.FILLED,
        )
        self.assertEqual(backtest_port.pending_order_ids, ())
        self.assertEqual(
            recording_port.submit_order(recording_strategy.intent),
            OrderState.ACCEPTED,
        )
        conflict = OrderIntent("repeated", "AAPL", "buy", 2.0, NOW)
        with self.assertRaises(ValueError):
            backtest_port.submit_order(conflict)
        with self.assertRaises(ValueError):
            recording_port.submit_order(conflict)

    def test_target_portfolio_adapter_emits_stable_delta_intents(self):
        try:
            from quantbot.trading.adapters import TargetPortfolioAdapter
        except ImportError as error:
            self.fail(str(error))

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        existing = OrderIntent("existing", "AAPL", "buy", 2.0, NOW)
        ledger.record_order(existing)
        ledger.accept_order("existing")
        ledger.apply_fill(
            FillEvent(
                "existing-fill",
                "existing",
                "AAPL",
                "buy",
                2.0,
                100.0,
                0.0,
                NOW + timedelta(seconds=1),
            )
        )
        market_bar = bar(10, open_price=100.0, close_price=100.0)
        snapshot = PointInTimeSnapshot(
            observed_at=market_bar.timestamp,
            bars={"AAPL": market_bar},
        )
        state = ledger.mark_to_market(
            {"AAPL": 100.0}, observed_at=market_bar.timestamp
        )

        buy_adapter = TargetPortfolioAdapter(FixedDailyStrategy({"AAPL": 0.50}))
        buy_intents = buy_adapter.on_event(state, snapshot)
        self.assertEqual(len(buy_intents), 1)
        self.assertEqual(buy_intents[0].side, "buy")
        self.assertEqual(buy_intents[0].quantity, 3.0)
        self.assertEqual(buy_adapter.on_event(state, snapshot), buy_intents)

        trim_adapter = TargetPortfolioAdapter(FixedDailyStrategy({"AAPL": 0.10}))
        trim_intent = trim_adapter.on_event(state, snapshot)[0]
        self.assertEqual(trim_intent.side, "sell")
        self.assertEqual(trim_intent.quantity, 1.0)

        liquidate_adapter = TargetPortfolioAdapter(FixedDailyStrategy({}))
        liquidate_intent = liquidate_adapter.on_event(state, snapshot)[0]
        self.assertEqual(liquidate_intent.side, "sell")
        self.assertEqual(liquidate_intent.quantity, 2.0)

    def test_zero_target_cancel_and_liquidation_do_not_require_market_price(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.domain import (
            OrderSnapshot,
            PortfolioSnapshot,
            PositionSnapshot,
        )

        snapshot = PointInTimeSnapshot(observed_at=NOW)
        working_intent = OrderIntent("zero-target-working", "AAPL", "buy", 1.0, NOW)
        working = OrderSnapshot(
            working_intent,
            OrderState.ACCEPTED,
            0.0,
            1.0,
        )
        working_state = PortfolioSnapshot(
            NOW,
            cash=1_000.0,
            orders={working_intent.client_order_id: OrderState.ACCEPTED},
            open_orders={working_intent.client_order_id: working},
            order_details={working_intent.client_order_id: working},
        )
        adapter = TargetPortfolioAdapter(FixedDailyStrategy({}))

        self.assertEqual(
            adapter.cancel_order_ids(working_state, snapshot),
            (working_intent.client_order_id,),
        )

        invested_state = PortfolioSnapshot(
            NOW,
            cash=500.0,
            positions={
                "AAPL": PositionSnapshot("AAPL", 5.0, 100.0, 100.0)
            },
        )
        liquidation = adapter.generate_intents(invested_state, snapshot)
        self.assertEqual(len(liquidation), 1)
        self.assertEqual(liquidation[0].symbol, "AAPL")
        self.assertEqual(liquidation[0].side, "sell")
        self.assertEqual(liquidation[0].quantity, 5.0)

    def test_zero_target_missing_price_does_not_block_other_symbols(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.domain import PortfolioSnapshot, PositionSnapshot

        snapshot = point_snapshot(0, {"MSFT": (10.0, 10.0, 100.0)})
        state = PortfolioSnapshot(
            snapshot.observed_at,
            cash=1_000.0,
            positions={
                "AAPL": PositionSnapshot("AAPL", 2.0, 100.0, 100.0)
            },
        )
        adapter = TargetPortfolioAdapter(
            FixedDailyStrategy({"AAPL": 0.0, "MSFT": 0.5})
        )

        intents = adapter.generate_intents(state, snapshot)

        self.assertEqual(
            [(intent.symbol, intent.side, intent.quantity) for intent in intents],
            [("AAPL", "sell", 2.0), ("MSFT", "buy", 60.0)],
        )

    def test_target_adapter_rejects_portfolio_state_from_the_future(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.domain import PortfolioSnapshot

        market_bar = bar(10, open_price=100.0, close_price=100.0)
        snapshot = PointInTimeSnapshot(
            observed_at=market_bar.timestamp,
            bars={"AAPL": market_bar},
        )
        future_state = PortfolioSnapshot(
            observed_at=snapshot.observed_at + timedelta(seconds=1),
            cash=1_000.0,
        )
        adapter = TargetPortfolioAdapter(FixedDailyStrategy({"AAPL": 0.5}))

        with self.assertRaises(ValueError):
            adapter.generate_intents(future_state, snapshot)

    def test_target_adapter_rejects_non_portfolio_state_with_value_error(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter

        market_bar = bar(10, open_price=100.0, close_price=100.0)
        snapshot = PointInTimeSnapshot(
            observed_at=market_bar.timestamp,
            bars={"AAPL": market_bar},
        )
        adapter = TargetPortfolioAdapter(FixedDailyStrategy({"AAPL": 0.5}))

        try:
            adapter.generate_intents(object(), snapshot)
        except ValueError:
            return
        except Exception as error:
            self.fail("expected ValueError, got %s" % type(error).__name__)
        self.fail("expected ValueError")

    def test_target_adapter_counts_working_remainder_before_creating_orders(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        first = point_snapshot(0, {"AAPL": (10.0, 10.0, 10.0)})
        second = point_snapshot(1, {"AAPL": (10.0, 10.0, 10.0)})
        daily = ScheduledDailyStrategy(
            {first.observed_at: {"AAPL": 1.0}, second.observed_at: {"AAPL": 1.0}}
        )
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger, TargetPortfolioAdapter(daily), execution
        )

        initial_intents = engine.process_event(first)
        later_intents = engine.process_event(second)

        self.assertEqual(len(initial_intents), 1)
        self.assertEqual(initial_intents[0].quantity, 100.0)
        self.assertEqual(later_intents, ())
        state = ledger.snapshot()
        self.assertEqual(state.positions["AAPL"].quantity, 10.0)
        self.assertEqual(len(state.open_orders), 1)
        working = next(iter(state.open_orders.values()))
        self.assertEqual(working.remaining_quantity, 90.0)
        self.assertEqual(execution.pending_order_ids, (working.intent.client_order_id,))
        self.assertEqual(daily.calls, [first.observed_at, second.observed_at])

    def test_target_change_cancels_working_order_before_replacement(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        first = point_snapshot(0, {"AAPL": (10.0, 10.0, 10.0)})
        second = point_snapshot(1, {"AAPL": (10.0, 10.0, 10.0)})
        daily = ScheduledDailyStrategy(
            {first.observed_at: {"AAPL": 1.0}, second.observed_at: {"AAPL": 0.5}}
        )
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger, TargetPortfolioAdapter(daily), execution
        )

        old_intent = engine.process_event(first)[0]
        replacement = engine.process_event(second)

        self.assertEqual(ledger.order_state(old_intent.client_order_id), OrderState.CANCELED)
        self.assertEqual(len(replacement), 1)
        self.assertEqual(replacement[0].symbol, "AAPL")
        self.assertEqual(replacement[0].side, "buy")
        self.assertEqual(replacement[0].quantity, 40.0)
        state = ledger.snapshot()
        self.assertEqual(tuple(state.open_orders), (replacement[0].client_order_id,))
        self.assertEqual(execution.pending_order_ids, (replacement[0].client_order_id,))
        self.assertEqual(daily.calls, [first.observed_at, second.observed_at])

    def test_target_change_to_current_position_only_cancels_working_order(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        first = point_snapshot(0, {"AAPL": (10.0, 10.0, 10.0)})
        second = point_snapshot(1, {"AAPL": (10.0, 10.0, 10.0)})
        daily = ScheduledDailyStrategy(
            {first.observed_at: {"AAPL": 1.0}, second.observed_at: {"AAPL": 0.1}}
        )
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger, TargetPortfolioAdapter(daily), execution
        )

        old_intent = engine.process_event(first)[0]
        replacement = engine.process_event(second)

        self.assertEqual(replacement, ())
        self.assertEqual(ledger.order_state(old_intent.client_order_id), OrderState.CANCELED)
        self.assertEqual(ledger.snapshot().positions["AAPL"].quantity, 10.0)
        self.assertEqual(ledger.snapshot().open_orders, {})
        self.assertEqual(execution.pending_order_ids, ())
        self.assertEqual(daily.calls, [first.observed_at, second.observed_at])

    def test_target_cancel_replace_is_independent_per_symbol(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        specs = {"AAPL": (10.0, 10.0, 10.0), "MSFT": (10.0, 10.0, 10.0)}
        first = point_snapshot(0, specs)
        second = point_snapshot(1, specs)
        daily = ScheduledDailyStrategy(
            {
                first.observed_at: {"AAPL": 0.5, "MSFT": 0.5},
                second.observed_at: {"AAPL": 0.3, "MSFT": 0.5},
            }
        )
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        execution = BacktestExecutionPort()
        engine = EventDrivenTradingEngine(
            ledger, TargetPortfolioAdapter(daily), execution
        )

        original = {intent.symbol: intent for intent in engine.process_event(first)}
        replacements = engine.process_event(second)

        self.assertEqual(len(replacements), 1)
        self.assertEqual(replacements[0].symbol, "AAPL")
        self.assertEqual(replacements[0].quantity, 20.0)
        self.assertEqual(
            ledger.order_state(original["AAPL"].client_order_id),
            OrderState.CANCELED,
        )
        self.assertEqual(
            ledger.order_state(original["MSFT"].client_order_id),
            OrderState.PARTIALLY_FILLED,
        )
        open_orders = ledger.snapshot().open_orders
        self.assertEqual(len(open_orders), 2)
        self.assertIn(original["MSFT"].client_order_id, open_orders)
        self.assertIn(replacements[0].client_order_id, open_orders)
        self.assertEqual(daily.calls, [first.observed_at, second.observed_at])

    def test_target_order_id_binds_generation_target_and_conflicts_fail_closed(self):
        from quantbot.trading.adapters import TargetPortfolioAdapter
        from quantbot.trading.domain import PortfolioSnapshot

        snapshot = point_snapshot(0, {"AAPL": (100.0, 100.0, 100.0)})
        adapter = TargetPortfolioAdapter(FixedDailyStrategy({"AAPL": 0.5}))
        first_state = PortfolioSnapshot(snapshot.observed_at, cash=1_000.0)
        changed_state = PortfolioSnapshot(snapshot.observed_at, cash=900.0)

        first_intent = adapter.generate_intents(first_state, snapshot)[0]
        conflicting_intent = adapter.generate_intents(changed_state, snapshot)[0]

        self.assertEqual(first_intent.client_order_id, conflicting_intent.client_order_id)
        self.assertNotEqual(first_intent, conflicting_intent)
        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        ledger.record_order(first_intent)
        with self.assertRaises(ValueError):
            ledger.record_order(conflicting_intent)

    def test_point_in_time_snapshots_share_the_same_next_event_execution_path(self):
        from quantbot.trading.engine import (
            BacktestExecutionPort,
            EventDrivenTradingEngine,
        )

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        engine = EventDrivenTradingEngine(
            ledger,
            SnapshotBuyOnceStrategy(),
            BacktestExecutionPort(),
        )
        first_bar = bar(0, open_price=100.0, close_price=101.0)
        first = PointInTimeSnapshot(first_bar.timestamp, bars={"AAPL": first_bar})
        second_bar = bar(1, open_price=105.0, close_price=106.0)
        second = PointInTimeSnapshot(second_bar.timestamp, bars={"AAPL": second_bar})

        engine.process_event(first)
        self.assertEqual(ledger.snapshot().fills, ())
        engine.process_event(second)

        filled = ledger.snapshot()
        self.assertIn("AAPL", filled.positions)
        self.assertEqual(filled.positions["AAPL"].quantity, 1.0)
        self.assertEqual(filled.fills[0].price, 105.0)
        self.assertEqual(filled.positions["AAPL"].market_price, 106.0)


if __name__ == "__main__":
    unittest.main()
