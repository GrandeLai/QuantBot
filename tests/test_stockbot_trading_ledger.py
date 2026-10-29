import sys
import unittest
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantbot.trading.domain import FillEvent, OrderIntent, OrderState


NOW = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)


def order(
    client_order_id="order-1",
    symbol="AAPL",
    side="buy",
    quantity=10.0,
    created_at=NOW,
):
    return OrderIntent(client_order_id, symbol, side, quantity, created_at)


def fill(
    fill_id="fill-1",
    client_order_id="order-1",
    symbol="AAPL",
    side="buy",
    quantity=4.0,
    price=100.0,
    fee=1.0,
    offset_seconds=1,
):
    return FillEvent(
        fill_id,
        client_order_id,
        symbol,
        side,
        quantity,
        price,
        fee,
        NOW + timedelta(seconds=offset_seconds),
    )


class PortfolioLedgerTest(unittest.TestCase):
    def test_zero_position_marks_survive_historical_multi_symbol_replay(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_100.0, observed_at=NOW)
        aapl = order(client_order_id="marked-aapl", quantity=10.0)
        msft = order(
            client_order_id="marked-msft",
            symbol="MSFT",
            quantity=1.0,
        )
        for intent in (aapl, msft):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
        mark_time = NOW + timedelta(seconds=5)
        empty_mark = ledger.mark_to_market(
            {"AAPL": 100.0, "MSFT": 200.0},
            observed_at=mark_time,
        )
        self.assertEqual(empty_mark.positions, {})
        self.assertEqual(empty_mark.marks["AAPL"].price, 100.0)
        self.assertEqual(empty_mark.marks["AAPL"].timestamp, mark_time)
        self.assertEqual(empty_mark.marks["MSFT"].price, 200.0)
        with self.assertRaises(TypeError):
            empty_mark.marks["AAPL"] = empty_mark.marks["AAPL"]

        aapl_fill = FillEvent(
            "marked-aapl-fill",
            aapl.client_order_id,
            "AAPL",
            "buy",
            10.0,
            10.0,
            0.0,
            NOW + timedelta(seconds=1),
        )
        ledger.require_reconciliation(aapl.client_order_id)
        ledger.reconcile_order(
            OrderReconciliation(
                aapl.client_order_id,
                OrderState.FILLED,
                10.0,
                (aapl_fill,),
            )
        )
        aapl_snapshot = ledger.snapshot()
        self.assertEqual(aapl_snapshot.positions["AAPL"].market_price, 100.0)
        self.assertEqual(aapl_snapshot.equity, 2_000.0)

        msft_fill = FillEvent(
            "marked-msft-fill",
            msft.client_order_id,
            "MSFT",
            "buy",
            1.0,
            20.0,
            0.0,
            NOW + timedelta(seconds=2),
        )
        ledger.require_reconciliation(msft.client_order_id)
        ledger.reconcile_order(
            OrderReconciliation(
                msft.client_order_id,
                OrderState.FILLED,
                1.0,
                (msft_fill,),
            )
        )
        multi = ledger.snapshot()
        self.assertEqual(multi.positions["AAPL"].market_price, 100.0)
        self.assertEqual(multi.positions["MSFT"].market_price, 200.0)
        self.assertEqual(set(multi.marks), {"AAPL", "MSFT"})

    def test_newer_fill_price_wins_older_mark_during_replay(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=10_000.0, observed_at=NOW)
        intent = order(client_order_id="fill-after-mark", quantity=20.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        first = fill(
            fill_id="fill-before-mark",
            client_order_id=intent.client_order_id,
            quantity=10.0,
            price=10.0,
            fee=0.0,
            offset_seconds=1,
        )
        ledger.apply_fill(first)
        ledger.mark_to_market(
            {"AAPL": 50.0},
            observed_at=NOW + timedelta(seconds=5),
        )
        ledger.require_reconciliation(intent.client_order_id)
        newer = fill(
            fill_id="fill-after-mark-new",
            client_order_id=intent.client_order_id,
            quantity=10.0,
            price=100.0,
            fee=0.0,
            offset_seconds=6,
        )

        ledger.reconcile_order(
            OrderReconciliation(
                intent.client_order_id,
                OrderState.FILLED,
                20.0,
                (first, newer),
            )
        )

        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.positions["AAPL"].market_price, 100.0)
        self.assertEqual(snapshot.equity, 10_900.0)
        self.assertEqual(snapshot.marks["AAPL"].price, 50.0)

    def test_mark_at_fill_timestamp_has_market_price_priority(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_100.0, observed_at=NOW)
        intent = order(client_order_id="same-time-mark", quantity=10.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        same_time = NOW + timedelta(seconds=5)
        ledger.apply_fill(
            FillEvent(
                "same-time-fill",
                intent.client_order_id,
                "AAPL",
                "buy",
                10.0,
                10.0,
                0.0,
                same_time,
            )
        )

        snapshot = ledger.mark_to_market(
            {"AAPL": 100.0},
            observed_at=same_time,
        )

        self.assertEqual(snapshot.positions["AAPL"].market_price, 100.0)
        self.assertEqual(snapshot.equity, 2_000.0)
        self.assertEqual(snapshot.marks["AAPL"].timestamp, same_time)

    def test_same_timestamp_mark_update_is_monotonic_and_last_wins(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        mark_time = NOW + timedelta(seconds=1)
        ledger.mark_to_market({"AAPL": 100.0}, observed_at=mark_time)

        snapshot = ledger.mark_to_market(
            {"AAPL": 101.0},
            observed_at=mark_time,
        )

        self.assertEqual(snapshot.marks["AAPL"].price, 101.0)
        self.assertEqual(snapshot.marks["AAPL"].timestamp, mark_time)

    def test_reconciliation_replays_all_orders_chronologically_for_cross_order_pnl(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        buy = order(client_order_id="historical-buy", quantity=20.0)
        ledger.record_order(buy)
        ledger.accept_order(buy.client_order_id)
        known_late_buy = fill(
            fill_id="known-late-buy",
            client_order_id=buy.client_order_id,
            quantity=10.0,
            price=20.0,
            fee=0.0,
            offset_seconds=2,
        )
        ledger.apply_fill(known_late_buy)

        sell = order(
            client_order_id="later-sell",
            side="sell",
            quantity=10.0,
            created_at=NOW + timedelta(seconds=2),
        )
        ledger.record_order(sell)
        ledger.accept_order(sell.client_order_id)
        later_sell = fill(
            fill_id="later-sell-fill",
            client_order_id=sell.client_order_id,
            side="sell",
            quantity=10.0,
            price=30.0,
            fee=0.0,
            offset_seconds=3,
        )
        ledger.apply_fill(later_sell)
        ledger.require_reconciliation(buy.client_order_id)

        missing_early_buy = fill(
            fill_id="missing-early-buy",
            client_order_id=buy.client_order_id,
            quantity=10.0,
            price=10.0,
            fee=0.0,
            offset_seconds=1,
        )
        result = OrderReconciliation(
            buy.client_order_id,
            OrderState.FILLED,
            20.0,
            (missing_early_buy, known_late_buy),
        )

        self.assertEqual(ledger.reconcile_order(result), OrderState.FILLED)

        snapshot = ledger.snapshot()
        position = snapshot.positions["AAPL"]
        self.assertEqual(position.quantity, 10.0)
        self.assertEqual(position.average_cost, 15.0)
        self.assertEqual(position.market_price, 30.0)
        self.assertEqual(snapshot.realized_pnl, 150.0)
        self.assertEqual(position.realized_pnl, 150.0)
        self.assertEqual(snapshot.cash, 1_000.0)
        self.assertEqual(snapshot.equity, 1_300.0)
        self.assertEqual(snapshot.orders[sell.client_order_id], OrderState.FILLED)
        self.assertEqual(
            snapshot.fills,
            (missing_early_buy, known_late_buy, later_sell),
        )

    def test_reconciliation_canonicalizes_quantity_close_final_overfill(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=10.0, observed_at=NOW)
        intent = order(client_order_id="decimal-replay", quantity=0.3)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        first = fill(
            fill_id="decimal-first",
            client_order_id=intent.client_order_id,
            quantity=0.1,
            price=10.0,
            fee=0.0,
        )
        ledger.apply_fill(first)
        ledger.require_reconciliation(intent.client_order_id)
        raw_final_quantity = intent.quantity - first.quantity
        for _ in range(4):
            raw_final_quantity = math.nextafter(raw_final_quantity, math.inf)
        raw_final = fill(
            fill_id="decimal-final",
            client_order_id=intent.client_order_id,
            quantity=raw_final_quantity,
            price=10.0,
            fee=0.0,
            offset_seconds=2,
        )

        ledger.reconcile_order(
            OrderReconciliation(
                intent.client_order_id,
                OrderState.FILLED,
                first.quantity + raw_final.quantity,
                (first, raw_final),
            )
        )

        snapshot = ledger.snapshot()
        detail = snapshot.order_details[intent.client_order_id]
        self.assertEqual(detail.state, OrderState.FILLED)
        self.assertEqual(detail.filled_quantity, 0.3)
        self.assertEqual(detail.remaining_quantity, 0.0)
        self.assertEqual(snapshot.positions["AAPL"].quantity, 0.3)
        self.assertEqual(snapshot.cash, 7.0)
        self.assertEqual(snapshot.fills, (first, raw_final))
        self.assertEqual(snapshot.fills[-1].quantity, raw_final_quantity)
        self.assertEqual(
            snapshot.effective_fill_quantities,
            {
                first.fill_id: first.quantity,
                raw_final.fill_id: intent.quantity - first.quantity,
            },
        )

    def test_reconciliation_canonicalizes_multiple_new_decimal_fills(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        for same_timestamp, reverse_input in ((False, False), (True, True)):
            with self.subTest(
                same_timestamp=same_timestamp,
                reverse_input=reverse_input,
            ):
                ledger = PortfolioLedger(initial_cash=10.0, observed_at=NOW)
                intent = order(
                    client_order_id=(
                        "all-new-same" if same_timestamp else "all-new-timed"
                    ),
                    quantity=0.3,
                )
                ledger.record_order(intent)
                ledger.accept_order(intent.client_order_id)
                ledger.require_reconciliation(intent.client_order_id)
                first_time = NOW + timedelta(seconds=1)
                final_time = first_time if same_timestamp else NOW + timedelta(seconds=2)
                first = FillEvent(
                    "a-first",
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    0.1,
                    10.0,
                    0.0,
                    first_time,
                )
                raw_final = FillEvent(
                    "z-final",
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    0.2,
                    10.0,
                    0.0,
                    final_time,
                )
                broker_fills = (
                    (raw_final, first) if reverse_input else (first, raw_final)
                )

                ledger.reconcile_order(
                    OrderReconciliation(
                        intent.client_order_id,
                        OrderState.FILLED,
                        first.quantity + raw_final.quantity,
                        broker_fills,
                    )
                )

                snapshot = ledger.snapshot()
                detail = snapshot.order_details[intent.client_order_id]
                self.assertEqual(detail.state, OrderState.FILLED)
                self.assertEqual(detail.filled_quantity, intent.quantity)
                self.assertEqual(
                    tuple(fill_event.fill_id for fill_event in snapshot.fills),
                    (first.fill_id, raw_final.fill_id),
                )
                self.assertEqual(snapshot.fills[-1].quantity, 0.2)
                self.assertEqual(
                    snapshot.effective_fill_quantities,
                    {
                        first.fill_id: first.quantity,
                        raw_final.fill_id: intent.quantity - first.quantity,
                    },
                )
                self.assertEqual(snapshot.positions["AAPL"].quantity, 0.3)
                self.assertEqual(snapshot.cash, 7.0)

    def test_reconciliation_fill_permutations_have_identical_economics(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        permutations = (
            ("a", "b", "c", "z"),
            ("c", "b", "a", "z"),
            ("z", "c", "b", "a"),
            ("b", "z", "c", "a"),
        )
        snapshots = []
        same_time = NOW + timedelta(seconds=1)
        for permutation in permutations:
            ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
            intent = order(
                client_order_id="permutation-order",
                quantity=1.0,
            )
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
            ledger.require_reconciliation(intent.client_order_id)
            fills_by_name = {
                "a": FillEvent(
                    "a",
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    0.1,
                    10.0,
                    0.01,
                    same_time,
                ),
                "b": FillEvent(
                    "b",
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    0.2,
                    20.0,
                    0.02,
                    same_time,
                ),
                "c": FillEvent(
                    "c",
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    0.3,
                    30.0,
                    0.03,
                    same_time,
                ),
                "z": FillEvent(
                    "z",
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    0.4,
                    40.0,
                    0.04,
                    same_time,
                ),
            }
            broker_fills = tuple(fills_by_name[name] for name in permutation)

            ledger.reconcile_order(
                OrderReconciliation(
                    intent.client_order_id,
                    OrderState.FILLED,
                    1.0,
                    broker_fills,
                )
            )
            snapshots.append(ledger.snapshot())

        stable_effective_z = 1.0 - math.fsum((0.1, 0.2, 0.3))
        for snapshot in snapshots:
            self.assertEqual(
                tuple(fill_event.fill_id for fill_event in snapshot.fills),
                ("a", "b", "c", "z"),
            )
            self.assertEqual(
                snapshot.effective_fill_quantities,
                {"a": 0.1, "b": 0.2, "c": 0.3, "z": stable_effective_z},
            )
            detail = next(iter(snapshot.order_details.values()))
            self.assertEqual(detail.state, OrderState.FILLED)
            self.assertEqual(detail.filled_quantity, 1.0)

        baseline = snapshots[0]
        for snapshot in snapshots[1:]:
            self.assertEqual(snapshot.fills, baseline.fills)
            self.assertEqual(snapshot.cash, baseline.cash)
            self.assertEqual(snapshot.positions, baseline.positions)
            self.assertEqual(snapshot.fees, baseline.fees)
            self.assertEqual(snapshot.effective_fill_quantities, baseline.effective_fill_quantities)

    def test_reconciliation_rejects_single_material_first_overfill(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=10.0, observed_at=NOW)
        intent = order(client_order_id="single-first-overfill", quantity=0.3)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.require_reconciliation(intent.client_order_id)
        overfill = fill(
            fill_id="single-first-overfill-fill",
            client_order_id=intent.client_order_id,
            quantity=0.31,
            price=10.0,
            fee=0.0,
        )
        before = ledger.snapshot()

        with self.assertRaises(ValueError):
            ledger.reconcile_order(
                OrderReconciliation(
                    intent.client_order_id,
                    OrderState.FILLED,
                    overfill.quantity,
                    (overfill,),
                )
            )

        after = ledger.snapshot()
        self.assertEqual(after.cash, before.cash)
        self.assertEqual(after.fills, before.fills)
        self.assertEqual(after.effective_fill_quantities, before.effective_fill_quantities)
        self.assertEqual(after.fees, before.fees)
        self.assertEqual(after.positions, before.positions)

    def test_effective_fill_quantities_survive_unrelated_full_replay(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        decimal = order(client_order_id="effective-decimal", quantity=0.3)
        unrelated = order(client_order_id="effective-unrelated", quantity=1.0)
        for intent in (decimal, unrelated):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
        same_time = NOW + timedelta(seconds=1)
        existing_z = FillEvent(
            "z-existing",
            decimal.client_order_id,
            "AAPL",
            "buy",
            0.1,
            10.0,
            0.1,
            same_time,
        )
        ledger.apply_fill(existing_z)
        ledger.require_reconciliation(decimal.client_order_id)
        missing_a = FillEvent(
            "a-missing",
            decimal.client_order_id,
            "AAPL",
            "buy",
            0.2,
            10.0,
            0.2,
            same_time,
        )
        ledger.reconcile_order(
            OrderReconciliation(
                decimal.client_order_id,
                OrderState.FILLED,
                0.1 + 0.2,
                (missing_a, existing_z),
            )
        )

        first_snapshot = ledger.snapshot()
        exact_missing = decimal.quantity - existing_z.quantity
        self.assertEqual(
            tuple(fill_event.fill_id for fill_event in first_snapshot.fills),
            ("a-missing", "z-existing"),
        )
        self.assertEqual(first_snapshot.fills[0].quantity, 0.2)
        self.assertEqual(
            first_snapshot.effective_fill_quantities,
            {
                missing_a.fill_id: exact_missing,
                existing_z.fill_id: existing_z.quantity,
            },
        )
        self.assertEqual(first_snapshot.positions["AAPL"].quantity, 0.3)
        self.assertEqual(first_snapshot.cash, 96.7)
        self.assertAlmostEqual(first_snapshot.fees, 0.3)

        ledger.require_reconciliation(unrelated.client_order_id)
        ledger.reconcile_order(
            OrderReconciliation(
                unrelated.client_order_id,
                OrderState.ACCEPTED,
                0.0,
            )
        )

        replayed = ledger.snapshot()
        self.assertEqual(replayed.fills, first_snapshot.fills)
        self.assertEqual(
            replayed.effective_fill_quantities,
            first_snapshot.effective_fill_quantities,
        )
        self.assertEqual(replayed.positions, first_snapshot.positions)
        self.assertEqual(replayed.cash, first_snapshot.cash)
        self.assertEqual(replayed.fees, first_snapshot.fees)

    def test_reconciliation_rejects_stale_revision_or_observation(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        intent = order(client_order_id="versioned-reconciliation", quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.require_reconciliation(intent.client_order_id)
        first_observation = NOW + timedelta(seconds=1)
        ledger.reconcile_order(
            OrderReconciliation(
                intent.client_order_id,
                OrderState.ACCEPTED,
                0.0,
                observed_at=first_observation,
                revision=2,
            )
        )
        accepted_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(accepted_detail.reconciliation_revision, 2)
        self.assertEqual(
            accepted_detail.reconciliation_observed_at,
            first_observation,
        )

        ledger.require_reconciliation(intent.client_order_id)
        stale_results = (
            OrderReconciliation(
                intent.client_order_id,
                OrderState.ACCEPTED,
                0.0,
                observed_at=first_observation,
                revision=1,
            ),
            OrderReconciliation(
                intent.client_order_id,
                OrderState.ACCEPTED,
                0.0,
                observed_at=NOW,
                revision=3,
            ),
        )
        for stale in stale_results:
            with self.subTest(stale=stale), self.assertRaises(ValueError):
                ledger.reconcile_order(stale)
            detail = ledger.snapshot().order_details[intent.client_order_id]
            self.assertEqual(detail.state, OrderState.RECONCILIATION_REQUIRED)
            self.assertEqual(detail.reconciliation_revision, 2)
            self.assertEqual(
                detail.reconciliation_observed_at,
                first_observation,
            )

        final_observation = NOW + timedelta(seconds=2)
        self.assertEqual(
            ledger.reconcile_order(
                OrderReconciliation(
                    intent.client_order_id,
                    OrderState.CANCELED,
                    0.0,
                    observed_at=final_observation,
                    revision=3,
                )
            ),
            OrderState.CANCELED,
        )
        final_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(final_detail.reconciliation_revision, 3)
        self.assertEqual(
            final_detail.reconciliation_observed_at,
            final_observation,
        )

    def test_same_reconciliation_revision_only_allows_exact_replay(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        intent = order(client_order_id="same-revision", quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.require_reconciliation(intent.client_order_id)
        accepted = OrderReconciliation(
            intent.client_order_id,
            OrderState.ACCEPTED,
            0.0,
            observed_at=NOW,
            revision=5,
        )
        ledger.reconcile_order(accepted)
        first_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(first_detail.reconciliation_cursor, accepted)

        ledger.require_reconciliation(intent.client_order_id)
        conflicting = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.0,
            observed_at=NOW,
            revision=5,
        )
        with self.assertRaisesRegex(ValueError, "revision"):
            ledger.reconcile_order(conflicting)

        detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(detail.state, OrderState.RECONCILIATION_REQUIRED)
        self.assertEqual(detail.reconciliation_cursor, accepted)
        self.assertEqual(detail.reconciliation_revision, 5)
        self.assertEqual(ledger.snapshot().cash, 100.0)
        self.assertEqual(ledger.snapshot().fills, ())

    def test_reconciliation_cursor_rejects_broker_economics_regression(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        intent = order(client_order_id="cursor-economics", quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.require_reconciliation(intent.client_order_id)
        broker_fill = fill(
            fill_id="cursor-half-fill",
            client_order_id=intent.client_order_id,
            quantity=0.5,
            price=10.0,
            fee=0.25,
        )
        partial = OrderReconciliation(
            intent.client_order_id,
            OrderState.PARTIALLY_FILLED,
            0.5,
            (broker_fill,),
            observed_at=broker_fill.filled_at,
            revision=10,
        )
        ledger.observe_reconciliation(partial, terminal_required=True)
        ledger.observe_reconciliation(partial)

        regressive = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.0,
            observed_at=NOW + timedelta(seconds=2),
            revision=11,
        )
        with self.assertRaisesRegex(ValueError, "economics|fill|regress"):
            ledger.reconcile_order(regressive)

        blocked = ledger.snapshot()
        blocked_detail = blocked.order_details[intent.client_order_id]
        self.assertEqual(blocked_detail.state, OrderState.RECONCILIATION_REQUIRED)
        self.assertTrue(blocked_detail.terminal_required)
        self.assertEqual(blocked_detail.reconciliation_cursor, partial)
        self.assertEqual(blocked.cash, 100.0)
        self.assertEqual(blocked.fills, ())
        self.assertEqual(blocked.effective_fill_quantities, {})
        self.assertEqual(blocked.fees, 0.0)

        terminal = OrderReconciliation(
            intent.client_order_id,
            OrderState.CANCELED,
            0.5,
            (broker_fill,),
            observed_at=NOW + timedelta(seconds=3),
            revision=12,
        )
        self.assertEqual(
            ledger.reconcile_order(terminal),
            OrderState.CANCELED,
        )
        resolved = ledger.snapshot()
        resolved_detail = resolved.order_details[intent.client_order_id]
        self.assertEqual(resolved_detail.filled_quantity, 0.5)
        self.assertEqual(resolved_detail.reconciliation_cursor, terminal)
        self.assertEqual(resolved.fills, (broker_fill,))
        self.assertEqual(
            resolved.effective_fill_quantities,
            {broker_fill.fill_id: broker_fill.quantity},
        )
        self.assertEqual(resolved.fees, broker_fill.fee)
        self.assertEqual(resolved.cash, 94.75)

        ledger.require_reconciliation(intent.client_order_id)
        reopened_active = OrderReconciliation(
            intent.client_order_id,
            OrderState.PARTIALLY_FILLED,
            0.5,
            (broker_fill,),
            observed_at=NOW + timedelta(seconds=4),
            revision=13,
        )
        with self.assertRaisesRegex(ValueError, "terminal|state"):
            ledger.observe_reconciliation(reopened_active)
        reopened_detail = ledger.snapshot().order_details[intent.client_order_id]
        self.assertEqual(reopened_detail.reconciliation_cursor, terminal)

    def test_reconciliation_imports_historical_broker_fills_atomically(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        cases = (
            (OrderState.PARTIALLY_FILLED, 0.5),
            (OrderState.CANCELED, 0.5),
            (OrderState.FILLED, 1.0),
        )
        for state, quantity in cases:
            with self.subTest(state=state):
                ledger = PortfolioLedger(
                    initial_cash=100.0,
                    observed_at=NOW + timedelta(minutes=10),
                )
                intent = order(
                    client_order_id="broker-import-" + state.value,
                    quantity=1.0,
                    created_at=NOW + timedelta(minutes=10),
                )
                ledger.record_order(intent)
                ledger.accept_order(intent.client_order_id)
                ledger.require_reconciliation(intent.client_order_id)
                ledger.mark_to_market(
                    {}, observed_at=NOW + timedelta(minutes=20)
                )
                broker_fill = FillEvent(
                    "broker-import-fill-" + state.value,
                    intent.client_order_id,
                    "AAPL",
                    "buy",
                    quantity,
                    10.0,
                    1.0,
                    NOW + timedelta(minutes=10, seconds=1),
                )
                result = OrderReconciliation(
                    intent.client_order_id,
                    state,
                    quantity,
                    (broker_fill,),
                )

                self.assertEqual(ledger.reconcile_order(result), state)

                snapshot = ledger.snapshot()
                detail = snapshot.order_details[intent.client_order_id]
                self.assertEqual(detail.filled_quantity, quantity)
                self.assertEqual(detail.remaining_quantity, 1.0 - quantity)
                self.assertEqual(snapshot.cash, 100.0 - quantity * 10.0 - 1.0)
                self.assertEqual(snapshot.positions["AAPL"].quantity, quantity)
                self.assertEqual(snapshot.fees, 1.0)
                self.assertEqual(snapshot.fills, (broker_fill,))
                self.assertFalse(snapshot.requires_reconciliation)

    def test_reconciliation_rejects_missing_conflicting_or_invalid_new_fills_atomically(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = order(client_order_id="broker-subset", quantity=2.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        local_fill = fill(
            fill_id="known-broker-fill",
            client_order_id=intent.client_order_id,
            quantity=0.5,
            price=10.0,
            fee=0.0,
        )
        ledger.apply_fill(local_fill)
        ledger.require_reconciliation(intent.client_order_id)
        before = ledger.snapshot()
        missing_local = FillEvent(
            "other-broker-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            0.5,
            10.0,
            0.0,
            NOW + timedelta(seconds=1),
        )
        conflicting_local = FillEvent(
            local_fill.fill_id,
            intent.client_order_id,
            "AAPL",
            "buy",
            0.5,
            11.0,
            0.0,
            local_fill.filled_at,
        )
        for broker_fill in (missing_local, conflicting_local):
            result = OrderReconciliation(
                intent.client_order_id,
                OrderState.PARTIALLY_FILLED,
                0.5,
                (broker_fill,),
            )
            with self.subTest(fill=broker_fill), self.assertRaises(ValueError):
                ledger.reconcile_order(result)
            self.assertEqual(ledger.snapshot(), before)

        atomic = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        atomic_intent = order(client_order_id="atomic-broker-import", quantity=1.0)
        atomic.record_order(atomic_intent)
        atomic.accept_order(atomic_intent.client_order_id)
        atomic.require_reconciliation(atomic_intent.client_order_id)
        first = FillEvent(
            "atomic-first",
            atomic_intent.client_order_id,
            "AAPL",
            "buy",
            0.5,
            10.0,
            0.0,
            NOW + timedelta(seconds=1),
        )
        unaffordable_second = FillEvent(
            "atomic-second",
            atomic_intent.client_order_id,
            "AAPL",
            "buy",
            0.5,
            1_000.0,
            0.0,
            NOW + timedelta(seconds=2),
        )
        atomic_before = atomic.snapshot()
        unaffordable_result = OrderReconciliation(
            atomic_intent.client_order_id,
            OrderState.FILLED,
            1.0,
            (first, unaffordable_second),
        )
        with self.assertRaises(ValueError):
            atomic.reconcile_order(unaffordable_result)
        atomic_after = atomic.snapshot()
        self.assertEqual(atomic_after.cash, atomic_before.cash)
        self.assertEqual(atomic_after.positions, atomic_before.positions)
        self.assertEqual(atomic_after.fees, atomic_before.fees)
        self.assertEqual(atomic_after.realized_pnl, atomic_before.realized_pnl)
        self.assertEqual(atomic_after.fills, atomic_before.fills)
        self.assertEqual(atomic_after.orders, atomic_before.orders)
        self.assertEqual(
            atomic_after.order_details[atomic_intent.client_order_id].reconciliation_cursor,
            unaffordable_result,
        )

    def test_nextafter_underfill_remains_partial_with_one_economic_quantity(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=10.0, observed_at=NOW)
        intent = order(client_order_id="strict-underfill", quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        underfill = math.nextafter(1.0, 0.0)
        executed = fill(
            fill_id="strict-underfill-fill",
            client_order_id=intent.client_order_id,
            quantity=underfill,
            price=10.0,
            fee=0.0,
        )

        ledger.apply_fill(executed)

        snapshot = ledger.snapshot()
        detail = snapshot.order_details[intent.client_order_id]
        self.assertEqual(detail.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(detail.filled_quantity, underfill)
        self.assertEqual(detail.remaining_quantity, 1.0 - underfill)
        self.assertEqual(snapshot.positions["AAPL"].quantity, underfill)
        self.assertEqual(snapshot.cash, 10.0 - underfill * 10.0)
        self.assertGreater(snapshot.cash, 0.0)
        self.assertEqual(snapshot.fills, (executed,))

    def test_underfilled_close_retains_exact_residual_position(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        buy = order(client_order_id="strict-close-seed", quantity=1.0)
        ledger.record_order(buy)
        ledger.accept_order(buy.client_order_id)
        ledger.apply_fill(
            fill(
                fill_id="strict-close-seed-fill",
                client_order_id=buy.client_order_id,
                quantity=1.0,
                price=10.0,
                fee=0.0,
            )
        )
        sell = order(
            client_order_id="strict-close-sell",
            side="sell",
            quantity=1.0,
            created_at=NOW + timedelta(seconds=1),
        )
        ledger.record_order(sell)
        ledger.accept_order(sell.client_order_id)
        underfill = math.nextafter(1.0, 0.0)
        ledger.apply_fill(
            fill(
                fill_id="strict-close-sell-fill",
                client_order_id=sell.client_order_id,
                side="sell",
                quantity=underfill,
                price=10.0,
                fee=0.0,
                offset_seconds=2,
            )
        )

        snapshot = ledger.snapshot()
        residual = 1.0 - underfill
        self.assertEqual(snapshot.positions["AAPL"].quantity, residual)
        self.assertEqual(
            snapshot.order_details[sell.client_order_id].remaining_quantity,
            residual,
        )
        self.assertEqual(
            snapshot.order_details[sell.client_order_id].state,
            OrderState.PARTIALLY_FILLED,
        )

    def test_reconciliation_requires_matching_broker_state_and_filled_quantity(self):
        from quantbot.trading.domain import OrderReconciliation
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = order(client_order_id="resolve-me", quantity=10.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        local_fill = fill(
                fill_id="resolve-fill",
                client_order_id=intent.client_order_id,
                quantity=4.0,
                price=10.0,
                fee=0.0,
            )
        ledger.apply_fill(local_fill)
        ledger.require_reconciliation(intent.client_order_id)
        self.assertTrue(ledger.requires_reconciliation)

        invalid_results = (
            OrderReconciliation(
                intent.client_order_id,
                OrderState.FILLED,
                4.0,
                (local_fill,),
            ),
        )
        for result in invalid_results:
            with self.subTest(result=result), self.assertRaises(ValueError):
                ledger.reconcile_order(result)
            self.assertEqual(
                ledger.order_state(intent.client_order_id),
                OrderState.RECONCILIATION_REQUIRED,
            )

        self.assertEqual(
            ledger.reconcile_order(
                OrderReconciliation(
                    intent.client_order_id,
                    OrderState.PARTIALLY_FILLED,
                    4.0,
                    (local_fill,),
                )
            ),
            OrderState.PARTIALLY_FILLED,
        )
        self.assertFalse(ledger.requires_reconciliation)

    def test_any_ulp_overfill_is_rejected_and_fillable_caps_exact_remainder(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = order(client_order_id="ulp-overfill", quantity=0.3)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.apply_fill(
            fill(
                fill_id="ulp-first",
                client_order_id=intent.client_order_id,
                quantity=0.1,
                price=10.0,
                fee=0.0,
            )
        )
        exact_remaining = intent.quantity - 0.1
        over_quantity = exact_remaining
        for _ in range(4):
            over_quantity = math.nextafter(over_quantity, math.inf)
        overfill = fill(
            fill_id="ulp-over",
            client_order_id=intent.client_order_id,
            quantity=over_quantity,
            price=10.0,
            fee=0.0,
            offset_seconds=2,
        )
        before = ledger.snapshot()

        self.assertEqual(ledger.fillable_quantity(overfill), exact_remaining)
        with self.assertRaises(ValueError):
            ledger.apply_fill(overfill)
        self.assertEqual(ledger.snapshot(), before)

    def test_allow_short_requires_a_real_bool(self):
        from quantbot.trading.ledger import PortfolioLedger

        PortfolioLedger(1_000.0, observed_at=NOW, allow_short=True)
        PortfolioLedger(1_000.0, observed_at=NOW, allow_short=False)
        for invalid in (1, 0, "true", "false", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                PortfolioLedger(
                    1_000.0,
                    observed_at=NOW,
                    allow_short=invalid,
                )

    def test_tiny_quantity_can_open_partially_fill_and_close_consistently(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        tiny_buy = OrderIntent("tiny-buy", "AAPL", "buy", 3e-12, NOW)
        ledger.record_order(tiny_buy)
        ledger.accept_order(tiny_buy.client_order_id)
        ledger.apply_fill(
            FillEvent(
                "tiny-buy-1",
                tiny_buy.client_order_id,
                "AAPL",
                "buy",
                1e-12,
                100.0,
                0.0,
                NOW + timedelta(seconds=1),
            )
        )

        partial = ledger.snapshot()
        self.assertEqual(partial.orders[tiny_buy.client_order_id], OrderState.PARTIALLY_FILLED)
        self.assertEqual(partial.positions["AAPL"].quantity, 1e-12)
        exact_remaining = ledger.remaining_order_quantity(tiny_buy.client_order_id)
        ledger.apply_fill(
            FillEvent(
                "tiny-buy-2",
                tiny_buy.client_order_id,
                "AAPL",
                "buy",
                exact_remaining,
                100.0,
                0.0,
                NOW + timedelta(seconds=2),
            )
        )
        self.assertEqual(ledger.snapshot().positions["AAPL"].quantity, 3e-12)
        self.assertEqual(ledger.order_state(tiny_buy.client_order_id), OrderState.FILLED)

        tiny_sell = OrderIntent(
            "tiny-sell",
            "AAPL",
            "sell",
            3e-12,
            NOW + timedelta(seconds=2),
        )
        ledger.record_order(tiny_sell)
        ledger.accept_order(tiny_sell.client_order_id)
        ledger.apply_fill(
            FillEvent(
                "tiny-sell-fill",
                tiny_sell.client_order_id,
                "AAPL",
                "sell",
                3e-12,
                100.0,
                0.0,
                NOW + timedelta(seconds=3),
            )
        )

        closed = ledger.snapshot()
        self.assertEqual(closed.positions, {})
        self.assertEqual(closed.cash, 1_000.0)
        ledger.assert_invariants()

    def test_tiny_overfill_is_rejected_before_any_ledger_mutation(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = OrderIntent("tiny-overfill", "AAPL", "buy", 1e-12, NOW)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        before = ledger.snapshot()
        overfill = FillEvent(
            "tiny-overfill-fill",
            intent.client_order_id,
            "AAPL",
            "buy",
            1.1e-12,
            100.0,
            0.0,
            NOW + timedelta(seconds=1),
        )

        with self.assertRaises(ValueError):
            ledger.apply_fill(overfill)
        self.assertEqual(ledger.snapshot(), before)

    def test_snapshot_exposes_only_active_orders_with_remaining_quantity(self):
        try:
            from quantbot.trading.domain import OrderSnapshot
        except ImportError as error:
            self.fail(str(error))
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=10_000.0, observed_at=NOW)
        intent = order(quantity=100.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.apply_fill(fill(quantity=10.0, price=10.0, fee=0.0))

        snapshot = ledger.snapshot()
        working = snapshot.open_orders[intent.client_order_id]
        self.assertIsInstance(working, OrderSnapshot)
        self.assertEqual(working.intent, intent)
        self.assertEqual(working.state, OrderState.PARTIALLY_FILLED)
        self.assertEqual(working.filled_quantity, 10.0)
        self.assertEqual(working.remaining_quantity, 90.0)

        ledger.cancel_order(intent.client_order_id)
        self.assertEqual(ledger.snapshot().open_orders, {})

    def test_new_order_cannot_move_ledger_time_backward_but_exact_replay_is_idempotent(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        original = order(client_order_id="original", quantity=1.0)
        ledger.record_order(original)
        ledger.accept_order(original.client_order_id)
        ledger.mark_to_market({}, observed_at=NOW + timedelta(minutes=10))
        before = ledger.snapshot()

        self.assertEqual(ledger.record_order(original), OrderState.ACCEPTED)
        late_order = OrderIntent(
            "late-order",
            "AAPL",
            "buy",
            1.0,
            NOW + timedelta(minutes=5),
        )
        with self.assertRaises(ValueError):
            ledger.record_order(late_order)
        self.assertEqual(ledger.snapshot(), before)

    def test_late_fill_without_journal_fails_closed_without_financial_mutation(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = order(quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.mark_to_market({}, observed_at=NOW + timedelta(minutes=2))
        before = ledger.snapshot()
        late_fill = fill(
            quantity=1.0,
            price=100.0,
            fee=0.0,
            offset_seconds=60,
        )

        with self.assertRaises(ValueError):
            ledger.apply_fill(late_fill)
        after = ledger.snapshot()
        self.assertEqual(after.cash, before.cash)
        self.assertEqual(after.positions, before.positions)
        self.assertEqual(after.fees, before.fees)
        self.assertEqual(after.realized_pnl, before.realized_pnl)
        self.assertEqual(after.fills, before.fills)
        self.assertEqual(
            after.effective_fill_quantities,
            before.effective_fill_quantities,
        )
        self.assertEqual(after.marks, before.marks)
        self.assertEqual(
            after.orders[intent.client_order_id],
            OrderState.RECONCILIATION_REQUIRED,
        )
        self.assertTrue(after.requires_reconciliation)

    def test_new_fill_earlier_than_fill_journal_fails_closed(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        later_order = order(client_order_id="later-journal-order", quantity=1.0)
        earlier_order = order(client_order_id="earlier-journal-order", quantity=1.0)
        for intent in (later_order, earlier_order):
            ledger.record_order(intent)
            ledger.accept_order(intent.client_order_id)
        committed = fill(
            fill_id="later-journal-fill",
            client_order_id=later_order.client_order_id,
            quantity=1.0,
            price=10.0,
            fee=0.0,
            offset_seconds=2,
        )
        ledger.apply_fill(committed)

        with self.assertRaises(ValueError):
            ledger.apply_fill(
                fill(
                    fill_id="earlier-journal-fill",
                    client_order_id=earlier_order.client_order_id,
                    quantity=1.0,
                    price=10.0,
                    fee=0.0,
                    offset_seconds=1,
                )
            )

        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.cash, 990.0)
        self.assertEqual(snapshot.fills, (committed,))
        self.assertEqual(
            snapshot.effective_fill_quantities,
            {committed.fill_id: committed.quantity},
        )
        self.assertEqual(
            snapshot.orders[earlier_order.client_order_id],
            OrderState.RECONCILIATION_REQUIRED,
        )

    def test_exact_fill_replay_remains_idempotent_after_time_advances(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = order(quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        original_fill = fill(quantity=1.0, price=100.0, fee=0.0)
        ledger.apply_fill(original_fill)
        ledger.mark_to_market(
            {"AAPL": 110.0}, observed_at=NOW + timedelta(minutes=2)
        )
        before = ledger.snapshot()

        self.assertFalse(ledger.apply_fill(original_fill))
        self.assertEqual(ledger.snapshot(), before)

    def test_backdated_mark_to_market_is_rejected_atomically(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        intent = order(quantity=1.0)
        ledger.record_order(intent)
        ledger.accept_order(intent.client_order_id)
        ledger.apply_fill(
            fill(quantity=1.0, price=100.0, fee=0.0, offset_seconds=60)
        )
        ledger.mark_to_market(
            {"AAPL": 110.0}, observed_at=NOW + timedelta(minutes=3)
        )
        before = ledger.snapshot()

        with self.assertRaises(ValueError):
            ledger.mark_to_market(
                {"AAPL": 90.0}, observed_at=NOW + timedelta(minutes=2)
            )
        self.assertEqual(ledger.snapshot(), before)

    def test_partial_fills_and_order_and_fill_ids_are_idempotent(self):
        try:
            from quantbot.trading.ledger import PortfolioLedger
        except ImportError as error:
            self.fail(str(error))

        ledger = PortfolioLedger(initial_cash=2_000.0, observed_at=NOW)
        intent = order()
        self.assertEqual(ledger.record_order(intent), OrderState.CREATED)
        self.assertEqual(ledger.record_order(intent), OrderState.CREATED)
        self.assertEqual(ledger.accept_order(intent.client_order_id), OrderState.ACCEPTED)

        first = fill()
        self.assertTrue(ledger.apply_fill(first))
        self.assertFalse(ledger.apply_fill(first))
        partial = ledger.snapshot()
        self.assertEqual(partial.orders[intent.client_order_id], OrderState.PARTIALLY_FILLED)
        self.assertEqual(partial.positions["AAPL"].quantity, 4.0)
        self.assertEqual(partial.positions["AAPL"].average_cost, 100.0)
        self.assertEqual(partial.cash, 1_599.0)
        self.assertEqual(len(partial.fills), 1)

        second = fill(
            fill_id="fill-2",
            quantity=6.0,
            price=102.0,
            offset_seconds=2,
        )
        self.assertTrue(ledger.apply_fill(second))
        completed = ledger.snapshot()
        self.assertEqual(completed.orders[intent.client_order_id], OrderState.FILLED)
        self.assertEqual(completed.positions["AAPL"].quantity, 10.0)
        self.assertAlmostEqual(completed.positions["AAPL"].average_cost, 101.2)
        self.assertEqual(completed.cash, 986.0)
        self.assertEqual(completed.fees, 2.0)

        with self.assertRaises(ValueError):
            ledger.record_order(order(quantity=11.0))
        with self.assertRaises(ValueError):
            ledger.apply_fill(fill(fill_id="fill-1", price=99.0))

    def test_illegal_transitions_overfills_cancel_and_reject_are_enforced(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        created = order(client_order_id="created", quantity=1.0)
        ledger.record_order(created)
        with self.assertRaises(ValueError):
            ledger.transition_order("created", OrderState.FILLED)
        self.assertEqual(ledger.cancel_order("created"), OrderState.CANCELED)

        canceled = order(client_order_id="canceled", quantity=1.0)
        ledger.record_order(canceled)
        ledger.accept_order("canceled")
        self.assertEqual(ledger.cancel_order("canceled"), OrderState.CANCELED)
        with self.assertRaises(ValueError):
            ledger.apply_fill(
                fill(fill_id="canceled-fill", client_order_id="canceled", quantity=1.0)
            )

        rejected = order(client_order_id="rejected", quantity=1.0)
        ledger.record_order(rejected)
        ledger.accept_order("rejected")
        self.assertEqual(ledger.reject_order("rejected"), OrderState.REJECTED)

        overfilled = order(client_order_id="overfilled", quantity=1.0)
        ledger.record_order(overfilled)
        ledger.accept_order("overfilled")
        with self.assertRaises(ValueError):
            ledger.apply_fill(
                fill(
                    fill_id="overfill",
                    client_order_id="overfilled",
                    quantity=1.01,
                )
            )
        self.assertEqual(ledger.order_state("overfilled"), OrderState.ACCEPTED)

    def test_cash_and_naked_short_guards_reject_unfunded_fills_atomically(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=100.0, observed_at=NOW)
        too_large = order(client_order_id="too-large", quantity=2.0)
        ledger.record_order(too_large)
        ledger.accept_order("too-large")
        with self.assertRaisesRegex(ValueError, "insufficient cash"):
            ledger.apply_fill(
                fill(
                    fill_id="unfunded",
                    client_order_id="too-large",
                    quantity=2.0,
                    price=60.0,
                    fee=0.0,
                )
            )

        short = order(
            client_order_id="naked-short",
            side="sell",
            quantity=1.0,
        )
        ledger.record_order(short)
        ledger.accept_order("naked-short")
        with self.assertRaisesRegex(ValueError, "naked short"):
            ledger.apply_fill(
                fill(
                    fill_id="short-fill",
                    client_order_id="naked-short",
                    side="sell",
                    quantity=1.0,
                    price=60.0,
                    fee=0.0,
                )
            )

        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.cash, 100.0)
        self.assertEqual(snapshot.positions, {})
        self.assertEqual(snapshot.fills, ())

    def test_mark_reduction_and_close_preserve_cash_position_and_pnl_identity(self):
        from quantbot.trading.ledger import PortfolioLedger

        ledger = PortfolioLedger(initial_cash=1_000.0, observed_at=NOW)
        buy = order(client_order_id="buy", quantity=10.0)
        ledger.record_order(buy)
        ledger.accept_order("buy")
        ledger.apply_fill(
            fill(
                fill_id="buy-fill",
                client_order_id="buy",
                quantity=10.0,
                price=50.0,
                fee=1.0,
            )
        )
        marked = ledger.mark_to_market(
            {"AAPL": 55.0}, observed_at=NOW + timedelta(minutes=1)
        )
        self.assertEqual(marked.unrealized_pnl, 50.0)
        self.assertEqual(marked.realized_pnl, 0.0)
        self.assertAlmostEqual(
            marked.equity - ledger.initial_cash,
            marked.realized_pnl + marked.unrealized_pnl - marked.fees,
        )

        reduce = order(
            client_order_id="reduce",
            side="sell",
            quantity=4.0,
            created_at=NOW + timedelta(minutes=1),
        )
        ledger.record_order(reduce)
        ledger.accept_order("reduce")
        ledger.apply_fill(
            fill(
                fill_id="reduce-fill",
                client_order_id="reduce",
                side="sell",
                quantity=4.0,
                price=60.0,
                fee=1.0,
                offset_seconds=61,
            )
        )
        reduced = ledger.mark_to_market(
            {"AAPL": 58.0}, observed_at=NOW + timedelta(minutes=2)
        )
        self.assertEqual(reduced.positions["AAPL"].quantity, 6.0)
        self.assertEqual(reduced.positions["AAPL"].average_cost, 50.0)
        self.assertEqual(reduced.realized_pnl, 40.0)
        self.assertEqual(reduced.unrealized_pnl, 48.0)
        self.assertEqual(reduced.fees, 2.0)

        close = order(
            client_order_id="close",
            side="sell",
            quantity=6.0,
            created_at=NOW + timedelta(minutes=2),
        )
        ledger.record_order(close)
        ledger.accept_order("close")
        ledger.apply_fill(
            fill(
                fill_id="close-fill",
                client_order_id="close",
                side="sell",
                quantity=6.0,
                price=55.0,
                fee=1.0,
                offset_seconds=121,
            )
        )
        closed = ledger.snapshot()
        self.assertEqual(closed.positions, {})
        self.assertEqual(closed.cash, 1_067.0)
        self.assertEqual(closed.realized_pnl, 70.0)
        self.assertEqual(closed.unrealized_pnl, 0.0)
        self.assertEqual(closed.fees, 3.0)
        self.assertAlmostEqual(
            closed.equity - ledger.initial_cash,
            closed.realized_pnl + closed.unrealized_pnl - closed.fees,
        )
        ledger.assert_invariants()


if __name__ == "__main__":
    unittest.main()
