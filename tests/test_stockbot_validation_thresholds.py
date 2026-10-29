"""The `[validation]` section must actually drive admission, and only tighten it.

Every acceptance threshold used to be a literal, duplicated between
`normal/experiment_runner.py` and `strategy_admission.py`, while the
`[validation]` section that names those thresholds had no consumer at all.
Editing `[validation]` changed nothing, which made the README's "runtime
configuration cannot weaken admission policy" claim vacuous in both directions.

These tests pin the two halves of the fix: the section is consumed, and the
threshold types refuse to be loosened from any construction path.
"""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config_loader import (
    _CANONICAL_KEYS,
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
from quantbot.strategy_admission import (
    DAYTRADE_ACCEPTANCE_THRESHOLDS,
    NORMAL_ACCEPTANCE_THRESHOLDS,
    DayTradeAcceptanceThresholds,
    NormalAcceptanceThresholds,
)

# Tests that the thresholds actually change an admission decision live in
# test_stockbot_strategy_admission, next to the sealed-permit fixture.


REPO = Path(__file__).resolve().parents[1]


def _config(**validation):
    return RuntimeConfig(
        DataRuntimeConfig(),
        NormalRuntimeConfig(),
        DayTradeRuntimeConfig(),
        PortfolioRuntimeConfig(),
        RiskRuntimeConfig(),
        ExecutionRuntimeConfig(),
        ValidationRuntimeConfig(**validation),
    )


class ValidationSectionIsConsumedTest(unittest.TestCase):
    def test_every_declared_validation_key_reaches_a_gate(self):
        config = _config()
        normal = config.normal_acceptance_thresholds()
        daytrade = config.daytrade_acceptance_thresholds()
        consumed = {
            "normal_min_net_excess_annual": normal.min_net_excess_annual,
            "normal_min_sharpe": normal.min_sharpe,
            "normal_max_drawdown": normal.max_drawdown,
            "daytrade_min_oos_months": daytrade.min_oos_months,
            "daytrade_min_oos_trades": daytrade.min_oos_trades,
            "min_paper_sessions": config.paper_promotion_min_sessions(),
        }

        self.assertEqual(set(consumed), set(_CANONICAL_KEYS["validation"]))

    def test_the_shipped_example_maps_onto_the_policy_defaults(self):
        config = load_runtime_config(REPO / "config" / "quantbot.research.toml")

        self.assertEqual(
            config.normal_acceptance_thresholds(), NORMAL_ACCEPTANCE_THRESHOLDS
        )
        self.assertEqual(
            config.daytrade_acceptance_thresholds(), DAYTRADE_ACCEPTANCE_THRESHOLDS
        )

    def test_a_stricter_section_produces_stricter_thresholds(self):
        config = _config(
            normal_min_sharpe=1.60,
            normal_min_net_excess_annual=0.08,
            normal_max_drawdown=0.15,
            daytrade_min_oos_trades=500,
        )
        normal = config.normal_acceptance_thresholds()
        daytrade = config.daytrade_acceptance_thresholds()

        self.assertEqual(normal.min_sharpe, 1.60)
        self.assertEqual(normal.min_net_excess_annual, 0.08)
        self.assertEqual(daytrade.min_oos_trades, 500)

    def test_the_section_cannot_loosen_any_threshold(self):
        for key, loose in (
            ("normal_min_net_excess_annual", 0.04),
            ("normal_min_sharpe", 1.19),
            ("daytrade_min_oos_months", 23),
            ("daytrade_min_oos_trades", 299),
            ("min_paper_sessions", 59),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    ValidationRuntimeConfig(**{key: loose})

    def test_the_drawdown_ceiling_cannot_exceed_the_policy_ceiling(self):
        with self.assertRaisesRegex(ValueError, "at most"):
            ValidationRuntimeConfig(normal_max_drawdown=0.151)

    def test_any_loadable_configuration_yields_valid_thresholds(self):
        for ceiling in (0.15, 0.12, 0.10):
            for vol in (0.15, 0.12, 0.10):
                with self.subTest(ceiling=ceiling, vol=vol):
                    config = RuntimeConfig(
                        DataRuntimeConfig(),
                        NormalRuntimeConfig(),
                        DayTradeRuntimeConfig(),
                        PortfolioRuntimeConfig(),
                        RiskRuntimeConfig(max_portfolio_vol_annual=vol),
                        ExecutionRuntimeConfig(),
                        ValidationRuntimeConfig(normal_max_drawdown=ceiling),
                    )
                    self.assertEqual(
                        config.normal_acceptance_thresholds().max_drawdown, ceiling
                    )


class ThresholdsAreStricterOnlyTest(unittest.TestCase):
    def test_normal_minimums_cannot_be_lowered(self):
        for field, loose in (
            ("min_net_excess_annual", 0.04),
            ("min_sharpe", 1.19),
            ("min_stressed_net_excess_annual", 0.02),
            ("min_positive_window_fraction", 0.59),
            ("min_baseline_cagr_improvement", 0.01),
            ("min_baseline_sharpe_improvement", 0.14),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    NormalAcceptanceThresholds(**{field: loose})

    def test_normal_maximums_cannot_be_raised(self):
        for field, loose in (
            ("max_drawdown", 0.151),
            ("max_annual_turnover", 4.01),
            ("max_top_three_profit_contribution", 0.51),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    NormalAcceptanceThresholds(**{field: loose})

    def test_daytrade_thresholds_cannot_be_weakened(self):
        for field, loose in (
            ("min_oos_months", 23),
            ("min_oos_trades", 299),
            ("min_profit_factor", 1.19),
            ("min_sharpe", 0.99),
            ("min_positive_quarter_fraction", 0.59),
            ("max_drawdown", 0.051),
            ("max_stressed_drawdown", 0.071),
            ("max_top_symbol_profit_contribution", 0.26),
            ("max_top_two_profit_contribution", 0.51),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    DayTradeAcceptanceThresholds(**{field: loose})

    def test_tightening_is_always_allowed(self):
        normal = NormalAcceptanceThresholds(min_sharpe=2.0, max_drawdown=0.10)
        daytrade = DayTradeAcceptanceThresholds(min_oos_trades=1000, max_drawdown=0.01)

        self.assertEqual(normal.min_sharpe, 2.0)
        self.assertEqual(normal.max_drawdown, 0.10)
        self.assertEqual(daytrade.min_oos_trades, 1000)
        self.assertEqual(daytrade.max_drawdown, 0.01)

    def test_integer_fields_reject_non_integers(self):
        with self.assertRaisesRegex(ValueError, "must be an integer"):
            DayTradeAcceptanceThresholds(min_oos_months=24.5)


if __name__ == "__main__":
    unittest.main()
