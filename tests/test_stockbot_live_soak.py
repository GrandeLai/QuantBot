import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file
from quantbot.research_cli import main
from quantbot.live import (
    LiveEquitySnapshotJournal,
    LiveTradingJournal,
    build_live_trading_evidence,
    live_day_for_timestamp,
    rollup_live_snapshots,
)


class StockBotLiveSoakTest(unittest.TestCase):
    def test_live_trading_journal_persists_sessions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "live_runs.jsonl"
            journal = LiveTradingJournal(path)
            journal.append(day="2024-01-02", errors=0, drawdown=0.01, notes="small capital")
            journal.append(day="2024-01-03", errors=1, drawdown=0.02, notes="order retry")

            runs = journal.read_all()

        self.assertEqual([run.day for run in runs], ["2024-01-02", "2024-01-03"])
        self.assertEqual(runs[1].errors, 1)
        self.assertEqual(runs[1].notes, "order retry")

    def test_live_trading_evidence_requires_minimum_clean_sessions_and_drawdown_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "live_runs.jsonl"
            journal = LiveTradingJournal(path)
            for day in range(1, 21):
                journal.append(day="2024-01-%02d" % day, errors=0, drawdown=0.03, notes="")

            evidence = build_live_trading_evidence(journal.read_all(), min_sessions=5, max_drawdown=0.15)

        self.assertTrue(evidence["live_trading_soak"].passed)

    def test_live_trading_evidence_fails_on_errors_or_excess_drawdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "live_runs.jsonl"
            journal = LiveTradingJournal(path)
            journal.append(day="2024-01-02", errors=0, drawdown=0.03, notes="")
            journal.append(day="2024-01-03", errors=1, drawdown=0.16, notes="")

            evidence = build_live_trading_evidence(journal.read_all(), min_sessions=2, max_drawdown=0.15)

        self.assertFalse(evidence["live_trading_soak"].passed)
        self.assertIn("errors=1", evidence["live_trading_soak"].detail)

    def test_live_equity_snapshots_roll_up_to_sessions_with_drawdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_log = Path(tmpdir) / "live_snapshots.jsonl"
            journal = LiveEquitySnapshotJournal(snapshot_log)
            journal.append(timestamp="2026-07-06T21:00:00Z", equity=100000.0, errors=0)
            journal.append(timestamp="2026-07-07T21:00:00Z", equity=102000.0, errors=0)
            journal.append(timestamp="2026-07-08T21:00:00Z", equity=96900.0, errors=1, notes="retry")

            rollup = rollup_live_snapshots(journal.read_all(), baseline_equity=100000.0)

        self.assertEqual(live_day_for_timestamp("2026-07-06T21:00:00Z"), "2026-07-06")
        self.assertEqual([session.day for session in rollup.sessions], ["2026-07-06", "2026-07-07", "2026-07-08"])
        self.assertAlmostEqual(rollup.sessions[0].drawdown, 0.0)
        self.assertAlmostEqual(rollup.sessions[2].drawdown, 0.05)
        self.assertEqual(rollup.sessions[2].errors, 1)
        self.assertIn("retry", rollup.sessions[2].notes)

    def test_cli_live_soak_record_and_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            live_log = Path(tmpdir) / "live_runs.jsonl"
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            stdout = io.StringIO()

            record_exit = main(
                [
                    "live-soak-record",
                    "--log",
                    str(live_log),
                    "--day",
                    "2024-01-02",
                    "--errors",
                    "0",
                    "--drawdown",
                    "0.01",
                    "--notes",
                    "ok",
                ],
                stdout=stdout,
            )
            journal = LiveTradingJournal(live_log)
            for day in range(3, 22):
                journal.append(
                    day="2024-01-%02d" % day,
                    errors=0,
                    drawdown=0.01,
                    notes="ok",
                )
            evidence_exit = main(
                [
                    "live-soak-evidence",
                    "--log",
                    str(live_log),
                    "--evidence-file",
                    str(evidence_file),
                    "--min-sessions",
                    "1",
                ],
                stdout=stdout,
            )
            evidence = load_evidence_file(evidence_file)

        self.assertEqual(record_exit, 0)
        self.assertEqual(evidence_exit, 0)
        self.assertTrue(evidence["live_trading_soak"].passed)
        self.assertEqual(evidence["live_trading_soak"].run_type, "live_trading")
        self.assertEqual(len(evidence["live_trading_soak"].dataset_hash), 64)

    def test_cli_live_soak_evidence_rejects_twenty_records_for_one_day_and_preserves_provenance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            live_log = Path(tmpdir) / "live_runs.jsonl"
            evidence_file = Path(tmpdir) / "evidence.jsonl"
            journal = LiveTradingJournal(live_log)
            for _record in range(20):
                journal.append(day="2024-01-02", errors=0, drawdown=0.01, notes="")
            stdout = io.StringIO()

            exit_code = main(
                [
                    "live-soak-evidence",
                    "--log",
                    str(live_log),
                    "--evidence-file",
                    str(evidence_file),
                ],
                stdout=stdout,
            )
            evidence = load_evidence_file(evidence_file)["live_trading_soak"]

        self.assertEqual(exit_code, 1)
        self.assertFalse(evidence.passed)
        self.assertIn("sessions=1", evidence.detail)
        self.assertIn("duplicate_day", evidence.detail)
        self.assertEqual(evidence.run_type, "live_trading")
        self.assertEqual(len(evidence.dataset_hash), 64)

    def test_cli_live_soak_snapshot_record_and_rollup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            snapshot_log = Path(tmpdir) / "live_snapshots.jsonl"
            runs_log = Path(tmpdir) / "live_runs.jsonl"
            stdout = io.StringIO()

            first_exit = main(
                [
                    "live-soak-snapshot-record",
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
                    "live-soak-snapshot-record",
                    "--log",
                    str(snapshot_log),
                    "--timestamp",
                    "2026-07-07T21:00:00Z",
                    "--equity",
                    "99000",
                ],
                stdout=stdout,
            )
            rollup_exit = main(
                [
                    "live-soak-rollup-snapshots",
                    "--snapshot-log",
                    str(snapshot_log),
                    "--runs-log",
                    str(runs_log),
                    "--baseline-equity",
                    "100000",
                ],
                stdout=stdout,
            )
            snapshots = LiveEquitySnapshotJournal(snapshot_log).read_all()
            runs = LiveTradingJournal(runs_log).read_all()

        payload = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(rollup_exit, 0)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(payload["sessions"], 2)
        self.assertAlmostEqual(runs[1].drawdown, 0.01)

    def test_cli_live_soak_status_reports_remaining_sessions_and_risk_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            live_log = Path(tmpdir) / "live_runs.jsonl"
            journal = LiveTradingJournal(live_log)
            journal.append(day="2024-01-02", errors=0, drawdown=0.03, notes="")
            journal.append(day="2024-01-03", errors=1, drawdown=0.16, notes="retry")
            stdout = io.StringIO()

            exit_code = main(
                [
                    "live-soak-status",
                    "--log",
                    str(live_log),
                    "--min-sessions",
                    "5",
                    "--max-drawdown",
                    "0.15",
                ],
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["sessions"], 2)
        self.assertEqual(payload["remaining_sessions"], 18)
        self.assertEqual(payload["errors"], 1)
        self.assertIn("duration", payload["reasons"])
        self.assertIn("runtime_errors", payload["reasons"])
        self.assertIn("drawdown", payload["reasons"])

    def test_cli_live_soak_status_counts_unique_days(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            live_log = Path(tmpdir) / "live_runs.jsonl"
            journal = LiveTradingJournal(live_log)
            for _record in range(20):
                journal.append(day="2024-01-02", errors=0, drawdown=0.01, notes="")
            stdout = io.StringIO()

            exit_code = main(
                ["live-soak-status", "--log", str(live_log)],
                stdout=stdout,
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["sessions"], 1)
        self.assertEqual(payload["remaining_sessions"], 19)
        self.assertIn("duplicate_day", payload["reasons"])


if __name__ == "__main__":
    unittest.main()
