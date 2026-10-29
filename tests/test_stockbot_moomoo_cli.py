"""CLI contracts for the isolated moomoo simulated-account integration."""

import argparse
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _client():
    client = MagicMock()
    client.close_error = None
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.snapshot.return_value = {"account": {"acc_id": "42"}}
    client.place_limit_order.return_value = {
        "order_id": "101",
        "order_status": "SUBMITTED",
        "reconciliation_required": False,
    }
    client.query_order.return_value = {
        "order_id": "101",
        "order_status": "SUBMITTED",
        "reconciliation_required": False,
    }
    client.cancel_order.return_value = {
        "order_id": "101",
        "action": "cancel_requested",
        "reconciliation_required": True,
    }
    client.reconcile_submission.return_value = {
        "order_id": "101",
        "submission_marker": "STOCKBOT:test",
        "reconciliation_required": False,
    }
    return client


class StockBotMoomooCliTest(unittest.TestCase):
    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_snapshot_uses_local_opend_and_explicit_simulate(self, factory):
        from quantbot.research_cli import main

        client = _client()
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            ["moomoo-simulate", "snapshot"],
            stdout=stdout,
            environ={
                "MOOMOO_OPEND_HOST": "localhost",
                "MOOMOO_OPEND_PORT": "22222",
                "MOOMOO_SIM_ACC_ID": "9007199254740993",
                "MOOMOO_SECURITY_FIRM": "FUTUINC",
                "FUTU_OPEND_HOST": "10.0.0.2",
                "FUTU_TRD_ENV": "REAL",
            },
        )

        self.assertEqual(code, 0)
        factory.assert_called_once_with(
            host="localhost",
            port=22222,
            acc_id="9007199254740993",
            security_firm="FUTUINC",
            trd_env="SIMULATE",
        )
        client.snapshot.assert_called_once_with()
        client.__exit__.assert_called_once()
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["environment"], "SIMULATE")
        self.assertFalse(payload["reconciliation_required"])

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_non_loopback_host_fails_before_client_construction(self, factory):
        from quantbot.research_cli import main

        stdout = io.StringIO()
        code = main(
            ["moomoo-simulate", "snapshot", "--host", "10.0.0.2"],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 1)
        factory.assert_not_called()
        self.assertIn("loopback", json.loads(stdout.getvalue())["error"])

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_place_requires_confirmation_before_client_construction(self, factory):
        from quantbot.research_cli import main

        stdout = io.StringIO()
        code = main(
            [
                "moomoo-simulate",
                "place",
                "--code",
                "US.AAPL",
                "--side",
                "BUY",
                "--quantity",
                "1",
                "--price",
                "1.00",
            ],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 1)
        factory.assert_not_called()
        self.assertIn(
            "--confirm-simulated-action",
            json.loads(stdout.getvalue())["error"],
        )

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_place_passes_validated_order_to_simulated_client(self, factory):
        from quantbot.research_cli import main

        client = _client()
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            [
                "moomoo-simulate",
                "place",
                "--code",
                "US.AAPL",
                "--side",
                "BUY",
                "--quantity",
                "1",
                "--price",
                "1.00",
                "--confirm-simulated-action",
            ],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 0)
        client.place_limit_order.assert_called_once_with(
            code="US.AAPL",
            side="BUY",
            quantity=1,
            price=1.0,
        )
        self.assertFalse(json.loads(stdout.getvalue())["reconciliation_required"])

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_query_is_read_only_and_does_not_require_confirmation(self, factory):
        from quantbot.research_cli import main

        client = _client()
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            ["moomoo-simulate", "query", "--order-id", "101"],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 0)
        client.query_order.assert_called_once_with("101")

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_cancel_requires_confirmation_before_client_construction(self, factory):
        from quantbot.research_cli import main

        stdout = io.StringIO()
        code = main(
            ["moomoo-simulate", "cancel", "--order-id", "101"],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 1)
        factory.assert_not_called()

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_cancel_acknowledgement_requires_reconciliation_exit_code(self, factory):
        from quantbot.research_cli import main

        client = _client()
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            [
                "moomoo-simulate",
                "cancel",
                "--order-id",
                "101",
                "--confirm-simulated-action",
            ],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 2)
        client.cancel_order.assert_called_once_with("101")
        self.assertTrue(json.loads(stdout.getvalue())["reconciliation_required"])

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_reconcile_requires_and_passes_submission_marker(self, factory):
        from quantbot.research_cli import main

        missing_stdout = io.StringIO()
        missing_code = main(
            ["moomoo-simulate", "reconcile"],
            stdout=missing_stdout,
            environ={},
        )
        self.assertEqual(missing_code, 1)
        factory.assert_not_called()

        client = _client()
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            [
                "moomoo-simulate",
                "reconcile",
                "--submission-marker",
                "STOCKBOT:test",
            ],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 0)
        client.reconcile_submission.assert_called_once_with("STOCKBOT:test")

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_uncertain_submission_returns_reconciliation_exit_code(self, factory):
        from quantbot.research_cli import main
        from quantbot.integrations.moomoo import MoomooSubmissionUncertainError

        client = _client()
        client.place_limit_order.side_effect = MoomooSubmissionUncertainError(
            submission_marker="STOCKBOT:test",
            order={"code": "US.AAPL"},
            cause=TimeoutError("submission outcome is unknown"),
        )
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            [
                "moomoo-simulate",
                "place",
                "--code",
                "US.AAPL",
                "--side",
                "BUY",
                "--quantity",
                "1",
                "--price",
                "1.00",
                "--confirm-simulated-action",
            ],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["reconciliation_required"])
        self.assertEqual(payload["submission_marker"], "STOCKBOT:test")
        self.assertEqual(payload["order"], {"code": "US.AAPL"})

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_uncertain_cancel_returns_reconciliation_exit_code(self, factory):
        from quantbot.research_cli import main
        from quantbot.integrations.moomoo import MoomooCancellationUncertainError

        client = _client()
        client.cancel_order.side_effect = MoomooCancellationUncertainError(
            order_id="101",
            cause=TimeoutError("cancellation outcome is unknown"),
        )
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            [
                "moomoo-simulate",
                "cancel",
                "--order-id",
                "101",
                "--confirm-simulated-action",
            ],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["reconciliation_required"])
        self.assertEqual(payload["order_id"], "101")

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_query_failure_preserves_reconciliation_requirement(self, factory):
        from quantbot.research_cli import main
        from quantbot.integrations.moomoo import MoomooOrderReconciliationError

        client = _client()
        client.query_order.side_effect = MoomooOrderReconciliationError(
            order_id="101",
            cause=TimeoutError("query outcome is unknown"),
        )
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            ["moomoo-simulate", "query", "--order-id", "101"],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["reconciliation_required"])
        self.assertEqual(payload["order_id"], "101")

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_close_failure_is_reported_without_overriding_success(self, factory):
        from quantbot.research_cli import main

        client = _client()
        client.close_error = RuntimeError("close failed")
        factory.return_value = client
        stdout = io.StringIO()
        code = main(
            ["moomoo-simulate", "snapshot"],
            stdout=stdout,
            environ={},
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout.getvalue())["warnings"],
            [{"code": "moomoo_context_close_failed", "detail": "close failed"}],
        )

    @patch("quantbot.cli_commands.moomoo.MoomooSimulatedTradingClient")
    def test_invalid_environment_values_fail_before_side_effect(self, factory):
        from quantbot.research_cli import main

        for environ in (
            {"MOOMOO_OPEND_PORT": "not-a-port"},
            {"MOOMOO_OPEND_PORT": "0"},
            {"MOOMOO_SECURITY_FIRM": "FUTUSECURITIES"},
        ):
            with self.subTest(environ=environ):
                stdout = io.StringIO()
                code = main(
                    ["moomoo-simulate", "snapshot"],
                    stdout=stdout,
                    environ=environ,
                )
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(stdout.getvalue())["ok"])
        factory.assert_not_called()

    def test_parser_exposes_only_moomoo_simulation_authentication_surface(self):
        from quantbot.cli_commands.registry import build_parser

        parser = build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertIn("moomoo-simulate", subparsers.choices)
        self.assertNotIn("futu-simulate", subparsers.choices)
        destinations = {
            action.dest for action in subparsers.choices["moomoo-simulate"]._actions
        }
        self.assertNotIn("trd_env", destinations)
        self.assertNotIn("token", destinations)


if __name__ == "__main__":
    unittest.main()
