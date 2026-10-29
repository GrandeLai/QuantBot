import io
import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import CompletionAudit, load_evidence_file
from quantbot.research_cli import main
from quantbot.daytrade import (
    DayTradeConfig,
    DayTradeMinuteCache,
    daytrade_config_to_payload,
    daytrade_followup_candidate_configs,
)
from quantbot.evidence import hash_payload, source_tree_hash


class StockBotCliTest(unittest.TestCase):
    def test_preflight_command_returns_nonzero_and_json_when_requirements_missing(self):
        stdout = io.StringIO()

        exit_code = main(
            ["preflight", "--json"],
            stdout=stdout,
            environ={"ALPACA_API_KEY": "key"},
            import_checker=lambda _name: False,
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertIn("ALPACA_SECRET_KEY", payload["missing_env"])
        self.assertIn("duckdb", payload["missing_packages"])

    def test_schedule_command_prints_after_close_plan(self):
        stdout = io.StringIO()

        exit_code = main(["schedule"], stdout=stdout)

        self.assertEqual(exit_code, 0)
        self.assertIn("US/Eastern", stdout.getvalue())
        self.assertIn("16:30", stdout.getvalue())
        self.assertIn("rebalance", stdout.getvalue())

    def test_acceptance_schedule_command_prints_acceptance_cycle_plan(self):
        stdout = io.StringIO()

        exit_code = main(["acceptance-schedule", "--json", "--evidence-file", "evidence.jsonl"], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["timezone"], "US/Eastern")
        self.assertEqual(payload["run_time"], "16:45")
        self.assertIn("acceptance-cycle", payload["command"])
        self.assertIn("--evidence-file evidence.jsonl", payload["command"])

    def test_paper_acceptance_command_evaluates_runs_json(self):
        stdout = io.StringIO()
        runs_json = json.dumps(
            [
                {"period": "week1", "errors": 0, "paper_return": 0.01, "backtest_return": 0.01},
                {"period": "week2", "errors": 0, "paper_return": 0.01, "backtest_return": 0.01},
                {"period": "week3", "errors": 0, "paper_return": 0.01, "backtest_return": 0.01},
                {"period": "week4", "errors": 0, "paper_return": 0.01, "backtest_return": 0.01},
            ]
        )

        exit_code = main(["paper-acceptance", "--runs-json", runs_json], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["weeks"], 4)

    def test_backtest_acceptance_command_writes_oos_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()
            summary_json = json.dumps(
                {
                    "annual_return": 0.17,
                    "benchmark_annual_return": 0.10,
                    "excess_alpha_annual": 0.07,
                    "annual_volatility": 0.14,
                    "sharpe": 1.2,
                    "max_drawdown": 0.16,
                }
            )

            exit_code = main(
                [
                    "backtest-acceptance",
                    "--summary-json",
                    summary_json,
                    "--annual-turnover",
                    "3.5",
                    "--two-way-cost-rate",
                    "0.0015",
                    "--summary-net-of-cost",
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
            )
            evidence = load_evidence_file(evidence_file)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["reasons"], [])
        self.assertTrue(evidence["oos_backtest_acceptance"].passed)

    def test_backtest_acceptance_rejects_gross_summary_when_cost_drag_is_positive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()
            summary_json = json.dumps(
                {
                    "excess_alpha_annual": 0.10,
                    "sharpe": 1.5,
                    "max_drawdown": 0.10,
                }
            )

            exit_code = main(
                [
                    "backtest-acceptance",
                    "--summary-json",
                    summary_json,
                    "--annual-turnover",
                    "1",
                    "--two-way-cost-rate",
                    "0.001",
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["reasons"], ["gross_summary_not_admissible"])

    def test_backtest_acceptance_reports_and_gates_on_net_alpha(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()
            summary = {
                "annual_return": 0.10,
                "benchmark_annual_return": 0.046,
                "excess_alpha_annual": 0.054,
                "annual_volatility": 0.10,
                "sharpe": 1.2,
                "max_drawdown": 0.10,
            }

            exit_code = main(
                [
                    "backtest-acceptance",
                    "--summary-json",
                    json.dumps(summary),
                    "--annual-turnover",
                    "4",
                    "--two-way-cost-rate",
                    "0.0015",
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertAlmostEqual(payload["net_excess_alpha_annual"], 0.048)
        self.assertIn("excess_alpha", payload["reasons"])
        self.assertIn("gross_summary_not_admissible", payload["reasons"])

    def test_backtest_acceptance_cli_rejects_non_finite_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()
            summary = {
                "annual_return": 0.10,
                "benchmark_annual_return": 0.04,
                "excess_alpha_annual": float("nan"),
                "annual_volatility": 0.10,
                "sharpe": float("inf"),
                "max_drawdown": 0.10,
            }

            exit_code = main(
                [
                    "backtest-acceptance",
                    "--summary-json",
                    json.dumps(summary),
                    "--annual-turnover",
                    "1",
                    "--summary-net-of-cost",
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["passed"])
        self.assertIn("non_finite_summary", payload["reasons"])

    def test_backtest_acceptance_cli_rejects_negative_or_non_finite_risk_inputs(self):
        base_summary = {
            "annual_return": 0.10,
            "benchmark_annual_return": 0.04,
            "excess_alpha_annual": 0.06,
            "annual_volatility": 0.10,
            "sharpe": 1.2,
            "max_drawdown": 0.10,
        }
        cases = (
            ({**base_summary, "max_drawdown": -0.01}, "1", "0.001", "negative_drawdown"),
            (base_summary, "-1", "0.001", "negative_turnover"),
            (base_summary, "inf", "0.001", "non_finite_turnover"),
            (base_summary, "1", "-0.001", "negative_cost_rate"),
        )
        for summary, turnover, cost_rate, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason), tempfile.TemporaryDirectory() as tmpdir:
                stdout = io.StringIO()
                exit_code = main(
                    [
                        "backtest-acceptance",
                        "--summary-json",
                        json.dumps(summary),
                        "--annual-turnover",
                        turnover,
                        "--two-way-cost-rate",
                        cost_rate,
                        "--summary-net-of-cost",
                        "--evidence-file",
                        str(Path(tmpdir) / "evidence.jsonl"),
                    ],
                    stdout=stdout,
                )
                payload = json.loads(stdout.getvalue())

            self.assertEqual(exit_code, 1)
            self.assertIn(expected_reason, payload["reasons"])

    def test_oos_backtest_run_command_downloads_prices_via_injected_loader_and_writes_evidence(self):
        prices = _synthetic_oos_prices()
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "oos-backtest-run",
                    "--symbols",
                    "AAA,BBB,CCC",
                    "--benchmark",
                    "SPY",
                    "--lookback-days",
                    "20",
                    "--top-n",
                    "1",
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
                oos_price_loader=lambda **_kwargs: prices,
            )
            evidence = load_evidence_file(evidence_file)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["passed"])
        self.assertEqual(payload["run_type"], "development_smoke")
        self.assertFalse(payload["formal_admission_eligible"])
        self.assertEqual(
            payload["admission_scope"],
            "legacy_fixed_universe_compatibility_only",
        )
        self.assertEqual(payload["reasons"], [])
        self.assertGreater(payload["validation_observations"], 500)
        item = evidence["oos_backtest_acceptance"]
        self.assertTrue(item.passed)
        self.assertIn("source=injected", item.detail)
        self.assertEqual(item.run_type, "development_smoke")
        normalized_prices = {
            ticker.upper(): {
                trading_date.isoformat(): float(value)
                for trading_date, value in series.items()
                if value is not None and float(value) > 0
            }
            for ticker, series in prices.items()
        }
        expected_config = {
            "symbols": ["AAA", "BBB", "CCC"],
            "benchmark": "SPY",
            "train_start": "2015-01-01",
            "train_end": "2022-12-31",
            "validation_start": "2023-01-01",
            "validation_end": "2024-12-31",
            "lookback_days": 20,
            "top_n": 1,
            "two_way_cost_rate": 0.0015,
            "data_start": "2014-01-01",
            "data_end": "2025-01-01",
            "source": {
                "requested": "auto",
                "resolved": "injected",
                "alpaca_data_url": "",
                "alpaca_feed": "",
            },
        }
        self.assertEqual(item.dataset_hash, hash_payload(normalized_prices))
        self.assertEqual(item.config_hash, hash_payload(expected_config))
        self.assertEqual(item.code_hash, source_tree_hash())
        audit = CompletionAudit(["oos_backtest_acceptance"]).evaluate(evidence)
        self.assertFalse(audit.complete)
        self.assertEqual(audit.failed_ids, ["oos_backtest_acceptance"])

    def test_daytrade_schedule_command_prints_intraday_plan(self):
        stdout = io.StringIO()

        exit_code = main(["daytrade-schedule", "--json"], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["timezone"], "US/Eastern")
        self.assertEqual(payload["run_time"], "09:45-15:50")
        self.assertIn("daytrade-paper-cycle", payload["command"])

    def test_daytrade_backtest_run_writes_logs_and_historical_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trades_log = root / "trades.jsonl"
            daily_log = root / "daily.jsonl"
            evidence_file = root / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--trades-log",
                    str(trades_log),
                    "--daily-log",
                    str(daily_log),
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
            )
            evidence = load_evidence_file(evidence_file)
            trades_log_exists = trades_log.exists()
            daily_log_exists = daily_log.exists()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["strategy"], "daytrade_intraday")
        self.assertEqual(payload["trade_count"], 1)
        item = evidence["daytrade_historical_backtest"]
        self.assertTrue(item.passed)
        self.assertEqual(
            payload["input_materialization_hash"],
            item.input_materialization_hash,
        )
        self.assertEqual(item.dataset_hash, item.input_materialization_hash)
        self.assertEqual(payload["output_hash"], item.output_hash)
        self.assertNotEqual(item.input_materialization_hash, item.output_hash)
        self.assertEqual(item.experiment_id, "daytrade-legacy-orb-smoke")
        self.assertTrue(trades_log_exists)
        self.assertTrue(daily_log_exists)

    def test_daytrade_backtest_accepts_ten_stock_symbols(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            captured = {}
            stdout = io.StringIO()

            def capture_loader(**kwargs):
                captured["symbols"] = kwargs["symbols"]
                return _synthetic_daytrade_rows()

            exit_code = main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--trades-log",
                    str(root / "trades.jsonl"),
                    "--daily-log",
                    str(root / "daily.jsonl"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=capture_loader,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(captured["symbols"]), 10)
        self.assertEqual(payload["trade_count"], 1)

    def test_daytrade_backtest_rejects_more_than_ten_stock_symbols_before_loading_data(self):
        loader_called = []

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaises(ValueError) as ctx:
                main(
                    [
                        "daytrade-backtest-run",
                        "--symbols",
                        "AAPL,MSFT,NVDA,AMZN,META,GOOGL,AMD,TSLA,AVGO,NFLX,CRM",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-02",
                        "--trades-log",
                        str(root / "trades.jsonl"),
                        "--daily-log",
                        str(root / "daily.jsonl"),
                        "--evidence-file",
                        str(root / "evidence.jsonl"),
                    ],
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: loader_called.append(True),
                )

        self.assertFalse(loader_called)
        self.assertIn("at most 10 stock symbols", str(ctx.exception))

    def test_daytrade_backtest_accepts_loss_circuit_breaker_args(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--max-losing-trades-per-day",
                    "1",
                    "--max-consecutive-losing-trades-per-day",
                    "1",
                    "--trades-log",
                    str(root / "trades.jsonl"),
                    "--daily-log",
                    str(root / "daily.jsonl"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["trade_count"], 1)

    def test_daytrade_cache_fill_and_backtest_read_cached_bars_without_loader(self):
        self.assertIn("daytrade_session_loader", inspect.signature(main).parameters)
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            fill_stdout = io.StringIO()

            fill_exit = main(
                [
                    "daytrade-cache-fill",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--cache-dir",
                    str(cache_dir),
                ],
                stdout=fill_stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
                daytrade_session_loader=lambda **_kwargs: [date(2024, 1, 2)],
            )
            backtest_stdout = io.StringIO()
            backtest_exit = main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--cache-dir",
                    str(cache_dir),
                    "--trades-log",
                    str(root / "trades.jsonl"),
                    "--daily-log",
                    str(root / "daily.jsonl"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=backtest_stdout,
                daytrade_minute_loader=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("loader should not run")),
                daytrade_session_loader=lambda **_kwargs: [date(2024, 1, 2)],
            )

        fill_payload = json.loads(fill_stdout.getvalue())
        backtest_payload = json.loads(backtest_stdout.getvalue())
        self.assertEqual(fill_exit, 0)
        self.assertGreater(fill_payload["bars"], 0)
        self.assertEqual(backtest_exit, 0)
        self.assertEqual(backtest_payload["source"], "cache:injected")
        self.assertEqual(backtest_payload["trade_count"], 1)

    def test_daytrade_cache_without_independent_calendar_does_not_hit_automatically(self):
        load_calls = []

        def load_again(**_kwargs):
            load_calls.append("minute")
            return _synthetic_daytrade_rows()

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            main(
                [
                    "daytrade-cache-fill",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--cache-dir",
                    str(cache_dir),
                ],
                stdout=io.StringIO(),
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
            )

            exit_code = main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--cache-dir",
                    str(cache_dir),
                    "--trades-log",
                    str(root / "trades.jsonl"),
                    "--daily-log",
                    str(root / "daily.jsonl"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=io.StringIO(),
                daytrade_minute_loader=load_again,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(load_calls, ["minute"])

    def test_daytrade_independent_calendar_detects_session_missing_for_all_symbols(self):
        self.assertIn("daytrade_session_loader", inspect.signature(main).parameters)
        expected_sessions = [date(2024, 1, 2), date(2024, 1, 3)]
        load_calls = []

        def load_complete(**_kwargs):
            load_calls.append("minute")
            return _synthetic_daytrade_rows_for_symbols_and_dates(
                ("AAPL", "MSFT"), expected_sessions
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            main(
                [
                    "daytrade-cache-fill",
                    "--symbols",
                    "AAPL,MSFT",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-dir",
                    str(cache_dir),
                ],
                stdout=io.StringIO(),
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows_for_symbols_and_dates(
                    ("AAPL", "MSFT"), [date(2024, 1, 2)]
                ),
            )
            missing = DayTradeMinuteCache(cache_dir, "injected").missing_months(
                ["AAPL", "MSFT"],
                expected_sessions[0],
                expected_sessions[-1],
                expected_sessions=expected_sessions,
            )

            exit_code = main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL,MSFT",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-dir",
                    str(cache_dir),
                    "--trades-log",
                    str(root / "trades.jsonl"),
                    "--daily-log",
                    str(root / "daily.jsonl"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=io.StringIO(),
                daytrade_minute_loader=load_complete,
                daytrade_session_loader=lambda **_kwargs: expected_sessions,
            )

        self.assertEqual(missing, [("AAPL", "2024-01"), ("MSFT", "2024-01")])
        self.assertEqual(exit_code, 0)
        self.assertEqual(load_calls, ["minute"])

    def test_daytrade_expected_sessions_json_overrides_session_loader(self):
        self.assertIn("daytrade_session_loader", inspect.signature(main).parameters)
        calls = []
        original_write_all = DayTradeMinuteCache.write_all

        def capture_write(cache, bars, **kwargs):
            calls.append(kwargs)
            return original_write_all(cache, bars, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir, mock.patch.object(
            DayTradeMinuteCache,
            "write_all",
            autospec=True,
            side_effect=capture_write,
        ):
            exit_code = main(
                [
                    "daytrade-cache-fill",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-03",
                    "--cache-dir",
                    tmpdir,
                    "--expected-sessions-json",
                    json.dumps(["2024-01-02", "2024-01-03"]),
                ],
                stdout=io.StringIO(),
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
                daytrade_session_loader=lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("JSON override must win")
                ),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            calls[0]["expected_sessions"],
            [date(2024, 1, 2), date(2024, 1, 3)],
        )

    def test_daytrade_alpaca_cache_fill_uses_trading_calendar(self):
        with tempfile.TemporaryDirectory() as tmpdir, mock.patch(
            "quantbot.integrations.alpaca.AlpacaDataClient.fetch_minute_bars",
            return_value=_synthetic_daytrade_rows(),
        ), mock.patch(
            "quantbot.integrations.alpaca.AlpacaTradingClient.fetch_calendar",
            return_value=[date(2024, 1, 2)],
            create=True,
        ) as fetch_calendar:
            exit_code = main(
                [
                    "daytrade-cache-fill",
                    "--data-source",
                    "alpaca",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--cache-dir",
                    tmpdir,
                ],
                stdout=io.StringIO(),
                environ={"ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret"},
            )

        self.assertEqual(exit_code, 0)
        fetch_calendar.assert_called_once_with(
            start=date(2024, 1, 2),
            end=date(2024, 1, 2),
        )

    def test_daytrade_cli_cache_writes_include_request_range_and_session_union(self):
        calls = []
        original_write_all = DayTradeMinuteCache.write_all

        def capture_write(cache, bars, **kwargs):
            calls.append(kwargs)
            return original_write_all(cache, bars, **kwargs)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with mock.patch.object(
                DayTradeMinuteCache,
                "write_all",
                autospec=True,
                side_effect=capture_write,
            ):
                main(
                    [
                        "daytrade-cache-fill",
                        "--symbols",
                        "AAPL",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-02",
                        "--cache-dir",
                        str(root / "fill-cache"),
                    ],
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
                )
                main(
                    [
                        "daytrade-backtest-run",
                        "--symbols",
                        "AAPL",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-02",
                        "--cache-dir",
                        str(root / "run-cache"),
                        "--trades-log",
                        str(root / "trades.jsonl"),
                        "--daily-log",
                        str(root / "daily.jsonl"),
                        "--evidence-file",
                        str(root / "evidence.jsonl"),
                    ],
                    stdout=io.StringIO(),
                    daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
                )

        self.assertEqual(len(calls), 2)
        for kwargs in calls:
            self.assertEqual(kwargs.get("request_start"), date(2024, 1, 2))
            self.assertEqual(kwargs.get("request_end"), date(2024, 1, 2))
            self.assertEqual(kwargs.get("expected_sessions"), [date(2024, 1, 2)])

    def test_daytrade_cached_loader_checkpoints_each_month_and_resumes_missing_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache_dir = root / "cache"
            command = [
                "daytrade-backtest-run",
                "--symbols",
                "AAPL",
                "--start",
                "2024-01-02",
                "--end",
                "2024-02-02",
                "--cache-dir",
                str(cache_dir),
                "--expected-sessions-json",
                json.dumps(["2024-01-02", "2024-02-02"]),
                "--trades-log",
                str(root / "trades.jsonl"),
                "--daily-log",
                str(root / "daily.jsonl"),
                "--evidence-file",
                str(root / "evidence.jsonl"),
            ]
            first_calls = []

            def interrupted_loader(*, symbols, start, end):
                first_calls.append((symbols, start, end))
                if start.month == 2:
                    raise RuntimeError("simulated second-month interruption")
                return _synthetic_daytrade_rows_for_dates([date(2024, 1, 2)])

            with self.assertRaisesRegex(RuntimeError, "second-month interruption"):
                main(
                    command,
                    stdout=io.StringIO(),
                    daytrade_minute_loader=interrupted_loader,
                )

            self.assertEqual(
                [(item[1], item[2]) for item in first_calls],
                [
                    (date(2024, 1, 2), date(2024, 1, 31)),
                    (date(2024, 2, 1), date(2024, 2, 2)),
                ],
            )
            self.assertTrue(
                (cache_dir / "injected" / "AAPL" / "2024-01.manifest.json").exists()
            )
            resumed_calls = []

            def resumed_loader(*, symbols, start, end):
                resumed_calls.append((symbols, start, end))
                return _synthetic_daytrade_rows_for_dates([date(2024, 2, 2)])

            exit_code = main(
                command,
                stdout=io.StringIO(),
                daytrade_minute_loader=resumed_loader,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [(item[1], item[2]) for item in resumed_calls],
            [(date(2024, 2, 1), date(2024, 2, 2))],
        )

    def test_daytrade_optimize_run_writes_best_config_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-optimize-run",
                    "--historical-audit-replay",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-03-31",
                    "--cache-dir",
                    str(root / "cache"),
                    "--train-months",
                    "1",
                    "--validation-months",
                    "1",
                    "--max-candidates",
                    "3",
                    "--output-file",
                    str(root / "optimize.json"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows_for_dates(
                    [date(2024, 1, 2), date(2024, 2, 2), date(2024, 3, 4)]
                ),
            )
            payload = json.loads((root / "optimize.json").read_text())
            evidence = load_evidence_file(root / "evidence.jsonl")

        self.assertEqual(exit_code, 1)
        self.assertIn("best_config", payload)
        self.assertIn("rankings", payload)
        self.assertEqual(payload["declared_trial_count"], 12)
        self.assertEqual(payload["run_type"], "historical_audit_replay")
        self.assertFalse(payload["selection_eligible"])
        self.assertLess(payload["completed_trial_count"], 12)
        self.assertEqual(payload["feature_snapshot_count"], 0)
        self.assertIn("feature_config", payload)
        self.assertIn("daytrade_walk_forward_optimization", evidence)

    def test_daytrade_optimize_builds_features_and_executes_all_registered_trials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-optimize-run",
                    "--historical-audit-replay",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-02-29",
                    "--cache-dir",
                    str(root / "cache"),
                    "--train-months",
                    "1",
                    "--validation-months",
                    "1",
                    "--max-candidates",
                    "13",
                    "--output-file",
                    str(root / "optimize.json"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=_synthetic_feature_daytrade_loader,
            )
            payload = json.loads((root / "optimize.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertGreater(payload["feature_snapshot_count"], 0)
        self.assertEqual(payload["declared_trial_count"], 12)
        self.assertEqual(payload["completed_trial_count"], 12)
        self.assertEqual(payload["execution_purpose"], "historical_trial_backfill")
        self.assertFalse(payload["selection_eligible"])
        self.assertEqual(len(payload["executed_trial_ids"]), 12)
        self.assertEqual(payload["feature_config"]["required_symbols"], [
            "AAPL",
            "SPY",
            "QQQ",
            "XLK",
        ])

    def test_daytrade_optimize_can_run_only_h6_market_confirmation_trials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-optimize-run",
                    "--experiment-set",
                    "h6-market",
                    "--historical-audit-replay",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-02-29",
                    "--cache-dir",
                    str(root / "cache"),
                    "--cost-bps",
                    "5",
                    "--max-volume-participation",
                    "0.10",
                    "--train-months",
                    "1",
                    "--validation-months",
                    "1",
                    "--max-candidates",
                    "4",
                    "--output-file",
                    str(root / "optimize.json"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=_synthetic_feature_daytrade_loader,
            )
            payload = json.loads((root / "optimize.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["experiment_set"], "h6-market")
        self.assertEqual(payload["run_type"], "historical_audit_replay")
        self.assertFalse(payload["selection_eligible"])
        self.assertEqual(payload["declared_trial_count"], 3)
        self.assertEqual(payload["completed_trial_count"], 3)
        self.assertEqual(
            payload["executed_trial_ids"],
            [
                "daytrade-h6-market-confirmation:trial-01",
                "daytrade-h6-market-confirmation:trial-02",
                "daytrade-h6-market-confirmation:trial-03",
            ],
        )
        self.assertTrue(payload["feature_coverage_complete"])

    def test_daytrade_optimize_can_run_only_h3_rvol_trials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-optimize-run",
                    "--experiment-set",
                    "h3-rvol",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2019-01-01",
                    "--end",
                    "2022-12-31",
                    "--access-mode",
                    "research",
                    "--cache-dir",
                    str(root / "cache"),
                    "--cost-bps",
                    "5",
                    "--max-volume-participation",
                    "0.10",
                    "--train-months",
                    "1",
                    "--validation-months",
                    "1",
                    "--max-candidates",
                    "4",
                    "--output-file",
                    str(root / "optimize.json"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=_synthetic_feature_daytrade_loader,
            )
            payload = json.loads((root / "optimize.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["experiment_set"], "h3-rvol")
        self.assertEqual(payload["run_type"], "historical_baseline_reconstruction")
        self.assertEqual(
            payload["execution_purpose"],
            "frozen_h3_baseline_reconstruction",
        )
        self.assertFalse(payload["selection_eligible"])
        self.assertEqual(payload["declared_trial_count"], 3)
        self.assertEqual(payload["completed_trial_count"], 3)
        self.assertEqual(
            payload["executed_trial_ids"],
            [
                "daytrade-h3-pre-entry-confirmation:trial-01",
                "daytrade-h3-pre-entry-confirmation:trial-02",
                "daytrade-h3-pre-entry-confirmation:trial-03",
            ],
        )
        self.assertTrue(payload["feature_coverage_complete"])

    def test_daytrade_optimize_can_run_only_h7_rvol_window_trials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-optimize-run",
                    "--experiment-set",
                    "h7-rvol-window",
                    "--historical-audit-replay",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-02-29",
                    "--cache-dir",
                    str(root / "cache"),
                    "--cost-bps",
                    "5",
                    "--max-volume-participation",
                    "0.10",
                    "--train-months",
                    "1",
                    "--validation-months",
                    "1",
                    "--max-candidates",
                    "3",
                    "--feature-observation-end",
                    "11:30",
                    "--output-file",
                    str(root / "optimize.json"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=_synthetic_feature_daytrade_loader,
            )
            payload = json.loads((root / "optimize.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["experiment_set"], "h7-rvol-window")
        self.assertEqual(payload["run_type"], "historical_audit_replay")
        self.assertFalse(payload["selection_eligible"])
        self.assertEqual(payload["declared_trial_count"], 2)
        self.assertEqual(payload["completed_trial_count"], 2)
        self.assertEqual(
            payload["executed_trial_ids"],
            [
                "daytrade-h7-rvol-entry-window:trial-01",
                "daytrade-h7-rvol-entry-window:trial-02",
            ],
        )
        self.assertEqual(payload["feature_config"]["observation_end"], "11:30")
        self.assertTrue(payload["feature_coverage_complete"])
        self.assertEqual(
            set(payload["statistical_validation"]["trial_daily_returns"]),
            {
                "daytrade-h7-rvol-entry-window:baseline",
                "daytrade-h7-rvol-entry-window:trial-01",
                "daytrade-h7-rvol-entry-window:trial-02",
            },
        )
        trial_configs = payload["statistical_validation"]["trial_configs"]
        self.assertEqual(
            trial_configs["daytrade-h7-rvol-entry-window:baseline"]["entry_end"],
            "10:30",
        )
        self.assertEqual(
            [
                trial_configs[name]["entry_end"]
                for name in (
                    "daytrade-h7-rvol-entry-window:trial-01",
                    "daytrade-h7-rvol-entry-window:trial-02",
                )
            ],
            ["11:00", "11:30"],
        )
        self.assertTrue(
            all(config["min_relative_volume"] == 2.0 for config in trial_configs.values())
        )
        self.assertEqual(
            payload["statistical_validation_hash"],
            hash_payload(payload["statistical_validation"]),
        )

    def test_daytrade_optimize_can_run_only_h8_rvol_risk_trials(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-optimize-run",
                    "--experiment-set",
                    "h8-rvol-risk",
                    "--historical-audit-replay",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-02-29",
                    "--cache-dir",
                    str(root / "cache"),
                    "--cost-bps",
                    "5",
                    "--max-volume-participation",
                    "0.10",
                    "--train-months",
                    "1",
                    "--validation-months",
                    "1",
                    "--max-candidates",
                    "3",
                    "--output-file",
                    str(root / "optimize.json"),
                    "--evidence-file",
                    str(root / "evidence.jsonl"),
                ],
                stdout=stdout,
                daytrade_minute_loader=_synthetic_feature_daytrade_loader,
            )
            payload = json.loads((root / "optimize.json").read_text())

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["experiment_set"], "h8-rvol-risk")
        self.assertEqual(payload["run_type"], "historical_audit_replay")
        self.assertFalse(payload["selection_eligible"])
        self.assertEqual(payload["declared_trial_count"], 2)
        self.assertEqual(payload["completed_trial_count"], 2)
        self.assertEqual(
            payload["executed_trial_ids"],
            [
                "daytrade-h8-rvol-risk-scale:trial-01",
                "daytrade-h8-rvol-risk-scale:trial-02",
            ],
        )
        trial_configs = payload["statistical_validation"]["trial_configs"]
        self.assertEqual(
            trial_configs["daytrade-h8-rvol-risk-scale:baseline"]["stop_basis"],
            "fixed_pct",
        )
        self.assertEqual(
            [
                trial_configs[name]["stop_basis"]
                for name in (
                    "daytrade-h8-rvol-risk-scale:trial-01",
                    "daytrade-h8-rvol-risk-scale:trial-02",
                )
            ],
            ["atr", "opening_range"],
        )
        self.assertTrue(
            all(config["min_relative_volume"] == 2.0 for config in trial_configs.values())
        )
        self.assertEqual(
            set(payload["statistical_validation"]["trial_metrics"]),
            set(trial_configs),
        )

    def test_retired_daytrade_experiments_fail_before_market_data_access(self):
        for experiment_set in (
            "h1-h4",
            "h6-market",
            "h7-rvol-window",
            "h8-rvol-risk",
        ):
            with self.subTest(experiment_set=experiment_set), tempfile.TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)

                def forbidden_loader(**_kwargs):
                    self.fail("retired experiment reached the market-data loader")

                with self.assertRaisesRegex(ValueError, "historical-audit-replay"):
                    main(
                        [
                            "daytrade-optimize-run",
                            "--experiment-set",
                            experiment_set,
                            "--symbols",
                            "AAPL",
                            "--start",
                            "2024-01-02",
                            "--end",
                            "2024-02-29",
                            "--output-file",
                            str(root / "optimize.json"),
                            "--evidence-file",
                            str(root / "evidence.jsonl"),
                        ],
                        daytrade_minute_loader=forbidden_loader,
                    )
                self.assertFalse((root / "optimize.json").exists())

    def test_retired_daytrade_audit_cannot_consume_exclusive_validation_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with self.assertRaisesRegex(ValueError, "cannot consume"):
                main(
                    [
                        "daytrade-optimize-run",
                        "--experiment-set",
                        "h6-market",
                        "--historical-audit-replay",
                        "--access-mode",
                        "fixed_holdout",
                        "--symbols",
                        "AAPL",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-02-29",
                        "--output-file",
                        str(root / "optimize.json"),
                        "--evidence-file",
                        str(root / "evidence.jsonl"),
                    ]
                )

    def test_retired_daytrade_comparison_fails_before_market_data_access(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "retired.json"
            config_path.write_text(
                json.dumps(
                    {
                        "best_config_name": "daytrade-h5-opening-range-top4:trial-01",
                        "best_config": {"max_active_symbols": 4},
                    }
                )
            )

            def forbidden_loader(**_kwargs):
                self.fail("retired comparison reached the market-data loader")

            with self.assertRaisesRegex(ValueError, "historical-audit-replay"):
                main(
                    [
                        "daytrade-compare-configs",
                        "--symbols",
                        "AAPL",
                        "--start",
                        "2024-01-02",
                        "--end",
                        "2024-01-31",
                        "--walk-forward-config-json",
                        str(config_path),
                    ],
                    daytrade_minute_loader=forbidden_loader,
                )

    def test_legacy_normal_and_static_daytrade_cannot_read_unreleased_sealed_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            calls = []

            def forbidden_loader(**kwargs):
                calls.append(kwargs)
                self.fail("unreleased sealed data reached a market-data loader")

            frozen_now = datetime(2026, 7, 20, tzinfo=timezone.utc)
            with mock.patch(
                "quantbot.validation_protocol._utc_now",
                return_value=frozen_now,
            ):
                with self.assertRaisesRegex(PermissionError, "not released"):
                    main(
                        [
                            "oos-backtest-run",
                            "--evidence-file",
                            str(root / "oos-evidence.jsonl"),
                            "--data-start",
                            "2026-08-03",
                            "--data-end",
                            "2026-08-04",
                        ],
                        oos_price_loader=forbidden_loader,
                    )
                with self.assertRaisesRegex(PermissionError, "not released"):
                    main(
                        [
                            "historical-replay",
                            "--evidence-file",
                            str(root / "historical-evidence.jsonl"),
                            "--paper-snapshot-log",
                            str(root / "paper-snapshots.jsonl"),
                            "--paper-runs-log",
                            str(root / "paper-runs.jsonl"),
                            "--live-snapshot-log",
                            str(root / "live-snapshots.jsonl"),
                            "--live-runs-log",
                            str(root / "live-runs.jsonl"),
                            "--data-start",
                            "2026-08-03",
                            "--data-end",
                            "2026-08-04",
                        ],
                        oos_price_loader=forbidden_loader,
                    )
                with self.assertRaisesRegex(PermissionError, "not released"):
                    main(
                        [
                            "daytrade-backtest-run",
                            "--symbols",
                            "AAPL",
                            "--start",
                            "2026-08-03",
                            "--end",
                            "2026-08-04",
                            "--trades-log",
                            str(root / "trades.jsonl"),
                            "--daily-log",
                            str(root / "daily.jsonl"),
                            "--evidence-file",
                            str(root / "daytrade-evidence.jsonl"),
                        ],
                        daytrade_minute_loader=forbidden_loader,
                    )

            self.assertEqual(calls, [])

    def test_daytrade_compare_configs_outputs_three_named_configs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "best.json"
            config_path.write_text(json.dumps({"best_config": {"symbol_filter": "opening-range", "max_active_symbols": 4}}))
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-compare-configs",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--cost-bps",
                    "7.5",
                    "--max-volume-participation",
                    "0.12",
                    "--walk-forward-config-json",
                    str(config_path),
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertIn("baseline_top10", payload["configs"])
        self.assertIn("opening_range_filter4", payload["configs"])
        self.assertIn("walk_forward_best", payload["configs"])
        self.assertEqual(payload["configs"]["baseline_top10"]["config"]["cost_bps"], 7.5)
        self.assertEqual(
            payload["configs"]["baseline_top10"]["config"]["max_volume_participation"],
            0.12,
        )

    def test_retired_h5_cannot_consume_fixed_holdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            baseline = DayTradeConfig(
                entry_end="10:30",
                cost_bps=5.0,
                max_volume_participation=0.10,
            )
            candidate_name, candidate = daytrade_followup_candidate_configs(baseline)[0]
            config_path = root / "candidate.json"
            config_path.write_text(
                json.dumps(
                    {
                        "best_config_name": candidate_name,
                        "best_config": daytrade_config_to_payload(candidate),
                    }
                ),
                encoding="utf-8",
            )
            manifest_path = root / "holdout.json"
            dataset_hash = "d" * 64
            manifest_path.write_text(
                json.dumps(
                    {
                        "manifest_version": 1,
                        "run_id": "daytrade-h5-test",
                        "status": "frozen_unrun",
                        "run_limit": 1,
                        "dataset_hash": dataset_hash,
                        "strategy_family": "daytrade",
                        "holdout_start": "2024-01-02",
                        "holdout_end": "2024-01-02",
                        "candidate_name": candidate_name,
                        "feature_required": False,
                        "data_source": "auto",
                        "alpaca_feed": "",
                        "symbols": ["AAPL"],
                        "feature_warmup_days": 60,
                        "minute_cache_schema_version": 2,
                        "baseline_config_hash": hash_payload(
                            daytrade_config_to_payload(baseline)
                        ),
                        "candidate_config_hash": hash_payload(
                            daytrade_config_to_payload(candidate)
                        ),
                        "code_hash": source_tree_hash(),
                        "stress_cost_bps": 12.0,
                    }
                ),
                encoding="utf-8",
            )
            calls = []

            def loader(**kwargs):
                calls.append(kwargs)
                return _synthetic_daytrade_rows()

            command = [
                "daytrade-compare-configs",
                "--symbols",
                "AAPL",
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-02",
                "--cache-dir",
                "",
                "--cost-bps",
                "5",
                "--max-volume-participation",
                "0.10",
                "--walk-forward-config-json",
                str(config_path),
                "--holdout-manifest",
                str(manifest_path),
                "--exposure-ledger",
                str(root / "exposure-ledger.jsonl"),
                "--run-id",
                "daytrade-h5-test",
                "--access-mode",
                "fixed_holdout",
                "--dataset-hash",
                dataset_hash,
                "--stress-cost-bps",
                "12",
            ]
            with self.assertRaisesRegex(ValueError, "historical-audit-replay"):
                main(
                    command,
                    stdout=io.StringIO(),
                    daytrade_minute_loader=loader,
                )
            with self.assertRaisesRegex(ValueError, "cannot consume"):
                main(
                    [*command, "--historical-audit-replay"],
                    stdout=io.StringIO(),
                    daytrade_minute_loader=loader,
                )
            self.assertEqual(calls, [])
            self.assertFalse((root / "exposure-ledger.jsonl").exists())

    def test_daytrade_status_reads_logs_and_reports_duration_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trades_log = root / "trades.jsonl"
            daily_log = root / "daily.jsonl"
            evidence_file = root / "evidence.jsonl"
            stdout = io.StringIO()
            main(
                [
                    "daytrade-backtest-run",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--trades-log",
                    str(trades_log),
                    "--daily-log",
                    str(daily_log),
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
            )
            status_stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-status",
                    "--trades-log",
                    str(trades_log),
                    "--daily-log",
                    str(daily_log),
                    "--min-sessions",
                    "20",
                ],
                stdout=status_stdout,
            )

        payload = json.loads(status_stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["sessions"], 1)
        self.assertEqual(payload["remaining_sessions"], 19)
        self.assertIn("duration", payload["reasons"])

    def test_daytrade_live_cycle_requires_explicit_confirmation_before_loading_data(self):
        stdout = io.StringIO()

        exit_code = main(
            [
                "daytrade-live-cycle",
                "--symbols",
                "AAPL",
                "--start",
                "2024-01-02",
                "--end",
                "2024-01-02",
            ],
            stdout=stdout,
            daytrade_minute_loader=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("loader should not run")),
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["error"], "confirm_live_required")

    def test_daytrade_live_cycle_rejects_order_submission_before_loading_or_writing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            orders = []
            stdout = io.StringIO()
            trades_log = root / "trades.jsonl"
            daily_log = root / "daily.jsonl"
            evidence_file = root / "evidence.jsonl"

            exit_code = main(
                [
                    "daytrade-live-cycle",
                    "--confirm-live",
                    "--submit-orders",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--trades-log",
                    str(trades_log),
                    "--daily-log",
                    str(daily_log),
                    "--evidence-file",
                    str(evidence_file),
                    "--min-sessions",
                    "1",
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: (_ for _ in ()).throw(
                    AssertionError("loader should not run")
                ),
                daytrade_order_submitter=lambda **kwargs: orders.append(kwargs),
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["error"], "live_trading_disabled")
        self.assertEqual(orders, [])
        self.assertFalse(trades_log.exists())
        self.assertFalse(daily_log.exists())
        self.assertFalse(evidence_file.exists())

    def test_daytrade_live_cycle_without_submission_is_labeled_historical_replay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_file = root / "evidence.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "daytrade-live-cycle",
                    "--confirm-live",
                    "--symbols",
                    "AAPL",
                    "--start",
                    "2024-01-02",
                    "--end",
                    "2024-01-02",
                    "--trades-log",
                    str(root / "trades.jsonl"),
                    "--daily-log",
                    str(root / "daily.jsonl"),
                    "--evidence-file",
                    str(evidence_file),
                    "--min-sessions",
                    "1",
                ],
                stdout=stdout,
                daytrade_minute_loader=lambda **_kwargs: _synthetic_daytrade_rows(),
            )
            evidence_payload = json.loads(evidence_file.read_text(encoding="utf-8"))

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(payload.get("run_type"), "historical_replay")
        self.assertEqual(evidence_payload.get("run_type"), "historical_replay")

def _synthetic_oos_prices():
    rates = {
        "AAA": (0.0012, 0.0004),
        "BBB": (0.0002, 0.0002),
        "CCC": (-0.0001, 0.0002),
        "SPY": (0.00035, 0.00015),
    }
    prices = {ticker: {} for ticker in rates}
    levels = {ticker: 100.0 for ticker in rates}
    current = date(2014, 1, 1)
    end = date(2024, 12, 31)
    index = 0
    while current <= end:
        if current.weekday() < 5:
            for ticker, (base, amplitude) in rates.items():
                levels[ticker] *= 1 + base + amplitude * ((index % 5) - 2)
                prices[ticker][current] = levels[ticker]
            index += 1
        current += timedelta(days=1)
    return prices


def _synthetic_daytrade_rows():
    return _synthetic_daytrade_rows_for_dates([date(2024, 1, 2)])


def _synthetic_daytrade_rows_for_dates(days):
    rows = []
    for day in days:
        start = datetime(day.year, day.month, day.day, 14, 30, tzinfo=timezone.utc)
        for offset in range(15):
            rows.append(
                {
                    "ticker": "AAPL",
                    "timestamp": start + timedelta(minutes=offset),
                    "open": 99.5,
                    "high": 100.0,
                    "low": 99.0,
                    "close": 99.8,
                    "volume": 1000,
                    "vwap": 99.7,
                }
            )
        rows.append(
            {
                "ticker": "AAPL",
                "timestamp": start + timedelta(minutes=15),
                "open": 100.2,
                "high": 101.2,
                "low": 100.8,
                "close": 101.0,
                "volume": 1500,
                "vwap": 100.5,
            }
        )
        rows.append(
            {
                "ticker": "AAPL",
                "timestamp": start + timedelta(minutes=16),
                "open": 101.0,
                "high": 102.0,
                "low": 100.9,
                "close": 101.9,
                "volume": 1200,
                "vwap": 101.0,
            }
        )
    return rows


def _synthetic_daytrade_rows_for_symbols_and_dates(symbols, days):
    rows = []
    for symbol in symbols:
        for row in _synthetic_daytrade_rows_for_dates(days):
            item = dict(row)
            item["ticker"] = symbol
            rows.append(item)
    return rows


def _synthetic_feature_daytrade_loader(*, symbols, start, end):
    eastern = ZoneInfo("US/Eastern")
    bases = {"AAPL": 100.0, "SPY": 500.0, "QQQ": 400.0, "XLK": 200.0}
    rows = []
    day = start
    session_index = 0
    while day <= end:
        if day.weekday() >= 5:
            day += timedelta(days=1)
            continue
        for symbol in symbols:
            base = bases.get(symbol, 100.0) * (1.0 + session_index * 0.0002)
            for local_time, close_offset, volume in (
                ((8, 0), 0.0, 500.0),
                ((9, 30), 0.0, 1_000.0),
                ((9, 44), 0.1, 1_000.0),
                ((9, 45), 0.3, 1_500.0),
                ((9, 46), 0.4, 1_200.0),
            ):
                timestamp = datetime(
                    day.year,
                    day.month,
                    day.day,
                    local_time[0],
                    local_time[1],
                    tzinfo=eastern,
                ).astimezone(timezone.utc)
                close = base + close_offset
                rows.append(
                    {
                        "ticker": symbol,
                        "timestamp": timestamp,
                        "open": base,
                        "high": close + 0.1,
                        "low": base - 0.1,
                        "close": close,
                        "volume": volume,
                        "vwap": (base + close) / 2.0,
                    }
                )
        session_index += 1
        day += timedelta(days=1)
    return rows


if __name__ == "__main__":
    unittest.main()
