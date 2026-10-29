import tempfile
import inspect
import json
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


import quantbot.daytrade as daytrade_module
from quantbot.daytrade import (
    DEFAULT_DAYTRADE_SYMBOLS,
    DayTradeBacktester,
    DayTradeConfig,
    DayTradeDailyResult,
    DayTradeDailyJournal,
    DayTradeMinuteCache,
    DayTradeBacktestResult,
    DayTradeRiskGate,
    DayTradeTrade,
    DayTradeTradeJournal,
    MAX_DAYTRADE_SYMBOLS,
    MinuteBar,
    OpeningRangeBreakoutStrategy,
    build_daytrade_historical_evidence,
    build_daytrade_soak_evidence,
    compare_daytrade_configs,
    daytrade_config_from_payload,
    daytrade_config_to_payload,
    daytrade_optimization_score,
    evaluate_daytrade_config,
    generate_daytrade_walk_forward_windows,
    run_daytrade_walk_forward_optimization,
    summarize_daytrade_status,
)
from quantbot.daytrade_strategy import IntradayFeatureSnapshot
from quantbot.trading import BrokerAccountSnapshot, MarginStatus


class StockBotDayTradeTest(unittest.TestCase):
    def test_default_daytrade_universe_is_capped_at_ten_stocks(self):
        config = DayTradeConfig()

        self.assertEqual(config.max_symbols, 10)
        self.assertEqual(MAX_DAYTRADE_SYMBOLS, 10)
        self.assertEqual(len(DEFAULT_DAYTRADE_SYMBOLS), 10)

    def test_daytrade_config_roundtrip_preserves_cost_and_participation(self):
        original = DayTradeConfig(cost_bps=5.0, max_volume_participation=0.15)

        restored = daytrade_config_from_payload(daytrade_config_to_payload(original))

        self.assertEqual(restored.cost_bps, 5.0)
        self.assertEqual(restored.max_volume_participation, 0.15)

    def test_opening_range_symbol_filter_limits_daily_active_universe(self):
        config = DayTradeConfig(
            symbol_filter="opening-range",
            max_active_symbols=1,
            require_positive_opening_momentum=True,
        )

        result = DayTradeBacktester(config).run(_filter_candidate_bars())

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].symbol, "AMD")
        self.assertEqual(result.trades[0].exit_reason, "take_profit")

    def test_loss_trade_circuit_breaker_stops_later_entries(self):
        config = DayTradeConfig(max_losing_trades_per_day=1)

        result = DayTradeBacktester(config).run(_loss_circuit_breaker_bars())

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].symbol, "AAPL")
        self.assertEqual(result.trades[0].exit_reason, "stop_loss")
        self.assertEqual(result.daily_results[0].stopped_reason, "max_losing_trades_per_day")

    def test_daily_return_uses_each_days_starting_equity(self):
        first_day = _session_bars(exit_kind="target")
        second_day = [
            MinuteBar(
                symbol=bar.symbol,
                timestamp=bar.timestamp + timedelta(days=1),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                vwap=bar.vwap,
            )
            for bar in first_day
        ]

        result = DayTradeBacktester().run(first_day + second_day)

        self.assertEqual(len(result.daily_results), 2)
        previous_equity = result.initial_capital
        for daily in result.daily_results:
            self.assertAlmostEqual(daily.return_pct, daily.pnl / previous_equity)
            self.assertAlmostEqual(daily.ending_equity, previous_equity + daily.pnl)
            previous_equity = daily.ending_equity
        self.assertAlmostEqual(
            result.ending_equity - result.initial_capital,
            sum(item.pnl for item in result.daily_results),
        )
        self.assertAlmostEqual(
            sum(item.pnl for item in result.daily_results),
            sum(trade.pnl for trade in result.trades),
        )

    def test_minute_cache_roundtrips_bars_without_losing_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DayTradeMinuteCache(Path(tmpdir), "injected")
            original = _session_bars(exit_kind="target")
            cache.write_all(original)

            loaded = cache.read(["AAPL"], datetime(2024, 1, 2).date(), datetime(2024, 1, 2).date())

        self.assertEqual(len(loaded), len(original))
        self.assertEqual(loaded[0].symbol, "AAPL")
        self.assertEqual(loaded[0].timestamp, original[0].timestamp)
        self.assertEqual(loaded[0].open, original[0].open)
        self.assertEqual(loaded[0].vwap, original[0].vwap)

    def test_minute_cache_merges_repeated_writes_in_same_month(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DayTradeMinuteCache(Path(tmpdir), "injected")
            first_batch = _session_bars(exit_kind="target")
            second_batch = [
                MinuteBar(
                    symbol=bar.symbol,
                    timestamp=bar.timestamp + timedelta(days=1),
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    vwap=bar.vwap,
                )
                for bar in first_batch
            ]

            cache.write_all(first_batch)
            cache.write_all(second_batch)
            loaded = cache.read(["AAPL"], datetime(2024, 1, 2).date(), datetime(2024, 1, 3).date())

        self.assertEqual(len(loaded), len(first_batch) + len(second_batch))
        self.assertEqual(loaded[0].timestamp.date(), datetime(2024, 1, 2).date())
        self.assertEqual(loaded[-1].timestamp.date(), datetime(2024, 1, 3).date())

    def test_minute_cache_uses_unique_temporary_files_for_repeated_same_process_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DayTradeMinuteCache(Path(tmpdir), "injected")
            bars = _session_bars(exit_kind="target")
            temporary_sources = []
            real_replace = daytrade_module.os.replace

            def capture_replace(source, destination):
                temporary_sources.append(Path(source).name)
                return real_replace(source, destination)

            with mock.patch.object(
                daytrade_module.os,
                "replace",
                side_effect=capture_replace,
            ):
                cache.write_all(bars)
                cache.write_all(bars)

        self.assertGreaterEqual(len(temporary_sources), 4)
        self.assertEqual(len(temporary_sources), len(set(temporary_sources)))

    def test_minute_cache_merges_same_month_request_and_session_manifest_coverage(self):
        first_start = datetime(2024, 1, 2).date()
        first_end = datetime(2024, 1, 15).date()
        second_start = datetime(2024, 1, 16).date()
        second_end = datetime(2024, 1, 31).date()
        sessions = [
            first_start + timedelta(days=offset)
            for offset in range((second_end - first_start).days + 1)
            if (first_start + timedelta(days=offset)).weekday() < 5
        ]
        base_bar = _session_bars(exit_kind="target")[0]

        def bars_for(days):
            return [
                MinuteBar(
                    symbol="AAPL",
                    timestamp=base_bar.timestamp + timedelta(days=(day - first_start).days),
                    open=base_bar.open,
                    high=base_bar.high,
                    low=base_bar.low,
                    close=base_bar.close,
                    volume=base_bar.volume,
                    vwap=base_bar.vwap,
                )
                for day in days
            ]

        first_sessions = [day for day in sessions if day <= first_end]
        second_sessions = [day for day in sessions if day >= second_start]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "injected")
            cache.write_all(
                bars_for(first_sessions),
                request_start=first_start,
                request_end=first_end,
                expected_sessions=first_sessions,
            )
            cache.write_all(
                bars_for(second_sessions),
                request_start=second_start,
                request_end=second_end,
                expected_sessions=second_sessions,
            )

            manifest = json.loads(
                (root / "injected" / "AAPL" / "2024-01.manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            coverage = cache.coverage(
                ["AAPL"],
                first_start,
                second_end,
                expected_sessions=sessions,
            )

        self.assertEqual(manifest["request_start"], "2024-01-02")
        self.assertEqual(manifest["request_end"], "2024-01-31")
        self.assertEqual(
            manifest["coverage_ranges"],
            [{"start": "2024-01-02", "end": "2024-01-31"}],
        )
        self.assertEqual(manifest["expected_sessions"], [day.isoformat() for day in sessions])
        self.assertTrue(coverage["complete"], coverage["reasons"])

    def test_minute_cache_writes_manifest_with_source_coverage_and_content_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "alpaca_minute_bars:sip")
            bars = _session_bars(exit_kind="target")

            cache.write_all(bars)
            manifest_path = root / "alpaca_minute_bars_sip" / "AAPL" / "2024-01.manifest.json"
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["provider"], "alpaca")
        self.assertEqual(manifest["feed"], "sip")
        self.assertEqual(manifest["symbol"], "AAPL")
        self.assertEqual(manifest["coverage_start"], "2024-01-02")
        self.assertEqual(manifest["coverage_end"], "2024-01-02")
        self.assertEqual(manifest["unique_bars"], len(bars))
        self.assertEqual(len(manifest["content_hash"]), 64)

    def test_minute_cache_partitions_utc_month_boundary_by_market_session(self):
        session_day = datetime(2024, 4, 30).date()
        after_hours_bar = MinuteBar(
            symbol="AAPL",
            timestamp=datetime(2024, 5, 1, 0, 0, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000.0,
            vwap=100.25,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "alpaca_minute_bars")

            paths = cache.write_all(
                [after_hours_bar],
                request_start=session_day,
                request_end=session_day,
                expected_sessions=[session_day],
            )
            coverage = cache.coverage(
                ["AAPL"],
                session_day,
                session_day,
                expected_sessions=[session_day],
            )
            rows = cache.read(["AAPL"], session_day, session_day)
            manifest = json.loads(
                (root / "alpaca_minute_bars" / "AAPL" / "2024-04.manifest.json").read_text()
            )

        self.assertEqual([path.name for path in paths], ["2024-04.jsonl"])
        self.assertTrue(coverage["complete"], coverage["reasons"])
        self.assertEqual(rows, [after_hours_bar])
        self.assertEqual(manifest["observed_sessions"], ["2024-04-30"])

    def test_minute_cache_does_not_treat_partial_or_legacy_month_as_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "injected")
            cache.write_all(_session_bars(exit_kind="target"))

            partial_missing = cache.missing_months(
                ["AAPL"],
                datetime(2024, 1, 2).date(),
                datetime(2024, 1, 31).date(),
            )
            manifest = root / "injected" / "AAPL" / "2024-01.manifest.json"
            manifest.unlink(missing_ok=True)
            legacy_missing = cache.missing_months(
                ["AAPL"],
                datetime(2024, 1, 2).date(),
                datetime(2024, 1, 2).date(),
            )
            legacy_rows = cache.read(
                ["AAPL"],
                datetime(2024, 1, 2).date(),
                datetime(2024, 1, 2).date(),
            )

        self.assertEqual(partial_missing, [("AAPL", "2024-01")])
        self.assertEqual(legacy_missing, [("AAPL", "2024-01")])
        self.assertTrue(legacy_rows)

    def test_minute_cache_rejects_tampered_content_hash(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "injected")
            cache.write_all(_session_bars(exit_kind="target"))
            month_path = root / "injected" / "AAPL" / "2024-01.jsonl"
            month_path.write_text(
                month_path.read_text(encoding="utf-8").replace('"open": 99.5', '"open": 99.6', 1),
                encoding="utf-8",
            )

            missing = cache.missing_months(
                ["AAPL"],
                datetime(2024, 1, 2).date(),
                datetime(2024, 1, 2).date(),
            )
            with self.assertRaisesRegex(ValueError, "content hash"):
                cache.read(
                    ["AAPL"],
                    datetime(2024, 1, 2).date(),
                    datetime(2024, 1, 2).date(),
                )

        self.assertEqual(missing, [("AAPL", "2024-01")])

    def test_minute_cache_expected_sessions_detect_missing_session_without_calendar_guess(self):
        self.assertIn("expected_sessions", inspect.signature(DayTradeMinuteCache.write_all).parameters)
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DayTradeMinuteCache(Path(tmpdir), "injected")
            bars = _session_bars(exit_kind="target")
            expected = [
                datetime(2024, 1, 2).date(),
                datetime(2024, 1, 3).date(),
            ]

            cache.write_all(
                bars,
                coverage_start=expected[0],
                coverage_end=expected[-1],
                expected_sessions=expected,
            )
            missing = cache.missing_months(
                ["AAPL"],
                expected[0],
                expected[-1],
                expected_sessions=expected,
            )

        self.assertEqual(missing, [("AAPL", "2024-01")])

    def test_minute_cache_invalid_manifest_is_missing_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "injected")
            cache.write_all(_session_bars(exit_kind="target"))
            manifest = root / "injected" / "AAPL" / "2024-01.manifest.json"
            manifest.write_text("{invalid", encoding="utf-8")

            try:
                missing = cache.missing_months(
                    ["AAPL"],
                    datetime(2024, 1, 2).date(),
                    datetime(2024, 1, 2).date(),
                )
            except Exception as error:
                self.fail("invalid manifest must fail closed, not raise: %s" % error)

        self.assertEqual(missing, [("AAPL", "2024-01")])

    def test_minute_cache_non_object_and_invalid_field_manifests_fail_closed(self):
        invalid_manifests = (
            [],
            {"request_start": "not-a-date"},
        )
        for invalid_manifest in invalid_manifests:
            with self.subTest(manifest=invalid_manifest), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                cache = DayTradeMinuteCache(root, "injected")
                cache.write_all(
                    _session_bars(exit_kind="target"),
                    request_start=datetime(2024, 1, 2).date(),
                    request_end=datetime(2024, 1, 2).date(),
                    expected_sessions=[datetime(2024, 1, 2).date()],
                )
                manifest_path = root / "injected" / "AAPL" / "2024-01.manifest.json"
                if isinstance(invalid_manifest, dict):
                    valid_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    valid_manifest.update(invalid_manifest)
                    invalid_manifest = valid_manifest
                manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")

                try:
                    coverage = cache.coverage(
                        ["AAPL"],
                        datetime(2024, 1, 2).date(),
                        datetime(2024, 1, 2).date(),
                        expected_sessions=[datetime(2024, 1, 2).date()],
                    )
                except Exception as error:
                    self.fail("invalid manifest must fail closed, not raise: %s" % error)

                self.assertFalse(coverage["complete"])
                self.assertEqual(coverage["missing_months"], [("AAPL", "2024-01")])

    def test_minute_cache_rejects_duplicate_timestamp_in_input(self):
        bars = _session_bars(exit_kind="target")
        duplicate = MinuteBar(
            symbol=bars[0].symbol,
            timestamp=bars[0].timestamp,
            open=200.0,
            high=201.0,
            low=199.0,
            close=200.0,
            volume=1.0,
            vwap=200.0,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DayTradeMinuteCache(Path(tmpdir), "injected")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                cache.write_all([bars[0], duplicate])

    def test_minute_cache_rolls_back_data_when_manifest_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = DayTradeMinuteCache(root, "injected")
            cache.write_all(_session_bars(exit_kind="target"))
            data_path = root / "injected" / "AAPL" / "2024-01.jsonl"
            manifest_path = root / "injected" / "AAPL" / "2024-01.manifest.json"
            original_data = data_path.read_bytes()
            original_manifest = manifest_path.read_bytes()
            original_atomic_write = __import__("quantbot.daytrade", fromlist=["_atomic_write_text"])._atomic_write_text
            calls = []

            def fail_second_write(path, content):
                calls.append(Path(path))
                if len(calls) == 2:
                    raise OSError("manifest commit failed")
                return original_atomic_write(path, content)

            next_day = [
                MinuteBar(
                    bar.symbol,
                    bar.timestamp + timedelta(days=1),
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.volume,
                    bar.vwap,
                )
                for bar in _session_bars(exit_kind="target")
            ]
            with mock.patch("quantbot.daytrade._atomic_write_text", side_effect=fail_second_write):
                with self.assertRaisesRegex(OSError, "manifest commit failed"):
                    cache.write_all(next_day)

            self.assertEqual(data_path.read_bytes(), original_data)
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertTrue(
                cache.coverage(
                    ["AAPL"],
                    datetime(2024, 1, 2).date(),
                    datetime(2024, 1, 2).date(),
                )["complete"]
            )

    def test_minute_cache_expected_sessions_detect_internal_symbol_gap(self):
        self.assertIn("request_start", inspect.signature(DayTradeMinuteCache.write_all).parameters)
        session_days = [
            datetime(2024, 1, 2).date(),
            datetime(2024, 1, 3).date(),
            datetime(2024, 1, 4).date(),
        ]
        base = _session_bars(exit_kind="target")[:1]
        bars = []
        for symbol, days in (("AAPL", (session_days[0], session_days[2])), ("MSFT", tuple(session_days))):
            for day in days:
                bars.append(
                    MinuteBar(
                        symbol,
                        base[0].timestamp + timedelta(days=(day - session_days[0]).days),
                        base[0].open,
                        base[0].high,
                        base[0].low,
                        base[0].close,
                        base[0].volume,
                        base[0].vwap,
                    )
                )

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = DayTradeMinuteCache(Path(tmpdir), "injected")
            cache.write_all(
                bars,
                request_start=session_days[0],
                request_end=session_days[-1],
                expected_sessions=session_days,
            )

            aapl = cache.missing_months(
                ["AAPL"], session_days[0], session_days[-1], expected_sessions=session_days
            )
            msft = cache.missing_months(
                ["MSFT"], session_days[0], session_days[-1], expected_sessions=session_days
            )

        self.assertEqual(aapl, [("AAPL", "2024-01")])
        self.assertEqual(msft, [])

    def test_entry_end_blocks_late_entries(self):
        result = DayTradeBacktester(DayTradeConfig(entry_end="09:45")).run(_session_bars(exit_kind="target"))

        self.assertEqual(result.trade_count, 0)

    def test_breakeven_trailing_and_max_holding_exits(self):
        breakeven = DayTradeBacktester(DayTradeConfig(breakeven_after_r=1.0)).run(_dynamic_exit_bars("breakeven"))
        trailing = DayTradeBacktester(
            DayTradeConfig(trailing_stop_after_r=1.0, reward_risk=5.0)
        ).run(_dynamic_exit_bars("trailing"))
        max_holding = DayTradeBacktester(DayTradeConfig(max_holding_minutes=1.0)).run(
            _dynamic_exit_bars("max_holding")
        )

        self.assertEqual(breakeven.trades[0].exit_reason, "stop_loss")
        self.assertAlmostEqual(breakeven.trades[0].exit_price, breakeven.trades[0].entry_price)
        self.assertEqual(trailing.trades[0].exit_reason, "stop_loss")
        self.assertGreater(trailing.trades[0].exit_price, trailing.trades[0].entry_price)
        self.assertEqual(max_holding.trades[0].exit_reason, "max_holding_minutes")

    def test_walk_forward_windows_and_score_penalize_drawdown(self):
        windows = generate_daytrade_walk_forward_windows(
            datetime(2024, 1, 2).date(),
            datetime(2024, 6, 30).date(),
        )
        low_drawdown = _fake_daytrade_result(max_drawdown=0.01)
        high_drawdown = _fake_daytrade_result(max_drawdown=0.08)

        self.assertEqual(windows[0].train_start.isoformat(), "2024-01-02")
        self.assertEqual(windows[0].train_end.isoformat(), "2024-03-30")
        self.assertEqual(windows[0].validation_start.isoformat(), "2024-04-01")
        self.assertEqual(windows[0].purge_days, 1)
        self.assertEqual(windows[0].embargo_days, 1)
        self.assertGreater(daytrade_optimization_score(low_drawdown), daytrade_optimization_score(high_drawdown))

    def test_fixed_config_comparison_executes_feature_dependent_candidate(self):
        bars = _session_bars(exit_kind="target")
        baseline = DayTradeConfig(entry_end="10:30", cost_bps=5.0)
        candidate = daytrade_config_from_payload(
            {"stop_basis": "atr"},
            base_config=baseline,
        )
        snapshot = IntradayFeatureSnapshot(
            symbol="AAPL",
            observed_at=bars[-2].timestamp,
            atr=2.0,
            relative_volume=2.0,
            gap_fraction=0.01,
            premarket_volume=100_000.0,
            spy_return=0.002,
            qqq_return=0.003,
            sector_relative_strength=0.004,
        )

        payload = compare_daytrade_configs(
            bars,
            baseline,
            candidate,
            walk_forward_name="daytrade-h4-risk-scale:trial-02",
            feature_snapshots=(snapshot,),
        )

        self.assertGreater(payload["walk_forward_best"]["trade_count"], 0)
        self.assertEqual(
            payload["walk_forward_best"]["candidate_name"],
            "daytrade-h4-risk-scale:trial-02",
        )

    def test_h6_candidate_blocks_breakout_when_spy_or_qqq_is_below_threshold(self):
        bars = _session_bars(exit_kind="target")
        baseline = DayTradeConfig(entry_end="10:30", cost_bps=5.0)
        candidate = daytrade_config_from_payload(
            {
                "min_directional_spy_return": 0.0,
                "min_directional_qqq_return": 0.0,
            },
            base_config=baseline,
        )
        snapshot = IntradayFeatureSnapshot(
            symbol="AAPL",
            observed_at=bars[-2].timestamp,
            atr=2.0,
            relative_volume=2.0,
            gap_fraction=0.01,
            premarket_volume=100_000.0,
            spy_return=-0.001,
            qqq_return=0.002,
            sector_relative_strength=0.004,
        )

        baseline_result = DayTradeBacktester(baseline).run(bars)
        candidate_result = evaluate_daytrade_config(
            bars,
            candidate,
            candidate_name="daytrade-h6-market-confirmation:trial-01",
            feature_snapshots=(snapshot,),
        )

        self.assertGreater(baseline_result.trade_count, 0)
        self.assertEqual(candidate_result["trade_count"], 0)

    def test_feature_filter_does_not_implicitly_change_position_sizing(self):
        bars = _session_bars(exit_kind="target")
        baseline = DayTradeConfig(entry_end="10:30", cost_bps=5.0)
        candidate = daytrade_config_from_payload(
            {
                "min_directional_spy_return": 0.0,
                "min_directional_qqq_return": 0.0,
            },
            base_config=baseline,
        )
        snapshot = IntradayFeatureSnapshot(
            symbol="AAPL",
            observed_at=bars[-2].timestamp,
            atr=2.0,
            relative_volume=2.0,
            gap_fraction=0.01,
            premarket_volume=100_000.0,
            spy_return=0.002,
            qqq_return=0.002,
            sector_relative_strength=0.004,
            max_abs_correlation=0.99,
        )

        baseline_result = DayTradeBacktester(baseline).run(bars)
        filtered_result = daytrade_module._run_candidate_backtest(
            "daytrade-h6-market-confirmation:trial-01",
            candidate,
            bars,
            (snapshot,),
        )
        resized_result = daytrade_module._run_candidate_backtest(
            "daytrade-h6-market-confirmation:trial-01",
            daytrade_config_from_payload(
                {"dynamic_risk_sizing": True},
                base_config=candidate,
            ),
            bars,
            (snapshot,),
        )

        self.assertEqual(filtered_result.trades[0].quantity, baseline_result.trades[0].quantity)
        self.assertLess(resized_result.trades[0].quantity, filtered_result.trades[0].quantity)

    def test_walk_forward_compounds_two_one_percent_validation_windows_from_base_capital(self):
        base_config = DayTradeConfig(account_equity=100_000.0, capital_fraction=0.10)
        session_days = [datetime(2024, month, 2).date() for month in range(1, 6)]
        bars = [
            MinuteBar(
                "AAPL",
                datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc),
                100.0,
                101.0,
                99.0,
                100.0,
                1_000.0,
                100.0,
            )
            for day in session_days
        ]

        def fake_run(_backtester, selected_bars):
            days = sorted({bar.timestamp.date() for bar in selected_bars})
            validation_days = [day for day in days if day.month >= 4]
            if not validation_days:
                return _fake_daytrade_result(max_drawdown=0.0)
            daily_results = [
                DayTradeDailyResult(
                    strategy="daytrade_intraday",
                    day=day,
                    trades=0,
                    pnl=100.0,
                    return_pct=0.01,
                    ending_equity=10_100.0,
                )
                for day in validation_days
            ]
            trades = [_fake_daytrade_trade(day, pnl=100.0) for day in validation_days]
            return _fake_daytrade_result_with_daily(daily_results, trades=trades)

        with mock.patch.object(
            daytrade_module,
            "_daytrade_candidate_configs",
            return_value=[("only_candidate", base_config)],
        ), mock.patch.object(
            DayTradeBacktester,
            "run",
            autospec=True,
            side_effect=fake_run,
        ):
            result = run_daytrade_walk_forward_optimization(
                bars,
                start=session_days[0],
                end=session_days[-1],
                base_config=base_config,
                train_months=3,
                validation_months=1,
            )

        self.assertEqual(len(result.windows), 2)
        self.assertEqual([item.day for item in result.validation.daily_results], session_days[3:])
        self.assertEqual(
            [item.ending_equity for item in result.validation.daily_results],
            [10_100.0, 10_200.0],
        )
        self.assertEqual(
            [item.pnl for item in result.validation.daily_results],
            [100.0, 100.0],
        )
        self.assertAlmostEqual(result.validation.daily_results[0].return_pct, 0.01)
        self.assertAlmostEqual(result.validation.daily_results[1].return_pct, 100.0 / 10_100.0)
        self.assertAlmostEqual(result.validation.ending_equity, 10_200.0)
        self.assertAlmostEqual(result.validation.cumulative_return, 0.02)
        self.assertAlmostEqual(
            result.validation.ending_equity - result.validation.initial_capital,
            sum(item.pnl for item in result.validation.daily_results),
        )
        self.assertAlmostEqual(
            sum(item.pnl for item in result.validation.daily_results),
            sum(trade.pnl for trade in result.validation.trades),
        )
        self.assertEqual([item.day for item in result.baseline.daily_results], session_days[3:])
        self.assertAlmostEqual(result.baseline.ending_equity, 10_200.0)
        self.assertAlmostEqual(result.baseline.cumulative_return, 0.02)
        self.assertAlmostEqual(
            result.baseline.ending_equity - result.baseline.initial_capital,
            sum(item.pnl for item in result.baseline.daily_results),
        )
        self.assertAlmostEqual(
            sum(item.pnl for item in result.baseline.daily_results),
            sum(trade.pnl for trade in result.baseline.trades),
        )
        stats = result.statistical_validation
        self.assertEqual(
            stats["observation_dates"],
            [day.isoformat() for day in session_days[3:]],
        )
        self.assertEqual(
            len(stats["trial_daily_returns"]["only_candidate"]),
            2,
        )
        self.assertEqual(stats["trial_metrics"]["only_candidate"]["trade_count"], 2)
        self.assertEqual(len(stats["trial_windows"]["only_candidate"]), 2)
        self.assertFalse(stats["available"])

    def test_opening_range_and_vwap_breakout_generate_profitable_intraday_trade(self):
        bars = _session_bars(exit_kind="target")
        strategy = OpeningRangeBreakoutStrategy()

        ranges = strategy.opening_ranges(bars)
        result = DayTradeBacktester().run(bars)
        evidence = build_daytrade_historical_evidence(result)

        opening_range = ranges[(result.daily_results[0].day, "AAPL")]
        self.assertEqual(opening_range.high, 100.0)
        self.assertEqual(opening_range.low, 99.0)
        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].exit_reason, "take_profit")
        self.assertGreater(result.trades[0].pnl, 0)
        self.assertTrue(evidence.passed)

    def test_statistical_validation_persists_aligned_oos_matrix_for_dsr_and_pbo(self):
        days = [date(2025, 1, index) for index in range(2, 10)]

        def daily(values):
            equity = 10_000.0
            output = []
            for day, value in zip(days, values):
                pnl = equity * value
                equity += pnl
                output.append(
                    DayTradeDailyResult(
                        strategy="daytrade_intraday",
                        day=day,
                        trades=1,
                        pnl=pnl,
                        return_pct=value,
                        ending_equity=equity,
                    )
                )
            return output

        selected = daily((0.01, -0.004, 0.008, -0.002, 0.009, -0.003, 0.007, -0.001))
        trial_a = daily((0.01, -0.004, 0.008, -0.002, 0.009, -0.003, 0.007, -0.001))
        trial_b = daily((-0.003, 0.004, -0.002, 0.003, -0.001, 0.002, -0.004, 0.001))

        payload = daytrade_module._daytrade_statistical_validation(
            selected,
            {"trial-a": trial_a, "trial-b": trial_b},
        )

        self.assertTrue(payload["available"])
        self.assertEqual(payload["observation_dates"], [day.isoformat() for day in days])
        self.assertEqual(set(payload["trial_daily_returns"]), {"trial-a", "trial-b"})
        self.assertEqual(payload["deflated_sharpe_ratio"]["trial_count"], 2)
        self.assertEqual(
            payload["probability_of_backtest_overfitting"]["strategy_count"],
            2,
        )

    def test_evaluate_config_includes_auditable_daily_return_series(self):
        payload = evaluate_daytrade_config(
            _session_bars(exit_kind="target"),
            DayTradeConfig(),
            candidate_name="baseline",
        )

        self.assertEqual(len(payload["daily_returns"]), payload["sessions"])
        self.assertEqual(
            payload["daily_returns"][-1]["ending_equity"],
            payload["ending_equity"],
        )

    def test_breakout_intent_fills_on_next_bar_open_and_unfilled_intent_is_not_trade(self):
        bars = _next_open_fill_bars()

        filled = DayTradeBacktester().run(bars)
        unfilled = DayTradeBacktester().run(bars[:-1])

        self.assertEqual(filled.trade_count, 1)
        self.assertEqual(filled.trades[0].entry_timestamp, bars[-1].timestamp)
        self.assertAlmostEqual(filled.trades[0].entry_price, bars[-1].open)
        self.assertEqual(unfilled.trade_count, 0)

    def test_pending_entry_is_canceled_when_next_bar_reaches_entry_end(self):
        result = DayTradeBacktester(
            DayTradeConfig(entry_end="09:46")
        ).run(_next_open_fill_bars())

        self.assertEqual(result.trade_count, 0)

    def test_pending_entry_is_canceled_at_force_exit_instead_of_round_tripping_same_bar(self):
        result = DayTradeBacktester(
            DayTradeConfig(entry_end="16:00", force_exit="09:46")
        ).run(_next_open_fill_bars())

        self.assertEqual(result.trade_count, 0)

    def test_daytrade_volume_participation_caps_next_bar_fill_quantity(self):
        self.assertTrue(hasattr(DayTradeConfig, "max_volume_participation"))
        bars = _next_open_fill_bars(next_bar_volume=10.0)

        result = DayTradeBacktester(
            DayTradeConfig(max_volume_participation=0.10)
        ).run(bars)

        self.assertEqual(result.trade_count, 1)
        self.assertLessEqual(result.trades[0].quantity, 1.0)

    def test_daytrade_cost_bps_is_charged_on_both_entry_and_exit(self):
        self.assertTrue(hasattr(DayTradeConfig, "cost_bps"))
        bars = _next_open_fill_bars()

        gross = DayTradeBacktester(DayTradeConfig(cost_bps=0.0)).run(bars)
        net = DayTradeBacktester(DayTradeConfig(cost_bps=10.0)).run(bars)

        self.assertGreater(net.trades[0].entry_price, gross.trades[0].entry_price)
        self.assertLess(net.trades[0].exit_price, gross.trades[0].exit_price)
        self.assertLess(net.trades[0].pnl, gross.trades[0].pnl)

    def test_daytrade_stop_gap_and_double_touch_use_worse_open_stop_first(self):
        result = DayTradeBacktester().run(_stop_gap_double_touch_bars())

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].exit_reason, "stop_loss")
        self.assertAlmostEqual(result.trades[0].exit_price, 99.0)

    def test_cross_symbol_capacity_keeps_higher_scored_pending_entry(self):
        result = DayTradeBacktester(
            DayTradeConfig(max_concurrent_positions=1)
        ).run(_filter_candidate_bars())

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].symbol, "AMD")
        self.assertNotEqual(result.daily_results[0].stopped_reason, "max_concurrent_positions")

    def test_pending_entry_waits_for_next_bar_of_same_symbol(self):
        bars = _next_open_fill_bars()
        trigger = bars[-2]
        aapl_fill = bars[-1]
        bars[-1:] = [
            MinuteBar(
                "MSFT",
                aapl_fill.timestamp,
                50.0,
                50.1,
                49.9,
                50.0,
                1000,
                50.0,
            ),
            MinuteBar(
                "AAPL",
                aapl_fill.timestamp + timedelta(minutes=1),
                aapl_fill.open,
                aapl_fill.high,
                aapl_fill.low,
                aapl_fill.close,
                aapl_fill.volume,
                aapl_fill.vwap,
            ),
        ]

        result = DayTradeBacktester().run(bars)

        self.assertEqual(result.trade_count, 1)
        self.assertEqual(result.trades[0].entry_timestamp, trigger.timestamp + timedelta(minutes=2))

    def test_stop_loss_vwap_exit_and_forced_exit_are_reported(self):
        stop = DayTradeBacktester().run(_session_bars(exit_kind="stop"))
        vwap = DayTradeBacktester(
            DayTradeConfig(cumulative_vwap=False, vwap_hysteresis_bps=0.0)
        ).run(_session_bars(exit_kind="vwap"))
        forced = DayTradeBacktester().run(_session_bars(exit_kind="forced"))

        self.assertEqual(stop.trades[0].exit_reason, "stop_loss")
        self.assertEqual(vwap.trades[0].exit_reason, "vwap_exit")
        self.assertEqual(forced.trades[0].exit_reason, "forced_exit")

    def test_risk_gate_blocks_daily_loss_trade_count_concurrency_and_broker_state(self):
        config = DayTradeConfig(account_equity=20_000, max_trades_per_day=2, max_concurrent_positions=1)
        gate = DayTradeRiskGate(config)
        broker_account = BrokerAccountSnapshot(
            broker="simulated",
            observed_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
            equity=20_000,
            buying_power=20_000,
            day_trade_buying_power=20_000,
            trading_blocked=False,
            shorting_enabled=False,
            margin_status=MarginStatus.SIMULATED,
        )
        blocked_account = BrokerAccountSnapshot(
            broker="broker",
            observed_at=datetime(2024, 1, 5, tzinfo=timezone.utc),
            equity=100_000,
            buying_power=100_000,
            day_trade_buying_power=100_000,
            trading_blocked=True,
            shorting_enabled=True,
            margin_status=MarginStatus.MARGIN,
        )

        broker_ok, broker_reasons = gate.can_open_new_trade(
            day=datetime(2024, 1, 5).date(),
            trades_started_today=0,
            open_positions=0,
            realized_pnl=0,
            broker_account=blocked_account,
            requested_notional=1_000,
        )
        trade_count_ok, trade_count_reasons = DayTradeRiskGate(config).can_open_new_trade(
            day=datetime(2024, 1, 5).date(),
            trades_started_today=2,
            open_positions=0,
            realized_pnl=0,
            broker_account=broker_account,
            requested_notional=1_000,
        )
        concurrency_ok, concurrency_reasons = DayTradeRiskGate(config).can_open_new_trade(
            day=datetime(2024, 1, 5).date(),
            trades_started_today=0,
            open_positions=1,
            realized_pnl=0,
            broker_account=broker_account,
            requested_notional=1_000,
        )
        loss_ok, loss_reasons = DayTradeRiskGate(config).can_open_new_trade(
            day=datetime(2024, 1, 5).date(),
            trades_started_today=0,
            open_positions=0,
            realized_pnl=-config.daily_loss_limit,
            broker_account=broker_account,
            requested_notional=1_000,
        )

        self.assertFalse(broker_ok)
        self.assertIn("account_trading_blocked", broker_reasons)
        self.assertFalse(trade_count_ok)
        self.assertIn("max_trades_per_day", trade_count_reasons)
        self.assertFalse(concurrency_ok)
        self.assertIn("max_concurrent_positions", concurrency_reasons)
        self.assertFalse(loss_ok)
        self.assertIn("daily_loss_limit", loss_reasons)

    def test_daytrade_journals_and_status_roundtrip(self):
        result = DayTradeBacktester().run(_session_bars(exit_kind="target"))
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trades_log = root / "trades.jsonl"
            daily_log = root / "daily.jsonl"
            DayTradeTradeJournal(trades_log).write_all(result.trades)
            DayTradeDailyJournal(daily_log).write_all(result.daily_results)

            trades = DayTradeTradeJournal(trades_log).read_all()
            daily = DayTradeDailyJournal(daily_log).read_all()
            status = summarize_daytrade_status(daily, trades, min_sessions=1, max_drawdown=0.05)
            evidence = build_daytrade_soak_evidence(daily, trades, "daytrade_paper_soak", min_sessions=1)

        self.assertEqual(trades[0].symbol, "AAPL")
        self.assertEqual(daily[0].trades, 1)
        self.assertTrue(status["passed"])
        self.assertTrue(evidence.passed)


def _session_bars(exit_kind: str):
    bars = []
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for offset in range(15):
        bars.append(
            MinuteBar(
                symbol="AAPL",
                timestamp=start + timedelta(minutes=offset),
                open=99.5,
                high=100.0,
                low=99.0,
                close=99.8,
                volume=1000,
                vwap=99.7,
            )
        )
    bars.append(
        MinuteBar(
            symbol="AAPL",
            timestamp=start + timedelta(minutes=15),
            open=100.2,
            high=101.2,
            low=100.8,
            close=101.0,
            volume=1500,
            vwap=100.5,
        )
    )
    if exit_kind == "target":
        bars.append(
            MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 102.0, 100.9, 101.9, 1200, 101.0)
        )
    elif exit_kind == "stop":
        bars.append(
            MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.1, 100.0, 100.2, 1200, 100.8)
        )
    elif exit_kind == "vwap":
        bars.extend(
            [
                MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.2, 100.8, 100.7, 1200, 101.0),
                MinuteBar("AAPL", start + timedelta(minutes=17), 100.7, 101.1, 100.7, 100.8, 1200, 101.0),
                MinuteBar("AAPL", start + timedelta(minutes=18), 100.8, 101.1, 100.7, 100.9, 1200, 101.0),
            ]
        )
    elif exit_kind == "forced":
        bars.extend(
            [
                MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.1, 100.8, 100.9, 1200, 101.0),
                MinuteBar("AAPL", datetime(2024, 1, 2, 20, 50, tzinfo=timezone.utc), 101.0, 101.1, 100.8, 100.9, 1200, 101.0),
            ]
        )
    return bars


def _next_open_fill_bars(next_bar_volume: float = 1200.0):
    bars = _session_bars(exit_kind="target")[:-1]
    trigger = bars[-1]
    bars.append(
        MinuteBar(
            "AAPL",
            trigger.timestamp + timedelta(minutes=1),
            102.0,
            102.2,
            101.8,
            102.1,
            next_bar_volume,
            102.0,
        )
    )
    return bars


def _stop_gap_double_touch_bars():
    bars = _session_bars(exit_kind="target")[:-1]
    trigger = bars[-1]
    bars.extend(
        [
            MinuteBar("AAPL", trigger.timestamp + timedelta(minutes=1), 101.0, 101.1, 100.9, 101.0, 1200, 101.0),
            MinuteBar("AAPL", trigger.timestamp + timedelta(minutes=2), 99.0, 103.0, 98.0, 100.0, 1200, 100.0),
        ]
    )
    return bars


def _filter_candidate_bars():
    bars = []
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for offset in range(15):
        bars.append(
            MinuteBar(
                symbol="AAPL",
                timestamp=start + timedelta(minutes=offset),
                open=100.0 if offset == 0 else 100.2,
                high=102.0,
                low=99.0,
                close=99.8,
                volume=1000,
                vwap=100.0,
            )
        )
        bars.append(
            MinuteBar(
                symbol="AMD",
                timestamp=start + timedelta(minutes=offset),
                open=50.0 if offset == 0 else 50.3,
                high=51.0,
                low=49.0,
                close=50.8,
                volume=2000,
                vwap=50.2,
            )
        )
    bars.extend(
        [
            MinuteBar("AAPL", start + timedelta(minutes=15), 102.0, 103.5, 101.8, 103.0, 3000, 102.2),
            MinuteBar("AMD", start + timedelta(minutes=15), 51.2, 52.2, 51.1, 52.0, 3000, 51.3),
            MinuteBar("AAPL", start + timedelta(minutes=16), 103.0, 104.0, 102.8, 103.8, 2000, 103.0),
            MinuteBar("AMD", start + timedelta(minutes=16), 52.0, 52.7, 51.9, 52.5, 2000, 52.0),
        ]
    )
    return bars


def _loss_circuit_breaker_bars():
    bars = []
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for offset in range(15):
        bars.append(
            MinuteBar("AAPL", start + timedelta(minutes=offset), 99.5, 100.0, 99.0, 99.8, 1000, 99.7)
        )
        bars.append(
            MinuteBar("AMD", start + timedelta(minutes=offset), 49.5, 51.0, 49.0, 50.0, 1000, 49.8)
        )
    bars.extend(
        [
            MinuteBar("AAPL", start + timedelta(minutes=15), 100.2, 101.2, 100.8, 101.0, 1500, 100.5),
            MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.1, 100.0, 100.2, 1200, 100.8),
            MinuteBar("AMD", start + timedelta(minutes=15), 50.0, 50.5, 49.8, 50.4, 1200, 50.1),
            MinuteBar("AMD", start + timedelta(minutes=16), 50.4, 50.6, 50.0, 50.5, 1200, 50.2),
            MinuteBar("AMD", start + timedelta(minutes=17), 51.1, 52.2, 51.0, 52.0, 1500, 51.4),
            MinuteBar("AMD", start + timedelta(minutes=18), 52.0, 53.0, 51.9, 52.7, 1200, 52.1),
        ]
    )
    return bars


def _dynamic_exit_bars(exit_kind: str):
    bars = []
    start = datetime(2024, 1, 2, 14, 30, tzinfo=timezone.utc)
    for offset in range(15):
        bars.append(
            MinuteBar("AAPL", start + timedelta(minutes=offset), 99.5, 100.0, 99.0, 99.8, 1000, 99.7)
        )
    bars.append(
        MinuteBar("AAPL", start + timedelta(minutes=15), 100.2, 101.2, 100.8, 101.0, 1500, 100.5)
    )
    if exit_kind == "breakeven":
        bars.extend(
            [
                MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.7, 101.0, 101.5, 1200, 101.1),
                MinuteBar("AAPL", start + timedelta(minutes=17), 101.3, 101.4, 100.9, 101.0, 1200, 101.1),
            ]
        )
    elif exit_kind == "trailing":
        bars.extend(
            [
                MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 102.0, 101.0, 101.8, 1200, 101.2),
                MinuteBar("AAPL", start + timedelta(minutes=17), 101.8, 101.9, 101.2, 101.3, 1200, 101.4),
            ]
        )
    elif exit_kind == "max_holding":
        bars.extend(
            [
                MinuteBar("AAPL", start + timedelta(minutes=16), 101.0, 101.3, 100.9, 101.1, 1200, 101.0),
                MinuteBar("AAPL", start + timedelta(minutes=17), 101.1, 101.3, 101.0, 101.2, 1200, 101.1),
            ]
        )
    return bars


def _fake_daytrade_result(max_drawdown: float):
    return DayTradeBacktestResult(
        strategy="daytrade_intraday",
        trades=[],
        daily_results=[],
        initial_capital=10000.0,
        ending_equity=10100.0,
        cumulative_return=0.01,
        annual_return=0.10,
        sharpe=1.0,
        max_drawdown=max_drawdown,
        win_rate=0.5,
        profit_factor=1.5,
        average_holding_minutes=10.0,
        trade_count=10,
    )


def _fake_daytrade_result_with_daily(daily_results, trades=()):
    return DayTradeBacktestResult(
        strategy="daytrade_intraday",
        trades=list(trades),
        daily_results=daily_results,
        initial_capital=10_000.0,
        ending_equity=daily_results[-1].ending_equity,
        cumulative_return=daily_results[-1].ending_equity / 10_000.0 - 1.0,
        annual_return=0.10,
        sharpe=1.0,
        max_drawdown=0.0,
        win_rate=0.0,
        profit_factor=0.0,
        average_holding_minutes=0.0,
        trade_count=len(trades),
    )


def _fake_daytrade_trade(day, pnl):
    entry_timestamp = datetime(day.year, day.month, day.day, 15, tzinfo=timezone.utc)
    return DayTradeTrade(
        strategy="daytrade_intraday",
        symbol="AAPL",
        day=day,
        entry_timestamp=entry_timestamp,
        exit_timestamp=entry_timestamp + timedelta(minutes=1),
        side="long",
        entry_price=100.0,
        exit_price=100.0 + pnl,
        quantity=1.0,
        pnl=pnl,
        return_pct=pnl / 100.0,
        exit_reason="take_profit",
        notional=100.0,
        holding_minutes=1.0,
    )


if __name__ == "__main__":
    unittest.main()
