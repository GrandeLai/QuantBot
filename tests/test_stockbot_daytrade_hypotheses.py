import sys
import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.daytrade import (
    DayTradeBacktester,
    DayTradeConfig,
    MinuteBar,
    _daytrade_candidate_configs,
    daytrade_followup_candidate_configs,
    daytrade_h3_candidate_configs,
    daytrade_h6_candidate_configs,
    daytrade_h7_candidate_configs,
    daytrade_h8_candidate_configs,
)
from quantbot.daytrade_strategy import (
    DayTradeHypothesisConfig,
    DayTradeRiskSizingConfig,
    IntradayFeatureBook,
    IntradayFeatureBuildConfig,
    IntradayFeatureSnapshot,
    PortfolioHeatState,
    build_intraday_feature_snapshots,
    evaluate_entry_filters,
    hypothesis_config_from_runtime,
    preregistered_daytrade_experiments,
    preregistered_daytrade_followup_experiments,
    preregistered_daytrade_h3_experiments,
    preregistered_daytrade_h6_experiments,
    preregistered_daytrade_h7_experiments,
    preregistered_daytrade_h8_experiments,
    risk_scaled_stop,
    session_cumulative_vwap,
    size_intraday_position,
    update_vwap_hysteresis,
)
from quantbot.config_loader import (
    DayTradeRuntimeConfig,
    PortfolioRuntimeConfig,
    RiskRuntimeConfig,
)


UTC = timezone.utc


class DayTradeHypothesisTest(unittest.TestCase):
    def test_feature_builder_is_point_in_time_and_prefix_stable(self):
        config = IntradayFeatureBuildConfig(
            symbols=("AAPL",),
            sector_proxy_by_symbol={"AAPL": "XLK"},
            atr_lookback_sessions=2,
            relative_volume_lookback_sessions=2,
            correlation_lookback_sessions=2,
            observation_start=time(9, 45),
            observation_end=time(9, 46),
        )
        bars = _point_in_time_feature_bars()
        decision_at = datetime(2025, 1, 6, 14, 45, tzinfo=UTC)
        prefix = [bar for bar in bars if bar.timestamp <= decision_at]

        prefix_snapshot = build_intraday_feature_snapshots(prefix, config)[-1]
        complete_snapshot = build_intraday_feature_snapshots(bars, config)[-1]

        self.assertEqual(prefix_snapshot, complete_snapshot)
        self.assertEqual(complete_snapshot.observed_at, decision_at)
        self.assertAlmostEqual(complete_snapshot.atr, 3.0)
        self.assertAlmostEqual(complete_snapshot.relative_volume, 2.0)
        self.assertAlmostEqual(complete_snapshot.gap_fraction, 104.0 / 103.0 - 1.0)
        self.assertEqual(complete_snapshot.premarket_volume, 500.0)
        self.assertAlmostEqual(complete_snapshot.spy_return, 501.0 / 500.0 - 1.0)
        self.assertAlmostEqual(complete_snapshot.qqq_return, 402.0 / 400.0 - 1.0)
        self.assertAlmostEqual(
            complete_snapshot.sector_relative_strength,
            (105.0 / 104.0 - 1.0) - (200.2 / 200.0 - 1.0),
        )

    def test_feature_book_does_not_reuse_a_previous_session_snapshot(self):
        first_session = datetime(2025, 1, 2, 14, 45, tzinfo=UTC)
        feature_book = IntradayFeatureBook((_feature(first_session),))

        self.assertIsNotNone(feature_book.latest("AAPL", first_session))
        self.assertIsNone(
            feature_book.latest(
                "AAPL",
                datetime(2025, 1, 3, 14, 45, tzinfo=UTC),
            )
        )

    def test_canonical_runtime_config_translates_to_strategy_and_backtester(self):
        runtime = DayTradeRuntimeConfig()
        portfolio = PortfolioRuntimeConfig()
        risk = RiskRuntimeConfig()

        execution = DayTradeConfig.from_runtime(runtime, portfolio, risk)
        hypothesis = hypothesis_config_from_runtime(runtime)

        self.assertEqual(execution.entry_end, "10:30")
        self.assertTrue(execution.cumulative_vwap)
        self.assertEqual(execution.stop_basis, "atr")
        self.assertEqual(execution.cost_bps, 5.0)
        self.assertEqual(execution.capital_fraction, 0.10)
        self.assertEqual(execution.min_directional_spy_return, 0.0)
        self.assertEqual(execution.min_directional_qqq_return, 0.0)
        self.assertEqual(execution.min_directional_sector_strength, 0.0)
        self.assertEqual(execution.min_relative_volume, 1.5)
        self.assertEqual(hypothesis.side, "long")
        self.assertEqual(hypothesis.min_relative_volume, 1.5)
        self.assertEqual(hypothesis.max_opening_range_atr, 1.5)

    def test_session_vwap_is_cumulative_and_prefix_stable(self):
        start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
        bars = (
            MinuteBar("AAPL", start, 100, 101, 99, 100, 100, 100),
            MinuteBar("AAPL", start + timedelta(minutes=1), 110, 111, 109, 110, 300, 110),
            MinuteBar("AAPL", start + timedelta(minutes=2), 90, 91, 89, 90, 100, 90),
        )

        prefix = session_cumulative_vwap(bars[:2])
        complete = session_cumulative_vwap(bars)

        self.assertAlmostEqual(complete[("AAPL", bars[0].timestamp)], 100.0)
        self.assertAlmostEqual(complete[("AAPL", bars[1].timestamp)], 107.5)
        self.assertEqual(
            {key: complete[key] for key in prefix},
            prefix,
            "future bars must not revise an earlier cumulative VWAP",
        )

    def test_vwap_exit_uses_hysteresis_and_consecutive_bars(self):
        count, should_exit = update_vwap_hysteresis(
            previous_below_count=0,
            close=99.96,
            session_vwap=100.0,
            hysteresis_bps=5.0,
            required_bars=2,
            side="long",
        )
        self.assertEqual((count, should_exit), (0, False))

        count, should_exit = update_vwap_hysteresis(
            previous_below_count=count,
            close=99.94,
            session_vwap=100.0,
            hysteresis_bps=5.0,
            required_bars=2,
            side="long",
        )
        self.assertEqual((count, should_exit), (1, False))
        count, should_exit = update_vwap_hysteresis(
            previous_below_count=count,
            close=99.93,
            session_vwap=100.0,
            hysteresis_bps=5.0,
            required_bars=2,
            side="long",
        )
        self.assertEqual((count, should_exit), (2, True))

    def test_backtester_uses_session_vwap_for_entry_and_hysteresis_for_exit(self):
        entry_bars = _cumulative_entry_bars()
        cumulative = DayTradeBacktester(
            DayTradeConfig(cumulative_vwap=True, vwap_hysteresis_bps=5.0)
        ).run(entry_bars)
        single_bar = DayTradeBacktester(
            DayTradeConfig(cumulative_vwap=False, vwap_hysteresis_bps=0.0)
        ).run(entry_bars)

        self.assertEqual(cumulative.trade_count, 1)
        self.assertEqual(single_bar.trade_count, 0)

        hysteresis = DayTradeBacktester(
            DayTradeConfig(
                cumulative_vwap=True,
                vwap_exit_bars=2,
                vwap_hysteresis_bps=5.0,
            )
        ).run(_hysteresis_exit_bars())
        self.assertEqual(hysteresis.trades[0].exit_reason, "vwap_exit")

    def test_backtester_applies_only_latest_pre_entry_features(self):
        bars = _cumulative_entry_bars()
        signal_at = bars[-2].timestamp
        strong = _feature(signal_at, relative_volume=2.0)
        hypothesis = DayTradeHypothesisConfig(
            min_relative_volume=1.5,
            min_directional_spy_return=0.0,
            min_directional_qqq_return=0.0,
        )
        backtester = DayTradeBacktester(
            DayTradeConfig(cumulative_vwap=True),
            hypothesis_config=hypothesis,
        )

        allowed = backtester.run(bars, feature_snapshots=(strong,))
        blocked = backtester.run(
            bars,
            feature_snapshots=(replace(strong, relative_volume=1.0),),
        )
        future_only = backtester.run(
            bars,
            feature_snapshots=(
                replace(strong, observed_at=signal_at + timedelta(seconds=1)),
            ),
        )

        self.assertEqual(allowed.trade_count, 1)
        self.assertEqual(blocked.trade_count, 0)
        self.assertEqual(future_only.trade_count, 0)

    def test_backtester_uses_atr_stop_and_correlation_aware_size(self):
        bars = _atr_stop_bars()
        signal_at = bars[-3].timestamp
        base_feature = _feature(signal_at, atr=4.0)
        hypothesis = DayTradeHypothesisConfig()
        fixed = DayTradeBacktester(
            DayTradeConfig(cumulative_vwap=True, stop_basis="fixed_pct"),
            hypothesis_config=hypothesis,
        ).run(bars, feature_snapshots=(base_feature,))
        atr = DayTradeBacktester(
            DayTradeConfig(
                cumulative_vwap=True,
                stop_basis="atr",
                atr_stop_multiple=0.5,
                max_concurrent_positions=1,
                dynamic_risk_sizing=True,
            ),
            hypothesis_config=hypothesis,
        ).run(bars, feature_snapshots=(base_feature,))
        correlated = DayTradeBacktester(
            DayTradeConfig(
                cumulative_vwap=True,
                stop_basis="atr",
                atr_stop_multiple=0.5,
                max_concurrent_positions=1,
                dynamic_risk_sizing=True,
            ),
            hypothesis_config=hypothesis,
        ).run(
            bars,
            feature_snapshots=(replace(base_feature, max_abs_correlation=0.8),),
        )

        self.assertEqual(fixed.trades[0].exit_reason, "stop_loss")
        self.assertEqual(atr.trades[0].exit_reason, "end_of_day_forced_exit")
        self.assertLess(correlated.trades[0].quantity, atr.trades[0].quantity)

    def test_backtester_runs_short_as_separate_borrow_and_ssr_experiment(self):
        bars = _short_breakdown_bars()
        signal_at = bars[-2].timestamp
        context = _feature(
            signal_at,
            atr=2.0,
            gap_fraction=-0.02,
            spy_return=-0.002,
            qqq_return=-0.003,
            sector_relative_strength=-0.004,
            borrow_available=True,
            ssr_active=False,
        )
        hypothesis = DayTradeHypothesisConfig(
            side="short",
            min_directional_spy_return=0.001,
            min_directional_qqq_return=0.001,
            min_directional_sector_strength=0.001,
            max_short_gap_abs=0.05,
        )
        backtester = DayTradeBacktester(
            DayTradeConfig(cumulative_vwap=True),
            hypothesis_config=hypothesis,
        )

        allowed = backtester.run(bars, feature_snapshots=(context,))
        blocked = backtester.run(
            bars,
            feature_snapshots=(replace(context, borrow_available=False),),
        )

        self.assertEqual(allowed.trade_count, 1)
        self.assertEqual(allowed.trades[0].side, "short")
        self.assertGreater(allowed.trades[0].pnl, 0.0)
        self.assertEqual(blocked.trade_count, 0)
        expensive_borrow = backtester.run(
            bars,
            feature_snapshots=(replace(context, annualized_borrow_rate=5.0),),
        )
        self.assertGreater(expensive_borrow.trades[0].borrow_cost, 0.0)
        self.assertLess(expensive_borrow.trades[0].pnl, allowed.trades[0].pnl)

    def test_pre_entry_context_and_atr_normalized_range_filter(self):
        observed_at = datetime(2025, 1, 2, 14, 45, tzinfo=UTC)
        context = IntradayFeatureSnapshot(
            symbol="AAPL",
            observed_at=observed_at,
            atr=4.0,
            relative_volume=2.0,
            gap_fraction=0.01,
            premarket_volume=100_000.0,
            spy_return=0.002,
            qqq_return=0.003,
            sector_relative_strength=0.004,
        )
        config = DayTradeHypothesisConfig(
            side="long",
            min_opening_range_atr=0.25,
            max_opening_range_atr=0.75,
            min_relative_volume=1.5,
            min_directional_gap=0.005,
            max_directional_gap=0.03,
            min_premarket_volume=50_000.0,
            min_directional_spy_return=0.001,
            min_directional_qqq_return=0.001,
            min_directional_sector_strength=0.002,
        )

        decision = evaluate_entry_filters(
            context=context,
            config=config,
            signal_at=observed_at,
            opening_range_width=2.0,
            proposed_price=101.0,
        )

        self.assertTrue(decision.allowed, decision.reasons)
        self.assertAlmostEqual(decision.opening_range_atr, 0.5)
        weak = evaluate_entry_filters(
            context=replace(context, relative_volume=1.0),
            config=config,
            signal_at=observed_at,
            opening_range_width=2.0,
            proposed_price=101.0,
        )
        self.assertIn("relative_volume", weak.reasons)
        with self.assertRaisesRegex(ValueError, "future"):
            evaluate_entry_filters(
                context=replace(context, observed_at=observed_at + timedelta(seconds=1)),
                config=config,
                signal_at=observed_at,
                opening_range_width=2.0,
                proposed_price=101.0,
            )

    def test_atr_or_opening_range_stop_replaces_fixed_percent_risk(self):
        atr_stop = risk_scaled_stop(
            entry_price=100.0,
            side="long",
            atr=4.0,
            opening_range_width=2.0,
            basis="atr",
            multiplier=0.5,
        )
        range_stop = risk_scaled_stop(
            entry_price=100.0,
            side="long",
            atr=4.0,
            opening_range_width=2.0,
            basis="opening_range",
            multiplier=0.75,
        )
        short_stop = risk_scaled_stop(
            entry_price=100.0,
            side="short",
            atr=4.0,
            opening_range_width=2.0,
            basis="atr",
            multiplier=0.5,
        )

        self.assertEqual(atr_stop, 98.0)
        self.assertEqual(range_stop, 98.5)
        self.assertEqual(short_stop, 102.0)

    def test_position_size_uses_mark_to_market_equity_heat_and_correlation(self):
        state = PortfolioHeatState(
            settled_equity=100_000.0,
            realized_pnl=-1_000.0,
            unrealized_pnl=-4_000.0,
            open_risk=1_200.0,
        )
        config = DayTradeRiskSizingConfig(
            per_trade_risk_fraction=0.01,
            max_portfolio_heat_fraction=0.025,
            correlation_penalty=0.5,
            max_position_notional_fraction=1.0,
        )

        size = size_intraday_position(
            entry_price=100.0,
            stop_price=98.0,
            state=state,
            config=config,
            max_abs_correlation=0.8,
        )

        self.assertEqual(size.mark_to_market_equity, 95_000.0)
        self.assertEqual(size.available_heat, 1_175.0)
        self.assertAlmostEqual(size.risk_budget, 570.0)
        self.assertAlmostEqual(size.quantity, 285.0)

    def test_short_experiment_models_borrow_ssr_and_gap_risk(self):
        observed_at = datetime(2025, 1, 2, 14, 45, tzinfo=UTC)
        context = IntradayFeatureSnapshot(
            symbol="AAPL",
            observed_at=observed_at,
            atr=4.0,
            relative_volume=2.0,
            gap_fraction=-0.02,
            premarket_volume=100_000.0,
            spy_return=-0.002,
            qqq_return=-0.003,
            sector_relative_strength=-0.004,
            borrow_available=False,
            ssr_active=True,
            best_bid=100.0,
        )
        config = DayTradeHypothesisConfig(
            side="short",
            min_directional_spy_return=0.001,
            min_directional_qqq_return=0.001,
            min_directional_sector_strength=0.002,
            max_short_gap_abs=0.05,
        )

        blocked = evaluate_entry_filters(
            context=context,
            config=config,
            signal_at=observed_at,
            opening_range_width=2.0,
            proposed_price=99.9,
        )
        self.assertIn("borrow_unavailable", blocked.reasons)
        self.assertIn("short_sale_restriction", blocked.reasons)

        allowed = evaluate_entry_filters(
            context=replace(context, borrow_available=True),
            config=config,
            signal_at=observed_at,
            opening_range_width=2.0,
            proposed_price=100.01,
        )
        self.assertTrue(allowed.allowed, allowed.reasons)
        gap_blocked = evaluate_entry_filters(
            context=replace(context, borrow_available=True, gap_fraction=-0.10),
            config=config,
            signal_at=observed_at,
            opening_range_width=2.0,
            proposed_price=100.01,
        )
        self.assertIn("short_gap_risk", gap_blocked.reasons)

    def test_legacy_optimizer_no_longer_builds_mechanical_138240_grid(self):
        candidates = _daytrade_candidate_configs(DayTradeConfig())
        registered = preregistered_daytrade_experiments()

        self.assertGreater(len(candidates), 1)
        self.assertLessEqual(len(candidates), 32)
        self.assertEqual(len({name for name, _config in candidates}), len(candidates))
        self.assertEqual(registered.total_declared_trials, 12)
        self.assertEqual(
            {name for name, _config in candidates if name != "baseline"},
            {
                trial_id
                for record in registered.records
                for trial_id in record.trial_ids
            },
        )
        h3 = [
            config
            for name, config in candidates
            if name.startswith("daytrade-h3-pre-entry-confirmation:")
        ]
        self.assertEqual([config.min_relative_volume for config in h3], [1.0, 1.5, 2.0])
        self.assertTrue(all(config.volume_multiplier == 1.0 for config in h3))
        self.assertTrue(all(not config.dynamic_risk_sizing for _name, config in candidates))

    def test_h5_followup_is_one_frozen_trial_outside_original_optimizer(self):
        base = DayTradeConfig(cost_bps=5.0, max_volume_participation=0.10)
        original = preregistered_daytrade_experiments()
        followup = preregistered_daytrade_followup_experiments()
        candidates = daytrade_followup_candidate_configs(base)

        self.assertEqual(original.total_declared_trials, 12)
        self.assertEqual(followup.total_declared_trials, 1)
        self.assertEqual(len(candidates), 1)
        name, config = candidates[0]
        self.assertEqual(name, "daytrade-h5-opening-range-top4:trial-01")
        self.assertEqual(config.symbol_filter, "opening-range")
        self.assertEqual(config.max_active_symbols, 4)
        self.assertTrue(config.require_positive_opening_momentum)
        self.assertEqual(config.cost_bps, 5.0)
        self.assertEqual(config.max_volume_participation, 0.10)
        self.assertNotIn(
            name,
            {candidate_name for candidate_name, _config in _daytrade_candidate_configs(base)},
        )

    def test_h6_market_confirmation_is_three_preregistered_single_variable_trials(self):
        base = DayTradeConfig(cost_bps=5.0, max_volume_participation=0.10)
        registry = preregistered_daytrade_h6_experiments()
        candidates = daytrade_h6_candidate_configs(base)

        self.assertEqual(registry.total_declared_trials, 3)
        self.assertEqual(
            [name for name, _config in candidates],
            [
                "daytrade-h6-market-confirmation:trial-01",
                "daytrade-h6-market-confirmation:trial-02",
                "daytrade-h6-market-confirmation:trial-03",
            ],
        )
        self.assertEqual(
            [config.min_directional_spy_return for _name, config in candidates],
            [0.0, 0.0005, 0.001],
        )
        self.assertEqual(
            [config.min_directional_qqq_return for _name, config in candidates],
            [0.0, 0.0005, 0.001],
        )
        self.assertTrue(all(config.symbol_filter == "none" for _name, config in candidates))
        self.assertTrue(
            set(name for name, _config in candidates).isdisjoint(
                name for name, _config in daytrade_followup_candidate_configs(base)
            )
        )

    def test_h3_rvol_subset_preserves_original_trial_identity(self):
        base = DayTradeConfig(volume_multiplier=1.25)
        registry = preregistered_daytrade_h3_experiments()
        candidates = daytrade_h3_candidate_configs(base)

        self.assertEqual(registry.total_declared_trials, 3)
        self.assertEqual(
            [name for name, _config in candidates],
            [
                "daytrade-h3-pre-entry-confirmation:trial-01",
                "daytrade-h3-pre-entry-confirmation:trial-02",
                "daytrade-h3-pre-entry-confirmation:trial-03",
            ],
        )
        self.assertEqual(
            [config.min_relative_volume for _name, config in candidates],
            [1.0, 1.5, 2.0],
        )
        self.assertTrue(all(config.volume_multiplier == 1.25 for _name, config in candidates))
        self.assertTrue(all(not config.dynamic_risk_sizing for _name, config in candidates))

    def test_h7_extends_only_entry_window_on_frozen_rvol2_baseline(self):
        base = DayTradeConfig(
            entry_end="10:30",
            min_relative_volume=2.0,
            cost_bps=5.0,
            max_volume_participation=0.10,
        )
        registry = preregistered_daytrade_h7_experiments()
        candidates = daytrade_h7_candidate_configs(base)

        self.assertEqual(registry.total_declared_trials, 2)
        self.assertEqual(
            [name for name, _config in candidates],
            [
                "daytrade-h7-rvol-entry-window:trial-01",
                "daytrade-h7-rvol-entry-window:trial-02",
            ],
        )
        self.assertEqual(
            [config.entry_end for _name, config in candidates],
            ["11:00", "11:30"],
        )
        self.assertTrue(
            all(config.min_relative_volume == 2.0 for _name, config in candidates)
        )
        self.assertTrue(all(config.cost_bps == 5.0 for _name, config in candidates))
        self.assertTrue(
            all(config.max_volume_participation == 0.10 for _name, config in candidates)
        )

    def test_h8_changes_only_stop_basis_on_frozen_rvol2_baseline(self):
        base = DayTradeConfig(
            entry_end="10:30",
            min_relative_volume=2.0,
            stop_basis="fixed_pct",
            cost_bps=5.0,
            max_volume_participation=0.10,
        )
        registry = preregistered_daytrade_h8_experiments()
        candidates = daytrade_h8_candidate_configs(base)

        self.assertEqual(registry.total_declared_trials, 2)
        self.assertEqual(
            [name for name, _config in candidates],
            [
                "daytrade-h8-rvol-risk-scale:trial-01",
                "daytrade-h8-rvol-risk-scale:trial-02",
            ],
        )
        self.assertEqual(
            [config.stop_basis for _name, config in candidates],
            ["atr", "opening_range"],
        )
        self.assertTrue(all(config.entry_end == "10:30" for _name, config in candidates))
        self.assertTrue(
            all(config.min_relative_volume == 2.0 for _name, config in candidates)
        )


if __name__ == "__main__":
    unittest.main()


def _cumulative_entry_bars():
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = [
        MinuteBar("AAPL", start + timedelta(minutes=index), 99.5, 100, 99, 99.8, 1_000, 99.7)
        for index in range(15)
    ]
    bars.extend(
        (
            MinuteBar("AAPL", start + timedelta(minutes=15), 100.2, 101.2, 100.1, 101, 1_500, 102),
            MinuteBar("AAPL", start + timedelta(minutes=16), 101, 102.2, 100.9, 102, 1_200, 102),
        )
    )
    return bars


def _hysteresis_exit_bars():
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = [
        MinuteBar("AAPL", start + timedelta(minutes=index), 100.8, 101.2, 100.0, 101.0, 1_000, 101.0)
        for index in range(15)
    ]
    bars.extend(
        (
            MinuteBar("AAPL", start + timedelta(minutes=15), 101.3, 101.7, 101.2, 101.5, 1_000, 101.0),
            MinuteBar("AAPL", start + timedelta(minutes=16), 101.5, 101.6, 101.4, 101.5, 1_000, 101.0),
            MinuteBar("AAPL", start + timedelta(minutes=17), 100.94, 101.0, 100.92, 100.94, 1_000, 101.0),
            MinuteBar("AAPL", start + timedelta(minutes=18), 100.94, 101.0, 100.92, 100.94, 1_000, 101.0),
        )
    )
    return bars


def _feature(observed_at, **changes):
    values = dict(
        symbol="AAPL",
        observed_at=observed_at,
        atr=2.0,
        relative_volume=2.0,
        gap_fraction=0.01,
        premarket_volume=100_000.0,
        spy_return=0.002,
        qqq_return=0.003,
        sector_relative_strength=0.004,
        borrow_available=False,
        ssr_active=False,
        best_bid=None,
        max_abs_correlation=0.0,
        annualized_borrow_rate=0.0,
    )
    values.update(changes)
    return IntradayFeatureSnapshot(**values)


def _atr_stop_bars():
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = [
        MinuteBar("AAPL", start + timedelta(minutes=index), 99.5, 100, 99, 99.8, 1_000, 99.7)
        for index in range(15)
    ]
    bars.extend(
        (
            MinuteBar("AAPL", start + timedelta(minutes=15), 100.2, 101.2, 100.1, 101.0, 1_500, 99.8),
            MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.2, 100.9, 101.0, 1_200, 100.0),
            MinuteBar("AAPL", start + timedelta(minutes=17), 100.5, 101.1, 100.0, 100.5, 1_200, 100.0),
        )
    )
    return bars


def _short_breakdown_bars():
    start = datetime(2025, 1, 2, 14, 30, tzinfo=UTC)
    bars = [
        MinuteBar("AAPL", start + timedelta(minutes=index), 100, 101, 99, 100, 1_000, 100)
        for index in range(15)
    ]
    bars.extend(
        (
            MinuteBar("AAPL", start + timedelta(minutes=15), 98.8, 98.9, 98.3, 98.5, 1_500, 99.0),
            MinuteBar("AAPL", start + timedelta(minutes=16), 98.5, 98.6, 97.5, 97.8, 1_200, 98.0),
        )
    )
    return bars


def _point_in_time_feature_bars():
    days = (
        (date(2025, 1, 2), 100.0, 100.0, 100.0, 100.0, 100.0),
        (date(2025, 1, 3), 102.0, 103.0, 200.0, 200.0, 200.0),
        (date(2025, 1, 6), 104.0, 105.0, 300.0, 500.0, 50_000.0),
    )
    benchmark_prices = {
        "SPY": ((498.0, 499.0), (499.0, 500.0), (500.0, 501.0)),
        "QQQ": ((396.0, 397.0), (398.0, 399.0), (400.0, 402.0)),
        "XLK": ((198.0, 198.5), (199.0, 199.5), (200.0, 200.2)),
    }
    bars = []
    for index, (day, open_price, close_price, volume, premarket, future_volume) in enumerate(days):
        regular_open = datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC)
        future_price = close_price if index < 2 else close_price * 5.0
        bars.extend(
            (
                MinuteBar(
                    "AAPL",
                    regular_open - timedelta(minutes=90),
                    open_price,
                    open_price,
                    open_price,
                    open_price,
                    premarket,
                    open_price,
                ),
                MinuteBar(
                    "AAPL",
                    regular_open,
                    open_price,
                    101.0 if index == 0 else (104.0 if index == 1 else 105.0),
                    99.0 if index == 0 else (101.0 if index == 1 else 103.0),
                    open_price,
                    volume / 2.0,
                    open_price,
                ),
                MinuteBar(
                    "AAPL",
                    regular_open + timedelta(minutes=15),
                    close_price,
                    close_price,
                    close_price,
                    close_price,
                    volume / 2.0,
                    close_price,
                ),
                MinuteBar(
                    "AAPL",
                    regular_open + timedelta(minutes=16),
                    future_price,
                    future_price,
                    future_price,
                    future_price,
                    future_volume,
                    close_price * 5.0,
                ),
            )
        )
        for symbol, prices in benchmark_prices.items():
            benchmark_open, benchmark_close = prices[index]
            bars.extend(
                (
                    MinuteBar(
                        symbol,
                        regular_open,
                        benchmark_open,
                        benchmark_open,
                        benchmark_open,
                        benchmark_open,
                        1_000.0,
                        benchmark_open,
                    ),
                    MinuteBar(
                        symbol,
                        regular_open + timedelta(minutes=15),
                        benchmark_close,
                        benchmark_close,
                        benchmark_close,
                        benchmark_close,
                        1_000.0,
                        benchmark_close,
                    ),
                    MinuteBar(
                        symbol,
                        regular_open + timedelta(minutes=16),
                        benchmark_close * 5.0,
                        benchmark_close * 5.0,
                        benchmark_close * 5.0,
                        benchmark_close * 5.0,
                        50_000.0,
                        benchmark_close * 5.0,
                    ),
                )
            )
    return sorted(bars, key=lambda item: (item.timestamp, item.symbol))
