"""The CLI must be able to read the configuration it validates.

`load_runtime_config` had no caller anywhere in the package: no command ever
read a TOML file, so every declared key was inert no matter how carefully it
was validated and hashed. These tests pin the resolution chain and the three
keys that depend on it.
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.cli_commands.support import resolve_alpaca_feed, resolve_runtime_config
from quantbot.config_loader import (
    SUPPORTED_BROKERS,
    ExecutionRuntimeConfig,
    NormalRuntimeConfig,
    load_runtime_config,
)
from quantbot.execution import (
    SUPPORTED_BROKERS as EXECUTION_SUPPORTED_BROKERS,
    AlpacaBrokerAdapter,
    IBKRBrokerAdapter,
    broker_adapter_type,
    build_broker_failover_executor,
)
from quantbot.oos_backtest import LEGACY_UNIVERSE_MODE, require_legacy_universe_mode


REPO = Path(__file__).resolve().parents[1]
EXAMPLE = str(REPO / "config" / "quantbot.research.toml")


def _args(**changes):
    values = dict(alpaca_feed="", config="")
    values.update(changes)
    return SimpleNamespace(**values)


class RuntimeConfigResolutionTest(unittest.TestCase):
    def test_no_path_means_no_config(self):
        self.assertIsNone(resolve_runtime_config(_args(), {}))

    def test_the_flag_loads_the_file(self):
        config = resolve_runtime_config(_args(config=EXAMPLE), {})

        self.assertIsNotNone(config)
        self.assertEqual(config.data.daily_feed, "sip")

    def test_the_environment_variable_loads_the_file(self):
        config = resolve_runtime_config(_args(), {"STOCKBOT_CONFIG": EXAMPLE})

        self.assertIsNotNone(config)

    def test_the_flag_beats_the_environment_variable(self):
        config = resolve_runtime_config(
            _args(config=EXAMPLE), {"STOCKBOT_CONFIG": "/nonexistent.toml"}
        )

        self.assertIsNotNone(config)

    def test_a_command_without_the_argument_is_unaffected(self):
        self.assertIsNone(resolve_runtime_config(SimpleNamespace(), {}))


class FeedResolutionTest(unittest.TestCase):
    """`data.daily_feed` and `data.minute_feed` had no consumer."""

    def setUp(self):
        self.config = load_runtime_config(EXAMPLE)

    def test_the_config_supplies_the_feed_when_nothing_overrides(self):
        self.assertEqual(
            resolve_alpaca_feed(_args(), {}, config=self.config), "sip"
        )
        self.assertEqual(
            resolve_alpaca_feed(_args(), {}, config=self.config, minute=True), "sip"
        )

    def test_the_flag_wins(self):
        self.assertEqual(
            resolve_alpaca_feed(_args(alpaca_feed="iex"), {}, config=self.config), "iex"
        )

    def test_the_environment_beats_the_config(self):
        self.assertEqual(
            resolve_alpaca_feed(
                _args(), {"ALPACA_DATA_FEED": "otc"}, config=self.config
            ),
            "otc",
        )

    def test_no_config_and_no_override_yields_no_feed(self):
        self.assertEqual(resolve_alpaca_feed(_args(), {}), "")

    def test_daily_and_minute_feeds_are_read_independently(self):
        config = SimpleNamespace(
            data=SimpleNamespace(daily_feed="sip", minute_feed="iex")
        )

        self.assertEqual(resolve_alpaca_feed(_args(), {}, config=config), "sip")
        self.assertEqual(
            resolve_alpaca_feed(_args(), {}, config=config, minute=True), "iex"
        )


class BrokerIdentityTest(unittest.TestCase):
    """`execution.primary_broker`/`backup_broker` were free strings, unread."""

    def test_the_config_and_execution_registries_agree(self):
        self.assertEqual(set(SUPPORTED_BROKERS), set(EXECUTION_SUPPORTED_BROKERS))

    def test_names_resolve_to_the_implemented_adapters(self):
        self.assertIs(broker_adapter_type("Alpaca"), AlpacaBrokerAdapter)
        self.assertIs(broker_adapter_type("IBKR"), IBKRBrokerAdapter)

    def test_an_undrivable_broker_is_refused(self):
        with self.assertRaisesRegex(ValueError, "unsupported broker"):
            broker_adapter_type("Robinhood")
        with self.assertRaisesRegex(ValueError, "execution.primary_broker"):
            ExecutionRuntimeConfig(primary_broker="Robinhood")

    def test_failover_requires_two_different_brokers(self):
        with self.assertRaisesRegex(ValueError, "different broker"):
            ExecutionRuntimeConfig(primary_broker="Alpaca", backup_broker="Alpaca")

    def test_the_executor_is_built_from_the_configured_identities(self):
        execution = ExecutionRuntimeConfig(primary_broker="IBKR", backup_broker="Alpaca")

        executor = build_broker_failover_executor(execution, object(), object())

        self.assertIsInstance(executor.primary.broker, IBKRBrokerAdapter)
        self.assertIsInstance(executor.backup.broker, AlpacaBrokerAdapter)

    def test_the_shipped_example_names_drivable_brokers(self):
        config = load_runtime_config(EXAMPLE)

        broker_adapter_type(config.execution.primary_broker)
        broker_adapter_type(config.execution.backup_broker)
        self.assertNotEqual(
            config.execution.primary_broker, config.execution.backup_broker
        )


class UniverseModeTest(unittest.TestCase):
    """`normal.universe_mode` was validated and hashed but read by nothing."""

    def test_the_legacy_path_refuses_a_point_in_time_declaration(self):
        with self.assertRaisesRegex(ValueError, "forbids the legacy static"):
            require_legacy_universe_mode("point_in_time")

    def test_the_legacy_path_accepts_its_own_declaration(self):
        self.assertIsNone(require_legacy_universe_mode(LEGACY_UNIVERSE_MODE))

    def test_the_default_declaration_is_point_in_time(self):
        self.assertEqual(NormalRuntimeConfig().universe_mode, "point_in_time")

    def test_the_legacy_mode_is_expressible(self):
        self.assertEqual(
            NormalRuntimeConfig(universe_mode="static_mega_cap").universe_mode,
            "static_mega_cap",
        )

    def test_an_unknown_mode_is_refused(self):
        with self.assertRaisesRegex(ValueError, "normal.universe_mode"):
            NormalRuntimeConfig(universe_mode="whatever")

    def test_the_shipped_example_forbids_the_legacy_universe(self):
        config = load_runtime_config(EXAMPLE)

        with self.assertRaises(ValueError):
            require_legacy_universe_mode(config.normal.universe_mode)


class DeclaredKeysHaveConsumersTest(unittest.TestCase):
    """A key that cannot change behaviour describes a control nobody has.

    This is the audit that found the whole class of defect, kept as a test so
    a newly added key cannot quietly join the dead list.  Entries in
    KNOWN_UNCONSUMED are deliberate and must carry a reason.
    """

    KNOWN_UNCONSUMED = {
        # Read only through the [broker] compatibility shim and the executor
        # factory, which take the value rather than grepping for the name.
        "execution.base_url": "passed to broker clients by deployment wiring",
        "daytrade.live_submission_enabled": "consumed by config validation: true is rejected, live remains closed",
        "portfolio.normal_fraction": "reserved for admitted portfolio deployment; standalone experiment uses explicit initial_equity",
    }

    def test_every_canonical_key_is_either_consumed_or_explained(self):
        import re

        from quantbot.config_loader import _CANONICAL_KEYS

        source = REPO / "src" / "quantbot"
        dead = []
        for section, keys in _CANONICAL_KEYS.items():
            for key in sorted(keys):
                if f"{section}.{key}" in self.KNOWN_UNCONSUMED:
                    continue
                pattern = re.compile(r"\." + re.escape(key) + r"\b")
                consumers = [path for path in source.rglob("*.py")
                             if path.name != "config_loader.py"
                             and not {"ai", "price_action"}.intersection(path.relative_to(source).parts)
                             and pattern.search(path.read_text(encoding="utf-8"))]
                if not consumers:
                    dead.append(f"{section}.{key}")

        self.assertEqual(
            dead,
            [],
            "declared configuration keys with no consumer outside config_loader: "
            + ", ".join(dead),
        )


if __name__ == "__main__":
    unittest.main()
