import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from quantbot.audit import load_evidence_file
from quantbot.research_cli import main
from quantbot.live import LiveEquitySnapshotJournal, LiveTradingJournal
from quantbot.paper import PaperAccountSnapshotJournal, PaperTradingJournal


class StockBotAcceptanceCycleTest(unittest.TestCase):
    def test_acceptance_cycle_records_snapshots_rolls_up_and_writes_current_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_file = root / "evidence.jsonl"
            paper_snapshots = root / "paper_snapshots.jsonl"
            paper_runs = root / "paper_runs.jsonl"
            live_snapshots = root / "live_snapshots.jsonl"
            live_runs = root / "live_runs.jsonl"
            PaperAccountSnapshotJournal(paper_snapshots).append(
                timestamp="2026-07-06T21:00:00Z",
                equity=100000.0,
            )
            LiveEquitySnapshotJournal(live_snapshots).append(
                timestamp="2026-07-06T21:00:00Z",
                equity=100000.0,
            )
            stdout = io.StringIO()

            exit_code = main(
                [
                    "acceptance-cycle",
                    "--evidence-file",
                    str(evidence_file),
                    "--paper-snapshot-log",
                    str(paper_snapshots),
                    "--paper-runs-log",
                    str(paper_runs),
                    "--paper-backtest-returns-json",
                    json.dumps({"2026-W28": 0.008}),
                    "--paper-equity",
                    "101000",
                    "--paper-timestamp",
                    "2026-07-10T21:00:00Z",
                    "--live-snapshot-log",
                    str(live_snapshots),
                    "--live-runs-log",
                    str(live_runs),
                    "--live-equity",
                    "99000",
                    "--live-timestamp",
                    "2026-07-07T21:00:00Z",
                    "--live-baseline-equity",
                    "100000",
                ],
                stdout=stdout,
            )
            evidence = load_evidence_file(evidence_file)
            paper = PaperTradingJournal(paper_runs).read_all()
            live = LiveTradingJournal(live_runs).read_all()

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["complete"])
        self.assertEqual(payload["paper"]["rollup"]["runs"], 1)
        self.assertEqual(payload["paper"]["status"]["weeks"], 1)
        self.assertEqual(payload["live"]["rollup"]["sessions"], 2)
        self.assertAlmostEqual(live[1].drawdown, 0.01)
        self.assertEqual(paper[0].period, "2026-W28")
        self.assertFalse(evidence["paper_trading_4_weeks"].passed)
        self.assertTrue(evidence["paper_vs_backtest_deviation"].passed)
        self.assertFalse(evidence["live_trading_soak"].passed)

    def test_acceptance_cycle_uses_custom_thresholds_for_evidence_and_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            evidence_file = root / "evidence.jsonl"
            paper_snapshots = root / "paper_snapshots.jsonl"
            live_snapshots = root / "live_snapshots.jsonl"
            PaperAccountSnapshotJournal(paper_snapshots).append(
                timestamp="2026-07-06T21:00:00Z", equity=100000.0
            )
            PaperAccountSnapshotJournal(paper_snapshots).append(
                timestamp="2026-07-10T21:00:00Z", equity=101000.0
            )
            LiveEquitySnapshotJournal(live_snapshots).append(
                timestamp="2026-07-06T21:00:00Z", equity=100000.0
            )
            LiveEquitySnapshotJournal(live_snapshots).append(
                timestamp="2026-07-07T21:00:00Z", equity=99000.0
            )
            stdout = io.StringIO()

            main(
                [
                    "acceptance-cycle",
                    "--evidence-file",
                    str(evidence_file),
                    "--paper-snapshot-log",
                    str(paper_snapshots),
                    "--paper-runs-log",
                    str(root / "paper_runs.jsonl"),
                    "--paper-backtest-returns-json",
                    json.dumps({"2026-W28": 0.008}),
                    "--skip-paper-snapshot",
                    "--min-paper-weeks",
                    "1",
                    "--max-paper-performance-deviation",
                    "0.10",
                    "--live-snapshot-log",
                    str(live_snapshots),
                    "--live-runs-log",
                    str(root / "live_runs.jsonl"),
                    "--live-baseline-equity",
                    "100000",
                    "--min-live-sessions",
                    "2",
                    "--max-live-drawdown",
                    "0.005",
                ],
                stdout=stdout,
            )
            payload = json.loads(stdout.getvalue())
            evidence = load_evidence_file(evidence_file)

        self.assertFalse(evidence["paper_trading_4_weeks"].passed)
        self.assertFalse(evidence["paper_vs_backtest_deviation"].passed)
        self.assertEqual(
            payload["paper"]["status"]["reasons"],
            ["duration", "performance_deviation"],
        )
        self.assertFalse(evidence["live_trading_soak"].passed)
        self.assertEqual(payload["live"]["status"]["reasons"], ["duration", "drawdown"])


if __name__ == "__main__":
    unittest.main()
