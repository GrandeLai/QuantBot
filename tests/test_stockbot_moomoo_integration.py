import importlib
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


import quantbot.integrations.moomoo as moomoo_integration
from quantbot.integrations.moomoo import (
    MoomooApiError,
    MoomooCancellationUncertainError,
    MoomooOrderReconciliationError,
    MoomooSimulatedTradingClient,
    MoomooSubmissionReconciliationError,
    MoomooSubmissionUncertainError,
)


class StockBotMoomooIntegrationTest(unittest.TestCase):
    def setUp(self):
        with moomoo_integration._PROCESS_RATE_LIMITERS_LOCK:
            moomoo_integration._PROCESS_RATE_LIMITERS.clear()

    def test_constructor_rejects_real_and_unknown_environment_before_sdk_import(self):
        with patch.object(
            importlib, "import_module", side_effect=AssertionError("must not import")
        ):
            with self.assertRaisesRegex(ValueError, "only permits"):
                MoomooSimulatedTradingClient(trd_env="REAL")
            with self.assertRaisesRegex(ValueError, "only permits"):
                MoomooSimulatedTradingClient(trd_env="unknown")

    def test_constructor_allows_only_local_opend_and_moomoo_us_firm(self):
        with self.assertRaisesRegex(ValueError, "local Moomoo OpenD"):
            MoomooSimulatedTradingClient(host="opend.internal")
        with self.assertRaisesRegex(ValueError, "unsupported Moomoo security_firm"):
            MoomooSimulatedTradingClient(security_firm="NONE")

    def test_imported_sdk_console_logging_is_disabled_before_use(self):
        context = FakeTradeContext()
        sdk = FakeMoomooSdk(context)

        class RecordingSysConfig:
            calls = []

            @classmethod
            def enable_console_log(cls, enabled):
                cls.calls.append(enabled)

        sdk.SysConfig = RecordingSysConfig
        with patch.object(importlib, "import_module", return_value=sdk):
            client = MoomooSimulatedTradingClient(context=context)
            snapshot = client.snapshot()

        self.assertEqual(snapshot["trd_env"], "SIMULATE")
        self.assertEqual(RecordingSysConfig.calls, [False])

    def test_creates_us_security_context_with_configured_connection_and_firm(self):
        context = FakeTradeContext()
        sdk = FakeMoomooSdk(context)
        probe_socket = FakeSocket()
        tcp_connect = Mock(return_value=probe_socket)
        client = MoomooSimulatedTradingClient(
            host="localhost",
            port=12345,
            acc_id=1001,
            sdk=sdk,
            tcp_connect=tcp_connect,
        )

        snapshot = client.snapshot()

        self.assertEqual(snapshot["acc_id"], 1001)
        self.assertEqual(
            sdk.context_calls,
            [
                {
                    "filter_trdmarket": FakeMoomooSdk.TrdMarket.US,
                    "host": "localhost",
                    "port": 12345,
                    "security_firm": FakeMoomooSdk.SecurityFirm.FUTUINC,
                }
            ],
        )
        self.assertEqual(context.unlock_calls, 0)
        tcp_connect.assert_called_once_with(
            ("localhost", 12345), timeout=2.0
        )
        self.assertEqual(probe_socket.close_calls, 1)
        self.assertEqual(context.sync_timeout_calls, [2.0])

    def test_creates_quote_context_with_configured_connection_and_firm(self):
        trade_context = FakeTradeContext()
        quote_context = FakeQuoteContext()
        sdk = FakeMoomooSdk(trade_context, quote_context)
        probe_socket = FakeSocket()
        tcp_connect = Mock(return_value=probe_socket)
        client = MoomooSimulatedTradingClient(
            host="localhost",
            port=12345,
            sdk=sdk,
            tcp_connect=tcp_connect,
        )

        result = client.market_snapshot(["US.AAPL"])

        self.assertEqual(result[0]["code"], "US.AAPL")
        self.assertEqual(
            sdk.quote_context_calls,
            [
                {
                    "host": "localhost",
                    "port": 12345,
                    "security_firm": FakeMoomooSdk.SecurityFirm.FUTUINC,
                }
            ],
        )
        tcp_connect.assert_called_once_with(
            ("localhost", 12345), timeout=2.0
        )
        self.assertEqual(probe_socket.close_calls, 1)
        self.assertEqual(quote_context.sync_timeout_calls, [2.0])

    def test_created_context_requires_sync_query_timeout_setter(self):
        context = MissingSyncTimeoutTradeContext()
        sdk = FakeMoomooSdk(context)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=sdk,
            tcp_connect=Mock(return_value=FakeSocket()),
        )

        with self.assertRaisesRegex(
            MoomooApiError, "missing set_sync_query_connect_timeout"
        ):
            client.snapshot()

        self.assertEqual(context.close_calls, 1)
        self.assertEqual(context.calls, [])

    def test_tcp_preflight_failure_does_not_create_sdk_context(self):
        context = FakeTradeContext()
        sdk = FakeMoomooSdk(context)
        tcp_connect = Mock(side_effect=ConnectionRefusedError("refused"))
        client = MoomooSimulatedTradingClient(
            host="127.0.0.1",
            port=11111,
            acc_id=1001,
            sdk=sdk,
            connect_timeout=0.25,
            tcp_connect=tcp_connect,
        )

        with self.assertRaisesRegex(MoomooApiError, "TCP preflight failed"):
            client.snapshot()

        tcp_connect.assert_called_once_with(
            ("127.0.0.1", 11111), timeout=0.25
        )
        self.assertEqual(sdk.context_calls, [])

    def test_injected_context_skips_tcp_preflight(self):
        context = FakeTradeContext()
        tcp_connect = Mock(side_effect=AssertionError("must not connect"))
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            tcp_connect=tcp_connect,
        )

        self.assertEqual(client.snapshot()["acc_id"], 1001)

        tcp_connect.assert_not_called()

    def test_snapshot_uses_fresh_simulated_queries_and_plain_data(self):
        context = FakeTradeContext(
            account_info=[{"total_assets": "125000.50", "power": 90000}],
            positions=[
                {
                    "code": "US.AAPL",
                    "qty": 10,
                    "can_sell_qty": 8,
                    "tags": ("core", "long"),
                }
            ],
            orders=[order_row(order_id=8001)],
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        snapshot = client.snapshot()

        self.assertEqual(snapshot["trd_env"], "SIMULATE")
        self.assertEqual(snapshot["market"], "US")
        self.assertEqual(snapshot["positions"][0]["tags"], ["core", "long"])
        account_call = context.call_for("accinfo_query")
        self.assertEqual(account_call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(account_call["acc_id"], 1001)
        self.assertTrue(account_call["refresh_cache"])
        self.assertEqual(account_call["currency"], FakeMoomooSdk.Currency.USD)
        position_call = context.call_for("position_list_query")
        self.assertEqual(position_call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(position_call["acc_id"], 1001)
        self.assertEqual(position_call["position_market"], FakeMoomooSdk.TrdMarket.US)
        self.assertTrue(position_call["refresh_cache"])
        order_call = context.call_for("order_list_query")
        self.assertEqual(order_call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(order_call["acc_id"], 1001)
        self.assertEqual(order_call["order_market"], FakeMoomooSdk.TrdMarket.US)
        self.assertTrue(order_call["refresh_cache"])

    def test_omitted_acc_id_requires_exactly_one_simulated_account(self):
        accounts = [account_row(1001), account_row(1002)]
        context = FakeTradeContext(accounts=accounts)
        client = MoomooSimulatedTradingClient(
            sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaisesRegex(MoomooApiError, "exactly one"):
            client.snapshot()

        self.assertEqual(context.calls_named("accinfo_query"), [])

    def test_explicit_acc_id_rejects_real_inactive_privileged_and_wrong_market_accounts(self):
        cases = (
            (account_row(1001, trd_env="REAL"), "not a SIMULATE"),
            (account_row(1001, acc_status="DISABLED"), "not ACTIVE"),
            (account_row(1001, acc_role="MASTER"), "unsupported acc_role"),
            (account_row(1001, acc_role="IPO"), "unsupported acc_role"),
            (
                account_row(1001, sim_acc_type="FUTURES"),
                "not a STOCK_AND_OPTION",
            ),
            (account_row(1001, trdmarket_auth=["HK"]), "US market"),
        )
        for account, message in cases:
            with self.subTest(message=message):
                context = FakeTradeContext(accounts=[account])
                client = MoomooSimulatedTradingClient(
                    acc_id=1001, sdk=FakeMoomooSdk(context), context=context
                )
                with self.assertRaisesRegex(MoomooApiError, message):
                    client.snapshot()

    def test_explicit_acc_id_ignores_real_accounts_and_selects_requested_simulation(self):
        context = FakeTradeContext(
            accounts=[
                account_row(9001, trd_env="REAL", sim_acc_type="NONE"),
                account_row(1001),
            ]
        )
        client = MoomooSimulatedTradingClient(
            acc_id="1001", sdk=FakeMoomooSdk(context), context=context
        )

        snapshot = client.snapshot()

        self.assertEqual(snapshot["acc_id"], 1001)

    def test_simulated_na_role_is_accepted(self):
        context = FakeTradeContext(accounts=[account_row(1001, acc_role="N/A")])
        client = MoomooSimulatedTradingClient(
            sdk=FakeMoomooSdk(context), context=context
        )

        snapshot = client.snapshot()

        self.assertEqual(snapshot["acc_id"], 1001)

    def test_implicit_selection_ignores_ineligible_accounts(self):
        context = FakeTradeContext(
            accounts=[
                account_row(2001, sim_acc_type="OPTION"),
                account_row(2002, trdmarket_auth=["HK"]),
                account_row(2003, sim_acc_type="STOCK"),
                account_row(1001, sim_acc_type="STOCK_AND_OPTION"),
            ]
        )
        client = MoomooSimulatedTradingClient(
            sdk=FakeMoomooSdk(context), context=context
        )

        snapshot = client.snapshot()

        self.assertEqual(snapshot["acc_id"], 1001)

    def test_places_only_limit_day_simulated_order_with_unique_reconciliation_marker(self):
        context = FakeTradeContext(place_result=[order_row(order_id=8001)])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        result = client.place_limit_order("us.aapl", "buy", 10, 123.45)

        call = context.call_for("place_order")
        self.assertEqual(call["code"], "US.AAPL")
        self.assertEqual(call["trd_side"], FakeMoomooSdk.TrdSide.BUY)
        self.assertEqual(call["order_type"], FakeMoomooSdk.OrderType.NORMAL)
        self.assertEqual(call["time_in_force"], FakeMoomooSdk.TimeInForce.DAY)
        self.assertEqual(call["session"], FakeMoomooSdk.Session.RTH)
        self.assertEqual(call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(call["acc_id"], 1001)
        self.assertTrue(call["remark"].startswith("STOCKBOT:"))
        self.assertEqual(result["submission_marker"], call["remark"])
        self.assertEqual(context.unlock_calls, 0)

    def test_place_accepts_explicit_broker_sized_submission_marker(self):
        context = FakeTradeContext(place_result=[order_row(order_id=8001)])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )
        marker = "STOCKBOT:" + "x" * 55

        result = client.place_limit_order(
            "US.AAPL", "BUY", 1, 100, submission_marker=marker
        )

        self.assertEqual(result["submission_marker"], marker)
        self.assertEqual(context.call_for("place_order")["remark"], marker)
        self.assertEqual(len(marker.encode("utf-8")), 64)

    def test_place_rejects_invalid_explicit_marker_before_any_sdk_call(self):
        context = FakeTradeContext()
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        for marker in (
            "manual-marker",
            "STOCKBOT:" + "x" * 56,
            "STOCKBOT:" + "股" * 19,
            " STOCKBOT:trimmed ",
        ):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(ValueError, "64 UTF-8 bytes"):
                    client.place_limit_order(
                        "US.AAPL",
                        "BUY",
                        1,
                        100,
                        submission_marker=marker,
                    )

        self.assertEqual(context.calls, [])

    def test_market_snapshot_returns_one_valid_row_per_requested_us_code(self):
        trade_context = FakeTradeContext()
        quote_context = FakeQuoteContext(
            snapshots=[
                market_snapshot_row("US.MSFT", last_price="420.25"),
                market_snapshot_row("US.AAPL", suspension=ScalarBool(False)),
            ]
        )
        client = MoomooSimulatedTradingClient(
            sdk=FakeMoomooSdk(trade_context, quote_context),
            context=trade_context,
            quote_context=quote_context,
        )

        rows = client.market_snapshot(["us.aapl", "US.MSFT"])

        self.assertEqual([row["code"] for row in rows], ["US.AAPL", "US.MSFT"])
        self.assertEqual(rows[1]["last_price"], 420.25)
        self.assertEqual(rows[0]["lot_size"], 1)
        self.assertIs(rows[0]["suspension"], False)
        self.assertEqual(
            quote_context.call_for("get_market_snapshot"),
            {"code_list": ["US.AAPL", "US.MSFT"]},
        )

    def test_market_snapshot_rejects_ambiguous_or_untradable_rows(self):
        base = market_snapshot_row("US.AAPL")
        cases = (
            ([], "missing=US.AAPL"),
            ([base, base], "duplicate code"),
            ([{**base, "code": "US.MSFT"}], "unexpected=US.MSFT"),
            (
                [
                    {
                        key: value
                        for key, value in base.items()
                        if key != "bid_price"
                    }
                ],
                "missing required field bid_price",
            ),
            ([{**base, "ask_price": "N/A"}], "must not be N/A"),
            ([{**base, "last_price": float("inf")}], "non-finite"),
            ([{**base, "suspension": True}], "suspended code"),
            ([{**base, "suspension": "False"}], "must be a boolean"),
            ([{**base, "update_time": "2026/08/07"}], "YYYY-MM-DD"),
        )
        for snapshots, message in cases:
            with self.subTest(message=message):
                trade_context = FakeTradeContext()
                quote_context = FakeQuoteContext(snapshots=snapshots)
                client = MoomooSimulatedTradingClient(
                    sdk=FakeMoomooSdk(trade_context, quote_context),
                    context=trade_context,
                    quote_context=quote_context,
                )
                with self.assertRaisesRegex(MoomooApiError, message):
                    client.market_snapshot(["US.AAPL"])

    def test_market_snapshot_rejects_invalid_request_before_quote_call(self):
        trade_context = FakeTradeContext()
        quote_context = FakeQuoteContext()
        client = MoomooSimulatedTradingClient(
            sdk=FakeMoomooSdk(trade_context, quote_context),
            context=trade_context,
            quote_context=quote_context,
        )

        for codes in ([], ["US.AAPL", "us.aapl"], ["HK.00700"], "US.AAPL"):
            with self.subTest(codes=codes):
                with self.assertRaises(ValueError):
                    client.market_snapshot(codes)

        self.assertEqual(quote_context.calls, [])

    def test_trading_day_normalizes_whole_and_half_day_types(self):
        for raw_type, expected in (
            ("WHOLE", {"time": "2026-08-07", "trade_date_type": "WHOLE"}),
            (
                "MORNING",
                {
                    "time": "2026-08-07",
                    "trade_date_type": "HALF",
                    "trade_session": "MORNING",
                },
            ),
            ("HALF", {"time": "2026-08-07", "trade_date_type": "HALF"}),
        ):
            with self.subTest(raw_type=raw_type):
                trade_context = FakeTradeContext()
                quote_context = FakeQuoteContext(
                    trading_days=[
                        {"time": "2026-08-07", "trade_date_type": raw_type}
                    ]
                )
                client = MoomooSimulatedTradingClient(
                    sdk=FakeMoomooSdk(trade_context, quote_context),
                    context=trade_context,
                    quote_context=quote_context,
                )

                result = client.trading_day(date(2026, 8, 7))

                self.assertEqual(result, expected)
                self.assertEqual(
                    quote_context.call_for("request_trading_days"),
                    {
                        "market": FakeMoomooSdk.TradeDateMarket.US,
                        "start": "2026-08-07",
                        "end": "2026-08-07",
                    },
                )

    def test_trading_day_rejects_missing_duplicate_mismatched_and_unknown_rows(self):
        cases = (
            ([], "exactly one matching row"),
            (
                [
                    {"time": "2026-08-07", "trade_date_type": "WHOLE"},
                    {"time": "2026-08-07", "trade_date_type": "WHOLE"},
                ],
                "exactly one matching row",
            ),
            (
                [{"time": "2026-08-08", "trade_date_type": "WHOLE"}],
                "exactly one matching row",
            ),
            (
                [{"time": "2026-08-07", "trade_date_type": "UNKNOWN"}],
                "unsupported trade_date_type",
            ),
        )
        for rows, message in cases:
            with self.subTest(message=message):
                trade_context = FakeTradeContext()
                quote_context = FakeQuoteContext(trading_days=rows)
                client = MoomooSimulatedTradingClient(
                    sdk=FakeMoomooSdk(trade_context, quote_context),
                    context=trade_context,
                    quote_context=quote_context,
                )
                with self.assertRaisesRegex(MoomooApiError, message):
                    client.trading_day("2026-08-07")

    def test_place_non_success_response_is_uncertain_without_string_guessing(self):
        for message in (
            "price is invalid",
            "Abnormal event: timeout while waiting for trade response",
        ):
            with self.subTest(message=message):
                context = FakeTradeContext(place_response=(1, message))
                client = MoomooSimulatedTradingClient(
                    acc_id=1001, sdk=FakeMoomooSdk(context), context=context
                )

                with self.assertRaises(MoomooSubmissionUncertainError) as raised:
                    client.place_limit_order("US.AAPL", "BUY", 1, 100)

                self.assertIn(message, str(raised.exception.cause))
                self.assertEqual(len(context.calls_named("place_order")), 1)

    def test_place_transport_exception_is_uncertain_and_never_retried(self):
        context = FakeTradeContext(place_exception=TimeoutError("socket timeout"))
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaises(MoomooSubmissionUncertainError) as raised:
            client.place_limit_order("US.AAPL", "BUY", 1, 100)

        error = raised.exception
        self.assertTrue(error.reconciliation_required)
        self.assertTrue(error.submission_marker.startswith("STOCKBOT:"))
        self.assertEqual(error.order["trd_env"], "SIMULATE")
        self.assertEqual(len(context.calls_named("place_order")), 1)

    def test_success_with_malformed_place_payload_is_uncertain(self):
        context = FakeTradeContext(place_result=[{"order_id": 8001}])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaises(MoomooSubmissionUncertainError) as raised:
            client.place_limit_order("US.AAPL", "BUY", 1, 100)

        self.assertIn("missing required field", str(raised.exception.cause))
        self.assertEqual(len(context.calls_named("place_order")), 1)

    def test_reconcile_submission_requires_one_exact_remark_match(self):
        marker = "STOCKBOT:" + "a" * 32
        matching = {**order_row(order_id=8001), "remark": marker}
        context = FakeTradeContext(
            orders=[matching, {**order_row(order_id=8002), "remark": "manual"}]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        order = client.reconcile_submission(marker)

        self.assertEqual(order["order_id"], "8001")
        self.assertEqual(order["submission_marker"], marker)
        call = context.call_for("order_list_query")
        self.assertEqual(call["order_market"], FakeMoomooSdk.TrdMarket.US)
        self.assertEqual(call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(call["acc_id"], 1001)
        self.assertTrue(call["refresh_cache"])
        self.assertEqual(context.calls_named("history_order_list_query"), [])

        for rows, message in (
            ([], "no today or recent-history order"),
            ([matching, matching], "got 2"),
        ):
            with self.subTest(message=message):
                ambiguous = FakeTradeContext(orders=rows)
                ambiguous_client = MoomooSimulatedTradingClient(
                    acc_id=1001,
                    sdk=FakeMoomooSdk(ambiguous),
                    context=ambiguous,
                )
                with self.assertRaises(
                    MoomooSubmissionReconciliationError
                ) as raised:
                    ambiguous_client.reconcile_submission(marker)
                self.assertTrue(raised.exception.reconciliation_required)
                self.assertIn(message, str(raised.exception.cause))

        with self.assertRaisesRegex(ValueError, "64 UTF-8 bytes"):
            client.reconcile_submission("STOCKBOT:" + "x" * 56)

    def test_reconcile_submission_falls_back_to_bounded_history(self):
        marker = "STOCKBOT:" + "b" * 32
        matching = {**order_row(order_id=8003), "remark": marker}
        context = FakeTradeContext(orders=[], history_orders=[matching])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        order = client.reconcile_submission(marker)

        self.assertEqual(order["order_id"], "8003")
        self.assertEqual(order["submission_marker"], marker)
        call = context.call_for("history_order_list_query")
        self.assertEqual(call["order_market"], FakeMoomooSdk.TrdMarket.US)
        self.assertEqual(call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(call["acc_id"], 1001)
        start = date.fromisoformat(call["start"])
        end = date.fromisoformat(call["end"])
        self.assertEqual((end - start).days, 89)

        ambiguous = FakeTradeContext(
            orders=[], history_orders=[matching, matching]
        )
        ambiguous_client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(ambiguous),
            context=ambiguous,
        )
        with self.assertRaises(MoomooSubmissionReconciliationError) as raised:
            ambiguous_client.reconcile_submission(marker)
        self.assertIn("history_order_list_query", str(raised.exception.cause))
        self.assertIn("duplicate order_id 8003", str(raised.exception.cause))

    def test_reconcile_submission_uses_cached_positive_match_after_fresh_timeout(self):
        marker = "STOCKBOT:" + "c" * 32
        matching = {**order_row(order_id=8004), "remark": marker}
        context = FakeTradeContext()
        context.order_list_query = Mock(
            side_effect=[(1, "PacketErr.Timeout"), (0, [matching])]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        order = client.reconcile_submission(marker)

        self.assertEqual(order["order_id"], "8004")
        self.assertEqual(context.order_list_query.call_count, 2)
        self.assertTrue(
            context.order_list_query.call_args_list[0].kwargs["refresh_cache"]
        )
        self.assertFalse(
            context.order_list_query.call_args_list[1].kwargs["refresh_cache"]
        )

    def test_query_order_uses_refresh_cache_and_rejects_unknown_status(self):
        context = FakeTradeContext(query_result=[order_row(order_id=8001)])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        order = client.query_order("8001")

        self.assertEqual(order["order_status"], "SUBMITTED")
        self.assertFalse(order["is_terminal"])
        self.assertTrue(order["reconciliation_required"])
        self.assertFalse(order["outcome_uncertain"])
        query_call = context.calls_named("order_list_query")[-1]
        self.assertEqual(query_call["order_id"], "8001")
        self.assertEqual(query_call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(query_call["acc_id"], 1001)
        self.assertEqual(query_call["order_market"], FakeMoomooSdk.TrdMarket.US)
        self.assertTrue(query_call["refresh_cache"])

        context.query_result = [order_row(order_id=8001, status="TIMEOUT")]
        timed_out = client.query_order("8001")
        self.assertTrue(timed_out["reconciliation_required"])
        self.assertTrue(timed_out["outcome_uncertain"])

        context.query_result = [order_row(order_id=8001, status="MYSTERY")]
        with self.assertRaises(MoomooOrderReconciliationError) as raised:
            client.query_order("8001")
        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(raised.exception.order_id, "8001")
        self.assertIn("unknown order_status", str(raised.exception.cause))

    def test_query_order_falls_back_to_bounded_history(self):
        order_id = "9007199254740993123"
        context = FakeTradeContext(
            query_result=[],
            history_orders=[
                order_row(order_id=8002),
                order_row(order_id=order_id),
            ],
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        order = client.query_order(order_id)

        self.assertEqual(order["order_id"], order_id)
        call = context.call_for("history_order_list_query")
        self.assertEqual(call["order_market"], FakeMoomooSdk.TrdMarket.US)
        self.assertEqual(call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(call["acc_id"], 1001)
        start = date.fromisoformat(call["start"])
        end = date.fromisoformat(call["end"])
        self.assertEqual((end - start).days, 89)

    def test_place_timeout_status_is_uncertain(self):
        context = FakeTradeContext(
            place_result=[order_row(order_id=8001, status="TIMEOUT")]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaises(MoomooSubmissionUncertainError) as raised:
            client.place_limit_order("US.AAPL", "BUY", 1, 100)

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertIn("TIMEOUT order status", str(raised.exception.cause))

    def test_query_order_sdk_failure_requires_reconciliation(self):
        context = FakeTradeContext(
            query_response=(1, "Abnormal event: query timeout")
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaises(MoomooOrderReconciliationError) as raised:
            client.query_order(8001)

        self.assertTrue(raised.exception.reconciliation_required)
        self.assertEqual(raised.exception.order_id, "8001")
        self.assertIn("query timeout", str(raised.exception.cause))
        self.assertEqual(len(context.calls_named("order_list_query")), 2)

    def test_cancel_order_uses_simulated_cancel_operation(self):
        context = FakeTradeContext(cancel_result=[{"order_id": 8001}])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        result = client.cancel_order(8001)

        call = context.call_for("modify_order")
        self.assertEqual(call["modify_order_op"], FakeMoomooSdk.ModifyOrderOp.CANCEL)
        self.assertEqual(call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(call["acc_id"], 1001)
        self.assertEqual(call["qty"], 0)
        self.assertEqual(call["price"], 0)
        self.assertEqual(
            result,
            {
                "order_id": "8001",
                "action": "cancel_requested",
                "reconciliation_required": True,
            },
        )

    def test_cancel_transport_and_malformed_success_are_uncertain_without_retry(self):
        for context in (
            FakeTradeContext(cancel_exception=TimeoutError("timeout")),
            FakeTradeContext(cancel_result=[]),
            FakeTradeContext(
                cancel_response=(
                    1,
                    "Abnormal event: timeout while waiting for cancel response",
                )
            ),
        ):
            with self.subTest(context=context):
                client = MoomooSimulatedTradingClient(
                    acc_id=1001, sdk=FakeMoomooSdk(context), context=context
                )
                with self.assertRaises(MoomooCancellationUncertainError) as raised:
                    client.cancel_order(8001)
                self.assertTrue(raised.exception.reconciliation_required)
                self.assertEqual(raised.exception.order_id, "8001")
                self.assertEqual(len(context.calls_named("modify_order")), 1)

    def test_order_id_rejects_float_to_avoid_precision_loss(self):
        context = FakeTradeContext()
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaisesRegex(ValueError, "positive integer"):
            client.query_order(8001.0)

        self.assertEqual(context.calls_named("order_list_query"), [])

    def test_large_decimal_order_id_round_trips_without_float_conversion(self):
        order_id = "3964270595789502688"
        context = FakeTradeContext(query_result=[order_row(order_id=order_id)])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        order = client.query_order(order_id)

        self.assertEqual(order["order_id"], order_id)
        self.assertEqual(
            context.calls_named("order_list_query")[-1]["order_id"], order_id
        )

    def test_nan_is_normalized_to_none_but_infinity_is_rejected(self):
        context = FakeTradeContext(
            account_info=[
                {
                    "total_assets": 1000,
                    "power": 500,
                    "optional_metric": float("nan"),
                }
            ]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )
        self.assertIsNone(client.snapshot()["account"]["optional_metric"])

        bad = FakeTradeContext(
            account_info=[{"total_assets": float("inf"), "power": 500}]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(bad), context=bad
        )
        with self.assertRaisesRegex(MoomooApiError, "non-finite"):
            client.snapshot()

    def test_sdk_errors_and_missing_required_fields_fail_closed(self):
        context = FakeTradeContext(account_response=(1, "OpenD rejected query"))
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )
        with self.assertRaisesRegex(MoomooApiError, "OpenD rejected query"):
            client.snapshot()

        malformed = FakeTradeContext(account_info=[{"total_assets": 1000}])
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(malformed), context=malformed
        )
        with self.assertRaisesRegex(MoomooApiError, "missing required field power"):
            client.snapshot()

    def test_context_manager_closes_once_and_closed_client_cannot_reopen(self):
        context = FakeTradeContext()
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with client:
            client.query_order(8001)
        client.close()

        self.assertEqual(context.close_calls, 1)
        with self.assertRaisesRegex(MoomooApiError, "closed"):
            client.snapshot()

    def test_close_closes_trade_and_quote_contexts_and_keeps_first_error(self):
        trade_failure = RuntimeError("trade close failed")
        quote_failure = RuntimeError("quote close failed")
        trade_context = FakeTradeContext(close_exception=trade_failure)
        quote_context = FakeQuoteContext(close_exception=quote_failure)
        client = MoomooSimulatedTradingClient(
            sdk=FakeMoomooSdk(trade_context, quote_context),
            context=trade_context,
            quote_context=quote_context,
        )

        client.close()
        client.close()

        self.assertEqual(trade_context.close_calls, 1)
        self.assertEqual(quote_context.close_calls, 1)
        self.assertIs(client.close_error, trade_failure)

    def test_close_quote_context_keeps_trade_context_open_and_is_idempotent(self):
        trade_context = FakeTradeContext()
        quote_context = FakeQuoteContext()
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(trade_context, quote_context),
            context=trade_context,
            quote_context=quote_context,
        )

        client.close_quote_context()
        client.close_quote_context()
        order = client.query_order(8001)

        self.assertEqual(quote_context.close_calls, 1)
        self.assertEqual(order["order_id"], "8001")
        self.assertEqual(trade_context.close_calls, 0)

    def test_orders_snapshot_is_one_fresh_simulated_us_query(self):
        context = FakeTradeContext()
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
        )

        orders = client.orders_snapshot()

        self.assertEqual([row["order_id"] for row in orders], ["8001"])
        call = context.calls_named("order_list_query")[-1]
        self.assertIs(call["refresh_cache"], True)
        self.assertEqual(call["acc_id"], 1001)
        self.assertEqual(call["trd_env"], FakeMoomooSdk.TrdEnv.SIMULATE)
        self.assertEqual(call["order_market"], FakeMoomooSdk.TrdMarket.US)

    def test_fresh_order_reads_share_one_ten_per_thirty_second_window(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            monotonic_clock=clock,
            sleep=clock.sleep,
        )

        for _ in range(5):
            client.orders_snapshot()
        clock.advance(10.0)
        for _ in range(5):
            client.query_order(8001)
        client.orders_snapshot()

        self.assertEqual(context.order_list_query_times[:5], [0.0] * 5)
        self.assertEqual(context.order_list_query_times[5:10], [10.0] * 5)
        self.assertEqual(context.order_list_query_times[10], 30.0)
        self.assertEqual(clock.sleeps, [20.0])

    def test_other_fresh_reads_enforce_their_own_ten_per_thirty_windows(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            monotonic_clock=clock,
            sleep=clock.sleep,
        )

        for _ in range(11):
            client.snapshot()

        self.assertEqual(context.accinfo_query_times[:10], [0.0] * 10)
        self.assertEqual(context.position_list_query_times[:10], [0.0] * 10)
        self.assertEqual(context.accinfo_query_times[10], 30.0)
        self.assertEqual(context.position_list_query_times[10], 30.0)
        self.assertEqual(context.order_list_query_times[10], 30.0)

        for _ in range(11):
            client.history_orders_snapshot("2026-01-01", "2026-01-02")

        self.assertEqual(context.history_order_list_query_times[:10], [30.0] * 10)
        self.assertEqual(context.history_order_list_query_times[10], 60.0)
        self.assertEqual(clock.sleeps, [30.0, 30.0])

    def test_rate_limits_are_shared_by_clients_for_the_same_opend_account(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        clients = [
            MoomooSimulatedTradingClient(
                acc_id=1001,
                sdk=FakeMoomooSdk(context),
                context=context,
                monotonic_clock=clock,
                sleep=clock.sleep,
            )
            for _ in range(2)
        ]

        for index in range(11):
            clients[index % 2].orders_snapshot()

        self.assertEqual(context.order_list_query_times[:10], [0.0] * 10)
        self.assertEqual(context.order_list_query_times[10], 30.0)
        self.assertEqual(clock.sleeps, [30.0])

    def test_place_order_enforces_minimum_interval_and_sliding_window(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            monotonic_clock=clock,
            sleep=clock.sleep,
        )

        for _ in range(16):
            client.place_limit_order("US.AAPL", "BUY", 1, 100)

        times = context.place_order_times
        self.assertEqual(len(times), 16)
        for previous, current in zip(times, times[1:]):
            self.assertGreaterEqual(current - previous + 1e-12, 0.02)
        self.assertGreaterEqual(times[15] - times[0], 30.0)

    def test_place_guard_runs_after_limiter_and_failure_never_reaches_sdk(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            monotonic_clock=clock,
            sleep=clock.sleep,
        )
        client.place_limit_order("US.AAPL", "BUY", 1, 100)
        guard_failure = RuntimeError("entry write window closed")
        observations = []

        def reject_write():
            observations.append(
                (clock(), len(context.calls_named("place_order")))
            )
            raise guard_failure

        with self.assertRaises(RuntimeError) as raised:
            client.place_limit_order(
                "US.AAPL",
                "BUY",
                1,
                100,
                pre_write_guard=reject_write,
            )

        self.assertIs(raised.exception, guard_failure)
        self.assertEqual(clock.sleeps, [0.02])
        self.assertEqual(observations, [(0.02, 1)])
        self.assertEqual(len(context.calls_named("place_order")), 1)

    def test_cancel_order_enforces_minimum_interval_and_sliding_window(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            monotonic_clock=clock,
            sleep=clock.sleep,
        )

        for _ in range(21):
            client.cancel_order(8001)

        times = context.modify_order_times
        self.assertEqual(len(times), 21)
        for previous, current in zip(times, times[1:]):
            self.assertGreaterEqual(current - previous + 1e-12, 0.04)
        self.assertGreaterEqual(times[20] - times[0], 30.0)

    def test_cancel_guard_runs_after_limiter_and_failure_never_reaches_sdk(self):
        clock = FakeMonotonicClock()
        context = TimedFakeTradeContext(clock)
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
            monotonic_clock=clock,
            sleep=clock.sleep,
        )
        client.cancel_order(8001)
        guard_failure = RuntimeError("cancel write window closed")
        observations = []

        def reject_write():
            observations.append(
                (clock(), len(context.calls_named("modify_order")))
            )
            raise guard_failure

        with self.assertRaises(RuntimeError) as raised:
            client.cancel_order(8001, pre_write_guard=reject_write)

        self.assertIs(raised.exception, guard_failure)
        self.assertEqual(clock.sleeps, [0.04])
        self.assertEqual(observations, [(0.04, 1)])
        self.assertEqual(len(context.calls_named("modify_order")), 1)

    def test_history_orders_snapshot_is_one_bounded_simulated_us_query(self):
        context = FakeTradeContext(
            history_orders=[order_row(order_id=8101, status="FILLED_ALL")]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
        )

        orders = client.history_orders_snapshot(
            "2026-01-01", date(2026, 3, 31)
        )

        self.assertEqual([row["order_id"] for row in orders], ["8101"])
        self.assertTrue(orders[0]["is_terminal"])
        calls = context.calls_named("history_order_list_query")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            {
                "order_market": FakeMoomooSdk.TrdMarket.US,
                "start": "2026-01-01",
                "end": "2026-03-31",
                "trd_env": FakeMoomooSdk.TrdEnv.SIMULATE,
                "acc_id": 1001,
            },
        )

    def test_history_orders_snapshot_rejects_invalid_or_unbounded_dates(self):
        context = FakeTradeContext(history_orders=[])
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
        )

        for start, end, message in (
            ("2026-02-02", "2026-02-01", "on or before"),
            ("2026-01-01", "2026-04-01", "89 days"),
            ("2026-1-1", "2026-01-02", "YYYY-MM-DD"),
        ):
            with self.subTest(start=start, end=end):
                with self.assertRaisesRegex(ValueError, message):
                    client.history_orders_snapshot(start, end)

        self.assertEqual(context.calls_named("history_order_list_query"), [])

    def test_history_orders_snapshot_rejects_duplicate_order_ids(self):
        duplicate = order_row(order_id=8102)
        context = FakeTradeContext(
            history_orders=[duplicate, dict(duplicate)]
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001,
            sdk=FakeMoomooSdk(context),
            context=context,
        )

        with self.assertRaisesRegex(
            MoomooApiError, "duplicate order_id 8102"
        ):
            client.history_orders_snapshot("2026-01-01", "2026-01-02")

        self.assertEqual(
            len(context.calls_named("history_order_list_query")), 1
        )

    def test_close_error_does_not_override_successful_write_result(self):
        close_failure = RuntimeError("close failed")
        context = FakeTradeContext(close_exception=close_failure)
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with client:
            result = client.place_limit_order("US.AAPL", "BUY", 1, 100)

        self.assertEqual(result["order_id"], "8001")
        self.assertIs(client.close_error, close_failure)
        with self.assertRaises(AttributeError):
            client.close_error = None

    def test_close_error_does_not_override_uncertain_write_error(self):
        write_failure = TimeoutError("write timeout")
        close_failure = RuntimeError("close failed")
        context = FakeTradeContext(
            place_exception=write_failure,
            close_exception=close_failure,
        )
        client = MoomooSimulatedTradingClient(
            acc_id=1001, sdk=FakeMoomooSdk(context), context=context
        )

        with self.assertRaises(MoomooSubmissionUncertainError) as raised:
            with client:
                client.place_limit_order("US.AAPL", "BUY", 1, 100)

        self.assertIs(raised.exception.cause, write_failure)
        self.assertIs(client.close_error, close_failure)


def account_row(
    acc_id,
    *,
    trd_env="SIMULATE",
    acc_status="ACTIVE",
    acc_role="N/A",
    sim_acc_type="STOCK_AND_OPTION",
    trdmarket_auth=None,
):
    return {
        "acc_id": acc_id,
        "trd_env": trd_env,
        "acc_status": acc_status,
        "acc_role": acc_role,
        "sim_acc_type": sim_acc_type,
        "trdmarket_auth": ["US"] if trdmarket_auth is None else trdmarket_auth,
    }


def order_row(order_id=8001, status="SUBMITTED"):
    return {
        "order_id": order_id,
        "code": "US.AAPL",
        "trd_side": "BUY",
        "order_status": status,
        "qty": 10,
        "dealt_qty": 0,
        "price": 123.45,
    }


def market_snapshot_row(code="US.AAPL", **overrides):
    row = {
        "code": code,
        "update_time": "2026-08-07 15:30:00",
        "last_price": 220.10,
        "bid_price": 220.09,
        "ask_price": 220.11,
        "lot_size": 1,
        "price_spread": 0.01,
        "suspension": False,
    }
    row.update(overrides)
    return row


class FakeMoomooSdk:
    RET_OK = 0

    class TrdEnv:
        SIMULATE = "SIMULATE"
        REAL = "REAL"

    class TrdMarket:
        US = "US"

    class TradeDateMarket:
        US = "US"

    class SecurityFirm:
        NONE = "NONE"
        FUTUSECURITIES = "FUTUSECURITIES"
        FUTUINC = "FUTUINC"
        FUTUSG = "FUTUSG"
        FUTUAU = "FUTUAU"
        FUTUCA = "FUTUCA"
        FUTUJP = "FUTUJP"
        FUTUMY = "FUTUMY"

    class Currency:
        USD = "USD"

    class TrdSide:
        BUY = "BUY"
        SELL = "SELL"

    class OrderType:
        NORMAL = "NORMAL"

    class TimeInForce:
        DAY = "DAY"

    class Session:
        RTH = "RTH"

    class ModifyOrderOp:
        CANCEL = "CANCEL"

    def __init__(self, context, quote_context=None):
        self.context = context
        self.quote_context = quote_context
        self.context_calls = []
        self.quote_context_calls = []

    def OpenSecTradeContext(self, **kwargs):
        self.context_calls.append(kwargs)
        return self.context

    def OpenQuoteContext(self, **kwargs):
        self.quote_context_calls.append(kwargs)
        if self.quote_context is None:
            raise AssertionError("quote context was not configured")
        return self.quote_context


class FakeTradeContext:
    def __init__(
        self,
        *,
        accounts=None,
        account_info=None,
        positions=None,
        orders=None,
        history_orders=None,
        query_result=None,
        query_response=None,
        place_result=None,
        cancel_result=None,
        cancel_response=None,
        cancel_exception=None,
        account_response=None,
        place_response=None,
        place_exception=None,
        sync_timeout_exception=None,
        close_exception=None,
    ):
        self.accounts = [account_row(1001)] if accounts is None else accounts
        self.account_info = (
            [{"total_assets": 100000, "power": 80000}]
            if account_info is None
            else account_info
        )
        self.positions = [] if positions is None else positions
        self.orders = [order_row()] if orders is None else orders
        self.history_orders = [] if history_orders is None else history_orders
        self.query_result = (
            [order_row()] if query_result is None else query_result
        )
        self.query_response = query_response
        self.place_result = (
            [order_row()] if place_result is None else place_result
        )
        self.cancel_result = (
            [{"order_id": 8001}] if cancel_result is None else cancel_result
        )
        self.cancel_response = cancel_response
        self.cancel_exception = cancel_exception
        self.account_response = account_response
        self.place_response = place_response
        self.place_exception = place_exception
        self.sync_timeout_exception = sync_timeout_exception
        self.close_exception = close_exception
        self.calls = []
        self.close_calls = 0
        self.sync_timeout_calls = []
        self.unlock_calls = 0

    def set_sync_query_connect_timeout(self, timeout):
        self.sync_timeout_calls.append(timeout)
        if self.sync_timeout_exception is not None:
            raise self.sync_timeout_exception

    def get_acc_list(self, **kwargs):
        self.calls.append(("get_acc_list", kwargs))
        return 0, self.accounts

    def accinfo_query(self, **kwargs):
        self.calls.append(("accinfo_query", kwargs))
        if self.account_response is not None:
            return self.account_response
        return 0, self.account_info

    def position_list_query(self, **kwargs):
        self.calls.append(("position_list_query", kwargs))
        return 0, self.positions

    def order_list_query(self, **kwargs):
        self.calls.append(("order_list_query", kwargs))
        if kwargs.get("order_id"):
            if self.query_response is not None:
                return self.query_response
            return 0, self.query_result
        return 0, self.orders

    def history_order_list_query(self, **kwargs):
        self.calls.append(("history_order_list_query", kwargs))
        return 0, self.history_orders

    def place_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        if self.place_exception is not None:
            raise self.place_exception
        if self.place_response is not None:
            return self.place_response
        return 0, self.place_result

    def modify_order(self, **kwargs):
        self.calls.append(("modify_order", kwargs))
        if self.cancel_exception is not None:
            raise self.cancel_exception
        if self.cancel_response is not None:
            return self.cancel_response
        return 0, self.cancel_result

    def unlock_trade(self, _password):
        self.unlock_calls += 1
        raise AssertionError("must never be called")

    def close(self):
        self.close_calls += 1
        if self.close_exception is not None:
            raise self.close_exception

    def call_for(self, name):
        return self.calls_named(name)[0]

    def calls_named(self, name):
        return [kwargs for operation, kwargs in self.calls if operation == name]


class FakeMonotonicClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class TimedFakeTradeContext(FakeTradeContext):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.accinfo_query_times = []
        self.position_list_query_times = []
        self.order_list_query_times = []
        self.history_order_list_query_times = []
        self.place_order_times = []
        self.modify_order_times = []

    def accinfo_query(self, **kwargs):
        self.accinfo_query_times.append(self.clock())
        return super().accinfo_query(**kwargs)

    def position_list_query(self, **kwargs):
        self.position_list_query_times.append(self.clock())
        return super().position_list_query(**kwargs)

    def order_list_query(self, **kwargs):
        self.order_list_query_times.append(self.clock())
        return super().order_list_query(**kwargs)

    def history_order_list_query(self, **kwargs):
        self.history_order_list_query_times.append(self.clock())
        return super().history_order_list_query(**kwargs)

    def place_order(self, **kwargs):
        self.place_order_times.append(self.clock())
        return super().place_order(**kwargs)

    def modify_order(self, **kwargs):
        self.modify_order_times.append(self.clock())
        return super().modify_order(**kwargs)


class FakeQuoteContext:
    def __init__(
        self,
        *,
        snapshots=None,
        trading_days=None,
        snapshot_response=None,
        trading_days_response=None,
        sync_timeout_exception=None,
        close_exception=None,
    ):
        self.snapshots = (
            [market_snapshot_row()] if snapshots is None else snapshots
        )
        self.trading_days = (
            [{"time": "2026-08-07", "trade_date_type": "WHOLE"}]
            if trading_days is None
            else trading_days
        )
        self.snapshot_response = snapshot_response
        self.trading_days_response = trading_days_response
        self.sync_timeout_exception = sync_timeout_exception
        self.close_exception = close_exception
        self.calls = []
        self.close_calls = 0
        self.sync_timeout_calls = []

    def set_sync_query_connect_timeout(self, timeout):
        self.sync_timeout_calls.append(timeout)
        if self.sync_timeout_exception is not None:
            raise self.sync_timeout_exception

    def get_market_snapshot(self, code_list):
        self.calls.append(("get_market_snapshot", {"code_list": code_list}))
        if self.snapshot_response is not None:
            return self.snapshot_response
        return 0, self.snapshots

    def request_trading_days(self, **kwargs):
        self.calls.append(("request_trading_days", kwargs))
        if self.trading_days_response is not None:
            return self.trading_days_response
        return 0, self.trading_days

    def close(self):
        self.close_calls += 1
        if self.close_exception is not None:
            raise self.close_exception

    def call_for(self, name):
        return self.calls_named(name)[0]

    def calls_named(self, name):
        return [kwargs for operation, kwargs in self.calls if operation == name]


class FakeSocket:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


class ScalarBool:
    def __init__(self, value):
        self.value = value

    def item(self):
        return self.value


class MissingSyncTimeoutTradeContext(FakeTradeContext):
    set_sync_query_connect_timeout = None


if __name__ == "__main__":
    unittest.main()
