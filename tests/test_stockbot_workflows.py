import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.costs import TradingCostModel
from quantbot.data.coverage import DataCoverageGate, audit_delisted_history_retention
from quantbot.data.sources import DataSourceRegistry
from quantbot.data.storage import PartitionedDataCatalog
from quantbot.factors.evaluation import FactorMetrics
from quantbot.factors.lifecycle import (
    FactorDecayAlert,
    FactorLifecycleStatus,
    QuarterlyIC,
    apply_decay_weight_adjustment,
    detect_ic_decay,
    review_factor_lifecycle,
)
from quantbot.llm_filter import (
    QualitativeSignal,
    filter_by_qualitative_signals,
    negative_event_tickers_from_sec_filings,
    score_earnings_call,
)
from quantbot.monitoring import DailyPipeline, PerformanceMonitor
from quantbot.optimization import (
    generate_quarterly_retrain_windows,
    generate_walk_forward_windows,
    optimize_walk_forward_factor_weights,
)
from quantbot.rdagent import FactorExperimentResult, RDDiscoveryLoop, MonthlyResearchReport, evaluate_rd_agent_admission
from quantbot.regime import Regime
from quantbot.regime_features import build_regime_features
from quantbot.research import FactorResearchChecklist, LookAheadReviewChecklist
from quantbot.tax import TaxLot, TaxPolicy
from quantbot.watchlist import WatchlistManager, adjusted_exit_percentile_for_turnover, earnings_blackout_symbols, watchlist_size_for_capital


class StockBotWorkflowTest(unittest.TestCase):
    def test_data_source_registry_matches_design_matrix_and_storage_partitions_by_date(self):
        registry = DataSourceRegistry.default()

        self.assertEqual(registry.primary_for("ohlcv").name, "Alpaca Data API")
        self.assertEqual(registry.backups_for("ohlcv")[0].name, "yfinance")
        self.assertEqual(registry.primary_for("macro").name, "FRED API")
        self.assertEqual(registry.primary_for("news_sentiment").name, "Alpaca News")
        self.assertEqual(registry.backups_for("news_sentiment")[0].name, "GDELT")
        self.assertEqual(registry.primary_for("search_trends").name, "PyTrends")

        with tempfile.TemporaryDirectory() as tmpdir:
            catalog = PartitionedDataCatalog(Path(tmpdir))
            path = catalog.partition_path("ohlcv", date(2024, 1, 2))
            self.assertEqual(path, Path(tmpdir) / "ohlcv" / "date=2024-01-02" / "data.parquet")
            query = catalog.duckdb_scan_sql("ohlcv")
            self.assertIn("read_parquet", query)
            self.assertIn(str(Path(tmpdir) / "ohlcv" / "date=*" / "data.parquet"), query)

    def test_data_coverage_gate_requires_history_from_2010_to_validation_end(self):
        rows = [
            {"ticker": "AAPL", "date": date(2010, 1, 1)},
            {"ticker": "AAPL", "date": date(2024, 12, 31)},
            {"ticker": "MSFT", "date": date(2011, 1, 1)},
            {"ticker": "MSFT", "date": date(2024, 12, 31)},
        ]

        report = DataCoverageGate(
            min_start_date=date(2010, 1, 1),
            required_end_date=date(2024, 12, 31),
            min_covered_tickers=1,
        ).evaluate(rows)

        self.assertTrue(report.passed)
        self.assertEqual(report.covered_tickers, ["AAPL"])
        self.assertEqual(report.missing_tickers, ["MSFT"])

    def test_survivor_bias_audit_requires_delisted_history_to_be_retained(self):
        rows = [
            {"ticker": "LIVE", "date": date(2024, 1, 1)},
            {"ticker": "DELISTED", "date": date(2018, 6, 1)},
        ]

        report = audit_delisted_history_retention(rows, delisted_tickers={"DELISTED", "MISSING"})

        self.assertFalse(report.passed)
        self.assertEqual(report.retained_delisted_tickers, ["DELISTED"])
        self.assertEqual(report.missing_delisted_tickers, ["MISSING"])
        self.assertEqual(report.last_date_by_ticker["DELISTED"], date(2018, 6, 1))

    def test_regime_feature_engineering_builds_vol_trend_macro_breadth_and_flow_features(self):
        rows = [
            {
                "date": date(2024, 1, day),
                "VIX": 10 + day,
                "SPY": 100 + day,
                "US10Y": 4.0,
                "US2Y": 3.5,
                "HY_SPREAD": 4.2,
                "IG_SPREAD": 1.2,
                "INFLATION_1Y": 2.2,
                "NYSE_ADV": 300 + day,
                "NYSE_DEC": 200,
                "NYSE_NH": 40 + day,
                "NYSE_NL": 10,
                "SPY_NET_FLOW": 100 * day,
            }
            for day in range(1, 6)
        ]

        features = build_regime_features(
            rows,
            vix_ma_window=3,
            vol_window=3,
            spy_mom_1m_window=2,
            spy_mom_3m_window=4,
            ma_short_window=2,
            ma_long_window=4,
            flow_window=2,
        )

        latest = features[-1]
        self.assertAlmostEqual(latest["vix_ma_ratio"], 15 / 14)
        self.assertAlmostEqual(latest["spy_mom_1m"], 105 / 103 - 1)
        self.assertAlmostEqual(latest["yield_curve"], 0.5)
        self.assertAlmostEqual(latest["credit_spread"], 3.0)
        self.assertAlmostEqual(latest["real_rate"], 1.8)
        self.assertAlmostEqual(latest["breadth"], 305 / 505)
        self.assertAlmostEqual(latest["new_highs"], 45 / 55)
        self.assertAlmostEqual(latest["spy_flow"], 450)
        self.assertEqual(latest["ma_signal"], 1)
        self.assertEqual(latest["earnings_season"], 1.0)

    def test_factor_lifecycle_marks_watchlist_after_two_weak_quarters_and_retired_after_three(self):
        weak_history = [
            QuarterlyIC("2024Q1", 0.009),
            QuarterlyIC("2024Q2", 0.008),
        ]
        retired_history = weak_history + [QuarterlyIC("2024Q3", 0.006)]

        watch = review_factor_lifecycle("momentum_decay", weak_history)
        retired = review_factor_lifecycle("momentum_decay", retired_history)

        self.assertEqual(watch.status, FactorLifecycleStatus.WATCHLIST)
        self.assertFalse(watch.request_replacement)
        self.assertEqual(retired.status, FactorLifecycleStatus.RETIRED)
        self.assertTrue(retired.request_replacement)

    def test_factor_lifecycle_flags_ic_decay_slope_for_crowding_review(self):
        alert = detect_ic_decay(
            "crowded_momentum",
            [
                QuarterlyIC("2024Q1", 0.050),
                QuarterlyIC("2024Q2", 0.041),
                QuarterlyIC("2024Q3", 0.032),
                QuarterlyIC("2024Q4", 0.021),
            ],
            slope_threshold=-0.005,
        )
        stable = detect_ic_decay(
            "quality",
            [
                QuarterlyIC("2024Q1", 0.030),
                QuarterlyIC("2024Q2", 0.032),
                QuarterlyIC("2024Q3", 0.031),
                QuarterlyIC("2024Q4", 0.033),
            ],
            slope_threshold=-0.005,
        )

        self.assertTrue(alert.is_decaying)
        self.assertLess(alert.ic_slope, -0.005)
        self.assertEqual(alert.recommended_action, "reduce_weight_and_review")
        self.assertFalse(stable.is_decaying)

    def test_decay_weight_adjustment_reduces_crowded_factor_weights(self):
        adjusted = apply_decay_weight_adjustment(
            weights={"momentum": 0.40, "quality": 0.30},
            alerts=[
                FactorDecayAlert("momentum", ic_slope=-0.01, is_decaying=True, recommended_action="reduce_weight_and_review"),
                FactorDecayAlert("quality", ic_slope=0.0, is_decaying=False, recommended_action="keep_active"),
            ],
            reduction=0.50,
        )

        self.assertEqual(adjusted, {"momentum": 0.20, "quality": 0.30})

    def test_walk_forward_windows_use_expanding_train_and_quarterly_step_without_peeking(self):
        windows = generate_walk_forward_windows(
            train_start=date(2015, 1, 1),
            first_validation_start=date(2019, 1, 1),
            final_validation_end=date(2019, 9, 30),
            validation_months=3,
            step_months=3,
        )

        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0].train_end, date(2018, 12, 31))
        self.assertEqual(windows[0].validation_start, date(2019, 1, 1))
        self.assertEqual(windows[0].validation_end, date(2019, 3, 31))
        self.assertEqual(windows[1].train_end, date(2019, 3, 31))
        self.assertLess(windows[1].train_end, windows[1].validation_start)

    def test_walk_forward_weight_optimizer_uses_training_ic_only(self):
        windows = generate_walk_forward_windows(
            train_start=date(2018, 1, 1),
            first_validation_start=date(2019, 1, 1),
            final_validation_end=date(2019, 3, 31),
            validation_months=3,
            step_months=3,
        )
        factor_ic_by_date = {
            date(2018, 1, 31): {"momentum": 0.06, "value": 0.01},
            date(2018, 2, 28): {"momentum": 0.04, "value": 0.01},
            date(2019, 1, 31): {"momentum": -0.02, "value": 0.20},
        }

        optimized = optimize_walk_forward_factor_weights(factor_ic_by_date, windows)

        self.assertEqual(len(optimized), 1)
        self.assertEqual(optimized[0].validation_start, date(2019, 1, 1))
        self.assertGreater(optimized[0].weights["momentum"], optimized[0].weights["value"])
        self.assertAlmostEqual(sum(optimized[0].weights.values()), 1.0)
        self.assertNotIn(date(2019, 1, 31), optimized[0].training_dates)

    def test_quarterly_retrain_windows_use_previous_four_years_for_self_trained_models(self):
        windows = generate_quarterly_retrain_windows(
            first_prediction_start=date(2023, 1, 1),
            final_prediction_end=date(2023, 6, 30),
            training_years=4,
        )

        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0].train_start, date(2019, 1, 1))
        self.assertEqual(windows[0].train_end, date(2022, 12, 31))
        self.assertEqual(windows[0].prediction_start, date(2023, 1, 1))
        self.assertEqual(windows[0].prediction_end, date(2023, 3, 31))
        self.assertEqual(windows[1].train_start, date(2019, 4, 1))
        self.assertEqual(windows[1].train_end, date(2023, 3, 31))

    def test_watchlist_manager_applies_entry_exit_hysteresis_stop_loss_events_and_crisis_clear(self):
        manager = WatchlistManager(entry_percentile=0.15, exit_percentile=0.25, stop_loss=0.08)
        scores = {("T%d" % index): float(11 - index) for index in range(1, 11)}

        watchlist = manager.update(
            scores=scores,
            previous_watchlist={"T3", "T8", "LOSS", "EVENT"},
            current_returns={"LOSS": -0.09},
            negative_events={"EVENT"},
            regime=Regime.R3_SIDEWAYS,
        )

        self.assertEqual(watchlist, ["T1", "T2", "T3"])
        self.assertEqual(
            manager.update(scores=scores, previous_watchlist=set(watchlist), regime=Regime.R5_CRISIS),
            [],
        )

    def test_watchlist_manager_blocks_new_entries_during_earnings_blackout(self):
        manager = WatchlistManager(entry_percentile=0.20, exit_percentile=0.40)
        scores = {"AAPL": 1.0, "MSFT": 0.9, "NVDA": 0.8, "TSLA": 0.7, "AMZN": 0.6}

        watchlist = manager.update(
            scores=scores,
            previous_watchlist={"MSFT"},
            earnings_blackout={"AAPL", "MSFT"},
            regime=Regime.R1_BULL_LOW_VOL,
        )

        self.assertEqual(watchlist, ["MSFT"])

    def test_watchlist_manager_requires_regime_sharpe_for_new_entries(self):
        manager = WatchlistManager(entry_percentile=0.40, exit_percentile=0.40)
        scores = {"LOW": 1.0, "PASS": 0.9, "OTHER": 0.8, "TAIL": 0.7, "LAST": 0.6}

        watchlist = manager.update(
            scores=scores,
            previous_watchlist=set(),
            regime_sharpe_by_ticker={"LOW": 0.70, "PASS": 0.85},
            min_regime_sharpe=0.80,
            regime=Regime.R1_BULL_LOW_VOL,
        )

        self.assertEqual(watchlist, ["PASS"])

    def test_earnings_blackout_symbols_marks_two_day_window_around_event_dates(self):
        blackout = earnings_blackout_symbols(
            signal_date=date(2024, 5, 10),
            earnings_dates={"AAPL": date(2024, 5, 8), "MSFT": date(2024, 5, 13), "NVDA": date(2024, 5, 20)},
            window_days=2,
        )

        self.assertEqual(blackout, {"AAPL"})

    def test_watchlist_size_scales_with_account_capital(self):
        self.assertEqual(watchlist_size_for_capital(50_000), 20)
        self.assertEqual(watchlist_size_for_capital(125_000), 30)
        self.assertEqual(watchlist_size_for_capital(250_000), 40)

    def test_exit_percentile_relaxes_when_expected_turnover_exceeds_target(self):
        self.assertAlmostEqual(adjusted_exit_percentile_for_turnover(0.25, expected_turnover=3.0, target_turnover=4.0), 0.25)
        self.assertAlmostEqual(adjusted_exit_percentile_for_turnover(0.25, expected_turnover=8.0, target_turnover=4.0), 0.50)

    def test_cost_model_estimates_commission_spread_impact_slippage_and_turnover_drag(self):
        model = TradingCostModel(
            commission_per_share=0.005,
            spread_bps=3,
            impact_bps=5,
            slippage_bps=4,
        )

        cost = model.estimate_order_cost(notional=10_000, shares=50)

        self.assertAlmostEqual(cost.total_cost, 12.25)
        self.assertAlmostEqual(model.annual_turnover_drag(two_way_cost_rate=0.0015, annual_turnover=4.0), 0.006)

    def test_tax_policy_prefers_short_term_losses_and_preserves_near_long_term_gains(self):
        policy = TaxPolicy(long_term_days=365, preserve_gain_within_days=30)
        lots = [
            TaxLot("AAPL", quantity=10, cost_basis=120, market_price=100, holding_days=40),
            TaxLot("MSFT", quantity=10, cost_basis=80, market_price=110, holding_days=350),
            TaxLot("NVDA", quantity=10, cost_basis=70, market_price=80, holding_days=120),
        ]

        ordered = policy.order_sell_candidates(lots)

        self.assertEqual([lot.ticker for lot in ordered], ["AAPL", "NVDA", "MSFT"])
        self.assertTrue(policy.is_wash_sale_blocked("AAPL", sold_loss_dates=[date(2024, 1, 10)], buy_date=date(2024, 2, 1)))

    def test_llm_qualitative_filter_removes_negative_sentiment_and_flags(self):
        kept = filter_by_qualitative_signals(
            ["AAPL", "MSFT", "NVDA"],
            {
                "AAPL": QualitativeSignal(sentiment_score=0.2, flag_keywords=[]),
                "MSFT": QualitativeSignal(sentiment_score=-0.4, flag_keywords=[]),
                "NVDA": QualitativeSignal(sentiment_score=0.4, flag_keywords=["guidance cut"]),
            },
        )

        self.assertEqual(kept, ["AAPL"])

    def test_earnings_call_scorer_parses_llm_json_and_maps_lowered_guidance_to_flag(self):
        llm = FakeLLMResponse(
            'Intro text {"sentiment_score": 0.45, "guidance": "lowered", "red_flags": ["margin pressure"]} trailing text'
        )

        signal = score_earnings_call("management commentary " * 400, llm_client=llm, max_chars=300)

        self.assertAlmostEqual(signal.sentiment_score, 0.45)
        self.assertEqual(signal.flag_keywords, ["margin pressure", "guidance lowered"])
        self.assertLessEqual(len(llm.prompts[0]), 700)

    def test_earnings_call_scorer_accepts_design_json_sentiment_and_confidence_fields(self):
        llm = FakeLLMResponse(
            'Result {"sentiment": 0.62, "guidance": "raised", "confidence": "high", "flag_keywords": ["beat"]}'
        )

        signal = score_earnings_call("management commentary " * 10, llm_client=llm, max_chars=120)

        self.assertAlmostEqual(signal.sentiment_score, 0.62)
        self.assertEqual(signal.flag_keywords, ["beat"])
        self.assertEqual(signal.confidence, "high")

    def test_sec_8k_negative_event_flags_feed_watchlist_exits(self):
        flagged = negative_event_tickers_from_sec_filings(
            {
                "AAPL": [{"form": "8-K", "items": "4.02"}],
                "MSFT": [{"form": "8-K", "items": "2.02"}],
                "NVDA": [{"form": "4", "items": ""}],
            }
        )

        self.assertEqual(flagged, {"AAPL"})

    def test_factor_research_checklist_requires_all_steps_and_blocks_lookahead_data(self):
        checklist = FactorResearchChecklist(
            completed_steps=set(range(1, 11)),
            signal_date=date(2024, 5, 10),
            data_release_date=date(2024, 5, 8),
        )
        lookahead = FactorResearchChecklist(
            completed_steps=set(range(1, 11)),
            signal_date=date(2024, 5, 10),
            data_release_date=date(2024, 5, 12),
        )

        self.assertTrue(checklist.is_admissible())
        self.assertFalse(lookahead.is_admissible())
        self.assertFalse(FactorResearchChecklist(completed_steps={1, 2}, signal_date=date(2024, 1, 1), data_release_date=date(2024, 1, 1)).is_admissible())

    def test_lookahead_review_checklist_requires_publication_and_slice_isolation_controls(self):
        passed = LookAheadReviewChecklist(
            uses_publication_dates=True,
            signal_after_data_release=True,
            no_future_columns=True,
            train_validation_separated=True,
            survivor_bias_checked=True,
            adjusted_prices_only=True,
        )
        failed = LookAheadReviewChecklist(
            uses_publication_dates=True,
            signal_after_data_release=False,
            no_future_columns=True,
            train_validation_separated=False,
            survivor_bias_checked=True,
            adjusted_prices_only=False,
        )

        self.assertTrue(passed.passed)
        self.assertEqual(passed.reasons(), [])
        self.assertFalse(failed.passed)
        self.assertEqual(
            failed.reasons(),
            ["signal_after_data_release", "train_validation_separated", "adjusted_prices_only"],
        )

    def test_rd_discovery_loop_retries_until_factor_passes_admission(self):
        attempts = []

        def runner(_description):
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                return FactorExperimentResult(
                    accepted=False,
                    metrics=FactorMetrics("short_interest_pressure", 0.01, 0.2, 0.50, 0.02, 1, 0.005),
                    error="IC below threshold",
                )
            return FactorExperimentResult(
                accepted=True,
                metrics=FactorMetrics("short_interest_pressure", 0.03, 0.5, 0.60, 0.10, 4, 0.03),
            )

        result = RDDiscoveryLoop(max_iterations=2, runner=runner).run("short interest pressure predicts underperformance")

        self.assertTrue(result.accepted)
        self.assertEqual(attempts, [1, 2])

    def test_rd_agent_admission_requires_manual_review_after_statistical_acceptance(self):
        result = FactorExperimentResult(
            accepted=True,
            metrics=FactorMetrics("rd_text_factor", 0.03, 0.5, 0.60, 0.10, 4, 0.03),
            economic_rationale="Negative filing tone predicts short-term underperformance after disclosure.",
        )

        pending = evaluate_rd_agent_admission(result, manual_reviewed=False)
        approved = evaluate_rd_agent_admission(result, manual_reviewed=True)

        self.assertFalse(pending.passed)
        self.assertIn("manual_review", pending.reasons)
        self.assertTrue(approved.passed)

    def test_rd_agent_admission_rejects_black_box_factors_without_economic_rationale(self):
        result = FactorExperimentResult(
            accepted=True,
            metrics=FactorMetrics("opaque_signal", 0.03, 0.5, 0.60, 0.10, 4, 0.03),
        )

        decision = evaluate_rd_agent_admission(result, manual_reviewed=True)

        self.assertFalse(decision.passed)
        self.assertIn("economic_rationale", decision.reasons)

    def test_monthly_research_report_summarizes_discoveries_rejections_and_retirements(self):
        report = MonthlyResearchReport(
            period="2026-06",
            accepted_factors=["short_interest_pressure"],
            rejected_factors=["overfit_news_chatter"],
            retired_factors=["old_momentum"],
        )

        payload = report.to_dict()

        self.assertEqual(payload["period"], "2026-06")
        self.assertEqual(payload["accepted_count"], 1)
        self.assertIn("short_interest_pressure", payload["summary"])
        self.assertIn("old_momentum", payload["summary"])

    def test_monitoring_reduces_position_scale_on_ir_degradation_and_pipeline_runs_in_order(self):
        monitor = PerformanceMonitor(historical_ir_mean=0.5, historical_ir_std=0.2)

        self.assertTrue(monitor.is_degraded(recent_ir=0.1))
        self.assertEqual(monitor.position_scale(recent_ir=0.1), 0.5)
        self.assertFalse(monitor.has_consecutive_degradation([0.1, 0.4, 0.1], required_months=3))
        self.assertTrue(monitor.has_consecutive_degradation([0.1, 0.1, 0.1], required_months=3))

        calls = []
        pipeline = DailyPipeline(
            data_update=lambda: calls.append("data"),
            factor_compute=lambda: calls.append("factor"),
            signal_generate=lambda: calls.append("signal"),
            execute=lambda: calls.append("execute"),
        )

        pipeline.run()
        self.assertEqual(calls, ["data", "factor", "signal", "execute"])

    def test_monitoring_degradation_action_requires_three_consecutive_bad_ir_months(self):
        monitor = PerformanceMonitor(historical_ir_mean=0.5, historical_ir_std=0.2)

        review = monitor.degradation_action([0.1, 0.1, 0.1])
        stable = monitor.degradation_action([0.1, 0.4, 0.1])

        self.assertTrue(review.manual_review)
        self.assertEqual(review.position_scale, 0.5)
        self.assertEqual(review.reason, "three_month_ir_degradation")
        self.assertFalse(stable.manual_review)
        self.assertEqual(stable.position_scale, 1.0)

    def test_daily_pipeline_health_report_records_success_and_stops_after_failed_stage(self):
        calls = []
        healthy = DailyPipeline(
            data_update=lambda: calls.append("data"),
            factor_compute=lambda: calls.append("factor"),
            signal_generate=lambda: calls.append("signal"),
            execute=lambda: calls.append("execute"),
        ).run_with_report()

        failed_calls = []

        def fail_factor():
            failed_calls.append("factor")
            raise RuntimeError("factor failed")

        failed = DailyPipeline(
            data_update=lambda: failed_calls.append("data"),
            factor_compute=fail_factor,
            signal_generate=lambda: failed_calls.append("signal"),
            execute=lambda: failed_calls.append("execute"),
        ).run_with_report()

        self.assertTrue(healthy.ok)
        self.assertEqual([stage.name for stage in healthy.stages], ["data_update", "factor_compute", "signal_generate", "execute"])
        self.assertFalse(failed.ok)
        self.assertEqual([stage.name for stage in failed.stages], ["data_update", "factor_compute"])
        self.assertEqual(failed.stages[-1].error, "factor failed")
        self.assertEqual(failed_calls, ["data", "factor"])


class FakeLLMResponse:
    def __init__(self, response):
        self.response = response
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.response


if __name__ == "__main__":
    unittest.main()
