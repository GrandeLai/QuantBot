"""The acceptance drawdown ceiling and the operational trigger are separate.

`risk.max_normal_drawdown` is the review/halt trigger for a running book.
`validation.normal_max_drawdown` is the backtest acceptance ceiling used to
admit or reject a strategy.  They intentionally share the original 15% value,
but remain separate controls with separate consumers.
"""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config_loader import (
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
from quantbot.normal.experiment_runner import NORMAL_ACCEPTANCE_MAX_DRAWDOWN
from quantbot.strategy_admission import NormalAcceptanceThresholds


REPO = Path(__file__).resolve().parents[1]


def _config(*, vol=0.15, ceiling=0.15):
    return RuntimeConfig(
        DataRuntimeConfig(),
        NormalRuntimeConfig(),
        DayTradeRuntimeConfig(),
        PortfolioRuntimeConfig(),
        RiskRuntimeConfig(max_portfolio_vol_annual=vol),
        ExecutionRuntimeConfig(),
        ValidationRuntimeConfig(normal_max_drawdown=ceiling),
    )


class AcceptanceCeilingConfigTest(unittest.TestCase):
    def test_the_ceiling_lives_in_validation_not_risk(self):
        config = _config()

        self.assertEqual(config.validation.normal_max_drawdown, 0.15)
        self.assertEqual(config.risk.max_normal_drawdown, 0.15)

    def test_the_original_admission_pair_is_loadable(self):
        config = _config(vol=0.15, ceiling=0.15)

        self.assertEqual(config.validation.normal_max_drawdown, 0.15)
        self.assertEqual(config.risk.max_portfolio_vol_annual, 0.15)

    def test_drawdown_admission_is_not_inferred_from_volatility(self):
        config = _config(vol=0.30, ceiling=0.10)

        self.assertEqual(config.validation.normal_max_drawdown, 0.10)
        self.assertEqual(config.risk.max_portfolio_vol_annual, 0.30)

    def test_a_stricter_ceiling_is_independent_of_the_volatility_target(self):
        config = _config(vol=0.08, ceiling=0.10)

        self.assertEqual(config.validation.normal_max_drawdown, 0.10)

    def test_an_absurd_ceiling_is_still_refused(self):
        with self.assertRaisesRegex(ValueError, "at most"):
            ValidationRuntimeConfig(normal_max_drawdown=0.90)

    def test_shipped_example_configuration_is_internally_consistent(self):
        config = load_runtime_config(REPO / "config" / "quantbot.research.toml")

        self.assertEqual(config.validation.normal_max_drawdown, 0.15)

    def test_the_ceiling_participates_in_the_public_payload(self):
        payload = _config().public_payload()

        self.assertEqual(payload["validation"]["normal_max_drawdown"], 0.15)


class RunnerThresholdTest(unittest.TestCase):
    def test_the_runner_default_matches_the_shipped_ceiling(self):
        config = load_runtime_config(REPO / "config" / "quantbot.research.toml")

        self.assertEqual(
            NORMAL_ACCEPTANCE_MAX_DRAWDOWN, config.validation.normal_max_drawdown
        )

    def test_the_runner_preserves_the_original_admission_ceiling(self):
        self.assertEqual(NORMAL_ACCEPTANCE_MAX_DRAWDOWN, 0.15)

    def test_the_threshold_type_refuses_to_be_weakened(self):
        self.assertEqual(NormalAcceptanceThresholds().max_drawdown, 0.15)
        self.assertEqual(NormalAcceptanceThresholds(max_drawdown=0.10).max_drawdown, 0.10)
        for loose in (0.1501, 0.5, 1.5):
            with self.subTest(loose=loose):
                with self.assertRaisesRegex(ValueError, "cannot weaken"):
                    NormalAcceptanceThresholds(max_drawdown=loose)


if __name__ == "__main__":
    unittest.main()
