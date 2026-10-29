import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.strategy_admission import (
    DayTradeAdmissionMetrics,
    EvaluationIdentity,
    NormalAdmissionMetrics,
    capital_allocation,
    build_normal_admission_evidence,
    evaluate_daytrade_admission,
    evaluate_normal_admission,
)
from quantbot.audit import CompletionAudit
from quantbot.acceptance_policy import ACCEPTANCE_POLICY_VERSION
from quantbot.exposure import ExposureLedger, ValidationWindowManifest
from quantbot.trading import RunManifest
from quantbot.validation_protocol import formal_sealed_warmup_interval


IDENTITY = EvaluationIdentity(
    start=date(2026, 8, 1),
    end=date(2028, 7, 31),
    dataset_hash="a" * 64,
    cost_model_hash="b" * 64,
    config_hash="d" * 64,
    code_hash="e" * 64,
)
TEST_HMAC_KEY = "strategy-admission-unit-test-key"


def normal_metrics(permit, **changes):
    values = dict(
        candidate_identity=IDENTITY,
        baseline_identity=IDENTITY,
        net_cagr=0.12,
        baseline_net_cagr=0.09,
        sharpe=1.40,
        baseline_sharpe=1.20,
        net_excess_annual=0.06,
        max_drawdown=0.12,
        annual_turnover=3.0,
        stressed_net_excess_annual=0.04,
        positive_window_fraction=0.70,
        dsr_confidence=0.97,
        pbo=0.04,
        top_three_profit_contribution=0.45,
        gross_leverage=1.0,
        declared_trials=12,
        completed_trials=12,
        sealed_data_access_permit=permit,
    )
    values.update(changes)
    return NormalAdmissionMetrics(**values)


def daytrade_metrics(permit, **changes):
    values = dict(
        candidate_identity=IDENTITY,
        baseline_identity=IDENTITY,
        oos_months=30,
        oos_trades=500,
        net_return_after_5bps=0.08,
        profit_factor=1.30,
        sharpe=1.20,
        max_drawdown=0.04,
        annual_net_return=0.08,
        baseline_annual_net_return=0.05,
        baseline_sharpe=0.90,
        baseline_max_drawdown=0.05,
        stressed_12bps_cumulative_return=-0.01,
        stressed_12bps_max_drawdown=0.06,
        positive_quarter_fraction=0.70,
        top_symbol_profit_contribution=0.20,
        top_two_profit_contribution=0.45,
        dsr_confidence=0.97,
        pbo=0.04,
        gross_leverage=1.0,
        declared_trials=12,
        completed_trials=12,
        sealed_data_access_permit=permit,
    )
    values.update(changes)
    return DayTradeAdmissionMetrics(**values)


class StrategyAdmissionTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.clock = patch(
            "quantbot.validation_protocol._utc_now",
            return_value=datetime(2028, 8, 1, tzinfo=timezone.utc),
        )
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.environment = patch.dict(
            os.environ,
            {
                "STOCKBOT_EXPOSURE_HMAC_KEY": TEST_HMAC_KEY,
                "STOCKBOT_EXPOSURE_LEDGER_PATH": self.tmpdir.name,
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        ledger = ExposureLedger(Path(self.tmpdir.name))
        self.normal_permit = self._sealed_permit(ledger, "normal", "normal-sealed")
        self.daytrade_permit = self._sealed_permit(
            ledger, "daytrade", "daytrade-sealed"
        )

    @staticmethod
    def _sealed_permit(
        ledger, strategy_family, run_id, *, include_warmup=True, **identity_changes
    ):
        identity = {
            "dataset_hash": IDENTITY.dataset_hash,
            "config_hash": IDENTITY.config_hash,
            "cost_model_hash": IDENTITY.cost_model_hash,
            "code_hash": IDENTITY.code_hash,
        }
        identity.update(identity_changes)
        warmup = (
            formal_sealed_warmup_interval(strategy_family, IDENTITY.start)
            if include_warmup
            else None
        )
        return ledger.claim(
            ValidationWindowManifest(
                strategy_family=strategy_family,
                mode="sealed",
                request_start=IDENTITY.start,
                request_end=IDENTITY.end,
                warmup_start=None if warmup is None else warmup.start,
                warmup_end=None if warmup is None else warmup.end,
                **identity,
            ),
            run_id=run_id,
            claimed_at=datetime(2028, 8, 1, tzinfo=timezone.utc),
        )

    def test_normal_strategy_requires_every_numeric_and_evidence_threshold(self):
        accepted = evaluate_normal_admission(normal_metrics(self.normal_permit))
        rejected = evaluate_normal_admission(
            normal_metrics(
                self.normal_permit,
                net_cagr=0.105,
                stressed_net_excess_annual=0.02,
                completed_trials=11,
                pbo=0.10,
            )
        )

        self.assertTrue(accepted.passed, accepted.reasons)
        self.assertIn("baseline_cagr_improvement", rejected.reasons)
        self.assertIn("two_x_cost_stress", rejected.reasons)
        self.assertIn("incomplete_trial_ledger", rejected.reasons)
        self.assertIn("pbo", rejected.reasons)

    def test_formal_sealed_admission_requires_each_family_complete_warmup(self):
        no_warmup_path = Path(self.tmpdir.name) / "no-warmup"
        with patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_LEDGER_PATH": str(no_warmup_path)},
            clear=False,
        ):
            ledger = ExposureLedger(no_warmup_path)
            normal_without_warmup = self._sealed_permit(
                ledger,
                "normal",
                "normal-without-warmup",
                include_warmup=False,
            )
            daytrade_without_warmup = self._sealed_permit(
                ledger,
                "daytrade",
                "daytrade-without-warmup",
                include_warmup=False,
            )
            self.assertIn(
                "sealed_holdout",
                evaluate_normal_admission(
                    normal_metrics(normal_without_warmup)
                ).reasons,
            )
            self.assertIn(
                "sealed_holdout",
                evaluate_daytrade_admission(
                    daytrade_metrics(daytrade_without_warmup)
                ).reasons,
            )

        self.assertNotIn(
            "sealed_holdout",
            evaluate_normal_admission(normal_metrics(self.normal_permit)).reasons,
        )
        self.assertNotIn(
            "sealed_holdout",
            evaluate_daytrade_admission(
                daytrade_metrics(self.daytrade_permit)
            ).reasons,
        )

    def test_normal_profit_contribution_is_a_finite_fraction(self):
        for invalid in (-0.01, 1.01, float("nan"), float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ValueError, "top_three_profit_contribution"
                ):
                    normal_metrics(
                        self.normal_permit,
                        top_three_profit_contribution=invalid,
                    )

    def test_normal_drawdown_ceiling_boundary_is_pinned(self):
        """The named ceiling preserves the original 15% admission contract."""

        from quantbot.strategy_admission import NORMAL_ACCEPTANCE_MAX_DRAWDOWN

        self.assertEqual(NORMAL_ACCEPTANCE_MAX_DRAWDOWN, 0.15)
        inside = evaluate_normal_admission(
            normal_metrics(self.normal_permit, max_drawdown=0.1499)
        )
        outside = evaluate_normal_admission(
            normal_metrics(self.normal_permit, max_drawdown=0.1501)
        )

        self.assertNotIn("max_drawdown", inside.reasons)
        self.assertIn("max_drawdown", outside.reasons)
        self.assertIn(
            "max_drawdown",
            evaluate_normal_admission(
                normal_metrics(self.normal_permit, max_drawdown=0.1974)
            ).reasons,
        )

    def test_daytrade_drawdown_ceilings_are_unchanged(self):
        from quantbot.strategy_admission import (
            DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN,
            DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN,
        )

        self.assertEqual(DAYTRADE_ACCEPTANCE_MAX_DRAWDOWN, 0.05)
        self.assertEqual(DAYTRADE_ACCEPTANCE_STRESSED_MAX_DRAWDOWN, 0.07)
        self.assertIn(
            "max_drawdown",
            evaluate_daytrade_admission(
                daytrade_metrics(self.daytrade_permit, max_drawdown=0.0501)
            ).reasons,
        )

    def test_stricter_thresholds_change_the_admission_decision(self):
        """`[validation]` is only meaningful if it can flip an outcome."""

        from quantbot.strategy_admission import NormalAcceptanceThresholds

        metrics = normal_metrics(self.normal_permit)
        default = evaluate_normal_admission(metrics)
        strict_sharpe = evaluate_normal_admission(
            metrics, NormalAcceptanceThresholds(min_sharpe=1.50)
        )
        shallow = normal_metrics(self.normal_permit, max_drawdown=0.14)
        strict_drawdown = evaluate_normal_admission(
            shallow, NormalAcceptanceThresholds(max_drawdown=0.10)
        )

        self.assertTrue(default.passed, default.reasons)
        self.assertIn("sharpe", strict_sharpe.reasons)
        self.assertNotIn("max_drawdown", evaluate_normal_admission(shallow).reasons)
        self.assertIn("max_drawdown", strict_drawdown.reasons)

    def test_gates_reject_the_wrong_threshold_type(self):
        from quantbot.strategy_admission import (
            DAYTRADE_ACCEPTANCE_THRESHOLDS,
            NORMAL_ACCEPTANCE_THRESHOLDS,
        )

        with self.assertRaisesRegex(TypeError, "NormalAcceptanceThresholds"):
            evaluate_normal_admission(
                normal_metrics(self.normal_permit), DAYTRADE_ACCEPTANCE_THRESHOLDS
            )
        with self.assertRaisesRegex(TypeError, "DayTradeAcceptanceThresholds"):
            evaluate_daytrade_admission(
                daytrade_metrics(self.daytrade_permit), NORMAL_ACCEPTANCE_THRESHOLDS
            )

    def test_daytrade_strategy_enforces_oos_cost_stress_and_concentration(self):
        accepted = evaluate_daytrade_admission(daytrade_metrics(self.daytrade_permit))
        rejected = evaluate_daytrade_admission(
            daytrade_metrics(
                self.daytrade_permit,
                oos_months=23,
                oos_trades=299,
                net_return_after_5bps=0.0,
                stressed_12bps_cumulative_return=-0.03,
                top_symbol_profit_contribution=0.30,
            )
        )

        self.assertTrue(accepted.passed, accepted.reasons)
        self.assertIn("oos_months", rejected.reasons)
        self.assertIn("oos_trades", rejected.reasons)
        self.assertIn("five_bps_net_return", rejected.reasons)
        self.assertIn("twelve_bps_stress", rejected.reasons)
        self.assertIn("single_symbol_concentration", rejected.reasons)

    def test_candidate_and_corrected_baseline_must_share_full_identity(self):
        mismatched = EvaluationIdentity(
            start=IDENTITY.start,
            end=IDENTITY.end,
            dataset_hash="c" * 64,
            cost_model_hash=IDENTITY.cost_model_hash,
            config_hash=IDENTITY.config_hash,
            code_hash=IDENTITY.code_hash,
        )

        normal = evaluate_normal_admission(
            normal_metrics(self.normal_permit, baseline_identity=mismatched)
        )
        daytrade = evaluate_daytrade_admission(
            daytrade_metrics(self.daytrade_permit, baseline_identity=mismatched)
        )

        self.assertIn("incomparable_baseline", normal.reasons)
        self.assertIn("incomparable_baseline", daytrade.reasons)

    def test_capital_stays_ninety_ten_until_both_strategies_pass(self):
        locked = capital_allocation(
            normal_admitted=True,
            daytrade_admitted=False,
            requested_normal_fraction=0.70,
            requested_daytrade_fraction=0.30,
        )
        unlocked = capital_allocation(
            normal_admitted=True,
            daytrade_admitted=True,
            requested_normal_fraction=0.70,
            requested_daytrade_fraction=0.30,
        )

        self.assertEqual((locked.normal_fraction, locked.daytrade_fraction), (0.90, 0.10))
        self.assertEqual((unlocked.normal_fraction, unlocked.daytrade_fraction), (0.70, 0.30))
        with self.assertRaisesRegex(ValueError, "sum to one"):
            capital_allocation(
                normal_admitted=True,
                daytrade_admitted=True,
                requested_normal_fraction=0.80,
                requested_daytrade_fraction=0.30,
            )

    def test_admission_builder_preserves_provenance_but_needs_execution_authentication(self):
        manifest = RunManifest(
            run_type="sealed_backtest",
            observed_at=datetime(2028, 8, 1, tzinfo=timezone.utc),
            dataset_hash=IDENTITY.dataset_hash,
            config_hash=IDENTITY.config_hash,
            code_hash=IDENTITY.code_hash,
            policy_version=ACCEPTANCE_POLICY_VERSION,
            cost_model_hash=IDENTITY.cost_model_hash,
            input_materialization_hash="1" * 64,
            output_hash="2" * 64,
            report_hash="3" * 64,
            cost_suite_hash="4" * 64,
            run_id="normal-sealed-admission",
            experiment_id="normal-h1-quality-diversification",
        )
        evidence = build_normal_admission_evidence(
            normal_metrics(self.normal_permit), manifest
        )

        self.assertTrue(evidence.passed)
        self.assertEqual(evidence.run_type, "sealed_backtest")
        self.assertEqual(evidence.report_hash, "3" * 64)
        self.assertFalse(
            CompletionAudit(["normal_strategy_admission"]).evaluate(
                {"normal_strategy_admission": evidence}
            ).complete
        )

    def test_admission_rejects_bool_adaptive_mismatch_and_bad_signature(self):
        with self.assertRaisesRegex(ValueError, "signed DataAccessPermit"):
            normal_metrics(True)

        adaptive_path = Path(self.tmpdir.name) / "adaptive"
        with patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_LEDGER_PATH": str(adaptive_path)},
            clear=False,
        ):
            adaptive_ledger = ExposureLedger(adaptive_path)
            adaptive_permit = adaptive_ledger.claim(
                ValidationWindowManifest(
                    strategy_family="normal",
                    mode="adaptive_development",
                    request_start=date(2021, 1, 1),
                    request_end=date(2024, 12, 31),
                    dataset_hash=IDENTITY.dataset_hash,
                    config_hash=IDENTITY.config_hash,
                    cost_model_hash=IDENTITY.cost_model_hash,
                    code_hash=IDENTITY.code_hash,
                ),
                run_id="normal-adaptive",
                claimed_at=datetime(2028, 8, 1, tzinfo=timezone.utc),
            )
        mismatch_path = Path(self.tmpdir.name) / "mismatch"
        with patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_LEDGER_PATH": str(mismatch_path)},
            clear=False,
        ):
            wrong_identity_ledger = ExposureLedger(mismatch_path)
            wrong_identity_permit = self._sealed_permit(
                wrong_identity_ledger,
                "normal",
                "normal-mismatched-sealed",
                config_hash="f" * 64,
            )
        alternate_path = Path(self.tmpdir.name) / "alternate"
        with patch.dict(
            os.environ,
            {"STOCKBOT_EXPOSURE_LEDGER_PATH": str(alternate_path)},
            clear=False,
        ):
            alternate_permit = self._sealed_permit(
                ExposureLedger(alternate_path),
                "normal",
                "normal-alternate-ledger",
            )
        bad_signature = replace(self.normal_permit, signature="0" * 64)

        for permit in (
            adaptive_permit,
            wrong_identity_permit,
            alternate_permit,
            bad_signature,
        ):
            decision = evaluate_normal_admission(normal_metrics(permit))
            self.assertIn("sealed_holdout", decision.reasons)


if __name__ == "__main__":
    unittest.main()
