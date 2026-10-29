import ast
import inspect
import math
import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.daytrade import (
    DayTradeBacktester,
    DayTradeConfig,
    DayTradeRiskGate,
    MinuteBar,
    OpeningRangeBreakoutStrategy,
)
from quantbot.execution import (
    AccountAwareBrokerAdapter,
    AlpacaBrokerAdapter,
    AutoExecutor,
    BrokerFailoverExecutor,
    BrokerSubmissionUncertainError,
    IBKRBrokerAdapter,
)
from quantbot.integrations.alpaca import AlpacaTradingClient
from quantbot.risk_controls import BrokerDayTradeProtector, PDTProtector
from quantbot.risk import RiskConfig
from quantbot.trading import (
    AccountStatePort,
    BrokerAccountRiskGate,
    BrokerAccountSnapshot,
    MarginStatus,
    StaticAccountStatePort,
)


OBSERVED_AT = datetime(2025, 1, 2, 14, 30, tzinfo=timezone.utc)


def account(
    *,
    broker="test",
    equity=1_000.0,
    buying_power=1_000.0,
    day_trade_buying_power=1_000.0,
    trading_blocked=False,
    shorting_enabled=True,
    margin_status=MarginStatus.MARGIN,
    restrictions=(),
):
    return BrokerAccountSnapshot(
        broker=broker,
        observed_at=OBSERVED_AT,
        equity=equity,
        buying_power=buying_power,
        day_trade_buying_power=day_trade_buying_power,
        trading_blocked=trading_blocked,
        shorting_enabled=shorting_enabled,
        margin_status=margin_status,
        restrictions=restrictions,
    )


class BrokerAccountDomainTest(unittest.TestCase):
    def test_snapshot_is_strict_immutable_and_protocol_has_static_adapter(self):
        snapshot = account(restrictions=["close_only"])
        port = StaticAccountStatePort(snapshot)
        without_daytrade_power = BrokerAccountSnapshot(
            broker="cash",
            observed_at=OBSERVED_AT,
            equity=1_000.0,
            buying_power=500.0,
            trading_blocked=False,
            shorting_enabled=False,
            margin_status=MarginStatus.CASH,
        )

        self.assertIs(port.account_snapshot(), snapshot)
        self.assertIsInstance(port, AccountStatePort)
        self.assertEqual(snapshot.restrictions, ("close_only",))
        self.assertIsNone(without_daytrade_power.day_trade_buying_power)
        with self.assertRaises(FrozenInstanceError):
            snapshot.equity = 2_000.0

    def test_snapshot_rejects_naive_time_bad_numbers_and_string_booleans(self):
        base = dict(
            broker="test",
            observed_at=OBSERVED_AT,
            equity=1_000.0,
            buying_power=1_000.0,
            day_trade_buying_power=1_000.0,
            trading_blocked=False,
            shorting_enabled=True,
            margin_status=MarginStatus.MARGIN,
        )
        invalid = (
            {"observed_at": OBSERVED_AT.replace(tzinfo=None)},
            {"equity": math.nan},
            {"buying_power": -1.0},
            {"day_trade_buying_power": math.inf},
            {"trading_blocked": "false"},
            {"shorting_enabled": "true"},
            {"margin_status": "margin"},
        )
        for override in invalid:
            with self.subTest(override=override), self.assertRaises(ValueError):
                BrokerAccountSnapshot(**dict(base, **override))


class BrokerAccountRiskGateTest(unittest.TestCase):
    def test_small_account_is_allowed_from_broker_facts_and_large_blocked_is_denied(self):
        gate = BrokerAccountRiskGate()

        allowed = gate.evaluate(account(equity=100.0), 50.0, side="buy", day_trade=True)
        blocked = gate.evaluate(
            account(equity=1_000_000.0, trading_blocked=True),
            50.0,
            side="buy",
            day_trade=True,
        )

        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.reasons, ())
        self.assertFalse(blocked.allowed)
        self.assertIn("account_trading_blocked", blocked.reasons)

    def test_missing_daytrade_power_unknown_margin_and_restrictions_fail_closed(self):
        gate = BrokerAccountRiskGate()

        missing = gate.evaluate(
            account(day_trade_buying_power=None), 50.0, side="buy", day_trade=True
        )
        unknown = gate.evaluate(
            account(margin_status=MarginStatus.UNKNOWN), 50.0, side="buy"
        )
        restricted = gate.evaluate(
            account(restrictions=("close_only",)), 50.0, side="buy"
        )

        self.assertIn("day_trade_buying_power_unavailable", missing.reasons)
        self.assertIn("margin_status_unknown", unknown.reasons)
        self.assertIn("account_restricted", restricted.reasons)

    def test_buying_power_and_shorting_are_broker_fact_checks(self):
        gate = BrokerAccountRiskGate()

        ordinary = gate.evaluate(account(buying_power=49.0), 50.0, side="buy")
        daytrade = gate.evaluate(
            account(day_trade_buying_power=49.0), 50.0, side="buy", day_trade=True
        )
        short = gate.evaluate(
            account(shorting_enabled=False), 50.0, side="short", day_trade=True
        )

        self.assertIn("insufficient_buying_power", ordinary.reasons)
        self.assertIn("insufficient_day_trade_buying_power", daytrade.reasons)
        self.assertIn("shorting_disabled", short.reasons)

    def test_daytrade_opening_requires_both_ordinary_and_daytrade_power(self):
        ordinary_missing = BrokerAccountRiskGate().evaluate(
            account(buying_power=0.0, day_trade_buying_power=100.0),
            50.0,
            side="buy",
            day_trade=True,
        )
        both_missing = BrokerAccountRiskGate().evaluate(
            account(buying_power=0.0, day_trade_buying_power=0.0),
            50.0,
            side="buy",
            day_trade=True,
        )

        self.assertEqual(ordinary_missing.reasons, ("insufficient_buying_power",))
        self.assertEqual(
            both_missing.reasons,
            (
                "insufficient_buying_power",
                "insufficient_day_trade_buying_power",
            ),
        )


class DayTradeBrokerRiskTest(unittest.TestCase):
    def test_config_and_gate_have_no_local_pdt_history_or_threshold(self):
        signature = inspect.signature(DayTradeConfig)
        self.assertNotIn("pdt_threshold", signature.parameters)
        self.assertNotIn("max_day_trades_5d", signature.parameters)
        self.assertNotIn("prior_day_trade_dates", inspect.signature(DayTradeRiskGate).parameters)

    def test_gate_combines_local_limits_with_current_broker_snapshot(self):
        config = DayTradeConfig(max_trades_per_day=2, max_concurrent_positions=1)
        gate = DayTradeRiskGate(config)

        allowed, reasons = gate.can_open_new_trade(
            day=OBSERVED_AT.date(),
            trades_started_today=0,
            open_positions=0,
            realized_pnl=0.0,
            broker_account=account(equity=100.0),
            requested_notional=50.0,
        )
        blocked, blocked_reasons = gate.can_open_new_trade(
            day=OBSERVED_AT.date(),
            trades_started_today=2,
            open_positions=0,
            realized_pnl=0.0,
            broker_account=account(equity=1_000_000.0, trading_blocked=True),
            requested_notional=50.0,
        )

        self.assertTrue(allowed, reasons)
        self.assertFalse(blocked)
        self.assertIn("max_trades_per_day", blocked_reasons)
        self.assertIn("account_trading_blocked", blocked_reasons)

    def test_backtester_generates_signal_before_checking_its_notional(self):
        sequence = []
        original_signal = OpeningRangeBreakoutStrategy.entry_signal

        def signal_spy(strategy, bar, opening_range):
            signal = original_signal(strategy, bar, opening_range)
            if signal is not None:
                sequence.append(("signal", signal.notional))
            return signal

        def gate_spy(
            gate,
            day,
            trades_started_today,
            open_positions,
            realized_pnl,
            broker_account,
            requested_notional,
        ):
            del gate, day, trades_started_today, open_positions, realized_pnl
            sequence.append(("gate", requested_notional, broker_account.margin_status))
            return True, []

        with mock.patch.object(OpeningRangeBreakoutStrategy, "entry_signal", signal_spy), mock.patch.object(
            DayTradeRiskGate, "can_open_new_trade", gate_spy
        ):
            result = DayTradeBacktester(
                DayTradeConfig(account_equity=1_000.0, capital_fraction=0.1)
            ).run(_daytrade_bars())

        self.assertGreaterEqual(result.trade_count, 1)
        self.assertEqual(sequence[0][0], "signal")
        self.assertEqual(sequence[1][0], "gate")
        self.assertEqual(sequence[1][1], sequence[0][1])
        self.assertEqual(sequence[1][2], MarginStatus.SIMULATED)

    def test_small_account_backtest_uses_deterministic_simulated_broker_facts(self):
        config = DayTradeConfig(account_equity=1_000.0, capital_fraction=0.1)

        first = DayTradeBacktester(config).run(_daytrade_bars())
        second = DayTradeBacktester(config).run(_daytrade_bars())

        self.assertEqual(first, second)
        self.assertEqual(first.trade_count, 1)


class AutoExecutorAccountRiskTest(unittest.TestCase):
    def test_prepare_is_side_effect_free_and_plan_is_immutable(self):
        broker = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))
        executor = AutoExecutor(broker)

        plan = executor.prepare_rebalance(
            target_weights={"AAPL": 0.6, "MSFT": 0.4},
            current_holdings={},
        )

        self.assertEqual(broker.orders, [])
        self.assertIs(plan.account_snapshot, broker.snapshot)
        self.assertEqual(tuple(order.symbol for order in plan.orders), ("AAPL", "MSFT"))
        self.assertEqual(tuple(order.risk_opening_notional for order in plan.orders), (60.0, 40.0))
        with self.assertRaises(FrozenInstanceError):
            plan.orders = ()
        with self.assertRaises(FrozenInstanceError):
            plan.orders[0].notional = 1.0

        executor.execute_rebalance(plan)
        self.assertEqual(len(broker.orders), 2)
        with self.assertRaisesRegex(ValueError, "already started"):
            executor.execute_rebalance(plan)
        self.assertEqual(len(broker.orders), 2)

    def test_executor_uses_snapshot_equity_and_preflights_cumulative_buys(self):
        broker = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))
        executor = AutoExecutor(broker)

        executor.rebalance(
            target_weights={"AAPL": 0.60, "MSFT": 0.40, "NVDA": 0.0},
            current_holdings={"NVDA": 0.20},
        )

        self.assertEqual(broker.snapshot_calls, 1)
        self.assertEqual(
            broker.orders,
            [
                ("AAPL", 60.0, "buy"),
                ("MSFT", 40.0, "buy"),
                ("NVDA", 20.0, "sell"),
            ],
        )

    def test_executor_rejects_blocked_unknown_and_total_buying_power_before_submit(self):
        snapshots = (
            account(trading_blocked=True),
            account(margin_status=MarginStatus.UNKNOWN),
            account(equity=100.0, buying_power=99.0),
        )
        for snapshot in snapshots:
            broker = RecordingSnapshotBroker(snapshot)
            with self.subTest(snapshot=snapshot), self.assertRaises(ValueError):
                AutoExecutor(broker).rebalance(
                    target_weights={"AAPL": 0.60, "MSFT": 0.40},
                    current_holdings={},
                )
            self.assertEqual(broker.orders, [])

    def test_executor_requires_shorting_fact_only_when_opening_short(self):
        disabled = account(equity=100.0, buying_power=100.0, shorting_enabled=False)
        reduction = RecordingSnapshotBroker(disabled)
        AutoExecutor(reduction).rebalance(
            target_weights={"AAPL": 0.0}, current_holdings={"AAPL": 0.1}
        )
        self.assertEqual(reduction.orders, [("AAPL", 10.0, "sell")])

        opening_short = RecordingSnapshotBroker(disabled)
        with self.assertRaisesRegex(ValueError, "shorting_disabled"):
            AutoExecutor(opening_short).rebalance(
                target_weights={"AAPL": -0.1}, current_holdings={}
            )
        self.assertEqual(opening_short.orders, [])

    def test_crossing_zero_consumes_power_only_for_the_opening_leg(self):
        crossing_short = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=10.0, shorting_enabled=True)
        )
        AutoExecutor(crossing_short).rebalance(
            target_weights={"AAPL": -0.1}, current_holdings={"AAPL": 0.2}
        )
        self.assertEqual(crossing_short.orders, [("AAPL", 30.0, "sell")])

        crossing_long = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=10.0, shorting_enabled=True)
        )
        AutoExecutor(crossing_long).rebalance(
            target_weights={"AAPL": 0.1}, current_holdings={"AAPL": -0.2}
        )
        self.assertEqual(crossing_long.orders, [("AAPL", 30.0, "buy")])

        cover_only = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=0.0, shorting_enabled=False)
        )
        AutoExecutor(cover_only).rebalance(
            target_weights={"AAPL": -0.1}, current_holdings={"AAPL": -0.2}
        )
        self.assertEqual(cover_only.orders, [("AAPL", 10.0, "buy")])

        disabled_short = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=10.0, shorting_enabled=False)
        )
        with self.assertRaisesRegex(ValueError, "shorting_disabled"):
            AutoExecutor(disabled_short).rebalance(
                target_weights={"AAPL": -0.1}, current_holdings={"AAPL": 0.2}
            )
        self.assertEqual(disabled_short.orders, [])

    def test_invalid_delta_weights_and_zero_rounded_orders_have_no_side_effects(self):
        for value in (-1.0, math.nan, math.inf, True):
            with self.subTest(min_trade_delta=value), self.assertRaises(ValueError):
                AutoExecutor(RecordingSnapshotBroker(account()), min_trade_delta=value)

        broker = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))
        for target in (math.nan, math.inf, -math.inf):
            with self.subTest(target=target), self.assertRaises(ValueError):
                AutoExecutor(broker).prepare_rebalance(
                    target_weights={"AAPL": target}, current_holdings={}
                )
        self.assertEqual(broker.orders, [])

        tiny = RecordingSnapshotBroker(account(equity=0.01, buying_power=0.01))
        tiny_executor = AutoExecutor(tiny)
        plan = tiny_executor.prepare_rebalance(
            target_weights={"AAPL": 0.10}, current_holdings={}
        )
        self.assertEqual(plan.orders, ())
        tiny_executor.execute_rebalance(plan)
        self.assertEqual(tiny.orders, [])


class BrokerSubmissionSafetyTest(unittest.TestCase):
    def test_first_submission_failure_is_uncertain_and_never_uses_backup(self):
        for records_before_error in (False, True):
            primary = FailingSubmissionBroker(
                account(equity=100.0, buying_power=100.0),
                fail_on_call=1,
                records_before_error=records_before_error,
            )
            backup = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))
            executor = BrokerFailoverExecutor(primary, backup)

            with self.subTest(records_before_error=records_before_error), self.assertRaises(
                BrokerSubmissionUncertainError
            ) as caught:
                executor.rebalance(
                    target_weights={"AAPL": 0.5}, current_holdings={}
                )

            error = caught.exception
            self.assertEqual(error.attempted_count, 1)
            self.assertEqual(error.succeeded_count, 0)
            self.assertEqual(len(error.attempted_orders), 1)
            self.assertEqual(error.succeeded_orders, ())
            self.assertIsInstance(error.original_error, RuntimeError)
            self.assertIn("reconciliation_required", repr(error))
            self.assertIn("succeeded_count=0", repr(error))
            self.assertEqual(backup.orders, [])

    def test_second_submission_failure_preserves_known_success_and_never_duplicates_to_backup(self):
        primary = FailingSubmissionBroker(
            account(equity=100.0, buying_power=100.0),
            fail_on_call=2,
            records_before_error=True,
        )
        backup = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))

        with self.assertRaises(BrokerSubmissionUncertainError) as caught:
            BrokerFailoverExecutor(primary, backup).rebalance(
                target_weights={"AAPL": 0.5, "MSFT": 0.5},
                current_holdings={},
            )

        self.assertEqual(caught.exception.attempted_count, 2)
        self.assertEqual(caught.exception.succeeded_count, 1)
        self.assertEqual(caught.exception.succeeded_orders[0].symbol, "AAPL")
        self.assertEqual([order[0] for order in primary.orders].count("AAPL"), 1)
        self.assertEqual(backup.orders, [])

    def test_primary_prepare_failure_can_use_backup_because_no_submit_started(self):
        primary = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=100.0, trading_blocked=True)
        )
        backup = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))

        result = BrokerFailoverExecutor(primary, backup).rebalance(
            target_weights={"AAPL": 0.5},
            current_holdings={},
            backup_current_holdings={},
        )

        self.assertFalse(result.primary_used)
        self.assertTrue(result.backup_used)
        self.assertEqual(primary.orders, [])
        self.assertEqual(backup.orders, [("AAPL", 50.0, "buy")])

    def test_prepare_failover_requires_explicit_backup_holdings(self):
        primary = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=100.0, trading_blocked=True)
        )
        backup = RecordingSnapshotBroker(account(equity=100.0, buying_power=100.0))

        with self.assertRaisesRegex(ValueError, "backup_current_holdings_required"):
            BrokerFailoverExecutor(primary, backup).rebalance(
                target_weights={"AAPL": 0.1}, current_holdings={"AAPL": 0.2}
            )

        self.assertEqual(backup.orders, [])

    def test_prepare_failover_uses_backup_brokers_own_holdings(self):
        primary = RecordingSnapshotBroker(
            account(equity=100.0, buying_power=100.0, trading_blocked=True)
        )
        backup = RecordingSnapshotBroker(
            account(
                equity=100.0,
                buying_power=100.0,
                shorting_enabled=False,
            )
        )

        result = BrokerFailoverExecutor(primary, backup).rebalance(
            target_weights={"AAPL": 0.1},
            current_holdings={"AAPL": 0.2},
            backup_current_holdings={},
        )

        self.assertTrue(result.backup_used)
        self.assertEqual(backup.orders, [("AAPL", 10.0, "buy")])

    def test_legacy_broker_requires_explicit_account_state_wrapper(self):
        legacy = LegacyBroker()
        with self.assertRaisesRegex(ValueError, "AccountStatePort"):
            AutoExecutor(legacy)

        wrapped = AccountAwareBrokerAdapter(
            legacy,
            StaticAccountStatePort(account(equity=100.0, buying_power=100.0)),
        )
        self.assertIsInstance(wrapped, AccountStatePort)
        AutoExecutor(wrapped).rebalance(
            target_weights={"AAPL": 0.5}, current_holdings={}
        )
        self.assertEqual(legacy.orders, [("AAPL", 50.0, "buy")])


class BrokerAdapterMappingTest(unittest.TestCase):
    def test_alpaca_object_and_ibkr_mapping_become_strict_snapshots(self):
        alpaca_payload = type(
            "Account",
            (),
            {
                "equity": "125000.50",
                "buying_power": "50000",
                "daytrading_buying_power": "40000",
                "trading_blocked": False,
                "account_blocked": False,
                "trade_suspended_by_user": False,
                "shorting_enabled": True,
                "status": "ACTIVE",
                "multiplier": "2",
            },
        )()
        alpaca = AlpacaBrokerAdapter(
            StaticPayloadClient(alpaca_payload), clock=lambda: OBSERVED_AT
        ).get_account_snapshot()
        ibkr = IBKRBrokerAdapter(
            StaticIBKRClient(
                {
                    "equity": 200_000.0,
                    "buying_power": 80_000.0,
                    "day_trade_buying_power": None,
                    "trading_blocked": False,
                    "shorting_enabled": False,
                    "margin_status": "cash",
                    "status": "ACTIVE",
                }
            ),
            clock=lambda: OBSERVED_AT,
        ).get_account_snapshot()

        self.assertEqual(alpaca.equity, 125_000.50)
        self.assertEqual(alpaca.day_trade_buying_power, 40_000.0)
        self.assertEqual(alpaca.margin_status, MarginStatus.MARGIN)
        self.assertEqual(ibkr.margin_status, MarginStatus.CASH)
        self.assertIsNone(ibkr.day_trade_buying_power)

    def test_broker_mapping_rejects_string_booleans_nan_negative_and_missing_power(self):
        valid = {
            "equity": 100.0,
            "buying_power": 100.0,
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
            "shorting_enabled": True,
            "status": "ACTIVE",
            "multiplier": "2",
        }
        invalid = (
            dict(valid, trading_blocked="false"),
            dict(valid, shorting_enabled="true"),
            dict(valid, equity=math.nan),
            dict(valid, buying_power=-1.0),
            {key: value for key, value in valid.items() if key != "buying_power"},
        )
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                AlpacaBrokerAdapter(
                    StaticPayloadClient(payload), clock=lambda: OBSERVED_AT
                ).get_account_snapshot()

    def test_non_active_broker_status_becomes_a_blocking_restriction(self):
        snapshot = AlpacaBrokerAdapter(
            StaticPayloadClient(
                {
                    "equity": 100.0,
                    "buying_power": 100.0,
                    "trading_blocked": False,
                    "account_blocked": False,
                    "trade_suspended_by_user": False,
                    "shorting_enabled": True,
                    "status": "FROZEN",
                    "margin_status": "margin",
                }
            ),
            clock=lambda: OBSERVED_AT,
        ).get_account_snapshot()

        decision = BrokerAccountRiskGate().evaluate(snapshot, 1.0)

        self.assertIn("account_status:frozen", snapshot.restrictions)
        self.assertIn("account_restricted", decision.reasons)

    def test_alpaca_trading_client_exposes_account_state_port_without_boolean_coercion(self):
        client = AlpacaTradingClient(
            "key",
            "secret",
            transport=StaticTransport(
                {
                    "equity": "1000",
                    "buying_power": "500",
                    "daytrading_buying_power": "250",
                    "trading_blocked": False,
                    "account_blocked": False,
                    "trade_suspended_by_user": False,
                    "shorting_enabled": True,
                    "status": "ACTIVE",
                    "multiplier": "2",
                }
            ),
        )

        snapshot = client.account_snapshot()

        self.assertIsInstance(client, AccountStatePort)
        self.assertEqual(snapshot.broker, "alpaca")
        self.assertIsNotNone(snapshot.observed_at.utcoffset())

    def test_broker_specific_schema_requires_every_safety_fact(self):
        alpaca = {
            "equity": 100.0,
            "buying_power": 100.0,
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
            "shorting_enabled": True,
            "status": "ACTIVE",
            "multiplier": "2",
        }
        for field in (
            "equity",
            "buying_power",
            "trading_blocked",
            "account_blocked",
            "trade_suspended_by_user",
            "shorting_enabled",
            "status",
            "multiplier",
        ):
            payload = dict(alpaca)
            payload.pop(field)
            with self.subTest(broker="alpaca", field=field), self.assertRaises(ValueError):
                AlpacaBrokerAdapter(
                    StaticPayloadClient(payload), clock=lambda: OBSERVED_AT
                ).account_snapshot()

        ibkr = {
            "equity": 100.0,
            "buying_power": 100.0,
            "trading_blocked": False,
            "shorting_enabled": True,
            "status": "ACTIVE",
            "margin_status": "margin",
        }
        for field in tuple(ibkr):
            payload = dict(ibkr)
            payload.pop(field)
            with self.subTest(broker="ibkr", field=field), self.assertRaises(ValueError):
                IBKRBrokerAdapter(
                    StaticIBKRClient(payload), clock=lambda: OBSERVED_AT
                ).account_snapshot()

    def test_alpaca_multiplier_four_is_margin_not_portfolio_margin(self):
        payload = {
            "equity": 100.0,
            "buying_power": 100.0,
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
            "shorting_enabled": True,
            "status": "ACTIVE",
            "multiplier": "4",
        }

        snapshot = AlpacaBrokerAdapter(
            StaticPayloadClient(payload), clock=lambda: OBSERVED_AT
        ).account_snapshot()

        self.assertEqual(snapshot.margin_status, MarginStatus.MARGIN)


class PDTCompatibilityAndSourceTest(unittest.TestCase):
    def test_compatibility_protector_delegates_to_broker_facts(self):
        allowed = PDTProtector(StaticAccountStatePort(account(equity=100.0)))
        blocked = BrokerDayTradeProtector(
            StaticAccountStatePort(account(equity=1_000_000.0, trading_blocked=True))
        )

        self.assertTrue(allowed.can_day_trade(requested_notional=50.0))
        self.assertFalse(blocked.can_day_trade(requested_notional=50.0))
        self.assertIn("account_trading_blocked", blocked.decision(50.0).reasons)

    def test_production_sources_have_no_local_pdt_constants_or_rolling_inference(self):
        root = Path(__file__).resolve().parents[1] / "src" / "quantbot"
        for path in root.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            names = {
                node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
            }
            attributes = {
                node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
            }
            numbers = {
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant)
                and isinstance(node.value, (int, float))
                and not isinstance(node.value, bool)
            }
            self.assertNotIn("pdt_threshold", names | attributes)
            self.assertNotIn("max_day_trades_5d", names | attributes)
            self.assertNotIn(25000, numbers)
            self.assertNotIn("rolling_five_day_symbols", source)

        self.assertFalse(hasattr(RiskConfig(), "pdt_protection"))

    def test_docs_describe_broker_reported_account_risk_not_local_thresholds(self):
        root = Path(__file__).resolve().parents[1]
        for path in (
            root / "README.md",
            root / "docs" / "history" / "StockBot" / "doc" / "implementation_status.md",
            root / "docs" / "history" / "StockBot" / "doc" / "us_quant_system_design.md",
        ):
            content = path.read_text(encoding="utf-8")
            self.assertNotIn("<$25K", content)
            self.assertNotIn("PDT protection", content)
            self.assertNotIn("预设每周交易次数上限", content)
            self.assertIn("broker", content.lower())

        implementation = (
            root / "docs" / "history" / "StockBot" / "doc" / "implementation_status.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("guarded live Alpaca order submission", implementation)
        self.assertIn("historical replay only", implementation)
        self.assertIn("live_trading_disabled", implementation)


class RecordingSnapshotBroker:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.snapshot_calls = 0
        self.orders = []

    def account_snapshot(self):
        self.snapshot_calls += 1
        return self.snapshot

    def get_portfolio_value(self):
        raise AssertionError("executor must use the account snapshot")

    def submit_order(self, symbol, notional, side, order_type="market", time_in_force="day"):
        del order_type, time_in_force
        self.orders.append((symbol, notional, side))

    def close_all_positions(self):
        pass


class FailingSubmissionBroker(RecordingSnapshotBroker):
    def __init__(self, snapshot, fail_on_call, records_before_error):
        super().__init__(snapshot)
        self.fail_on_call = fail_on_call
        self.records_before_error = records_before_error
        self.submit_calls = 0

    def submit_order(self, symbol, notional, side, order_type="market", time_in_force="day"):
        del order_type, time_in_force
        self.submit_calls += 1
        order = (symbol, notional, side)
        if self.submit_calls == self.fail_on_call:
            if self.records_before_error:
                self.orders.append(order)
            raise RuntimeError("broker response lost")
        self.orders.append(order)


class LegacyBroker:
    def __init__(self):
        self.orders = []

    def get_portfolio_value(self):
        return 100.0

    def submit_order(self, symbol, notional, side, order_type="market", time_in_force="day"):
        del order_type, time_in_force
        self.orders.append((symbol, notional, side))

    def close_all_positions(self):
        pass


class StaticPayloadClient:
    def __init__(self, payload):
        self.payload = payload

    def get_account(self):
        return self.payload


class StaticIBKRClient:
    def __init__(self, payload):
        self.payload = payload

    def account_snapshot(self):
        return self.payload


class StaticTransport:
    def __init__(self, payload):
        self.payload = payload

    def get_json(self, *args, **kwargs):
        del args, kwargs
        return self.payload


def _daytrade_bars():
    def bar(minute, open_price, high, low, close, volume, vwap):
        return MinuteBar(
            "AAPL",
            datetime(2024, 1, 2, 14, minute, tzinfo=timezone.utc),
            open_price,
            high,
            low,
            close,
            volume,
            vwap,
        )

    return [
        bar(30, 100.0, 101.0, 99.5, 100.5, 1000, 100.0),
        bar(31, 100.5, 100.8, 100.0, 100.4, 1000, 100.2),
        bar(45, 101.0, 102.2, 100.9, 102.0, 2000, 101.0),
        bar(46, 102.0, 102.5, 101.8, 102.2, 2000, 101.5),
        bar(47, 102.2, 104.0, 102.0, 103.5, 2000, 102.0),
    ]


if __name__ == "__main__":
    unittest.main()
