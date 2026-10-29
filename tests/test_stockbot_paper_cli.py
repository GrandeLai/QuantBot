import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file
from quantbot.research_cli import main
from quantbot.paper import PaperAccountSnapshotJournal, PaperTradingJournal


class StockBotPaperCliTest(unittest.TestCase):
    def test_cli_paper_run_record_appends_run_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "paper_runs.jsonl"
            stdout = io.StringIO()

            exit_code = main(
                [
                    "paper-run-record",
                    "--log",
                    str(log),
                    "--period",
                    "week1",
                    "--errors",
                    "0",
                    "--paper-return",
                    "0.01",
                    "--backtest-return",
                    "0.011",
                ],
                stdout=stdout,
            )
            runs = PaperTradingJournal(log).read_all()

        self.assertEqual(exit_code, 0)
        self.assertEqual(runs[0].period, "week1")
        self.assertEqual(runs[0].errors, 0)

    def test_cli_paper_evidence_writes_both_required_evidence_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "paper_runs.jsonl"
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            journal = PaperTradingJournal(log)
            for week in range(1, 5):
                journal.append(period="week%d" % week, errors=0, paper_return=0.01, backtest_return=0.01)
            stdout = io.StringIO()

            exit_code = main(
                ["paper-evidence", "--log", str(log), "--evidence-file", str(evidence_file)],
                stdout=stdout,
            )
            evidence = load_evidence_file(evidence_file)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["passed"])
        self.assertTrue(evidence["paper_trading_4_weeks"].passed)
        self.assertTrue(evidence["paper_vs_backtest_deviation"].passed)
        self.assertEqual(evidence["paper_trading_4_weeks"].run_type, "paper_trading")
        self.assertEqual(len(evidence["paper_trading_4_weeks"].dataset_hash), 64)

    def test_cli_paper_evidence_rejects_four_records_for_one_period_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "paper_runs.jsonl"
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            journal = PaperTradingJournal(log)
            for _record in range(4):
                journal.append(
                    period="2026-W28",
                    errors=0,
                    paper_return=0.01,
                    backtest_return=0.01,
                )
            stdout = io.StringIO()

            exit_code = main(
                ["paper-evidence", "--log", str(log), "--evidence-file", str(evidence_file)],
                stdout=stdout,
            )
            evidence = load_evidence_file(evidence_file)["paper_trading_4_weeks"]

        self.assertEqual(exit_code, 1)
        self.assertFalse(evidence.passed)
        self.assertIn("1 paper trading periods", evidence.detail)
        self.assertIn("duplicate_period", evidence.detail)
        self.assertEqual(evidence.run_type, "paper_trading")
        self.assertEqual(len(evidence.dataset_hash), 64)

    def test_cli_paper_status_reports_remaining_weeks_and_deviation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "paper_runs.jsonl"
            journal = PaperTradingJournal(log)
            journal.append(period="week1", errors=0, paper_return=0.010, backtest_return=0.011)
            journal.append(period="week2", errors=0, paper_return=0.009, backtest_return=0.010)
            stdout = io.StringIO()

            exit_code = main(["paper-status", "--log", str(log)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["weeks"], 2)
        self.assertEqual(payload["remaining_weeks"], 2)
        self.assertEqual(payload["errors"], 0)
        self.assertIn("duration", payload["reasons"])

    def test_cli_paper_status_counts_unique_periods(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "paper_runs.jsonl"
            journal = PaperTradingJournal(log)
            for _record in range(4):
                journal.append(
                    period="2026-W28",
                    errors=0,
                    paper_return=0.01,
                    backtest_return=0.01,
                )
            stdout = io.StringIO()

            exit_code = main(["paper-status", "--log", str(log)], stdout=stdout)

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["weeks"], 1)
        self.assertEqual(payload["remaining_weeks"], 3)
        self.assertIn("duplicate_period", payload["reasons"])

    def test_cli_paper_snapshot_record_and_rollup_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_log = Path(tmpdir) / "paper_snapshots.jsonl"
            runs_log = Path(tmpdir) / "paper_runs.jsonl"
            stdout = io.StringIO()

            first_exit = main(
                [
                    "paper-snapshot-record",
                    "--log",
                    str(snapshot_log),
                    "--timestamp",
                    "2026-07-06T21:00:00Z",
                    "--equity",
                    "100000",
                ],
                stdout=stdout,
            )
            second_exit = main(
                [
                    "paper-snapshot-record",
                    "--log",
                    str(snapshot_log),
                    "--timestamp",
                    "2026-07-10T21:00:00Z",
                    "--equity",
                    "101000",
                ],
                stdout=stdout,
            )
            rollup_exit = main(
                [
                    "paper-rollup-snapshots",
                    "--snapshot-log",
                    str(snapshot_log),
                    "--runs-log",
                    str(runs_log),
                    "--backtest-returns-json",
                    json.dumps({"2026-W28": 0.008}),
                ],
                stdout=stdout,
            )
            snapshots = PaperAccountSnapshotJournal(snapshot_log).read_all()
            runs = PaperTradingJournal(runs_log).read_all()

        lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(rollup_exit, 0)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(lines[-1]["runs"], 1)
        self.assertEqual(runs[0].period, "2026-W28")
        self.assertAlmostEqual(runs[0].paper_return, 0.01)


if __name__ == "__main__":
    unittest.main()
