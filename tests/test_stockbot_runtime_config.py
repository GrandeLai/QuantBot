import dataclasses
import json
import math
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config_loader import (
    BrokerRuntimeConfig,
    DataRuntimeConfig,
    DayTradeRuntimeConfig,
    ExecutionRuntimeConfig,
    NormalRuntimeConfig,
    PortfolioRuntimeConfig,
    RiskRuntimeConfig,
    RuntimeConfig,
    ValidationRuntimeConfig,
    load_runtime_config,
)


CANONICAL_CONFIG = """
[data]
root = "/var/lib/quantbot"
daily_feed = "iex"
minute_feed = "sip"
schema_version = 2
require_complete_coverage = true

[normal]
benchmark = "QQQ"
universe_mode = "point_in_time"
execution_price = "next_open"
cost_bps_per_side = 4.5
lookback_days = 126
top_n = 8
residual_estimation_months = 48
residual_skip_months = 1
covariance_shrinkage = 0.4
no_trade_band = 0.003
turnover_penalty = 0.6
exit_rank_buffer = 4
max_adv_participation = 0.008
momentum_rebound_multiplier = 0.45

[daytrade]
entry_start = "09:50"
entry_end = "10:25"
cumulative_vwap = false
side = "short"
vwap_hysteresis_bps = 7.0
stop_basis = "opening_range"
atr_stop_multiple = 0.6
opening_range_stop_multiple = 0.8
min_opening_range_atr = 0.2
max_opening_range_atr = 1.2
min_relative_volume = 1.8
min_directional_gap = 0.002
max_directional_gap = 0.04
min_premarket_volume = 50000
min_directional_spy_return = 0.001
min_directional_qqq_return = 0.0015
min_directional_sector_strength = 0.002
max_short_gap_abs = 0.04
max_portfolio_heat_fraction = 0.018
correlation_penalty = 0.4
cost_bps_per_side = 6.0
max_volume_participation = 0.05
live_submission_enabled = false

[portfolio]
initial_cash = 250000
normal_fraction = 0.8
daytrade_fraction = 0.2

[risk]
max_single_stock_weight = 0.04
max_sector_weight = 0.20
max_portfolio_vol_annual = 0.12
max_normal_drawdown = 0.14
max_daytrade_drawdown = 0.04
max_market_beta_exposure = 0.85

[execution]
primary_broker = "Alpaca"
backup_broker = "IBKR"
base_url = "https://paper-api.alpaca.markets"
slippage_bps_per_side = 1.5

[validation]
normal_min_net_excess_annual = 0.06
normal_min_sharpe = 1.3
daytrade_min_oos_months = 30
daytrade_min_oos_trades = 400
min_paper_sessions = 75
"""


class StockBotRuntimeConfigTest(unittest.TestCase):
    def _load(self, text: str, environ=None):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "quantbot.toml"
            path.write_text(text, encoding="utf-8")
            return load_runtime_config(path, environ=environ)

    def test_loads_all_canonical_sections_with_typed_values(self):
        config = self._load(CANONICAL_CONFIG, environ={})

        self.assertEqual(config.data.root, Path("/var/lib/quantbot"))
        self.assertEqual(config.data.daily_feed, "iex")
        self.assertTrue(config.data.require_complete_coverage)
        self.assertEqual(config.normal.benchmark, "QQQ")
        self.assertEqual(config.normal.lookback_days, 126)
        self.assertEqual(config.normal.residual_estimation_months, 48)
        self.assertEqual(config.normal.covariance_shrinkage, 0.4)
        self.assertEqual(config.normal.max_adv_participation, 0.008)
        self.assertEqual(config.daytrade.entry_start, "09:50")
        self.assertFalse(config.daytrade.cumulative_vwap)
        self.assertEqual(config.daytrade.side, "short")
        self.assertEqual(config.daytrade.stop_basis, "opening_range")
        self.assertEqual(config.daytrade.min_relative_volume, 1.8)
        self.assertEqual(config.daytrade.max_portfolio_heat_fraction, 0.018)
        self.assertEqual(config.portfolio.initial_cash, 250000.0)
        self.assertEqual(config.portfolio.normal_fraction, 0.8)
        self.assertEqual(config.risk.max_sector_weight, 0.20)
        self.assertEqual(config.risk.max_market_beta_exposure, 0.85)
        self.assertEqual(config.execution.primary_broker, "Alpaca")
        self.assertEqual(config.execution.slippage_bps_per_side, 1.5)
        self.assertEqual(config.validation.daytrade_min_oos_trades, 400)

    def test_empty_toml_uses_trustworthy_defaults(self):
        config = self._load("", environ={})

        self.assertEqual(config.data.root, Path("data"))
        self.assertEqual(config.data.daily_feed, "sip")
        self.assertEqual(config.data.minute_feed, "sip")
        self.assertEqual(config.data.schema_version, 1)
        self.assertTrue(config.data.require_complete_coverage)
        self.assertEqual(config.normal.benchmark, "SPY")
        self.assertEqual(config.normal.universe_mode, "point_in_time")
        self.assertEqual(config.normal.execution_price, "next_open")
        self.assertEqual(config.normal.cost_bps_per_side, 5.0)
        self.assertEqual(config.normal.lookback_days, 252)
        self.assertEqual(config.normal.top_n, 20)
        self.assertEqual(config.normal.residual_estimation_months, 36)
        self.assertEqual(config.normal.residual_skip_months, 1)
        self.assertEqual(config.normal.covariance_shrinkage, 0.50)
        self.assertEqual(config.normal.no_trade_band, 0.0025)
        self.assertEqual(config.normal.turnover_penalty, 0.50)
        self.assertEqual(config.normal.exit_rank_buffer, 5)
        self.assertEqual(config.normal.max_adv_participation, 0.01)
        self.assertEqual(config.normal.momentum_rebound_multiplier, 0.50)
        self.assertEqual(config.daytrade.entry_start, "09:45")
        self.assertEqual(config.daytrade.entry_end, "10:30")
        self.assertTrue(config.daytrade.cumulative_vwap)
        self.assertEqual(config.daytrade.side, "long")
        self.assertEqual(config.daytrade.vwap_hysteresis_bps, 5.0)
        self.assertEqual(config.daytrade.stop_basis, "atr")
        self.assertEqual(config.daytrade.atr_stop_multiple, 0.75)
        self.assertEqual(config.daytrade.opening_range_stop_multiple, 0.75)
        self.assertEqual(config.daytrade.min_opening_range_atr, 0.25)
        self.assertEqual(config.daytrade.max_opening_range_atr, 1.5)
        self.assertEqual(config.daytrade.min_relative_volume, 1.5)
        self.assertEqual(config.daytrade.max_short_gap_abs, 0.05)
        self.assertEqual(config.daytrade.max_portfolio_heat_fraction, 0.02)
        self.assertEqual(config.daytrade.correlation_penalty, 0.50)
        self.assertEqual(config.daytrade.cost_bps_per_side, 5.0)
        self.assertEqual(config.daytrade.max_volume_participation, 0.10)
        self.assertFalse(config.daytrade.live_submission_enabled)
        self.assertEqual(config.portfolio.initial_cash, 100000.0)
        self.assertEqual(config.portfolio.normal_fraction, 0.90)
        self.assertEqual(config.portfolio.daytrade_fraction, 0.10)
        self.assertEqual(config.risk.max_single_stock_weight, 0.05)
        self.assertEqual(config.risk.max_sector_weight, 0.25)
        self.assertEqual(config.risk.max_portfolio_vol_annual, 0.15)
        self.assertEqual(config.risk.max_normal_drawdown, 0.15)
        self.assertEqual(config.risk.max_daytrade_drawdown, 0.05)
        self.assertEqual(config.risk.max_market_beta_exposure, 1.0)
        self.assertEqual(config.execution.primary_broker, "Alpaca")
        self.assertEqual(config.execution.backup_broker, "IBKR")
        self.assertEqual(config.execution.base_url, "https://paper-api.alpaca.markets")
        self.assertEqual(config.execution.slippage_bps_per_side, 0.0)
        self.assertEqual(config.validation.normal_min_net_excess_annual, 0.05)
        self.assertEqual(config.validation.normal_min_sharpe, 1.2)
        self.assertEqual(config.validation.daytrade_min_oos_months, 24)
        self.assertEqual(config.validation.daytrade_min_oos_trades, 300)
        self.assertEqual(config.validation.min_paper_sessions, 60)

    def test_runtime_and_nested_sections_are_immutable(self):
        config = self._load("", environ={})

        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.data = config.data
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.data.root = Path("other")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.execution.primary_broker = "Other"

    def test_stockbot_data_root_environment_overrides_toml_without_entering_payload_as_secret(self):
        config = self._load(
            '[data]\nroot = "data"\n',
            environ={"STOCKBOT_DATA_ROOT": "/external/pit-quantbot"},
        )

        self.assertEqual(config.data.root, Path("/external/pit-quantbot"))
        self.assertEqual(config.public_payload()["data"]["root"], "/external/pit-quantbot")

    def test_environment_secrets_are_runtime_only_and_redacted(self):
        config = self._load(
            "",
            environ={"ALPACA_API_KEY": "visible-only-at-runtime", "ALPACA_SECRET_KEY": "super-secret"},
        )

        self.assertFalse(hasattr(config.execution, "api_key"))
        self.assertFalse(hasattr(config.execution, "secret_key"))
        self.assertEqual(config.broker.api_key, "visible-only-at-runtime")
        self.assertEqual(config.broker.secret_key, "super-secret")
        rendered = repr(config)
        self.assertNotIn("visible-only-at-runtime", rendered)
        self.assertNotIn("super-secret", rendered)
        payload = config.public_payload()
        self.assertNotIn("api_key", repr(payload))
        self.assertNotIn("secret_key", repr(payload))
        self.assertEqual(set(payload), {"data", "normal", "daytrade", "portfolio", "risk", "execution", "validation"})
        self.assertEqual(payload["data"]["root"], "data")
        self.assertEqual(payload["normal"]["residual_estimation_months"], 36)
        self.assertEqual(payload["normal"]["covariance_shrinkage"], 0.50)
        self.assertEqual(payload["daytrade"]["stop_basis"], "atr")
        self.assertEqual(payload["daytrade"]["min_relative_volume"], 1.5)
        self.assertEqual(payload["risk"]["max_market_beta_exposure"], 1.0)

    def test_generic_serializers_fail_closed_instead_of_exposing_secrets(self):
        config = self._load(
            "",
            environ={"ALPACA_API_KEY": "serializer-key", "ALPACA_SECRET_KEY": "serializer-secret"},
        )
        broker = config.broker

        self.assertFalse(dataclasses.is_dataclass(config))
        self.assertFalse(dataclasses.is_dataclass(broker))
        with self.assertRaises(TypeError):
            dataclasses.asdict(config)
        with self.assertRaises(TypeError):
            dataclasses.asdict(broker)
        with self.assertRaises((AttributeError, TypeError)):
            json.dumps(config, default=lambda value: value.__dict__)
        with self.assertRaises((AttributeError, TypeError)):
            json.dumps(broker, default=lambda value: value.__dict__)
        self.assertNotIn("serializer-key", repr(config))
        self.assertNotIn("serializer-secret", repr(config))
        self.assertNotIn("serializer-key", repr(broker))
        self.assertNotIn("serializer-secret", repr(broker))

    def test_none_environment_reads_process_environment(self):
        with patch.dict(
            os.environ,
            {"ALPACA_API_KEY": "process-key", "ALPACA_SECRET_KEY": "process-secret"},
            clear=True,
        ):
            config = self._load("")

        self.assertEqual(config.broker.api_key, "process-key")
        self.assertEqual(config.broker.secret_key, "process-secret")

    def test_explicit_empty_environment_does_not_fall_back_to_process_environment(self):
        with patch.dict(
            os.environ,
            {"ALPACA_API_KEY": "process-key", "ALPACA_SECRET_KEY": "process-secret"},
            clear=True,
        ):
            config = self._load("", environ={})

        self.assertEqual(config.broker.api_key, "")
        self.assertEqual(config.broker.secret_key, "")

    def test_rejects_non_mapping_environment_with_stable_error(self):
        with self.assertRaisesRegex(ValueError, r"^environment"):
            self._load("", environ=[("ALPACA_API_KEY", "key")])

    def test_rejects_non_string_environment_secret_with_full_path(self):
        with self.assertRaisesRegex(ValueError, r"environment\.ALPACA_API_KEY"):
            self._load("", environ={"ALPACA_API_KEY": 123})

    def test_rejects_secret_keys_anywhere_in_toml(self):
        invalid_documents = (
            ("[execution]\napi_key = \"not-allowed\"\n", "execution.api_key"),
            ("[broker]\nsecret_key = \"not-allowed\"\n", "broker.secret_key"),
            ("[data.credentials]\napi_key = \"not-allowed\"\n", "data.credentials.api_key"),
        )

        for document, path in invalid_documents:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_rejects_unknown_top_level_section_with_full_path(self):
        with self.assertRaisesRegex(ValueError, "unknown_section"):
            self._load("[unknown_section]\nvalue = 1\n", environ={})

    def test_rejects_unknown_nested_key_with_full_path(self):
        with self.assertRaisesRegex(ValueError, r"normal\.mystery"):
            self._load("[normal]\nmystery = 1\n", environ={})

    def test_rejects_wrong_scalar_types_including_bool_as_number(self):
        invalid_documents = (
            ("[data]\nroot = 1\n", "data.root"),
            ("[data]\nrequire_complete_coverage = 1\n", "data.require_complete_coverage"),
            ("[normal]\ntop_n = true\n", "normal.top_n"),
            ("[data]\nschema_version = true\n", "data.schema_version"),
            ("[portfolio]\ninitial_cash = true\n", "portfolio.initial_cash"),
            ("[daytrade]\ncumulative_vwap = \"true\"\n", "daytrade.cumulative_vwap"),
        )

        for document, path in invalid_documents:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_rejects_non_finite_numeric_values(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, r"normal\.cost_bps_per_side"):
                    self._load("[normal]\ncost_bps_per_side = %s\n" % value, environ={})

    def test_rejects_out_of_range_values_with_full_path(self):
        invalid_documents = (
            ("[normal]\ncost_bps_per_side = -0.1\n", "normal.cost_bps_per_side"),
            ("[execution]\nslippage_bps_per_side = -0.1\n", "execution.slippage_bps_per_side"),
            ("[daytrade]\nmax_volume_participation = 0\n", "daytrade.max_volume_participation"),
            ("[daytrade]\nmax_volume_participation = 1.1\n", "daytrade.max_volume_participation"),
            ("[daytrade]\ncorrelation_penalty = 1.1\n", "daytrade.correlation_penalty"),
            ("[daytrade]\nmax_portfolio_heat_fraction = 0\n", "daytrade.max_portfolio_heat_fraction"),
            ("[normal]\ntop_n = 0\n", "normal.top_n"),
            ("[normal]\nlookback_days = -1\n", "normal.lookback_days"),
            ("[data]\nschema_version = 0\n", "data.schema_version"),
            ("[portfolio]\ninitial_cash = 0\n", "portfolio.initial_cash"),
            ("[risk]\nmax_single_stock_weight = 1.1\n", "risk.max_single_stock_weight"),
            ("[validation]\ndaytrade_min_oos_months = 0\n", "validation.daytrade_min_oos_months"),
        )

        for document, path in invalid_documents:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_rejects_invalid_daytrade_hypothesis_configuration(self):
        invalid_documents = (
            ("[daytrade]\nside = \"both\"\n", "daytrade.side"),
            ("[daytrade]\nstop_basis = \"percent\"\n", "daytrade.stop_basis"),
            (
                "[daytrade]\nmin_opening_range_atr = 2\nmax_opening_range_atr = 1\n",
                "daytrade.max_opening_range_atr",
            ),
            (
                "[daytrade]\nmin_directional_gap = 0.1\nmax_directional_gap = 0.01\n",
                "daytrade.max_directional_gap",
            ),
        )
        for document, path in invalid_documents:
            with self.subTest(path=path), self.assertRaisesRegex(
                ValueError, path.replace(".", r"\.")
            ):
                self._load(document, environ={})

    def test_rejects_integer_too_large_for_finite_runtime_number(self):
        huge_integer = "9" * 400

        with self.assertRaisesRegex(ValueError, r"portfolio\.initial_cash"):
            self._load("[portfolio]\ninitial_cash = %s\n" % huge_integer, environ={})

    def test_rejects_empty_required_strings_with_full_path(self):
        invalid_documents = (
            ("[data]\ndaily_feed = \" \"\n", "data.daily_feed"),
            ("[data]\nminute_feed = \"\"\n", "data.minute_feed"),
            ("[normal]\nbenchmark = \"\"\n", "normal.benchmark"),
            ("[execution]\nprimary_broker = \"\"\n", "execution.primary_broker"),
            ("[execution]\nbase_url = \" \"\n", "execution.base_url"),
        )

        for document, path in invalid_documents:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_rejects_invalid_portfolio_fraction_sum(self):
        with self.assertRaisesRegex(ValueError, r"portfolio\.normal_fraction"):
            self._load(
                "[portfolio]\nnormal_fraction = 0.8\ndaytrade_fraction = 0.1\n",
                environ={},
            )

    def test_rejects_negative_portfolio_fraction(self):
        with self.assertRaisesRegex(ValueError, r"portfolio\.daytrade_fraction"):
            self._load(
                "[portfolio]\nnormal_fraction = 1.1\ndaytrade_fraction = -0.1\n",
                environ={},
            )

    def test_rejects_invalid_or_reversed_daytrade_times(self):
        invalid_documents = (
            ("[daytrade]\nentry_start = \"9:45\"\n", "daytrade.entry_start"),
            ("[daytrade]\nentry_end = \"24:00\"\n", "daytrade.entry_end"),
            ("[daytrade]\nentry_start = \"10:30\"\nentry_end = \"10:30\"\n", "daytrade.entry_start"),
            ("[daytrade]\nentry_start = \"11:00\"\nentry_end = \"10:30\"\n", "daytrade.entry_start"),
        )

        for document, path in invalid_documents:
            with self.subTest(document=document):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_rejects_configuration_that_weakens_safety_policy(self):
        invalid_documents = (
            ("[data]\nrequire_complete_coverage = false\n", "data.require_complete_coverage"),
            ("[daytrade]\nlive_submission_enabled = true\n", "daytrade.live_submission_enabled"),
            (
                "[validation]\nnormal_min_net_excess_annual = 0.049\n",
                "validation.normal_min_net_excess_annual",
            ),
            ("[validation]\nnormal_min_sharpe = 1.19\n", "validation.normal_min_sharpe"),
            ("[validation]\ndaytrade_min_oos_months = 23\n", "validation.daytrade_min_oos_months"),
            ("[validation]\ndaytrade_min_oos_trades = 299\n", "validation.daytrade_min_oos_trades"),
            ("[validation]\nmin_paper_sessions = 59\n", "validation.min_paper_sessions"),
        )

        for document, path in invalid_documents:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_rejects_unsupported_normal_modes_with_full_path(self):
        invalid_documents = (
            ("[normal]\nuniverse_mode = \"current_winners\"\n", "normal.universe_mode"),
            ("[normal]\nexecution_price = \"same_close\"\n", "normal.execution_price"),
        )

        for document, path in invalid_documents:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    self._load(document, environ={})

    def test_all_public_sections_enforce_invariants_at_direct_construction(self):
        invalid_constructors = (
            (lambda: DataRuntimeConfig(root="data"), "data.root"),
            (lambda: DataRuntimeConfig(daily_feed=""), "data.daily_feed"),
            (lambda: DataRuntimeConfig(schema_version=True), "data.schema_version"),
            (lambda: DataRuntimeConfig(require_complete_coverage=False), "data.require_complete_coverage"),
            (lambda: NormalRuntimeConfig(universe_mode="current"), "normal.universe_mode"),
            (lambda: NormalRuntimeConfig(execution_price="same_close"), "normal.execution_price"),
            (lambda: NormalRuntimeConfig(cost_bps_per_side=math.nan), "normal.cost_bps_per_side"),
            (lambda: NormalRuntimeConfig(lookback_days=True), "normal.lookback_days"),
            (lambda: NormalRuntimeConfig(covariance_shrinkage=1.1), "normal.covariance_shrinkage"),
            (lambda: NormalRuntimeConfig(residual_estimation_months=12), "normal.residual_estimation_months"),
            (lambda: DayTradeRuntimeConfig(entry_start="9:45"), "daytrade.entry_start"),
            (
                lambda: DayTradeRuntimeConfig(entry_start="10:30", entry_end="10:30"),
                "daytrade.entry_start",
            ),
            (lambda: DayTradeRuntimeConfig(cost_bps_per_side=math.inf), "daytrade.cost_bps_per_side"),
            (lambda: DayTradeRuntimeConfig(max_volume_participation=0.0), "daytrade.max_volume_participation"),
            (lambda: DayTradeRuntimeConfig(live_submission_enabled=True), "daytrade.live_submission_enabled"),
            (lambda: PortfolioRuntimeConfig(initial_cash=True), "portfolio.initial_cash"),
            (
                lambda: PortfolioRuntimeConfig(normal_fraction=0.8, daytrade_fraction=0.1),
                "portfolio.normal_fraction",
            ),
            (lambda: PortfolioRuntimeConfig(daytrade_fraction=-0.1), "portfolio.daytrade_fraction"),
            (lambda: RiskRuntimeConfig(max_single_stock_weight=True), "risk.max_single_stock_weight"),
            (lambda: RiskRuntimeConfig(max_sector_weight=math.nan), "risk.max_sector_weight"),
            (lambda: RiskRuntimeConfig(max_portfolio_vol_annual=-0.1), "risk.max_portfolio_vol_annual"),
            (lambda: RiskRuntimeConfig(max_market_beta_exposure=0), "risk.max_market_beta_exposure"),
            (lambda: ExecutionRuntimeConfig(primary_broker=""), "execution.primary_broker"),
            (lambda: ExecutionRuntimeConfig(backup_broker=1), "execution.backup_broker"),
            (lambda: ExecutionRuntimeConfig(slippage_bps_per_side=True), "execution.slippage_bps_per_side"),
            (
                lambda: ValidationRuntimeConfig(normal_min_net_excess_annual=0.049),
                "validation.normal_min_net_excess_annual",
            ),
            (lambda: ValidationRuntimeConfig(normal_min_sharpe=math.inf), "validation.normal_min_sharpe"),
            (
                lambda: ValidationRuntimeConfig(daytrade_min_oos_months=True),
                "validation.daytrade_min_oos_months",
            ),
            (lambda: ValidationRuntimeConfig(min_paper_sessions=59), "validation.min_paper_sessions"),
        )

        for constructor, path in invalid_constructors:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ValueError, path.replace(".", r"\.")):
                    constructor()

    def test_runtime_config_rejects_wrong_section_types(self):
        valid_sections = {
            "data": DataRuntimeConfig(),
            "normal": NormalRuntimeConfig(),
            "daytrade": DayTradeRuntimeConfig(),
            "portfolio": PortfolioRuntimeConfig(),
            "risk": RiskRuntimeConfig(),
            "execution": ExecutionRuntimeConfig(),
            "validation": ValidationRuntimeConfig(),
        }

        for section_name in valid_sections:
            with self.subTest(section=section_name):
                invalid_sections = dict(valid_sections)
                invalid_sections[section_name] = object()
                with self.assertRaisesRegex(ValueError, section_name):
                    RuntimeConfig(**invalid_sections)

    def test_runtime_config_rejects_section_subclass_that_bypasses_post_init(self):
        class UnsafeDataRuntimeConfig(DataRuntimeConfig):
            def __post_init__(self):
                pass

        unsafe_data = UnsafeDataRuntimeConfig(root="not-a-path")

        with self.assertRaisesRegex(ValueError, r"data"):
            RuntimeConfig(
                data=unsafe_data,
                normal=NormalRuntimeConfig(),
                daytrade=DayTradeRuntimeConfig(),
                portfolio=PortfolioRuntimeConfig(),
                risk=RiskRuntimeConfig(),
                execution=ExecutionRuntimeConfig(),
                validation=ValidationRuntimeConfig(),
            )

    def test_legacy_broker_facade_validates_inputs_and_hides_direct_secrets(self):
        broker = BrokerRuntimeConfig(
            "Alpaca",
            "https://paper.example",
            "direct-key",
            "direct-secret",
            "IBKR",
        )

        self.assertEqual(broker.api_key, "direct-key")
        self.assertEqual(broker.secret_key, "direct-secret")
        self.assertNotIn("direct-key", repr(broker))
        self.assertNotIn("direct-secret", repr(broker))
        with self.assertRaisesRegex(ValueError, r"broker\.primary"):
            BrokerRuntimeConfig("", "https://paper.example")
        with self.assertRaisesRegex(ValueError, r"environment\.ALPACA_API_KEY"):
            BrokerRuntimeConfig("Alpaca", "https://paper.example", api_key=123)

    def test_legacy_broker_section_maps_to_execution_and_read_only_view(self):
        # The names must be brokers the package can actually drive; the point
        # of this test is the [broker] -> [execution] mapping, not the strings.
        config = self._load(
            """
[broker]
primary = "IBKR"
backup = "Alpaca"
base_url = "https://legacy.example"
""",
            environ={"ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret"},
        )

        self.assertEqual(config.execution.primary_broker, "IBKR")
        self.assertEqual(config.execution.backup_broker, "Alpaca")
        self.assertEqual(config.execution.base_url, "https://legacy.example")
        self.assertEqual(config.broker.primary, config.execution.primary_broker)
        self.assertEqual(config.broker.backup, config.execution.backup_broker)
        self.assertEqual(config.broker.base_url, config.execution.base_url)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            config.broker.primary = "mutated"

    def test_legacy_broker_config_type_preserves_original_constructor(self):
        broker = BrokerRuntimeConfig(primary="Alpaca", base_url="https://paper.example")

        self.assertEqual(broker.primary, "Alpaca")
        self.assertEqual(broker.backup, "IBKR")
        self.assertEqual(broker.base_url, "https://paper.example")

    def test_legacy_invalid_value_reports_legacy_input_path(self):
        with self.assertRaisesRegex(ValueError, r"broker\.primary"):
            self._load("[broker]\nprimary = 1\n", environ={})

    def test_rejects_legacy_and_canonical_execution_sections_together(self):
        with self.assertRaisesRegex(ValueError, "broker"):
            self._load(
                "[broker]\nprimary = \"Alpaca\"\n[execution]\nprimary_broker = \"Alpaca\"\n",
                environ={},
            )

    def test_public_payload_is_stable_and_has_no_legacy_broker_section(self):
        first = self._load(CANONICAL_CONFIG, environ={"ALPACA_API_KEY": "one"}).public_payload()
        second = self._load(CANONICAL_CONFIG, environ={"ALPACA_API_KEY": "two"}).public_payload()

        self.assertEqual(first, second)
        self.assertNotIn("broker", first)
        self.assertEqual(first["execution"]["primary_broker"], "Alpaca")

    def test_example_config_and_docs_expose_only_canonical_structure(self):
        example = Path("config/quantbot.research.toml").read_text(encoding="utf-8")
        parsed = tomllib.loads(example)
        readme = Path("docs/history/StockBot/README.md").read_text(encoding="utf-8")

        self.assertEqual(
            set(parsed),
            {"data", "normal", "daytrade", "portfolio", "risk", "execution", "validation"},
        )
        self.assertNotIn("api_key", example)
        self.assertNotIn("secret_key", example)
        self.assertIn("ALPACA_API_KEY", readme)
        self.assertIn("ALPACA_SECRET_KEY", readme)
        self.assertIn("[execution]", readme)
        self.assertIn("[broker]", readme)
        self.assertIn("compatibility", readme)
        self.assertIn("live_trading_disabled", readme)
        self.assertIn("historical replay", readme)
        self.assertIn("--submit-orders", readme)

    def test_pyproject_requires_python_312(self):
        pyproject = Path("pyproject.toml").read_text(encoding="utf-8")

        self.assertIn('requires-python = ">=3.12"', pyproject)


if __name__ == "__main__":
    unittest.main()
