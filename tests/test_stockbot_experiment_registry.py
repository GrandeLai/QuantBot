import sys
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.daytrade_strategy.experiments import (
    preregistered_daytrade_experiments,
    preregistered_daytrade_global_experiments,
)
from quantbot.experiment_dispositions import (
    ExperimentDisposition,
    FIXED_EXPERIMENT_DISPOSITIONS,
)
from quantbot.experiment_registry import ExperimentRegistry
from quantbot.normal.experiments import preregistered_normal_experiments


class ExperimentRegistryTest(unittest.TestCase):
    def setUp(self):
        self.registry = ExperimentRegistry()
        self.now = datetime(2025, 1, 1, tzinfo=timezone.utc)

    def test_single_family_experiment_allows_one_addition_and_at_most_three_values(self):
        record = self.registry.register_single_family(
            experiment_id="normal-residual-momentum",
            strategy_family="normal",
            baseline_components=("corrected_baseline",),
            candidate_components=("corrected_baseline", "residual_momentum"),
            parameter_name="window_months",
            parameter_values=(3, 6, 12),
            preregistered_at=self.now,
        )

        self.assertEqual(record.added_component, "residual_momentum")
        self.assertEqual(record.declared_trial_count, 3)
        self.assertEqual(self.registry.total_declared_trials, 3)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.registry.register_single_family(
                experiment_id="too-many-families",
                strategy_family="normal",
                baseline_components=("baseline",),
                candidate_components=("baseline", "quality", "reversal"),
                parameter_name="enabled",
                parameter_values=(True,),
                preregistered_at=self.now,
            )
        with self.assertRaisesRegex(ValueError, "at most three"):
            self.registry.register_single_family(
                experiment_id="too-many-values",
                strategy_family="daytrade",
                baseline_components=("orb",),
                candidate_components=("orb", "cumulative_vwap"),
                parameter_name="hysteresis_bps",
                parameter_values=(0, 1, 2, 3),
                preregistered_at=self.now,
            )

    def test_interaction_grid_is_capped_at_32_and_every_trial_is_recorded_once(self):
        record = self.registry.register_interaction(
            experiment_id="daytrade-approved-interaction",
            strategy_family="daytrade",
            components=("orb", "rvol", "atr_stop"),
            parameter_grid={
                "opening_minutes": (15, 30),
                "rvol_floor": (1.2, 1.5),
                "atr_stop": (0.8, 1.0),
            },
            preregistered_at=self.now,
        )

        self.assertEqual(record.declared_trial_count, 8)
        for trial_id in record.trial_ids:
            self.registry.record_trial(record.experiment_id, trial_id)
        self.assertEqual(self.registry.completed_trial_count, 8)
        with self.assertRaisesRegex(ValueError, "already recorded"):
            self.registry.record_trial(record.experiment_id, record.trial_ids[0])
        with self.assertRaisesRegex(ValueError, "32"):
            self.registry.register_interaction(
                experiment_id="mechanical-grid",
                strategy_family="daytrade",
                components=("orb", "everything"),
                parameter_grid={
                    "a": (1, 2, 3),
                    "b": (1, 2, 3),
                    "c": (1, 2, 3),
                    "d": (1, 2),
                },
                preregistered_at=self.now,
            )

    def test_registry_rejects_duplicate_ids_unknown_trials_and_mutation(self):
        record = self.registry.register_single_family(
            experiment_id="one",
            strategy_family="normal",
            baseline_components=("baseline",),
            candidate_components=("baseline", "quality"),
            parameter_name="quality_weight",
            parameter_values=(0.25,),
            preregistered_at=self.now,
        )
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register_single_family(
                experiment_id="one",
                strategy_family="normal",
                baseline_components=("baseline",),
                candidate_components=("baseline", "reversal"),
                parameter_name="weight",
                parameter_values=(0.25,),
                preregistered_at=self.now,
            )
        with self.assertRaisesRegex(ValueError, "unknown trial"):
            self.registry.record_trial("one", "not-declared")
        with self.assertRaises(TypeError):
            record.parameter_grid["quality_weight"] = (1.0,)

    def test_known_registry_import_counts_global_trials_without_changing_local_run(self):
        historical = ExperimentRegistry()
        historical_record = historical.register_single_family(
            experiment_id="historical-rvol",
            strategy_family="daytrade",
            baseline_components=("orb",),
            candidate_components=("orb", "rvol"),
            parameter_name="rvol_floor",
            parameter_values=(1.0, 1.5, 2.0),
            preregistered_at=self.now,
        )
        historical.record_trial(
            historical_record.experiment_id, historical_record.trial_ids[0]
        )
        local_record = self.registry.register_single_family(
            experiment_id="current-market-filter",
            strategy_family="daytrade",
            baseline_components=("orb",),
            candidate_components=("market_filter", "orb"),
            parameter_name="threshold_bps",
            parameter_values=(0, 5),
            preregistered_at=self.now,
        )

        self.assertEqual(self.registry.import_registry(historical), 1)
        self.assertEqual(self.registry.import_registry(historical), 0)

        self.assertEqual(self.registry.records, (local_record,))
        self.assertEqual(self.registry.total_declared_trials, 2)
        self.assertEqual(self.registry.completed_trial_count, 0)
        self.assertEqual(self.registry.total_known_declared_trials, 5)
        self.assertEqual(self.registry.global_declared_trial_count, 5)
        self.assertEqual(self.registry.known_completed_trial_count, 1)
        self.assertEqual(
            self.registry.count_known_trials(
                (local_record.trial_ids[0], historical_record.trial_ids[0])
            ),
            2,
        )
        with self.assertRaisesRegex(ValueError, "globally known"):
            self.registry.count_known_trials(("unregistered:trial-01",))
        with self.assertRaisesRegex(ValueError, "already registered"):
            self.registry.register_single_family(
                experiment_id=historical_record.experiment_id,
                strategy_family="daytrade",
                baseline_components=("orb",),
                candidate_components=("atr", "orb"),
                parameter_name="enabled",
                parameter_values=(True,),
                preregistered_at=self.now,
            )

    def test_fixed_dispositions_are_immutable_and_match_profitability_verdicts(self):
        expected = {
            "normal-h1-quality-diversification": ExperimentDisposition.PREREGISTERED,
            "normal-h2-capital-efficiency": (
                ExperimentDisposition.COMPLETED_REJECTED
            ),
            "daytrade-h3-pre-entry-confirmation": (
                ExperimentDisposition.CANDIDATE_SURVIVED
            ),
            "daytrade-h4-risk-scale": ExperimentDisposition.RETIRED,
            "daytrade-h5-opening-range-top4": ExperimentDisposition.RETIRED,
            "daytrade-h6-market-confirmation": ExperimentDisposition.RETIRED,
            "daytrade-h7-rvol-entry-window": ExperimentDisposition.RETIRED,
            "daytrade-h8-rvol-risk-scale": ExperimentDisposition.RETIRED,
            "daytrade-h9-rvol-reward-risk": ExperimentDisposition.PREREGISTERED,
        }

        self.assertEqual(
            {
                experiment_id: record.disposition
                for experiment_id, record in FIXED_EXPERIMENT_DISPOSITIONS.items()
            },
            expected,
        )
        with self.assertRaises(TypeError):
            FIXED_EXPERIMENT_DISPOSITIONS["daytrade-h4-risk-scale"] = None
        with self.assertRaises(FrozenInstanceError):
            FIXED_EXPERIMENT_DISPOSITIONS[
                "daytrade-h4-risk-scale"
            ].disposition = ExperimentDisposition.PREREGISTERED

    def test_candidate_selection_allows_only_active_dispositions(self):
        daytrade = preregistered_daytrade_global_experiments()
        normal = preregistered_normal_experiments()

        self.assertEqual(
            daytrade.disposition("daytrade-h3-pre-entry-confirmation"),
            ExperimentDisposition.CANDIDATE_SURVIVED,
        )
        self.assertEqual(
            daytrade.require_candidate_selectable(
                "daytrade-h3-pre-entry-confirmation"
            ).experiment_id,
            "daytrade-h3-pre-entry-confirmation",
        )
        self.assertEqual(
            daytrade.require_candidate_selectable(
                "daytrade-h9-rvol-reward-risk"
            ).experiment_id,
            "daytrade-h9-rvol-reward-risk",
        )
        self.assertTrue(
            normal.is_candidate_selectable("normal-h1-quality-diversification")
        )

        for experiment_id in (
            "daytrade-h4-risk-scale",
            "daytrade-h5-opening-range-top4",
            "daytrade-h6-market-confirmation",
            "daytrade-h7-rvol-entry-window",
            "daytrade-h8-rvol-risk-scale",
        ):
            with self.subTest(experiment_id=experiment_id):
                self.assertFalse(daytrade.is_candidate_selectable(experiment_id))
                with self.assertRaisesRegex(
                    ValueError, r"cannot be selected.*disposition=retired"
                ):
                    daytrade.require_candidate_selectable(experiment_id)

    def test_retirement_does_not_erase_trials_or_global_multiplicity(self):
        registry = preregistered_daytrade_global_experiments()
        declared_before = registry.global_declared_trial_count
        known_before = registry.known_trial_ids
        retired = next(
            record
            for record in registry.known_records
            if record.experiment_id == "daytrade-h4-risk-scale"
        )

        with self.assertRaisesRegex(ValueError, "disposition=retired"):
            registry.require_candidate_selectable(retired.experiment_id)

        self.assertEqual(declared_before, 21)
        self.assertEqual(registry.global_declared_trial_count, declared_before)
        self.assertEqual(registry.known_trial_ids, known_before)

        historical = preregistered_daytrade_experiments()
        historical_h4 = next(
            record
            for record in historical.records
            if record.experiment_id == "daytrade-h4-risk-scale"
        )
        historical.record_trial(
            historical_h4.experiment_id, historical_h4.trial_ids[0]
        )
        self.assertIn(
            (historical_h4.experiment_id, historical_h4.trial_ids[0]),
            historical.known_completed_trials,
        )
        self.assertEqual(historical.global_declared_trial_count, 12)
        self.assertNotIn(retired, registry.selectable_candidate_records)

    def test_unfixed_registered_experiment_defaults_to_preregistered(self):
        record = self.registry.register_single_family(
            experiment_id="new-frozen-hypothesis",
            strategy_family="normal",
            baseline_components=("baseline",),
            candidate_components=("baseline", "new_signal"),
            parameter_name="enabled",
            parameter_values=(True,),
            preregistered_at=self.now,
        )

        self.assertEqual(
            self.registry.disposition(record.experiment_id),
            ExperimentDisposition.PREREGISTERED,
        )
        self.assertEqual(
            self.registry.require_candidate_selectable(record.experiment_id), record
        )
        with self.assertRaisesRegex(ValueError, "not registered"):
            self.registry.disposition("unknown-experiment")

    def test_all_disposition_values_have_explicit_selection_semantics(self):
        expected = {
            ExperimentDisposition.PREREGISTERED: (True, False),
            ExperimentDisposition.CANDIDATE_SURVIVED: (True, False),
            ExperimentDisposition.COMPLETED_REJECTED: (False, True),
            ExperimentDisposition.RETIRED: (False, True),
            ExperimentDisposition.ADMITTED: (False, True),
        }

        self.assertEqual(
            {
                disposition: (
                    disposition.allows_candidate_selection,
                    disposition.is_terminal,
                )
                for disposition in ExperimentDisposition
            },
            expected,
        )


if __name__ == "__main__":
    unittest.main()
