"""CLI contracts for the Moomoo simulation-only engineering Paper canary."""

import argparse
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


DEFAULT_SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "META",
    "GOOGL",
    "AMD",
    "TSLA",
    "AVGO",
    "NFLX",
)


def _client() -> MagicMock:
    client = MagicMock()
    client.close_error = None
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    return client


class StockBotMoomooPaperCliTest(unittest.TestCase):
    def test_confirmation_is_required_before_client_or_runtime_construction(self):
        from quantbot.research_cli import main

        stdout = io.StringIO()
        with (
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient"
            ) as client_factory,
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary"
            ) as canary_factory,
        ):
            code = main(
                ["moomoo-paper-canary"],
                stdout=stdout,
                environ={},
            )

        self.assertEqual(code, 1)
        client_factory.assert_not_called()
        canary_factory.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["environment"], "SIMULATE")
        self.assertIn("--confirm-simulated-action", payload["error"])

    def test_default_run_is_explicit_simulation_and_requires_flat_completion(self):
        from quantbot.research_cli import main

        client = _client()
        canary = MagicMock()
        canary.run.return_value = {
            "status": "completed_flat",
            "flat": True,
            "reconciliation_required": False,
        }
        stdout = io.StringIO()
        with (
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient",
                return_value=client,
            ) as client_factory,
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooPaperCanaryConfig"
            ) as config_factory,
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary",
                return_value=canary,
            ) as canary_factory,
        ):
            config = config_factory.return_value
            code = main(
                ["moomoo-paper-canary", "--confirm-simulated-action"],
                stdout=stdout,
                environ={},
            )

        self.assertEqual(code, 0)
        client_factory.assert_called_once_with(
            host="127.0.0.1",
            port=11111,
            acc_id=None,
            security_firm="FUTUINC",
            trd_env="SIMULATE",
        )
        config_factory.assert_called_once_with(
            symbols=DEFAULT_SYMBOLS,
            gross_equity_fraction=0.10,
            liquidation_lead_minutes=10,
            poll_interval_seconds=5.0,
            max_quote_age_seconds=5.0,
            marketable_limit_buffer_bps=50.0,
        )
        _, kwargs = canary_factory.call_args
        self.assertIs(canary_factory.call_args.args[0], client)
        self.assertEqual(
            kwargs["state_path"],
            Path("data/moomoo_paper_canary_state.json"),
        )
        self.assertEqual(
            kwargs["lock_path"],
            Path("data/moomoo_paper_canary_state.lock"),
        )
        self.assertIs(kwargs["config"], config)
        canary.run.assert_called_once_with()
        canary.preflight.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["flat"])
        self.assertFalse(payload["preflight_only"])

    def test_preflight_only_never_calls_run_and_emits_parseable_result(self):
        from quantbot.research_cli import main

        client = _client()
        canary = MagicMock()
        canary.preflight.return_value = {
            "ok": True,
            "status": "preflight_ready",
            "orders_submitted": 0,
        }
        stdout = io.StringIO()
        with (
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient",
                return_value=client,
            ),
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary",
                return_value=canary,
            ),
        ):
            code = main(
                [
                    "moomoo-paper-canary",
                    "--preflight-only",
                    "--confirm-simulated-action",
                ],
                stdout=stdout,
                environ={},
            )

        self.assertEqual(code, 0)
        canary.preflight.assert_called_once_with()
        canary.run.assert_not_called()
        client.place_limit_order.assert_not_called()
        client.cancel_order.assert_not_called()
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["preflight_only"])
        self.assertEqual(payload["result"]["orders_submitted"], 0)

    def test_runtime_events_and_final_result_are_valid_ndjson(self):
        from quantbot.research_cli import main

        client = _client()
        canary = MagicMock()
        canary.run.return_value = {"flat": True, "status": "completed_flat"}

        def make_canary(*_args, **kwargs):
            kwargs["event_sink"]({"kind": "session_started", "sequence": 1})
            return canary

        stdout = io.StringIO()
        with (
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient",
                return_value=client,
            ),
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary",
                side_effect=make_canary,
            ),
        ):
            code = main(
                ["moomoo-paper-canary", "--confirm-simulated-action"],
                stdout=stdout,
                environ={},
            )

        self.assertEqual(code, 0)
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([record["type"] for record in records], ["event", "result"])
        self.assertEqual(records[0]["event"]["kind"], "session_started")
        self.assertTrue(records[-1]["flat"])

    def test_halted_or_unreconciled_result_uses_exit_code_two(self):
        from quantbot.research_cli import main

        for result in (
            {"flat": False, "halted": True, "reconciliation_required": False},
            {"flat": False, "halted": False, "reconciliation_required": True},
        ):
            with self.subTest(result=result):
                client = _client()
                canary = MagicMock()
                canary.run.return_value = result
                stdout = io.StringIO()
                with (
                    patch(
                        "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient",
                        return_value=client,
                    ),
                    patch(
                        "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary",
                        return_value=canary,
                    ),
                ):
                    code = main(
                        ["moomoo-paper-canary", "--confirm-simulated-action"],
                        stdout=stdout,
                        environ={},
                    )
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_runtime_halt_is_structured_and_uses_exit_code_two(self):
        from quantbot.research_cli import main
        from quantbot.moomoo_paper import MoomooPaperCanaryHalt

        client = _client()
        canary = MagicMock()
        canary.run.side_effect = MoomooPaperCanaryHalt(
            "durable state requires reconciliation",
            payload={"status": "RECOVERY_REQUIRED", "run_id": "run-1"},
            reconciliation_required=True,
        )
        stdout = io.StringIO()
        with (
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient",
                return_value=client,
            ),
            patch(
                "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary",
                return_value=canary,
            ),
        ):
            code = main(
                ["moomoo-paper-canary", "--confirm-simulated-action"],
                stdout=stdout,
                environ={},
            )

        self.assertEqual(code, 2)
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["halted"])
        self.assertTrue(payload["reconciliation_required"])
        self.assertEqual(
            payload["details"],
            {"status": "RECOVERY_REQUIRED", "run_id": "run-1"},
        )

    def test_status_text_or_malformed_preflight_cannot_bypass_flat_proof(self):
        from quantbot.research_cli import main

        cases = (
            (
                [],
                {"flat": False, "status": "completed_flat"},
            ),
            (
                ["--preflight-only"],
                {"status": "preflight_ready"},
            ),
            (
                ["--preflight-only"],
                {"ok": False, "flat": True, "status": "preflight_ready"},
            ),
        )
        for extra_args, result in cases:
            with self.subTest(extra_args=extra_args, result=result):
                client = _client()
                canary = MagicMock()
                if extra_args:
                    canary.preflight.return_value = result
                else:
                    canary.run.return_value = result
                stdout = io.StringIO()
                with (
                    patch(
                        "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient",
                        return_value=client,
                    ),
                    patch(
                        "quantbot.cli_commands.moomoo_paper.MoomooPaperCanary",
                        return_value=canary,
                    ),
                ):
                    code = main(
                        [
                            "moomoo-paper-canary",
                            *extra_args,
                            "--confirm-simulated-action",
                        ],
                        stdout=stdout,
                        environ={},
                    )
                self.assertEqual(code, 2)
                self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_invalid_local_config_fails_before_client_construction(self):
        from quantbot.research_cli import main

        for extra_args in (
            ("--capital-fraction", "nan"),
            ("--capital-fraction", "1.01"),
            ("--symbols", "AAPL,AAPL"),
            ("--poll-seconds", "0"),
            ("--state-file", ""),
            ("--lock-file", ""),
        ):
            with self.subTest(extra_args=extra_args):
                stdout = io.StringIO()
                with patch(
                    "quantbot.cli_commands.moomoo_paper.MoomooSimulatedTradingClient"
                ) as client_factory:
                    code = main(
                        [
                            "moomoo-paper-canary",
                            "--confirm-simulated-action",
                            *extra_args,
                        ],
                        stdout=stdout,
                        environ={},
                    )
                self.assertEqual(code, 1)
                client_factory.assert_not_called()
                self.assertFalse(json.loads(stdout.getvalue())["ok"])

    def test_parser_has_no_real_account_or_unbounded_risk_switches(self):
        from quantbot.cli_commands.registry import build_parser

        parser = build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        command_parser = subparsers.choices["moomoo-paper-canary"]
        actions = {action.dest: action for action in command_parser._actions}
        option_strings = {
            option
            for action in command_parser._actions
            for option in action.option_strings
        }
        self.assertEqual(
            option_strings,
            {
                "-h",
                "--help",
                "--symbols",
                "--capital-fraction",
                "--flatten-minutes-before-close",
                "--poll-seconds",
                "--max-quote-age-seconds",
                "--marketable-limit-buffer-bps",
                "--state-file",
                "--lock-file",
                "--host",
                "--port",
                "--acc-id",
                "--security-firm",
                "--preflight-only",
                "--confirm-simulated-action",
            },
        )
        self.assertEqual(
            actions["state_file"].default,
            "data/moomoo_paper_canary_state.json",
        )
        self.assertEqual(
            actions["lock_file"].default,
            "data/moomoo_paper_canary_state.lock",
        )
        self.assertEqual(actions["symbols"].default, ",".join(DEFAULT_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
