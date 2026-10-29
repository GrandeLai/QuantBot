import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.research_cli import main
from quantbot.live import (
    LiveEquitySnapshotJournal,
    LiveTradingJournal,
    LiveTradingRun,
    build_live_trading_evidence,
    evaluate_live_trading_runs,
)
from quantbot.paper import (
    PaperAccountSnapshotJournal,
    PaperTradingAcceptance,
    PaperTradingJournal,
    PaperTradingRun,
    build_paper_trading_evidence,
)


class StockBotAcceptanceNumericValidationTest(unittest.TestCase):
    def test_paper_evaluator_rejects_nonfinite_returns_and_negative_errors(self):
        decision = PaperTradingAcceptance(min_weeks=2).evaluate(
            [
                PaperTradingRun("2026-W01", 0, float("nan"), 0.01),
                PaperTradingRun("2026-W02", -1, 0.01, float("inf")),
            ]
        )

        self.assertFalse(decision.passed)
        self.assertIn("invalid_errors", decision.reasons)
        self.assertIn("invalid_paper_return", decision.reasons)
        self.assertIn("invalid_backtest_return", decision.reasons)
        self.assertTrue(math.isfinite(decision.performance_deviation))

        huge = PaperTradingAcceptance(min_weeks=1).evaluate(
            [PaperTradingRun("2026-W03", 0, 10**400, 0.01)]
        )
        self.assertFalse(huge.passed)
        self.assertIn("invalid_paper_return", huge.reasons)

    def test_live_evaluator_rejects_error_cancellation_and_invalid_drawdown_domain(self):
        decision = evaluate_live_trading_runs(
            [
                LiveTradingRun("2026-07-01", 1, float("nan")),
                LiveTradingRun("2026-07-02", -1, -0.01),
                LiveTradingRun("2026-07-03", 0, float("inf")),
                LiveTradingRun("2026-07-04", 0, 1.01),
            ],
            min_sessions=4,
        )

        self.assertFalse(decision.passed)
        self.assertEqual(decision.errors, 1)
        self.assertIn("invalid_errors", decision.reasons)
        self.assertIn("invalid_drawdown", decision.reasons)
        self.assertTrue(math.isfinite(decision.worst_drawdown))

    def test_bad_paper_journal_is_read_but_evidence_and_status_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "paper.jsonl"
            records = [
                {
                    "period": "2026-W%02d" % week,
                    "errors": 0,
                    "paper_return": float("nan") if week == 1 else 0.01,
                    "backtest_return": 0.01,
                }
                for week in range(1, 5)
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            runs = PaperTradingJournal(path).read_all()
            evidence = build_paper_trading_evidence(runs)
            stdout = io.StringIO()
            exit_code = main(["paper-status", "--log", str(path)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertTrue(math.isnan(runs[0].paper_return))
        self.assertFalse(evidence["paper_trading_4_weeks"].passed)
        self.assertFalse(evidence["paper_vs_backtest_deviation"].passed)
        self.assertIn("invalid_paper_return", evidence["paper_vs_backtest_deviation"].detail)
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["passed"])
        self.assertIn("invalid_paper_return", payload["reasons"])
        self.assertTrue(math.isfinite(payload["performance_deviation"]))

    def test_bad_live_journal_is_read_but_evidence_and_status_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "live.jsonl"
            records = []
            for day in range(1, 21):
                records.append(
                    {
                        "day": "2026-07-%02d" % day,
                        "errors": 1 if day == 1 else -1 if day == 2 else 0,
                        "drawdown": float("nan") if day == 3 else 0.01,
                    }
                )
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            runs = LiveTradingJournal(path).read_all()
            evidence = build_live_trading_evidence(runs)["live_trading_soak"]
            stdout = io.StringIO()
            exit_code = main(["live-soak-status", "--log", str(path)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertTrue(math.isnan(runs[2].drawdown))
        self.assertFalse(evidence.passed)
        self.assertIn("invalid_errors", evidence.detail)
        self.assertIn("invalid_drawdown", evidence.detail)
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["passed"])
        self.assertEqual(payload["errors"], 1)
        self.assertIn("invalid_errors", payload["reasons"])
        self.assertIn("invalid_drawdown", payload["reasons"])

    def test_journal_record_entries_reject_invalid_numeric_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases = [
                (
                    "invalid_errors",
                    lambda: PaperTradingJournal(root / "paper.jsonl").append(
                        "2026-W01", -1, 0.01, 0.01
                    ),
                ),
                (
                    "invalid_paper_return",
                    lambda: PaperTradingJournal(root / "paper.jsonl").append(
                        "2026-W01", 0, float("nan"), 0.01
                    ),
                ),
                (
                    "invalid_backtest_return",
                    lambda: PaperTradingJournal(root / "paper.jsonl").append(
                        "2026-W01", 0, 0.01, float("inf")
                    ),
                ),
                (
                    "invalid_equity",
                    lambda: PaperAccountSnapshotJournal(root / "paper-snapshots.jsonl").append(
                        "2026-07-01T00:00:00Z", float("nan")
                    ),
                ),
                (
                    "invalid_drawdown",
                    lambda: LiveTradingJournal(root / "live.jsonl").append(
                        "2026-07-01", 0, 1.01
                    ),
                ),
                (
                    "invalid_equity",
                    lambda: LiveEquitySnapshotJournal(root / "live-snapshots.jsonl").append(
                        "2026-07-01T00:00:00Z", float("inf")
                    ),
                ),
            ]

            for reason, record in cases:
                with self.subTest(reason=reason):
                    with self.assertRaisesRegex(ValueError, reason):
                        record()

    def test_cli_record_commands_return_one_without_writing_invalid_values(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cases = [
                (
                    root / "paper.jsonl",
                    [
                        "paper-run-record",
                        "--log",
                        str(root / "paper.jsonl"),
                        "--period",
                        "2026-W01",
                        "--errors",
                        "0",
                        "--paper-return",
                        "nan",
                        "--backtest-return",
                        "0.01",
                    ],
                ),
                (
                    root / "paper-snapshots.jsonl",
                    [
                        "paper-snapshot-record",
                        "--log",
                        str(root / "paper-snapshots.jsonl"),
                        "--timestamp",
                        "2026-07-01T00:00:00Z",
                        "--equity",
                        "inf",
                    ],
                ),
                (
                    root / "live.jsonl",
                    [
                        "live-soak-record",
                        "--log",
                        str(root / "live.jsonl"),
                        "--day",
                        "2026-07-01",
                        "--errors",
                        "-1",
                        "--drawdown",
                        "0.01",
                    ],
                ),
                (
                    root / "live-snapshots.jsonl",
                    [
                        "live-soak-snapshot-record",
                        "--log",
                        str(root / "live-snapshots.jsonl"),
                        "--timestamp",
                        "2026-07-01T00:00:00Z",
                        "--equity",
                        "nan",
                    ],
                ),
            ]

            for path, argv in cases:
                with self.subTest(command=argv[0]):
                    stdout = io.StringIO()
                    exit_code = main(argv, stdout=stdout)
                    self.assertEqual(exit_code, 1)
                    self.assertFalse(path.exists())
                    self.assertFalse(json.loads(stdout.getvalue())["recorded"])


if __name__ == "__main__":
    unittest.main()
