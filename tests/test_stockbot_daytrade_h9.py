import json
import os
import sys
import unittest
from dataclasses import fields
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


import quantbot.daytrade as daytrade_module
from quantbot.daytrade import (
    DayTradeBacktestResult,
    DayTradeBacktester,
    DayTradeConfig,
    DayTradeOptimizationResult,
    MinuteBar,
    build_daytrade_walk_forward_evidence,
    compare_daytrade_configs,
    daytrade_candidate_feature_requirements,
    daytrade_h9_candidate_configs,
    evaluate_daytrade_config,
    run_daytrade_walk_forward_optimization,
)
from quantbot.daytrade_strategy import (
    DailyUniverseSnapshot,
    IntradayFeatureBuildConfig,
    IntradayFeatureRequirements,
    IntradayFeatureSnapshot,
    build_intraday_feature_snapshots,
    preregistered_daytrade_h9_experiments,
    preregistered_daytrade_global_experiments,
)
from quantbot.daytrade_validation_artifact import (
    verify_daytrade_adaptive_validation_artifact,
)
from tests.test_stockbot_daytrade_validation_artifact import (
    ISSUER as ADAPTIVE_ISSUER,
    KEY as ADAPTIVE_KEY,
    _signed_artifact,
)
from tests.historical_trial_fixtures import (
    sign_historical_trial_artifact_payload,
    sign_historical_trial_proof_payload,
    signed_historical_trial_artifact,
)


UTC = timezone.utc


class DayTradeDynamicUniverseTest(unittest.TestCase):
    def test_dynamic_schedule_filters_daily_tickers_and_aggregates_stable_id(self):
        first_day = date(2025, 1, 2)
        second_day = date(2025, 1, 3)
        bars = (
            _winning_session("AAPL", first_day)
            + _winning_session("MSFT", first_day)
            + _winning_session("XYZ", second_day)
        )
        schedule = (
            DailyUniverseSnapshot(first_day, {"security-1": "AAPL"}),
            DailyUniverseSnapshot(second_day, {"security-1": "XYZ"}),
        )

        result = DayTradeBacktester().run(bars, universe_schedule=schedule)

        self.assertEqual(result.trade_count, 2)
        self.assertEqual({trade.symbol for trade in result.trades}, {"AAPL", "XYZ"})
        self.assertEqual(
            {trade.security_id for trade in result.trades},
            {"security-1"},
        )
        self.assertEqual(result.pnl_by_security_id["security-1"]["trades"], 2)

    def test_dynamic_schedule_fails_closed_when_a_bar_day_has_no_snapshot(self):
        bars = _winning_session("AAPL", date(2025, 1, 2))

        with self.assertRaisesRegex(ValueError, "missing trading days"):
            DayTradeBacktester().run(bars, universe_schedule=())

    def test_daily_snapshot_is_immutable_and_rejects_ambiguous_tickers(self):
        snapshot = DailyUniverseSnapshot(
            date(2025, 1, 2),
            {" security-1 ": "aapl"},
        )

        self.assertEqual(snapshot.ticker_by_security_id["security-1"], "AAPL")
        self.assertEqual(snapshot.security_id_for("aapl"), "security-1")
        with self.assertRaises(TypeError):
            snapshot.ticker_by_security_id["security-2"] = "MSFT"
        with self.assertRaisesRegex(ValueError, "multiple security_ids"):
            DailyUniverseSnapshot(
                date(2025, 1, 2),
                {"security-1": "AAPL", "security-2": "aapl"},
            )

    def test_legacy_static_run_keeps_symbol_identity_fallback(self):
        result = DayTradeBacktester().run(
            _winning_session("AAPL", date(2025, 1, 2))
        )

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].security_id, "")
        self.assertIn("AAPL", result.pnl_by_security_id)

    def test_evaluate_and_compare_forward_one_dynamic_schedule(self):
        trading_day = date(2025, 1, 2)
        bars = _winning_session("AAPL", trading_day) + _winning_session(
            "MSFT",
            trading_day,
        )
        schedule = (
            DailyUniverseSnapshot(trading_day, {"security-1": "AAPL"}),
        )

        evaluated = evaluate_daytrade_config(
            bars,
            DayTradeConfig(),
            candidate_name="baseline",
            universe_schedule=schedule,
        )
        compared = compare_daytrade_configs(
            bars,
            DayTradeConfig(),
            DayTradeConfig(),
            universe_schedule=schedule,
        )

        self.assertEqual(evaluated["trade_count"], 1)
        self.assertEqual(evaluated["by_security_id"]["security-1"]["trades"], 1)
        self.assertTrue(
            all(item["trade_count"] == 1 for item in compared.values())
        )


class IntradayFeatureRequirementsTest(unittest.TestCase):
    def test_default_still_requires_full_proxy_and_market_feature_inputs(self):
        with self.assertRaisesRegex(ValueError, "sector proxy mapping required"):
            IntradayFeatureBuildConfig(symbols=("AAPL",))

        config = IntradayFeatureBuildConfig(
            symbols=("AAPL",),
            sector_proxy_by_symbol={"AAPL": "XLK"},
        )

        self.assertEqual(
            config.required_symbols,
            ("AAPL", "SPY", "QQQ", "XLK"),
        )

    def test_h3_and_h9_can_build_rvol_without_proxy_market_or_correlation(self):
        h3 = daytrade_candidate_feature_requirements(
            "daytrade-h3-pre-entry-confirmation:trial-03"
        )
        h9 = IntradayFeatureRequirements.for_candidate(
            "daytrade-h9-rvol-reward-risk:trial-01"
        )
        self.assertEqual(h3, IntradayFeatureRequirements.rvol_only())
        self.assertEqual(h9, IntradayFeatureRequirements.rvol_only())
        config = IntradayFeatureBuildConfig(
            symbols=("AAPL",),
            relative_volume_lookback_sessions=2,
            observation_start=time(9, 45),
            observation_end=time(9, 46),
            requirements=h9,
        )

        with mock.patch(
            "quantbot.daytrade_strategy.features._maximum_prior_correlation",
            side_effect=AssertionError("correlation must not be computed"),
        ) as correlation:
            snapshots = build_intraday_feature_snapshots(
                _rvol_only_feature_bars(),
                config,
            )

        self.assertEqual(config.required_symbols, ("AAPL",))
        correlation.assert_not_called()
        self.assertEqual(len(snapshots), 1)
        self.assertAlmostEqual(snapshots[0].relative_volume, 2.0)
        self.assertEqual(snapshots[0].spy_return, 0.0)
        self.assertEqual(snapshots[0].max_abs_correlation, 0.0)


class DayTradeH9Test(unittest.TestCase):
    def test_sealed_financial_run_uses_frozen_adaptive_statistics_without_h1_h8_returns(self):
        session_days = tuple(date(2026, month, 2) for month in range(1, 7))
        sessions = tuple(_winning_session("AAPL", day) for day in session_days)
        bars = tuple(bar for session in sessions for bar in session)
        feature_snapshots = tuple(
            IntradayFeatureSnapshot(
                symbol="AAPL",
                observed_at=bar.timestamp,
                atr=1.0,
                relative_volume=2.0,
                gap_fraction=0.0,
                premarket_volume=0.0,
                spy_return=0.0,
                qqq_return=0.0,
                sector_relative_strength=0.0,
            )
            for session in sessions
            for bar in session[-2:]
        )
        schedule = tuple(
            DailyUniverseSnapshot(day, {"security-1": "AAPL"})
            for day in session_days
        )
        baseline = DayTradeConfig(
            entry_end="10:30",
            min_relative_volume=2.0,
            reward_risk=1.5,
            cost_bps=5.0,
        )
        candidate_name, candidate = daytrade_h9_candidate_configs(baseline)[0]
        adaptive = verify_daytrade_adaptive_validation_artifact(
            _signed_artifact(),
            trusted_keys={ADAPTIVE_ISSUER: ADAPTIVE_KEY},
        )

        result = run_daytrade_walk_forward_optimization(
            bars,
            start=session_days[0],
            end=session_days[-1],
            base_config=baseline,
            train_months=3,
            validation_months=1,
            max_candidates=2,
            feature_snapshots=feature_snapshots,
            experiment_registry=preregistered_daytrade_global_experiments(),
            candidate_configs=(
                ("daytrade-h9-rvol-reward-risk:baseline", baseline),
                (candidate_name, candidate),
            ),
            universe_schedule=schedule,
            adaptive_validation_artifact=adaptive,
        )

        statistics = result.statistical_validation
        self.assertEqual(result.declared_trial_count, 21)
        self.assertEqual(result.completed_trial_count, 21)
        self.assertEqual(
            statistics["statistical_source"],
            "signed_adaptive_validation_artifact",
        )
        self.assertEqual(
            statistics["deflated_sharpe_ratio"]["confidence"],
            adaptive.dsr_confidence,
        )
        self.assertEqual(
            statistics["probability_of_backtest_overfitting"]["probability"],
            adaptive.pbo,
        )
        self.assertEqual(
            set(statistics["trial_daily_returns"]),
            {
                "daytrade-h9-rvol-reward-risk:baseline",
                candidate_name,
            },
        )
        raw = statistics["canonical_execution_inputs"]
        self.assertEqual(
            set(raw),
            {
                "initial_capital",
                "observation_dates",
                "baseline_5bps",
                "candidate_5bps",
                "candidate_12bps",
            },
        )
        self.assertEqual(
            raw["observation_dates"],
            [item.isoformat() for item in session_days],
        )
        for arm_name in (
            "baseline_5bps",
            "candidate_5bps",
            "candidate_12bps",
        ):
            arm = raw[arm_name]
            cost_bps = 12.0 if arm_name == "candidate_12bps" else 5.0
            cost_rate = cost_bps / 10_000.0
            self.assertEqual(set(arm), {"daily", "trades"})
            self.assertEqual(
                [item["day"] for item in arm["daily"]],
                raw["observation_dates"],
            )
            equity = raw["initial_capital"]
            pnl_by_day = {}
            for trade in arm["trades"]:
                self.assertEqual(
                    set(trade),
                    {
                        "day",
                        "entry_timestamp",
                        "exit_timestamp",
                        "side",
                        "entry_event_price",
                        "exit_event_price",
                        "entry_price",
                        "exit_price",
                        "quantity",
                        "notional",
                        "borrow_cost",
                        "pnl",
                        "security_id",
                    },
                )
                self.assertEqual(trade["security_id"], "security-1")
                if trade["side"] == "long":
                    expected_entry = trade["entry_event_price"] * (
                        1.0 + cost_rate
                    )
                    expected_exit = trade["exit_event_price"] * (
                        1.0 - cost_rate
                    )
                else:
                    expected_entry = trade["entry_event_price"] * (
                        1.0 - cost_rate
                    )
                    expected_exit = trade["exit_event_price"] * (
                        1.0 + cost_rate
                    )
                self.assertAlmostEqual(trade["entry_price"], expected_entry)
                self.assertAlmostEqual(trade["exit_price"], expected_exit)
                self.assertAlmostEqual(
                    trade["notional"],
                    trade["entry_price"] * trade["quantity"],
                )
                direction = 1.0 if trade["side"] == "long" else -1.0
                self.assertAlmostEqual(
                    trade["pnl"],
                    direction
                    * (trade["exit_price"] - trade["entry_price"])
                    * trade["quantity"]
                    - trade["borrow_cost"],
                )
                pnl_by_day[trade["day"]] = (
                    pnl_by_day.get(trade["day"], 0.0) + trade["pnl"]
                )
            for daily in arm["daily"]:
                self.assertAlmostEqual(
                    daily["pnl"],
                    pnl_by_day.get(daily["day"], 0.0),
                )
                equity += daily["pnl"]
                self.assertAlmostEqual(daily["ending_equity"], equity)
        self.assertNotEqual(
            raw["candidate_5bps"]["trades"][0]["entry_price"],
            raw["candidate_12bps"]["trades"][0]["entry_price"],
        )

    def test_fixed_h3_h9_uses_all_24_months_while_walk_forward_stays_diagnostic(self):
        session_days = tuple(
            date(year, month, 2)
            for year in (2023, 2024)
            for month in range(1, 13)
        )
        sessions = tuple(_winning_session("AAPL", day) for day in session_days)
        bars = tuple(bar for session in sessions for bar in session)
        feature_snapshots = tuple(
            IntradayFeatureSnapshot(
                symbol="AAPL",
                observed_at=bar.timestamp,
                atr=1.0,
                relative_volume=2.0,
                gap_fraction=0.0,
                premarket_volume=0.0,
                spy_return=0.0,
                qqq_return=0.0,
                sector_relative_strength=0.0,
            )
            for session in sessions
            for bar in session[-2:]
        )
        schedule = tuple(
            DailyUniverseSnapshot(day, {"security-1": "AAPL"})
            for day in session_days
        )
        baseline = DayTradeConfig(
            entry_end="10:30",
            min_relative_volume=2.0,
            reward_risk=1.5,
            cost_bps=5.0,
        )
        h9_name, h9_config = daytrade_h9_candidate_configs(baseline)[0]

        result = run_daytrade_walk_forward_optimization(
            bars,
            start=session_days[0],
            end=session_days[-1],
            base_config=baseline,
            train_months=3,
            validation_months=1,
            max_candidates=2,
            feature_snapshots=feature_snapshots,
            experiment_registry=preregistered_daytrade_global_experiments(),
            candidate_configs=(
                ("daytrade-h9-rvol-reward-risk:baseline", baseline),
                (h9_name, h9_config),
            ),
            universe_schedule=schedule,
        )

        fixed = result.statistical_validation
        diagnostic = fixed["walk_forward_diagnostic"]
        fixed_months = {
            value[:7] for value in fixed["observation_dates"]
        }
        diagnostic_months = {
            value[:7] for value in diagnostic["observation_dates"]
        }
        self.assertEqual(fixed["evaluation_scope"], "full_request_interval")
        self.assertEqual(len(fixed_months), 24)
        self.assertEqual(len(diagnostic_months), 21)
        self.assertEqual(
            fixed["trial_metrics"][h9_name]["sessions"],
            24,
        )
        self.assertEqual(
            diagnostic["purpose"],
            "selection_diagnostic_only",
        )

    def test_immutable_h1_h8_history_artifact_has_a_formal_completion_path(self):
        registry = preregistered_daytrade_global_experiments()
        dates = tuple(date(2024, 1, day) for day in range(2, 10))
        local_ids = {
            trial_id
            for record in registry.records
            for trial_id in record.trial_ids
        }
        historical_ids = sorted(set(registry.known_trial_ids).difference(local_ids))
        experiment_by_trial = {
            trial_id: record.experiment_id
            for record in registry.known_records
            for trial_id in record.trial_ids
            if trial_id in historical_ids
        }
        issuer = "daytrade-history-test"
        signing_key = b"daytrade-history-test-key"
        historical_identity = {
            "source_dataset_hash": "1" * 64,
            "minute_data_hash": "2" * 64,
            "calendar_hash": "3" * 64,
            "construction_evidence_hash": "4" * 64,
        }
        trial_returns = {
            trial_id: [
                ((index % 5) - 2) * 0.0001 + offset * 0.00001
                for index, _day in enumerate(dates)
            ]
            for offset, trial_id in enumerate(historical_ids)
        }
        artifact, payload, historical_dataset_hash = (
            signed_historical_trial_artifact(
                issuer=issuer,
                signing_key=signing_key,
                historical_identity=historical_identity,
                observation_dates=dates,
                trial_returns=trial_returns,
                experiment_by_trial=experiment_by_trial,
                evaluation_dataset_hash="d" * 64,
            )
        )

        with mock.patch.dict(
            os.environ,
            {
                "STOCKBOT_TRUSTED_EXPERIMENT_KEYS_JSON": json.dumps(
                    {issuer: signing_key.hex()}
                )
            },
        ):
            historical, completed, metadata = (
                daytrade_module._validated_historical_trial_returns(
                    artifact,
                    registry=registry,
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )
            )
            registry.import_known_experiments((), completed_trials=completed)

            self.assertEqual(set(historical), set(historical_ids))
            self.assertEqual(len(completed), 20)
            self.assertEqual(registry.global_completed_trial_count, 20)
            self.assertTrue(metadata["available"])
            self.assertEqual(metadata["issuer"], issuer)
            self.assertEqual(metadata["permit_dataset_hash"], "d" * 64)

            first_trial = historical_ids[0]
            tampered_proof_payload = {
                **payload,
                "trial_execution_evidence": {
                    **payload["trial_execution_evidence"],
                    first_trial: {
                        **payload["trial_execution_evidence"][first_trial],
                        "signature": "0" * 64,
                    },
                },
            }
            tampered_proof_artifact = sign_historical_trial_artifact_payload(
                tampered_proof_payload,
                signing_key=signing_key,
                evaluation_dataset_hash="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "proof signature is invalid"):
                daytrade_module._validated_historical_trial_returns(
                    tampered_proof_artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

            missing_cost_proof = dict(
                payload["trial_execution_evidence"][first_trial]
            )
            missing_cost_proof.pop("permit_cost_model_hash")
            missing_cost_payload = {
                **payload,
                "trial_execution_evidence": {
                    **payload["trial_execution_evidence"],
                    first_trial: missing_cost_proof,
                },
            }
            missing_cost_artifact = sign_historical_trial_artifact_payload(
                missing_cost_payload,
                signing_key=signing_key,
                evaluation_dataset_hash="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "proof fields are invalid"):
                daytrade_module._validated_historical_trial_returns(
                    missing_cost_artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

            stale_policy_proof = sign_historical_trial_proof_payload(
                {
                    **payload["trial_execution_evidence"][first_trial],
                    "policy_version": "quantbot.acceptance.stale",
                },
                signing_key=signing_key,
            )
            stale_policy_payload = {
                **payload,
                "trial_execution_evidence": {
                    **payload["trial_execution_evidence"],
                    first_trial: stale_policy_proof,
                },
            }
            stale_policy_artifact = sign_historical_trial_artifact_payload(
                stale_policy_payload,
                signing_key=signing_key,
                evaluation_dataset_hash="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "policy_version is not current"):
                daytrade_module._validated_historical_trial_returns(
                    stale_policy_artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

            rebound_returns_payload = {
                **payload,
                "trial_daily_returns": {
                    **payload["trial_daily_returns"],
                    first_trial: [
                        0.5,
                        *payload["trial_daily_returns"][first_trial][1:],
                    ],
                },
            }
            rebound_returns_artifact = sign_historical_trial_artifact_payload(
                rebound_returns_payload,
                signing_key=signing_key,
                evaluation_dataset_hash="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "return_history_hash mismatch"):
                daytrade_module._validated_historical_trial_returns(
                    rebound_returns_artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

            with self.assertRaisesRegex(ValueError, "permit dataset identity"):
                daytrade_module._validated_historical_trial_returns(
                    artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="e" * 64,
                )

            tampered = {
                **artifact,
                "trial_daily_returns": {
                    **artifact["trial_daily_returns"],
                    historical_ids[0]: [0.5, *artifact["trial_daily_returns"][historical_ids[0]][1:]],
                },
            }
            with self.assertRaisesRegex(ValueError, "content_hash mismatch"):
                daytrade_module._validated_historical_trial_returns(
                    tampered,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

            wrong_identity_payload = {
                **payload,
                "trial_execution_evidence": {
                    **payload["trial_execution_evidence"],
                    historical_ids[0]: {
                        **payload["trial_execution_evidence"][historical_ids[0]],
                        "permit_dataset_hash": "f" * 64,
                    },
                },
            }
            wrong_identity_artifact = sign_historical_trial_artifact_payload(
                wrong_identity_payload,
                signing_key=signing_key,
                evaluation_dataset_hash="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "permit_manifest_hash mismatch"):
                daytrade_module._validated_historical_trial_returns(
                    wrong_identity_artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

            unknown_payload = {**payload, "issuer": "unknown-history-issuer"}
            unknown_artifact = sign_historical_trial_artifact_payload(
                unknown_payload,
                signing_key=signing_key,
                evaluation_dataset_hash="d" * 64,
            )
            with self.assertRaisesRegex(ValueError, "issuer key is missing"):
                daytrade_module._validated_historical_trial_returns(
                    unknown_artifact,
                    registry=preregistered_daytrade_global_experiments(),
                    observation_dates=dates,
                    initial_capital=10_000.0,
                    evaluation_dataset_hash="d" * 64,
                )

    def test_fixed_candidate_quarters_and_stable_id_concentration_are_real_aggregates(self):
        daily = (
            mock.Mock(day=date(2024, 1, 2), return_pct=0.10),
            mock.Mock(day=date(2024, 2, 2), return_pct=-0.05),
            mock.Mock(day=date(2024, 4, 2), return_pct=-0.01),
        )
        trades = (
            mock.Mock(security_id="sec-a", symbol="AAA", pnl=60.0),
            mock.Mock(security_id="sec-b", symbol="BBB", pnl=30.0),
            mock.Mock(security_id="sec-c", symbol="CCC", pnl=10.0),
            mock.Mock(security_id="sec-d", symbol="DDD", pnl=-20.0),
        )

        quarters = daytrade_module._quarterly_compounded_returns(daily)
        concentration = daytrade_module._profit_concentration(trades)

        self.assertAlmostEqual(quarters["2024-Q1"], 1.10 * 0.95 - 1.0)
        self.assertAlmostEqual(quarters["2024-Q2"], -0.01)
        self.assertTrue(concentration["available"])
        self.assertEqual(concentration["profit_by_security_id"]["sec-d"], -20.0)
        self.assertAlmostEqual(concentration["top_symbol_profit_contribution"], 0.60)
        self.assertAlmostEqual(concentration["top_two_profit_contribution"], 0.90)

    def test_walk_forward_evidence_requires_available_dsr_95_and_pbo_05(self):
        baseline = DayTradeBacktestResult(
            strategy="daytrade",
            trades=[],
            daily_results=[],
            initial_capital=10_000.0,
            ending_equity=10_100.0,
            cumulative_return=0.01,
            annual_return=0.04,
            sharpe=1.0,
            max_drawdown=0.04,
            win_rate=0.50,
            profit_factor=1.1,
            average_holding_minutes=10.0,
            trade_count=100,
        )
        validation = DayTradeBacktestResult(
            **{
                **baseline.__dict__,
                "cumulative_return": 0.02,
                "sharpe": 1.5,
                "max_drawdown": 0.03,
                "profit_factor": 1.3,
            }
        )

        def evidence(statistics):
            result = DayTradeOptimizationResult(
                best_config=DayTradeConfig(),
                best_config_name="candidate",
                baseline=baseline,
                validation=validation,
                rankings=[],
                windows=[],
                fragile=False,
                declared_trial_count=1,
                completed_trial_count=1,
                statistical_validation=statistics,
            )
            return build_daytrade_walk_forward_evidence(result)

        self.assertFalse(evidence({"available": False}).passed)
        self.assertFalse(
            evidence(
                {
                    "available": True,
                    "deflated_sharpe_ratio": {"available": True, "confidence": 0.949},
                    "probability_of_backtest_overfitting": {"available": True, "probability": 0.05},
                }
            ).passed
        )
        self.assertFalse(
            evidence(
                {
                    "available": True,
                    "deflated_sharpe_ratio": {"available": True, "confidence": 0.95},
                    "probability_of_backtest_overfitting": {"available": True, "probability": 0.051},
                }
            ).passed
        )
        self.assertTrue(
            evidence(
                {
                    "available": True,
                    "deflated_sharpe_ratio": {"available": True, "confidence": 0.95},
                    "probability_of_backtest_overfitting": {"available": True, "probability": 0.05},
                }
            ).passed
        )

    def test_global_h1_h9_ledger_counts_all_21_trials_without_reopening_history(self):
        registry = preregistered_daytrade_global_experiments()

        self.assertEqual(registry.total_declared_trials, 1)
        self.assertEqual(registry.global_declared_trial_count, 21)
        self.assertEqual(registry.global_completed_trial_count, 0)
        self.assertEqual(len(registry.known_trial_ids), 21)

    def test_h9_is_one_preregistered_reward_risk_only_trial(self):
        baseline = DayTradeConfig(
            min_relative_volume=2.0,
            reward_risk=1.5,
            cost_bps=5.0,
            max_volume_participation=0.10,
        )
        registry = preregistered_daytrade_h9_experiments()
        candidates = daytrade_h9_candidate_configs(baseline)

        self.assertEqual(registry.total_declared_trials, 1)
        self.assertEqual(
            registry.records[0].trial_ids,
            ("daytrade-h9-rvol-reward-risk:trial-01",),
        )
        self.assertIn("reward_risk_1_5", registry.records[0].baseline_components)
        self.assertEqual(len(candidates), 1)
        name, candidate = candidates[0]
        self.assertEqual(name, registry.records[0].trial_ids[0])
        self.assertEqual(candidate.reward_risk, 2.0)
        changed = {
            item.name
            for item in fields(DayTradeConfig)
            if getattr(candidate, item.name) != getattr(baseline, item.name)
        }
        self.assertEqual(changed, {"reward_risk"})

        with self.assertRaisesRegex(ValueError, "min_relative_volume=2.0"):
            daytrade_h9_candidate_configs(DayTradeConfig())
        with self.assertRaisesRegex(ValueError, "reward_risk=1.5"):
            daytrade_h9_candidate_configs(
                DayTradeConfig(min_relative_volume=2.0, reward_risk=2.0)
            )

    def test_h9_execution_consumes_only_rvol_from_snapshot(self):
        bars = _winning_session("AAPL", date(2025, 1, 2))
        config = DayTradeConfig(
            min_relative_volume=2.0,
            reward_risk=2.0,
            min_directional_spy_return=0.50,
            min_directional_qqq_return=0.50,
            min_directional_sector_strength=0.50,
        )
        rvol_only_snapshot = IntradayFeatureSnapshot(
            symbol="AAPL",
            observed_at=bars[-2].timestamp,
            atr=1.0,
            relative_volume=2.0,
            gap_fraction=0.0,
            premarket_volume=0.0,
            spy_return=0.0,
            qqq_return=0.0,
            sector_relative_strength=0.0,
        )

        payload = evaluate_daytrade_config(
            bars,
            config,
            candidate_name="daytrade-h9-rvol-reward-risk:trial-01",
            feature_snapshots=(rvol_only_snapshot,),
        )

        self.assertEqual(payload["trade_count"], 1)

    def test_walk_forward_reuses_one_schedule_for_h3_baseline_and_h9(self):
        first_day = date(2025, 1, 2)
        second_day = date(2025, 2, 2)
        first_bars = _winning_session("AAPL", first_day)
        second_bars = _winning_session("AAPL", second_day)
        bars = first_bars + second_bars
        snapshots = tuple(
            IntradayFeatureSnapshot(
                symbol="AAPL",
                observed_at=session_bars[-2].timestamp,
                atr=1.0,
                relative_volume=2.0,
                gap_fraction=0.0,
                premarket_volume=0.0,
                spy_return=0.0,
                qqq_return=0.0,
                sector_relative_strength=0.0,
            )
            for session_bars in (first_bars, second_bars)
        )
        daily_snapshots = (
            DailyUniverseSnapshot(first_day, {"security-1": "AAPL"}),
            DailyUniverseSnapshot(second_day, {"security-1": "AAPL"}),
        )
        baseline = DayTradeConfig(
            entry_end="09:46",
            min_relative_volume=2.0,
            reward_risk=1.5,
        )
        h9_name, h9_config = daytrade_h9_candidate_configs(baseline)[0]
        seen_schedules = []
        original_run = daytrade_module._run_candidate_backtest

        def recording_run(*args, **kwargs):
            schedule = kwargs.get("universe_schedule")
            if schedule is None and len(args) >= 5:
                schedule = args[4]
            seen_schedules.append(schedule)
            return original_run(*args, **kwargs)

        with mock.patch.object(
            daytrade_module,
            "_run_candidate_backtest",
            side_effect=recording_run,
        ):
            optimization = run_daytrade_walk_forward_optimization(
                bars,
                start=first_day,
                end=second_day,
                base_config=baseline,
                train_months=1,
                validation_months=1,
                feature_snapshots=snapshots,
                experiment_registry=preregistered_daytrade_global_experiments(),
                candidate_configs=(
                    ("daytrade-h3-pre-entry-confirmation:trial-03", baseline),
                    (h9_name, h9_config),
                ),
                universe_schedule=daily_snapshots,
            )

        self.assertTrue(seen_schedules)
        self.assertIsNotNone(seen_schedules[0])
        self.assertTrue(
            all(schedule is seen_schedules[0] for schedule in seen_schedules)
        )
        self.assertIs(
            seen_schedules[0][first_day],
            daily_snapshots[0],
        )
        self.assertEqual(optimization.declared_trial_count, 21)
        self.assertEqual(optimization.completed_trial_count, 1)
        self.assertFalse(optimization.statistical_validation["available"])
        self.assertIn(
            "immutable aligned H1-H8 return histories are missing",
            optimization.statistical_validation["reason"],
        )

    def test_h9_target_uses_cost_adjusted_next_minute_fill(self):
        bars = _winning_session(
            "AAPL",
            date(2025, 1, 2),
            fill_open=102.0,
            fill_high=105.0,
            fill_low=102.5,
        )
        config = DayTradeConfig(reward_risk=2.0, cost_bps=100.0)

        result = DayTradeBacktester(config).run(bars)

        self.assertEqual(result.trade_count, 1)
        trade = result.trades[0]
        expected_entry = 102.0 * 1.01
        expected_stop = expected_entry * (1.0 - config.stop_loss_pct)
        expected_target = expected_entry + (expected_entry - expected_stop) * 2.0
        self.assertAlmostEqual(trade.entry_price, expected_entry)
        self.assertAlmostEqual(trade.entry_event_price, 102.0)
        self.assertAlmostEqual(trade.exit_event_price, expected_target)
        self.assertAlmostEqual(trade.exit_price, expected_target * 0.99)
        self.assertEqual(trade.entry_timestamp, bars[-1].timestamp)

    def test_short_trade_preserves_event_prices_and_twelve_bps_fills(self):
        config = DayTradeConfig(cost_bps=12.0)
        backtester = DayTradeBacktester(config)
        entry_event_price = 100.0
        exit_event_price = 98.0
        quantity = 10.0
        entry_price = backtester._entry_fill_price(
            "short",
            entry_event_price,
        )
        signal = daytrade_module.DayTradeSignal(
            strategy=config.strategy_name,
            symbol="AAPL",
            timestamp=datetime(2025, 1, 2, 14, 46, tzinfo=UTC),
            entry_price=entry_price,
            stop_price=101.0,
            target_price=98.0,
            quantity=quantity,
            notional=entry_price * quantity,
            side="short",
            security_id="security-1",
            entry_event_price=entry_event_price,
        )
        exit_bar = MinuteBar(
            "AAPL",
            datetime(2025, 1, 2, 14, 47, tzinfo=UTC),
            exit_event_price,
            exit_event_price,
            exit_event_price,
            exit_event_price,
            1_000.0,
            exit_event_price,
        )
        exit_price = backtester._exit_fill_price(
            "short",
            exit_event_price,
        )

        trade = daytrade_module._close_trade(
            signal,
            exit_bar,
            exit_price,
            "take_profit",
            exit_event_price=exit_event_price,
        )

        self.assertEqual(trade.entry_event_price, entry_event_price)
        self.assertEqual(trade.exit_event_price, exit_event_price)
        self.assertAlmostEqual(trade.entry_price, entry_event_price * 0.9988)
        self.assertAlmostEqual(trade.exit_price, exit_event_price * 1.0012)
        self.assertAlmostEqual(
            trade.pnl,
            (trade.entry_price - trade.exit_price) * quantity,
        )

    def test_h9_double_touch_remains_stop_first(self):
        bars = _winning_session(
            "AAPL",
            date(2025, 1, 2),
            fill_open=102.0,
            fill_high=105.0,
            fill_low=100.0,
        )

        result = DayTradeBacktester(
            DayTradeConfig(min_relative_volume=2.0, reward_risk=2.0)
        ).run(bars)

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].exit_reason, "stop_loss")


def _winning_session(
    symbol,
    trading_day,
    *,
    fill_open=101.0,
    fill_high=103.0,
    fill_low=100.9,
):
    start = datetime(
        trading_day.year,
        trading_day.month,
        trading_day.day,
        14,
        30,
        tzinfo=UTC,
    )
    bars = [
        MinuteBar(
            symbol,
            start + timedelta(minutes=index),
            99.5,
            100.0,
            99.0,
            99.8,
            1_000.0,
            99.7,
        )
        for index in range(15)
    ]
    bars.extend(
        (
            MinuteBar(
                symbol,
                start + timedelta(minutes=15),
                100.2,
                101.2,
                100.1,
                101.0,
                1_500.0,
                99.8,
            ),
            MinuteBar(
                symbol,
                start + timedelta(minutes=16),
                fill_open,
                fill_high,
                fill_low,
                fill_high,
                2_000.0,
                fill_open,
            ),
        )
    )
    return bars


def _rvol_only_feature_bars():
    bars = []
    for day_offset, volume_multiplier in ((0, 1.0), (1, 1.0), (2, 2.0)):
        start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC) + timedelta(
            days=day_offset
        )
        bars.extend(
            (
                MinuteBar("AAPL", start, 100, 101, 99, 100, 100 * volume_multiplier),
                MinuteBar(
                    "AAPL",
                    start + timedelta(minutes=15),
                    100,
                    101,
                    99,
                    100,
                    100 * volume_multiplier,
                ),
            )
        )
    return bars


if __name__ == "__main__":
    unittest.main()
