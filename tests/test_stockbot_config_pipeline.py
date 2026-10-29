import sys
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.config import SystemConfig
from quantbot.factors.candidate_pool import CandidateFactor, CandidatePoolBuilder
from quantbot.factors.evaluation import FactorMetrics
from quantbot.regime import Regime
from quantbot.regime_model import (
    HMMRegimeAdapter,
    RegimeConsensusValidator,
    evaluate_regime_accuracy,
    validate_hmm_oos_window,
)
from quantbot.scheduler import AcceptanceCycleSchedulePlan, DailySchedulePlan, MonthlyResearchSchedulePlan
from quantbot.universe import UniverseBuilder


class StockBotConfigPipelineTest(unittest.TestCase):
    def test_default_config_captures_appendix_and_risk_parameters(self):
        config = SystemConfig.default()

        self.assertEqual(config.market.exchange, ("NYSE", "NASDAQ"))
        self.assertEqual(config.market.benchmark, "SPY")
        self.assertEqual(config.market.target_alpha_annual, 0.05)
        self.assertEqual(config.factor_pool.min_size, 20)
        self.assertEqual(config.factor_pool.max_size, 40)
        self.assertEqual(config.regime.n_states, 5)
        self.assertEqual(config.regime.min_confidence, 0.60)
        self.assertEqual(config.risk.max_single_stock_weight, 0.05)
        self.assertEqual(config.execution.primary_broker, "Alpaca")
        self.assertEqual(config.execution.backup_broker, "IBKR")

    def test_candidate_pool_builder_keeps_20_to_40_factors_with_source_diversity(self):
        factors = []
        for source, count, base_icir in [("alpha158", 20, 0.40), ("kronos", 15, 0.50), ("rd_agent", 15, 0.45)]:
            for index in range(count):
                factors.append(
                    CandidateFactor(
                        name="%s_%02d" % (source, index),
                        source=source,
                        metrics=FactorMetrics("%s_%02d" % (source, index), 0.03, base_icir + index / 1000, 0.58, 0.10, 5, 0.03),
                    )
                )

        pool = CandidatePoolBuilder(target_size=30).build(factors)
        counts = pool.source_counts()

        self.assertEqual(len(pool.factors), 30)
        self.assertEqual(counts["alpha158"], 12)
        self.assertEqual(counts["kronos"], 9)
        self.assertEqual(counts["rd_agent"], 9)
        self.assertGreaterEqual(pool.average_icir, 0.40)

    def test_candidate_pool_builder_counts_rd_agent_and_custom_as_shared_differentiated_bucket(self):
        factors = []
        for source, count, base_icir in [
            ("alpha158", 20, 0.90),
            ("kronos", 20, 0.80),
            ("rd_agent", 3, 0.70),
            ("custom", 10, 0.36),
        ]:
            for index in range(count):
                factors.append(
                    CandidateFactor(
                        name="%s_%02d" % (source, index),
                        source=source,
                        metrics=FactorMetrics("%s_%02d" % (source, index), 0.03, base_icir + index / 1000, 0.58, 0.10, 5, 0.03),
                    )
                )

        pool = CandidatePoolBuilder(target_size=30).build(factors)
        counts = pool.source_counts()

        self.assertEqual(len(pool.factors), 30)
        self.assertEqual(counts["alpha158"], 12)
        self.assertEqual(counts["kronos"], 9)
        self.assertEqual(counts.get("rd_agent", 0) + counts.get("custom", 0), 9)
        self.assertGreaterEqual(counts["custom"], 6)

    def test_hmm_adapter_maps_probability_vector_to_regime_and_confidence(self):
        adapter = HMMRegimeAdapter(
            model=FakeHMM([[0.05, 0.70, 0.10, 0.10, 0.05]]),
            scaler=FakeScaler(),
            regime_order=[
                Regime.R1_BULL_LOW_VOL,
                Regime.R2_BULL_HIGH_VOL,
                Regime.R3_SIDEWAYS,
                Regime.R4_BEAR,
                Regime.R5_CRISIS,
            ],
        )

        prediction = adapter.predict([{"vix_level": 18, "spy_mom_1m": 0.03}])

        self.assertEqual(prediction.regime, Regime.R2_BULL_HIGH_VOL)
        self.assertEqual(prediction.confidence, 0.70)
        self.assertEqual(prediction.source, "hmm")

    def test_regime_accuracy_gate_requires_seventy_percent_oos_hit_rate(self):
        passed = evaluate_regime_accuracy(
            predicted=[Regime.R1_BULL_LOW_VOL, Regime.R2_BULL_HIGH_VOL, Regime.R3_SIDEWAYS, Regime.R4_BEAR],
            actual=[Regime.R1_BULL_LOW_VOL, Regime.R2_BULL_HIGH_VOL, Regime.R5_CRISIS, Regime.R4_BEAR],
            min_accuracy=0.70,
        )
        failed = evaluate_regime_accuracy(
            predicted=[Regime.R1_BULL_LOW_VOL, Regime.R2_BULL_HIGH_VOL, Regime.R3_SIDEWAYS, Regime.R4_BEAR],
            actual=[Regime.R1_BULL_LOW_VOL, Regime.R5_CRISIS, Regime.R5_CRISIS, Regime.R4_BEAR],
            min_accuracy=0.70,
        )

        self.assertTrue(passed.passed)
        self.assertAlmostEqual(passed.accuracy, 0.75)
        self.assertFalse(failed.passed)

    def test_hmm_oos_window_requires_2010_to_2018_training_and_2019_plus_prediction(self):
        valid = validate_hmm_oos_window(
            train_start=date(2010, 1, 1),
            train_end=date(2018, 12, 31),
            prediction_start=date(2019, 1, 1),
        )
        invalid = validate_hmm_oos_window(
            train_start=date(2012, 1, 1),
            train_end=date(2019, 1, 2),
            prediction_start=date(2019, 1, 1),
        )

        self.assertTrue(valid.passed)
        self.assertEqual(valid.reasons, [])
        self.assertFalse(invalid.passed)
        self.assertIn("insufficient_training_history", invalid.reasons)
        self.assertIn("train_window_after_2018", invalid.reasons)
        self.assertIn("lookahead_overlap", invalid.reasons)

    def test_regime_consensus_boosts_confidence_when_hmm_and_rule_agree_and_penalizes_conflicts(self):
        validator = RegimeConsensusValidator(confidence_boost=0.10, conflict_penalty=0.50)

        agreed = validator.combine(
            model_prediction=type("Prediction", (), {"regime": Regime.R2_BULL_HIGH_VOL, "confidence": 0.70, "source": "hmm"})(),
            rule_prediction=type("Prediction", (), {"regime": Regime.R2_BULL_HIGH_VOL, "confidence": 1.0, "source": "rule"})(),
        )
        conflicted = validator.combine(
            model_prediction=type("Prediction", (), {"regime": Regime.R2_BULL_HIGH_VOL, "confidence": 0.70, "source": "hmm"})(),
            rule_prediction=type("Prediction", (), {"regime": Regime.R4_BEAR, "confidence": 1.0, "source": "rule"})(),
        )

        self.assertEqual(agreed.regime, Regime.R2_BULL_HIGH_VOL)
        self.assertAlmostEqual(agreed.confidence, 0.80)
        self.assertEqual(agreed.source, "hmm+rule")
        self.assertEqual(conflicted.regime, Regime.R2_BULL_HIGH_VOL)
        self.assertAlmostEqual(conflicted.confidence, 0.35)
        self.assertEqual(conflicted.source, "hmm:rule_conflict")

    def test_daily_schedule_plan_places_pipeline_after_us_market_close(self):
        plan = DailySchedulePlan.after_us_close()

        self.assertEqual(plan.timezone, "US/Eastern")
        self.assertEqual(plan.run_time, "16:30")
        self.assertEqual(plan.steps, ("data_update", "factor_compute", "signal_generate", "rebalance"))

    def test_acceptance_cycle_schedule_plan_runs_after_daily_pipeline(self):
        plan = AcceptanceCycleSchedulePlan.after_us_close(evidence_file="evidence.jsonl")

        self.assertEqual(plan.timezone, "US/Eastern")
        self.assertEqual(plan.run_time, "16:45")
        self.assertIn("acceptance-cycle", plan.command)
        self.assertIn("--evidence-file evidence.jsonl", plan.command)
        self.assertEqual(plan.steps[0], "capture_paper_snapshot")

    def test_monthly_research_schedule_plan_runs_rd_agent_factor_discovery(self):
        plan = MonthlyResearchSchedulePlan.rd_agent_discovery()

        self.assertEqual(plan.timezone, "US/Eastern")
        self.assertEqual(plan.day, 1)
        self.assertEqual(plan.run_time, "18:00")
        self.assertEqual(plan.steps, ("rd_agent_factor_discovery", "factor_admission", "candidate_pool_refresh"))

    def test_universe_builder_combines_indices_and_applies_liquidity_history_filters(self):
        rows = []
        for offset in range(5):
            rows.extend(
                [
                    {"date": date(2024, 1, 1 + offset), "ticker": "AAPL", "adjusted_close": 100, "volume": 100_000},
                    {"date": date(2024, 1, 1 + offset), "ticker": "RUSSELL", "adjusted_close": 50, "volume": 120_000},
                    {"date": date(2024, 1, 1 + offset), "ticker": "ILLIQ", "adjusted_close": 10, "volume": 1_000},
                ]
            )

        universe = UniverseBuilder(
            sp500={"AAPL", "ILLIQ"},
            russell1000={"RUSSELL"},
            min_avg_dollar_volume=5_000_000,
            min_history_days=5,
            lookback_days=5,
        ).build(rows)

        self.assertEqual(universe, ["AAPL", "RUSSELL"])


class FakeScaler:
    def transform(self, rows):
        return rows


class FakeHMM:
    def __init__(self, probabilities):
        self.probabilities = probabilities

    def predict_proba(self, rows):
        return self.probabilities


if __name__ == "__main__":
    unittest.main()
