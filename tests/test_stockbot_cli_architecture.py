"""Architecture contracts for the modular StockBot CLI."""

import argparse
import ast
import inspect
import io
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


EXPECTED_COMMANDS = {
    "preflight",
    "strategy-readiness",
    "schedule",
    "acceptance-schedule",
    "paper-acceptance",
    "paper-run-record",
    "paper-snapshot-record",
    "paper-rollup-snapshots",
    "paper-evidence",
    "paper-status",
    "live-smoke",
    "completion-audit",
    "experiment-acceptance-report",
    "collect-evidence",
    "runtime-evidence",
    "runtime-smoke",
    "backtest-acceptance",
    "oos-backtest-run",
    "historical-replay",
    "normal-experiment-run",
    "daytrade-schedule",
    "daytrade-cache-fill",
    "daytrade-backtest-run",
    "daytrade-optimize-run",
    "daytrade-compare-configs",
    "daytrade-paper-cycle",
    "daytrade-live-cycle",
    "daytrade-status",
    "moomoo-simulate",
    "moomoo-paper-canary",
    "live-soak-record",
    "live-soak-snapshot-record",
    "live-soak-rollup-snapshots",
    "live-soak-evidence",
    "live-soak-status",
    "acceptance-cycle",
}

EXPECTED_MAIN_PARAMETERS = [
    "argv",
    "stdout",
    "environ",
    "import_checker",
    "runtime_smoke_suite_factory",
    "oos_price_loader",
    "daytrade_minute_loader",
    "daytrade_order_submitter",
    "paper_account_value_loader",
    "daytrade_session_loader",
    "normal_experiment_loader",
]


def _decision_nodes(function) -> int:
    tree = ast.parse(inspect.getsource(function))
    return sum(
        isinstance(node, (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler))
        + (len(node.values) - 1 if isinstance(node, ast.BoolOp) else 0)
        for node in ast.walk(tree)
    )


class StockBotCliArchitectureTest(unittest.TestCase):
    def test_registry_has_exactly_36_commands_with_unique_callable_handlers(self):
        from quantbot.cli_commands.registry import COMMAND_SPECS, build_parser

        names = [spec.name for spec in COMMAND_SPECS]
        handlers = [spec.handler for spec in COMMAND_SPECS]
        self.assertEqual(set(names), EXPECTED_COMMANDS)
        self.assertEqual(len(names), 36)
        self.assertEqual(len(set(names)), 36)
        self.assertEqual(len(set(handlers)), 36)
        self.assertTrue(all(callable(handler) for handler in handlers))

        parser = build_parser()
        subparsers = next(
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        )
        self.assertEqual(set(subparsers.choices), EXPECTED_COMMANDS)
        for command, command_parser in subparsers.choices.items():
            self.assertTrue(callable(command_parser.get_default("_handler")), command)

    def test_main_is_a_small_dispatch_composition_root(self):
        from quantbot.research_cli import main

        source = inspect.getsource(main)
        tree = ast.parse(source)
        self.assertLessEqual(len(source.splitlines()), 60)
        self.assertLessEqual(_decision_nodes(main), 3)
        self.assertNotIn("args.command", source)
        self.assertFalse(
            any(
                isinstance(node, ast.Compare)
                and isinstance(node.left, ast.Attribute)
                and node.left.attr == "command"
                for node in ast.walk(tree)
            )
        )

    def test_main_injection_signature_is_unchanged(self):
        from quantbot.research_cli import main

        self.assertEqual(list(inspect.signature(main).parameters), EXPECTED_MAIN_PARAMETERS)

    def test_all_commands_have_small_named_handlers(self):
        from quantbot.cli_commands.registry import COMMAND_SPECS

        for spec in COMMAND_SPECS:
            with self.subTest(command=spec.name):
                self.assertEqual(spec.handler.__name__, "handle_" + spec.name.replace("-", "_"))
                self.assertLessEqual(_decision_nodes(spec.handler), 12)

    def test_command_modules_never_import_stockbot_cli(self):
        package = ROOT / "src" / "quantbot" / "cli_commands"
        for path in package.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    imported = [node.module or ""]
                else:
                    continue
                self.assertNotIn("quantbot.cli", imported, str(path))

    def test_parser_registration_helpers_have_no_duplicate_bodies(self):
        registry_path = (
            ROOT / "src" / "quantbot" / "cli_commands" / "registry.py"
        )
        tree = ast.parse(registry_path.read_text(encoding="utf-8"))
        bodies = {}
        duplicates = []
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("_"):
                continue
            normalized = ast.dump(
                ast.Module(body=node.body, type_ignores=[]),
                include_attributes=False,
            )
            if normalized in bodies:
                duplicates.append((bodies[normalized], node.name))
            else:
                bodies[normalized] = node.name

        self.assertEqual(duplicates, [])

    def test_live_submit_is_rejected_before_any_loader_or_submitter(self):
        from quantbot.research_cli import main

        calls = []

        def forbidden(*_args, **_kwargs):
            calls.append("called")
            raise AssertionError("live submit rejection must happen before side effects")

        stdout = io.StringIO()
        exit_code = main(
            [
                "daytrade-live-cycle",
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-02",
                "--submit-orders",
            ],
            stdout=stdout,
            daytrade_minute_loader=forbidden,
            daytrade_order_submitter=forbidden,
            daytrade_session_loader=forbidden,
        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(json.loads(stdout.getvalue())["error"], "live_trading_disabled")


if __name__ == "__main__":
    unittest.main()
