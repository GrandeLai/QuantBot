import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class TradingDomainTest(unittest.TestCase):
    def test_terminal_order_details_and_all_fills_are_cross_validated(self):
        from quantbot.trading.domain import (
            FillEvent,
            OrderIntent,
            OrderSnapshot,
            OrderState,
            PortfolioSnapshot,
        )

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)

        def intent(order_id, quantity=10.0, symbol="AAPL", side="buy"):
            return OrderIntent(
                order_id,
                symbol,
                side,
                quantity,
                observed_at - timedelta(minutes=1),
            )

        filled_intent = intent("detail-filled", quantity=4.0)
        canceled_intent = intent("detail-canceled")
        rejected_intent = intent("detail-rejected", quantity=2.0)
        recon_intent = intent("detail-reconciliation", quantity=5.0)
        details = {
            filled_intent.client_order_id: OrderSnapshot(
                filled_intent, OrderState.FILLED, 4.0, 0.0
            ),
            canceled_intent.client_order_id: OrderSnapshot(
                canceled_intent, OrderState.CANCELED, 3.0, 7.0
            ),
            rejected_intent.client_order_id: OrderSnapshot(
                rejected_intent, OrderState.REJECTED, 0.0, 2.0
            ),
            recon_intent.client_order_id: OrderSnapshot(
                recon_intent,
                OrderState.RECONCILIATION_REQUIRED,
                2.0,
                3.0,
            ),
        }
        orders = {order_id: detail.state for order_id, detail in details.items()}
        fills = (
            FillEvent("detail-filled-fill", "detail-filled", "AAPL", "buy", 4.0, 10.0, 0.0, observed_at),
            FillEvent("detail-canceled-fill", "detail-canceled", "AAPL", "buy", 3.0, 10.0, 0.0, observed_at),
            FillEvent("detail-recon-fill", "detail-reconciliation", "AAPL", "buy", 2.0, 10.0, 0.0, observed_at),
        )

        valid = PortfolioSnapshot(
            observed_at,
            1_000.0,
            orders=orders,
            order_details=details,
            fills=fills,
        )
        self.assertEqual(set(valid.orders), set(valid.order_details))
        self.assertTrue(valid.requires_reconciliation)
        self.assertEqual(
            tuple(valid.reconciliation_orders),
            (recon_intent.client_order_id,),
        )
        with self.assertRaises(TypeError):
            valid.order_details["new"] = details[filled_intent.client_order_id]

        invalid_snapshots = (
            {
                "orders": {filled_intent.client_order_id: OrderState.FILLED},
                "order_details": {},
                "fills": (),
            },
            {
                "orders": {filled_intent.client_order_id: OrderState.FILLED},
                "order_details": {
                    filled_intent.client_order_id: details[filled_intent.client_order_id]
                },
                "fills": (
                    FillEvent("wrong-filled-total", "detail-filled", "AAPL", "buy", 3.0, 10.0, 0.0, observed_at),
                ),
            },
            {
                "orders": {canceled_intent.client_order_id: OrderState.CANCELED},
                "order_details": {
                    canceled_intent.client_order_id: details[canceled_intent.client_order_id]
                },
                "fills": (
                    FillEvent("wrong-canceled-total", "detail-canceled", "AAPL", "buy", 2.0, 10.0, 0.0, observed_at),
                ),
            },
            {
                "orders": {rejected_intent.client_order_id: OrderState.REJECTED},
                "order_details": {
                    rejected_intent.client_order_id: details[rejected_intent.client_order_id]
                },
                "fills": (
                    FillEvent("rejected-has-fill", "detail-rejected", "AAPL", "buy", 1.0, 10.0, 0.0, observed_at),
                ),
            },
            {
                "orders": {filled_intent.client_order_id: OrderState.FILLED},
                "order_details": {
                    filled_intent.client_order_id: details[filled_intent.client_order_id]
                },
                "fills": (
                    FillEvent("wrong-terminal-side", "detail-filled", "AAPL", "sell", 4.0, 10.0, 0.0, observed_at),
                ),
            },
        )
        for kwargs in invalid_snapshots:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PortfolioSnapshot(observed_at, 1_000.0, **kwargs)

        active_intent = intent("active-detail", quantity=1.0)
        active_detail = OrderSnapshot(
            active_intent, OrderState.ACCEPTED, 0.0, 1.0
        )
        mismatching_open_intent = intent(
            "active-detail", quantity=1.0, symbol="MSFT"
        )
        with self.assertRaises(ValueError):
            PortfolioSnapshot(
                observed_at,
                1_000.0,
                orders={active_intent.client_order_id: OrderState.ACCEPTED},
                order_details={active_intent.client_order_id: active_detail},
                open_orders={
                    active_intent.client_order_id: OrderSnapshot(
                        mismatching_open_intent,
                        OrderState.ACCEPTED,
                        0.0,
                        1.0,
                    )
                },
            )

    def test_public_reconciliation_result_contract_is_explicit(self):
        from quantbot import trading

        self.assertTrue(hasattr(trading.OrderState, "RECONCILIATION_REQUIRED"))
        self.assertTrue(hasattr(trading, "OrderReconciliation"))
        result = trading.OrderReconciliation(
            "broker-order",
            trading.OrderState.ACCEPTED,
            0.0,
        )
        self.assertEqual(result.client_order_id, "broker-order")
        self.assertEqual(result.state, trading.OrderState.ACCEPTED)
        self.assertEqual(result.filled_quantity, 0.0)
        self.assertTrue(hasattr(result, "fills"))
        self.assertEqual(result.fills, ())
        self.assertTrue(hasattr(result, "observed_at"))
        self.assertTrue(hasattr(result, "revision"))
        self.assertIsNone(result.observed_at)
        self.assertIsNone(result.revision)
        with self.assertRaises(ValueError):
            trading.OrderReconciliation("broker-order", "accepted", 0.0)

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        first = trading.FillEvent(
            "broker-fill-1",
            "broker-order",
            "AAPL",
            "buy",
            0.25,
            100.0,
            0.1,
            observed_at,
        )
        second = trading.FillEvent(
            "broker-fill-2",
            "broker-order",
            "AAPL",
            "buy",
            0.25,
            101.0,
            0.1,
            observed_at + timedelta(seconds=1),
        )
        partial = trading.OrderReconciliation(
            "broker-order",
            trading.OrderState.PARTIALLY_FILLED,
            0.5,
            (first, second),
            observed_at=second.filled_at,
            revision=3,
        )
        self.assertEqual(partial.fills, (first, second))
        self.assertEqual(partial.observed_at, second.filled_at)
        self.assertEqual(partial.revision, 3)

        for kwargs in (
            {"observed_at": datetime(2025, 1, 2, 14, 30)},
            {"revision": -1},
            {"revision": True},
            {"revision": 1.0},
            {"observed_at": first.filled_at, "revision": 4},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                trading.OrderReconciliation(
                    "broker-order",
                    trading.OrderState.PARTIALLY_FILLED,
                    0.5,
                    (first, second),
                    **kwargs,
                )

        invalid_results = (
            ("broker-order", trading.OrderState.PARTIALLY_FILLED, 0.5, (first, first)),
            ("broker-order", trading.OrderState.PARTIALLY_FILLED, 0.5, (first,)),
            ("broker-order", trading.OrderState.PARTIALLY_FILLED, 0.25, (second, first)),
            ("broker-order", trading.OrderState.ACCEPTED, 0.25, (first,)),
            ("broker-order", trading.OrderState.PARTIALLY_FILLED, 0.0, ()),
        )
        for args in invalid_results:
            with self.subTest(args=args), self.assertRaises(ValueError):
                trading.OrderReconciliation(*args)

    def test_order_snapshot_uses_ulp_conservation_and_state_semantics(self):
        from quantbot.trading.domain import OrderIntent, OrderSnapshot, OrderState

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        decimal_intent = OrderIntent("decimal", "AAPL", "buy", 0.3, observed_at)
        decimal_snapshot = OrderSnapshot(
            decimal_intent,
            OrderState.PARTIALLY_FILLED,
            0.1,
            0.2,
        )
        self.assertEqual(decimal_snapshot.remaining_quantity, 0.2)

        large_intent = OrderIntent("large", "AAPL", "buy", 1e12, observed_at)
        with self.assertRaises(ValueError):
            OrderSnapshot(
                large_intent,
                OrderState.PARTIALLY_FILLED,
                1.0,
                large_intent.quantity - 1.0 + 0.125,
            )

        intent = OrderIntent("semantic", "AAPL", "buy", 10.0, observed_at)
        invalid_states = (
            (OrderState.CREATED, 1.0, 9.0),
            (OrderState.ACCEPTED, 1.0, 9.0),
            (OrderState.PARTIALLY_FILLED, 0.0, 10.0),
            (OrderState.FILLED, 9.0, 1.0),
            (OrderState.CANCELED, 10.0, 0.0),
            (OrderState.REJECTED, 1.0, 9.0),
        )
        for state, filled, remaining in invalid_states:
            with self.subTest(state=state), self.assertRaises(ValueError):
                OrderSnapshot(intent, state, filled, remaining)

        self.assertEqual(
            OrderSnapshot(intent, OrderState.CANCELED, 4.0, 6.0).state,
            OrderState.CANCELED,
        )

    def test_portfolio_snapshot_enforces_order_fill_and_time_consistency(self):
        from quantbot.trading.domain import (
            FillEvent,
            OrderIntent,
            OrderSnapshot,
            OrderState,
            PortfolioSnapshot,
        )

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        intent = OrderIntent(
            "working",
            "AAPL",
            "buy",
            10.0,
            observed_at - timedelta(minutes=1),
        )
        fill = FillEvent(
            "working-fill",
            intent.client_order_id,
            intent.symbol,
            intent.side,
            4.0,
            100.0,
            0.0,
            observed_at,
        )
        working = OrderSnapshot(
            intent,
            OrderState.PARTIALLY_FILLED,
            4.0,
            6.0,
        )
        valid = PortfolioSnapshot(
            observed_at,
            1_000.0,
            orders={intent.client_order_id: OrderState.PARTIALLY_FILLED},
            open_orders={intent.client_order_id: working},
            order_details={intent.client_order_id: working},
            fills=(fill,),
        )
        self.assertEqual(valid.open_orders[intent.client_order_id], working)

        invalid_kwargs = (
            {
                "orders": {intent.client_order_id: OrderState.PARTIALLY_FILLED},
                "open_orders": {},
                "order_details": {intent.client_order_id: working},
                "fills": (fill,),
            },
            {
                "orders": {intent.client_order_id: OrderState.ACCEPTED},
                "open_orders": {intent.client_order_id: working},
                "order_details": {intent.client_order_id: working},
                "fills": (fill,),
            },
            {
                "orders": {intent.client_order_id: OrderState.PARTIALLY_FILLED},
                "open_orders": {intent.client_order_id: working},
                "order_details": {intent.client_order_id: working},
                "fills": (
                    FillEvent(
                        "future-fill",
                        intent.client_order_id,
                        "AAPL",
                        "buy",
                        4.0,
                        100.0,
                        0.0,
                        observed_at + timedelta(seconds=1),
                    ),
                ),
            },
            {
                "orders": {intent.client_order_id: OrderState.PARTIALLY_FILLED},
                "open_orders": {intent.client_order_id: working},
                "order_details": {intent.client_order_id: working},
                "fills": (
                    FillEvent(
                        "orphan-fill",
                        "unknown-order",
                        "AAPL",
                        "buy",
                        4.0,
                        100.0,
                        0.0,
                        observed_at,
                    ),
                ),
            },
            {
                "orders": {intent.client_order_id: OrderState.PARTIALLY_FILLED},
                "open_orders": {intent.client_order_id: working},
                "order_details": {intent.client_order_id: working},
                "fills": (
                    FillEvent(
                        "wrong-total",
                        intent.client_order_id,
                        "AAPL",
                        "buy",
                        3.0,
                        100.0,
                        0.0,
                        observed_at,
                    ),
                ),
            },
        )
        for kwargs in invalid_kwargs:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                PortfolioSnapshot(observed_at, 1_000.0, **kwargs)

        future_intent = OrderIntent(
            "future-order",
            "AAPL",
            "buy",
            1.0,
            observed_at + timedelta(seconds=1),
        )
        with self.assertRaises(ValueError):
            future_detail = OrderSnapshot(
                future_intent,
                OrderState.ACCEPTED,
                0.0,
                1.0,
            )
            PortfolioSnapshot(
                observed_at,
                1_000.0,
                orders={future_intent.client_order_id: OrderState.ACCEPTED},
                open_orders={
                    future_intent.client_order_id: future_detail
                },
                order_details={future_intent.client_order_id: future_detail},
            )

    def test_trading_domain_module_exists(self):
        domain_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "quantbot"
            / "trading"
            / "domain.py"
        )

        self.assertTrue(domain_path.is_file())

    def test_trading_package_exports_shared_public_contract(self):
        from quantbot import trading

        public_names = (
            "PointInTimeSnapshot",
            "Bar",
            "Quote",
            "SignalFrame",
            "TargetPortfolio",
            "OrderIntent",
            "OrderReconciliation",
            "OrderSnapshot",
            "OrderState",
            "FillEvent",
            "MarkSnapshot",
            "PositionSnapshot",
            "PortfolioSnapshot",
            "RunManifest",
            "BacktestReport",
            "Clock",
            "MarketDataPort",
            "ExecutionPort",
            "DailyStrategy",
            "IntradayStrategy",
            "OrderManagingStrategy",
            "PortfolioLedger",
            "EventDrivenTradingEngine",
            "BacktestExecutionPort",
            "RecordingExecutionPort",
            "TargetPortfolioAdapter",
        )

        missing = [name for name in public_names if not hasattr(trading, name)]
        self.assertEqual(missing, [])

    def test_bar_is_immutable_and_rejects_invalid_market_data(self):
        try:
            from quantbot.trading.domain import Bar
        except ImportError as error:
            self.fail(str(error))

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        bar = Bar(
            symbol="AAPL",
            timestamp=observed_at,
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=1_000.0,
        )

        with self.assertRaises(FrozenInstanceError):
            bar.close = 101.0

        invalid_overrides = (
            {"timestamp": observed_at.replace(tzinfo=None)},
            {"open": float("nan")},
            {"close": 0.0},
            {"high": 101.0},
            {"low": 101.0},
            {"volume": -1.0},
        )
        base = {
            "symbol": "AAPL",
            "timestamp": observed_at,
            "open": 100.0,
            "high": 103.0,
            "low": 99.0,
            "close": 102.0,
            "volume": 1_000.0,
        }
        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ValueError):
                Bar(**dict(base, **override))

    def test_quote_requires_valid_spread_sizes_and_aware_time(self):
        try:
            from quantbot.trading.domain import Quote
        except ImportError as error:
            self.fail(str(error))

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        quote = Quote(
            symbol="AAPL",
            timestamp=observed_at,
            bid=100.0,
            ask=100.2,
            bid_size=10.0,
            ask_size=12.0,
        )

        self.assertAlmostEqual(quote.mid_price, 100.1)
        invalid_overrides = (
            {"timestamp": observed_at.replace(tzinfo=None)},
            {"bid": 0.0},
            {"ask": float("inf")},
            {"ask": 99.9},
            {"bid_size": -1.0},
            {"ask_size": float("nan")},
        )
        base = {
            "symbol": "AAPL",
            "timestamp": observed_at,
            "bid": 100.0,
            "ask": 100.2,
            "bid_size": 10.0,
            "ask_size": 12.0,
        }
        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ValueError):
                Quote(**dict(base, **override))

    def test_orders_and_fills_require_stable_ids_side_and_positive_quantity(self):
        try:
            from quantbot.trading.domain import FillEvent, OrderIntent, OrderState
        except ImportError as error:
            self.fail(str(error))

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        intent = OrderIntent(
            client_order_id="strategy-aapl-001",
            symbol="AAPL",
            side="buy",
            quantity=10.0,
            created_at=observed_at,
        )
        fill = FillEvent(
            fill_id="broker-fill-001",
            client_order_id=intent.client_order_id,
            symbol="AAPL",
            side="buy",
            quantity=4.0,
            price=101.0,
            fee=0.25,
            filled_at=observed_at,
        )

        self.assertEqual(intent.quantity, 10.0)
        self.assertEqual(fill.quantity, 4.0)
        self.assertEqual(OrderState.PARTIALLY_FILLED.value, "partially_filled")

        invalid_intents = (
            {"client_order_id": ""},
            {"side": "hold"},
            {"quantity": 0.0},
            {"quantity": -1.0},
            {"created_at": observed_at.replace(tzinfo=None)},
        )
        intent_base = {
            "client_order_id": "strategy-aapl-001",
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 10.0,
            "created_at": observed_at,
        }
        for override in invalid_intents:
            with self.subTest(intent=override), self.assertRaises(ValueError):
                OrderIntent(**dict(intent_base, **override))

        invalid_fills = (
            {"fill_id": ""},
            {"side": "hold"},
            {"quantity": 0.0},
            {"price": -1.0},
            {"fee": -0.01},
            {"filled_at": observed_at.replace(tzinfo=None)},
        )
        fill_base = {
            "fill_id": "broker-fill-001",
            "client_order_id": "strategy-aapl-001",
            "symbol": "AAPL",
            "side": "buy",
            "quantity": 4.0,
            "price": 101.0,
            "fee": 0.25,
            "filled_at": observed_at,
        }
        for override in invalid_fills:
            with self.subTest(fill=override), self.assertRaises(ValueError):
                FillEvent(**dict(fill_base, **override))

    def test_point_in_time_and_strategy_frames_are_deeply_immutable(self):
        try:
            from quantbot.trading.domain import (
                Bar,
                PointInTimeSnapshot,
                Quote,
                SignalFrame,
                TargetPortfolio,
            )
        except ImportError as error:
            self.fail(str(error))

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        bar = Bar("AAPL", observed_at, 100.0, 102.0, 99.0, 101.0, 1_000.0)
        quote = Quote("AAPL", observed_at, 100.8, 101.2, 10.0, 12.0)
        signal_values = {"AAPL": 0.75}
        target_weights = {"AAPL": 0.60, "SPY": -0.10}
        signals = SignalFrame(observed_at=observed_at, signals=signal_values)
        targets = TargetPortfolio(observed_at=observed_at, weights=target_weights)
        snapshot = PointInTimeSnapshot(
            observed_at=observed_at,
            bars={"AAPL": bar},
            quotes={"AAPL": quote},
            signals=signals,
        )

        signal_values["AAPL"] = -1.0
        target_weights["AAPL"] = 0.0
        self.assertEqual(signals.signals["AAPL"], 0.75)
        self.assertEqual(targets.weights["AAPL"], 0.60)
        self.assertAlmostEqual(snapshot.latest_price("AAPL"), 101.0)
        with self.assertRaises(TypeError):
            targets.weights["AAPL"] = 0.0
        with self.assertRaises(KeyError):
            snapshot.latest_price("MSFT")

        future = datetime(2025, 1, 2, 14, 31, tzinfo=timezone.utc)
        future_bar = Bar("AAPL", future, 100.0, 102.0, 99.0, 101.0, 1_000.0)
        with self.assertRaises(ValueError):
            PointInTimeSnapshot(observed_at=observed_at, bars={"AAPL": future_bar})
        with self.assertRaises(ValueError):
            SignalFrame(observed_at=observed_at, signals={"AAPL": float("nan")})
        with self.assertRaises(ValueError):
            TargetPortfolio(observed_at=observed_at, weights={"AAPL": float("inf")})

    def test_point_in_time_latest_price_uses_newer_event_and_quote_on_tie(self):
        from quantbot.trading.domain import Bar, PointInTimeSnapshot, Quote

        older = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        newer = datetime(2025, 1, 2, 14, 31, tzinfo=timezone.utc)
        old_quote = Quote("AAPL", older, 99.0, 101.0)
        new_quote = Quote("AAPL", newer, 119.0, 121.0)
        old_bar = Bar("AAPL", older, 108.0, 112.0, 107.0, 110.0, 100.0)
        new_bar = Bar("AAPL", newer, 108.0, 112.0, 107.0, 110.0, 100.0)

        newer_bar_snapshot = PointInTimeSnapshot(
            newer,
            bars={"AAPL": new_bar},
            quotes={"AAPL": old_quote},
        )
        newer_quote_snapshot = PointInTimeSnapshot(
            newer,
            bars={"AAPL": old_bar},
            quotes={"AAPL": new_quote},
        )
        tied_snapshot = PointInTimeSnapshot(
            newer,
            bars={"AAPL": new_bar},
            quotes={"AAPL": new_quote},
        )

        self.assertEqual(newer_bar_snapshot.latest_price("AAPL"), 110.0)
        self.assertEqual(newer_quote_snapshot.latest_price("AAPL"), 120.0)
        self.assertEqual(tied_snapshot.latest_price("AAPL"), 120.0)

    def test_portfolio_run_and_report_values_validate_financial_domains(self):
        try:
            from quantbot.trading.domain import (
                BacktestReport,
                FillEvent,
                OrderIntent,
                OrderSnapshot,
                OrderState,
                PortfolioSnapshot,
                PositionSnapshot,
                RunManifest,
            )
        except ImportError as error:
            self.fail(str(error))

        observed_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        position = PositionSnapshot(
            symbol="AAPL",
            quantity=5.0,
            average_cost=100.0,
            market_price=104.0,
            realized_pnl=2.0,
        )
        positions = {"AAPL": position}
        completed_intent = OrderIntent(
            "order-1", "AAPL", "buy", 1.0, observed_at
        )
        completed_detail = OrderSnapshot(
            completed_intent, OrderState.FILLED, 1.0, 0.0
        )
        completed_fill = FillEvent(
            "order-1-fill",
            "order-1",
            "AAPL",
            "buy",
            1.0,
            100.0,
            1.0,
            observed_at,
        )
        portfolio = PortfolioSnapshot(
            observed_at=observed_at,
            cash=500.0,
            positions=positions,
            orders={"order-1": OrderState.FILLED},
            order_details={"order-1": completed_detail},
            fills=(completed_fill,),
            fees=1.0,
            realized_pnl=2.0,
        )
        manifest = RunManifest(
            run_type="backtest",
            observed_at=observed_at,
            dataset_hash="dataset-sha256",
            config_hash="config-sha256",
            code_hash="code-sha256",
        )
        report = BacktestReport(
            manifest=manifest,
            started_at=observed_at,
            ended_at=observed_at,
            initial_equity=1_000.0,
            final_equity=1_100.0,
            fees=3.0,
            realized_pnl=80.0,
            unrealized_pnl=23.0,
            order_count=2,
            fill_count=3,
        )

        positions.clear()
        self.assertEqual(portfolio.positions["AAPL"], position)
        self.assertEqual(position.market_value, 520.0)
        self.assertEqual(position.unrealized_pnl, 20.0)
        self.assertEqual(portfolio.equity, 1_020.0)
        self.assertEqual(portfolio.unrealized_pnl, 20.0)
        self.assertAlmostEqual(report.total_return, 0.10)
        self.assertIsNone(manifest.policy_version)

        with self.assertRaises(ValueError):
            PositionSnapshot("AAPL", 0.0, 100.0, 104.0)
        with self.assertRaises(ValueError):
            PortfolioSnapshot(observed_at, -1.0)
        with self.assertRaises(ValueError):
            RunManifest(
                run_type="backtest",
                observed_at=observed_at.replace(tzinfo=None),
                dataset_hash="dataset",
                config_hash="config",
                code_hash="code",
            )
        with self.assertRaises(ValueError):
            BacktestReport(
                manifest=manifest,
                started_at=observed_at,
                ended_at=observed_at,
                initial_equity=0.0,
                final_equity=1.0,
            )

    def test_backtest_report_enforces_pnl_conservation_and_manifest_time_envelope(self):
        from quantbot.trading.domain import BacktestReport, RunManifest

        started_at = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)
        ended_at = datetime(2025, 1, 2, 15, 30, tzinfo=timezone.utc)
        valid_manifest = RunManifest(
            "backtest",
            ended_at,
            "dataset",
            "config",
            "code",
        )

        with self.assertRaises(ValueError):
            BacktestReport(
                manifest=valid_manifest,
                started_at=started_at,
                ended_at=ended_at,
                initial_equity=1_000.0,
                final_equity=1_100.0,
                fees=1.0,
                realized_pnl=80.0,
                unrealized_pnl=20.0,
            )

        stale_manifest = RunManifest(
            "backtest",
            started_at,
            "dataset",
            "config",
            "code",
        )
        with self.assertRaises(ValueError):
            BacktestReport(
                manifest=stale_manifest,
                started_at=started_at,
                ended_at=ended_at,
                initial_equity=1_000.0,
                final_equity=1_100.0,
                fees=0.0,
                realized_pnl=100.0,
                unrealized_pnl=0.0,
            )


if __name__ == "__main__":
    unittest.main()
